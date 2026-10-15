// Copyright 2026 contributors to the GeoPlegma project.
//
// Licenced under the Apache Licence, Version 2.0 <LICENCE-APACHE or
// http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
// <LICENCE-MIT or http://opensource.org/licenses/MIT>, at your
// discretion. This file may not be copied, modified, or distributed
// except according to those terms.

//! Full-scale benchmark: run the actual `geoplegma` workflow (H3 hydration +
//! area-weighted CBS overlay) over every one of `own_data`'s ~37,600 H3-9
//! cells for a single year, and time it.
//!
//! Unlike `own_data_cbs_overlay.rs` (which spot-checks 5 hand-picked cells
//! and compares against the normalized stored values), this example doesn't
//! compare against anything — it just runs the pipeline at full scale and
//! reports throughput. Two things make this tractable rather than a naive
//! O(cells x squares) scan:
//!
//! 1. **Spatial index**: the ~75,000 CBS squares are bulk-loaded into an
//!    `rstar` R-tree keyed by bounding box. Each hex only tests intersection
//!    against the handful of squares whose bbox actually overlaps its own,
//!    not all 75,000.
//! 2. **Bulk province fetch, done once, outside the timed workflow**:
//!    `geoplegma` has no HTTP client and doesn't do data acquisition — that's
//!    a separate concern. The CBS data was fetched ahead of time with
//!    `own_data/fetch_cbs_full_province.py`, which tiles the province bbox
//!    into an 8x8 grid and fetches tiles in parallel (PDOK's OGC API caps
//!    `limit` at 1000/page with cursor pagination, so tiling is what makes a
//!    province-wide pull fast rather than serial — ~75,600 squares in ~23s
//!    wall time). Output: `own_data/cbs_vk100_zuidholland_2022.ndjson`, one
//!    line per square: `{"geom": [[lon,lat], ...], "inw": <f64|null>, "won":
//!    <f64|null>}`.
//!
//! What *is* timed, and attributed to `geoplegma` specifically, is the H3
//! hydration (`H3Impl::zone_from_id` for every cell) versus the overlay math
//! (`geo`'s `BooleanOps`/`GeodesicArea`, which geoplegma doesn't provide) —
//! reported separately below.

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::time::{Duration, Instant};

use geo::algorithm::orient::{Direction, Orient};
use geo::{BooleanOps, BoundingRect, GeodesicArea};
use geoplegma::api::DggrsApiConfig;
use geoplegma::get;
use geoplegma::types::{DggrsUid, ZoneId};
use parquet::file::reader::{FileReader, SerializedFileReader};
use parquet::record::RowAccessor;
use rstar::{AABB, RTree, RTreeObject};

struct CbsSquare {
    polygon: geo::Polygon<f64>,
    inwoners: Option<f64>,
    woningen: Option<f64>,
}

struct IndexedRect {
    index: usize,
    min: [f64; 2],
    max: [f64; 2],
}

impl RTreeObject for IndexedRect {
    type Envelope = AABB<[f64; 2]>;
    fn envelope(&self) -> Self::Envelope {
        AABB::from_corners(self.min, self.max)
    }
}

fn read_unique_h3_ids(path: &str) -> Result<Vec<String>, Box<dyn std::error::Error>> {
    let file = File::open(path)?;
    let reader = SerializedFileReader::new(file)?;
    let schema = reader.metadata().file_metadata().schema_descr();
    let h3_idx = (0..schema.num_columns())
        .find(|&i| schema.column(i).name() == "h3_id")
        .ok_or("column 'h3_id' not found")?;

    let mut ids: Vec<String> = reader
        .get_row_iter(None)?
        .map(|row| Ok::<_, Box<dyn std::error::Error>>(row?.get_string(h3_idx)?.clone()))
        .collect::<Result<Vec<_>, _>>()?;
    ids.sort_unstable();
    ids.dedup();
    Ok(ids)
}

fn load_cbs_squares(path: &str) -> Result<Vec<CbsSquare>, Box<dyn std::error::Error>> {
    let reader = BufReader::new(File::open(path)?);
    let mut squares = Vec::new();

    for line in reader.lines() {
        let line = line?;
        if line.is_empty() {
            continue;
        }
        let v: serde_json::Value = serde_json::from_str(&line)?;
        let coords: Vec<geo::Coord<f64>> = v["geom"]
            .as_array()
            .ok_or("'geom' is not an array")?
            .iter()
            .map(|p| {
                let arr = p.as_array().expect("coordinate pair");
                geo::coord! { x: arr[0].as_f64().unwrap(), y: arr[1].as_f64().unwrap() }
            })
            .collect();

        // Same PDOK winding-order issue as own_data_cbs_overlay.rs: re-orient
        // before any geodesic area math, or GeodesicArea silently returns the
        // area of the sphere's other side.
        let polygon = geo::Polygon::new(coords.into(), vec![]).orient(Direction::Default);

        squares.push(CbsSquare {
            polygon,
            inwoners: v["inw"].as_f64(),
            woningen: v["won"].as_f64(),
        });
    }
    Ok(squares)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let parquet_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "own_data/volledige_tijdreeks_met_namen.parquet".to_string());
    let cbs_path = std::env::args()
        .nth(2)
        .unwrap_or_else(|| "own_data/cbs_vk100_zuidholland_2022.ndjson".to_string());

    let t0 = Instant::now();
    let h3_ids = read_unique_h3_ids(&parquet_path)?;
    println!(
        "Loaded {} unique H3 cells from {parquet_path} in {:?}",
        h3_ids.len(),
        t0.elapsed()
    );

    let t1 = Instant::now();
    let squares = load_cbs_squares(&cbs_path)?;
    println!(
        "Loaded {} CBS squares from {cbs_path} in {:?}",
        squares.len(),
        t1.elapsed()
    );

    let t2 = Instant::now();
    let rtree_items: Vec<IndexedRect> = squares
        .iter()
        .enumerate()
        .map(|(index, sq)| {
            let rect = sq
                .polygon
                .bounding_rect()
                .expect("CBS square has a valid bounding rect");
            IndexedRect {
                index,
                min: [rect.min().x, rect.min().y],
                max: [rect.max().x, rect.max().y],
            }
        })
        .collect();
    let rtree = RTree::bulk_load(rtree_items);
    println!(
        "Built R-tree spatial index over {} squares in {:?}",
        squares.len(),
        t2.elapsed()
    );

    let h3 = get(DggrsUid::H3)?;
    let config = DggrsApiConfig {
        region: true,
        center: false,
        vertex_count: false,
        children: false,
        neighbors: false,
        area_sqm: false,
        densify: false,
    };

    let mut hydrate_time = Duration::ZERO;
    let mut overlay_time = Duration::ZERO;
    let mut total_inwoners = 0.0;
    let mut total_woningen = 0.0;
    let mut zero_coverage = 0usize;
    let mut full_coverage = 0usize;
    let mut candidates_tested = 0u64;
    // Partition-of-unity correctness check: how much of each CBS square's area
    // got claimed by *some* hex. Should never exceed 1.0 (that would mean two
    // hexes double-counted the same patch of ground) and should be ~1.0 for
    // squares fully inside the dataset's covered area (anything less there
    // would mean area was silently lost).
    let mut square_weight_total = vec![0.0f64; squares.len()];
    let mut hex_results: Vec<(String, Vec<geo::Coord<f64>>, f64, f64, f64)> =
        Vec::with_capacity(h3_ids.len());

    let t3 = Instant::now();
    for (i, id) in h3_ids.iter().enumerate() {
        let hstart = Instant::now();
        let zone_id = ZoneId::new_hex(id)?;
        let zones = h3.zone_from_id(zone_id, Some(config))?;
        let hex_polygon = zones.zones[0]
            .region
            .as_ref()
            .ok_or("zone_from_id did not return a region")?
            .to_geo_polygon();
        hydrate_time += hstart.elapsed();

        let ostart = Instant::now();
        let hex_area = hex_polygon.geodesic_area_unsigned();
        let hex_rect = hex_polygon
            .bounding_rect()
            .expect("H3 hex has a valid bounding rect");
        let query = AABB::from_corners(
            [hex_rect.min().x, hex_rect.min().y],
            [hex_rect.max().x, hex_rect.max().y],
        );

        let mut inw = 0.0;
        let mut won = 0.0;
        let mut covered_area = 0.0;
        for item in rtree.locate_in_envelope_intersecting(query) {
            candidates_tested += 1;
            let square = &squares[item.index];
            let overlap_area = hex_polygon.intersection(&square.polygon).geodesic_area_unsigned();
            if overlap_area <= 0.0 {
                continue;
            }
            let weight = overlap_area / square.polygon.geodesic_area_unsigned();
            covered_area += overlap_area;
            square_weight_total[item.index] += weight;
            if let Some(v) = square.inwoners {
                inw += v * weight;
            }
            if let Some(v) = square.woningen {
                won += v * weight;
            }
        }
        overlay_time += ostart.elapsed();

        total_inwoners += inw;
        total_woningen += won;
        let coverage_frac = covered_area / hex_area;
        if covered_area <= 0.0 {
            zero_coverage += 1;
        } else if coverage_frac > 0.999 {
            full_coverage += 1;
        }
        hex_results.push((id.clone(), hex_polygon.exterior().0.clone(), inw, won, coverage_frac));

        if (i + 1) % 5000 == 0 {
            println!(
                "  ... {}/{} hexes processed ({:?} elapsed)",
                i + 1,
                h3_ids.len(),
                t3.elapsed()
            );
        }
    }
    let total_time = t3.elapsed();
    let n = h3_ids.len() as f64;

    println!("\n=== Timing ({} hexes) ===", h3_ids.len());
    println!(
        "H3 hydration (geoplegma):     {hydrate_time:>10.2?}  ({:>7.1} us/hex)",
        hydrate_time.as_micros() as f64 / n
    );
    println!(
        "Overlay computation (geo):    {overlay_time:>10.2?}  ({:>7.1} us/hex, avg {:.1} candidates/hex)",
        overlay_time.as_micros() as f64 / n,
        candidates_tested as f64 / n
    );
    println!(
        "Total processing time:        {total_time:>10.2?}  ({:.0} hexes/sec)",
        n / total_time.as_secs_f64()
    );

    println!("\n=== Results ({cbs_path}, area-weighted, pre-normalization) ===");
    println!("Total apportioned aantal_inwoners across all hexes: {total_inwoners:.0}");
    println!("Total apportioned aantal_woningen across all hexes: {total_woningen:.0}");
    println!(
        "Hexes with zero CBS coverage:   {zero_coverage:>7} ({:.1}%)",
        100.0 * zero_coverage as f64 / n
    );
    println!(
        "Hexes with >99.9% CBS coverage: {full_coverage:>7} ({:.1}%)",
        100.0 * full_coverage as f64 / n
    );

    // --- Correctness check: partition of unity over the CBS squares -----
    let raw_total_inwoners: f64 = squares.iter().filter_map(|s| s.inwoners).sum();
    let raw_total_woningen: f64 = squares.iter().filter_map(|s| s.woningen).sum();
    let mut overcounted = 0usize; // weight_total > 1 + eps: double-counting bug
    let mut fully_claimed = 0usize; // weight_total ~= 1: fully inside some hex(es)
    let mut partially_claimed = 0usize; // 0 < weight_total < 1: straddles a hex boundary or dataset edge
    let mut unclaimed = 0usize; // weight_total ~= 0: outside every hex entirely
    let mut max_weight = 0.0f64;
    // 1e-3 relative tolerance: geodesic-area arithmetic on many small overlap
    // slivers accumulates floating-point noise well above f64 epsilon: this
    // still catches a real double-counting bug (which would overshoot by a
    // whole fractional hex, not a rounding-sized amount).
    const TOLERANCE: f64 = 1e-3;
    for &w in &square_weight_total {
        max_weight = max_weight.max(w);
        if w > 1.0 + TOLERANCE {
            overcounted += 1;
        } else if w > 1.0 - TOLERANCE {
            fully_claimed += 1;
        } else if w > 1e-9 {
            partially_claimed += 1;
        } else {
            unclaimed += 1;
        }
    }
    let sq_n = squares.len() as f64;

    println!("\n=== Correctness check: partition of unity over CBS squares ===");
    println!(
        "Max total weight assigned to any single square: {max_weight:.6} (must be <= 1.0 -- >1 means double-counting)"
    );
    println!(
        "  overcounted (weight > 1.0):        {overcounted:>7} ({:.2}%)  <- should be 0",
        100.0 * overcounted as f64 / sq_n
    );
    println!(
        "  fully claimed (weight ~= 1.0):      {fully_claimed:>7} ({:.1}%)",
        100.0 * fully_claimed as f64 / sq_n
    );
    println!(
        "  partially claimed (0 < weight < 1): {partially_claimed:>7} ({:.1}%)  (straddles a hex boundary or the dataset's edge)",
        100.0 * partially_claimed as f64 / sq_n
    );
    println!(
        "  unclaimed (weight ~= 0):            {unclaimed:>7} ({:.1}%)  (outside every hex -- e.g. beyond province boundary)",
        100.0 * unclaimed as f64 / sq_n
    );
    println!("\n=== Correctness check: mass conservation ===");
    println!(
        "Raw CBS total aantal_inwoners in fetched squares: {raw_total_inwoners:.0}   Apportioned total: {total_inwoners:.0}   ({:.1}% of raw mass captured by the hex set)",
        100.0 * total_inwoners / raw_total_inwoners
    );
    println!(
        "Raw CBS total aantal_woningen in fetched squares: {raw_total_woningen:.0}   Apportioned total: {total_woningen:.0}   ({:.1}% of raw mass captured by the hex set)",
        100.0 * total_woningen / raw_total_woningen
    );

    // --- Export per-hex results for visualization ------------------------
    let out_path = std::env::args()
        .nth(3)
        .unwrap_or_else(|| "own_data/province_overlay_result.ndjson".to_string());
    let mut out = String::new();
    for (id, exterior, inw, won, coverage) in &hex_results {
        let coords: Vec<String> = exterior
            .iter()
            .map(|c| format!("[{:.5},{:.5}]", c.x, c.y))
            .collect();
        out.push_str(&format!(
            "{{\"id\":\"{id}\",\"geom\":[{}],\"inw\":{inw:.2},\"won\":{won:.2},\"cov\":{coverage:.4}}}\n",
            coords.join(",")
        ));
    }
    std::fs::write(&out_path, out)?;
    println!("\nWrote per-hex results ({} hexes) to {out_path}", hex_results.len());

    Ok(())
}
