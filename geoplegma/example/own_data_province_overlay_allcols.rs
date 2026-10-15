// Copyright 2026 contributors to the GeoPlegma project.
//
// Licenced under the Apache Licence, Version 2.0 <LICENCE-APACHE or
// http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
// <LICENCE-MIT or http://opensource.org/licenses/MIT>, at your
// discretion. This file may not be copied, modified, or distributed
// except according to those terms.

//! Same pipeline as `own_data_province_overlay.rs`, but overlaying all 135
//! numeric CBS columns for one year instead of just 2 (aantal_inwoners /
//! aantal_woningen), to see how column count affects runtime.
//!
//! The key design point: the expensive part is the geometric intersection
//! between a hex and a CBS square (`hex_polygon.intersection(&square.polygon)`)
//! — that produces one `weight` per (hex, square) pair, independent of how
//! many columns you care about. Computing it once per candidate and then
//! looping over all 135 columns to accumulate `value * weight` is nearly
//! free compared to running the geometry 135 times over. So the expectation
//! going in is: overlay time barely moves vs. the 2-column run; only a
//! small, cheap accumulation loop is added per candidate.
//!
//! Input: `own_data/cbs_vk100_zuidholland_<year>_allcols.ndjson` (one line
//! per square: `{"geom": [[lon,lat], ...], "v": [<f64|null>, ...]}`, `v`
//! positional against the sidecar `..._allcols.columns.json` column list) —
//! produced by `own_data/fetch_cbs_full_province_allcols.py <year>`.

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
    values: Vec<Option<f64>>,
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

fn load_columns(path: &str) -> Result<Vec<String>, Box<dyn std::error::Error>> {
    let raw = std::fs::read_to_string(path)?;
    let v: serde_json::Value = serde_json::from_str(&raw)?;
    Ok(v.as_array()
        .ok_or("columns file is not a JSON array")?
        .iter()
        .map(|s| s.as_str().unwrap().to_string())
        .collect())
}

fn load_cbs_squares(path: &str, n_cols: usize) -> Result<Vec<CbsSquare>, Box<dyn std::error::Error>> {
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
        let polygon = geo::Polygon::new(coords.into(), vec![]).orient(Direction::Default);

        let values: Vec<Option<f64>> = v["v"]
            .as_array()
            .ok_or("'v' is not an array")?
            .iter()
            .map(|x| x.as_f64())
            .collect();
        assert_eq!(values.len(), n_cols, "value vector length must match column list");

        squares.push(CbsSquare { polygon, values });
    }
    Ok(squares)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let parquet_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "own_data/volledige_tijdreeks_met_namen.parquet".to_string());
    let cbs_path = std::env::args()
        .nth(2)
        .unwrap_or_else(|| "own_data/cbs_vk100_zuidholland_2022_allcols.ndjson".to_string());
    let columns_path = std::env::args()
        .nth(3)
        .unwrap_or_else(|| "own_data/cbs_vk100_zuidholland_2022_allcols.columns.json".to_string());

    let columns = load_columns(&columns_path)?;
    println!("Overlaying {} numeric columns.", columns.len());

    // A handful of columns worth visualizing per-hex, not just as province
    // totals. Resolved to indices into `columns` once, up front.
    const EXPORT_COLUMNS: &[&str] = &[
        "aantal_inwoners",
        "aantal_woningen",
        "gemiddelde_woz_waarde_woning",
        "gemiddeld_gasverbruik_woning",
    ];
    let export_indices: Vec<usize> = EXPORT_COLUMNS
        .iter()
        .map(|name| {
            columns
                .iter()
                .position(|c| c == name)
                .unwrap_or_else(|| panic!("export column '{name}' not found in {columns_path}"))
        })
        .collect();

    let t0 = Instant::now();
    let h3_ids = read_unique_h3_ids(&parquet_path)?;
    println!(
        "Loaded {} unique H3 cells from {parquet_path} in {:?}",
        h3_ids.len(),
        t0.elapsed()
    );

    let t1 = Instant::now();
    let squares = load_cbs_squares(&cbs_path, columns.len())?;
    println!(
        "Loaded {} CBS squares ({} columns each) from {cbs_path} in {:?}",
        squares.len(),
        columns.len(),
        t1.elapsed()
    );

    let t2 = Instant::now();
    let rtree_items: Vec<IndexedRect> = squares
        .iter()
        .enumerate()
        .map(|(index, sq)| {
            let rect = sq.polygon.bounding_rect().expect("valid bounding rect");
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
    let mut candidates_tested = 0u64;
    let mut grand_totals = vec![0.0f64; columns.len()];
    let mut per_hex = vec![0.0f64; columns.len()]; // scratch, reused per hex
    let mut hex_results: Vec<(String, Vec<geo::Coord<f64>>, Vec<f64>, f64)> =
        Vec::with_capacity(h3_ids.len());

    let t3 = Instant::now();
    for id in &h3_ids {
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
        let hex_rect = hex_polygon.bounding_rect().expect("valid bounding rect");
        let query = AABB::from_corners(
            [hex_rect.min().x, hex_rect.min().y],
            [hex_rect.max().x, hex_rect.max().y],
        );

        per_hex.iter_mut().for_each(|v| *v = 0.0);
        let mut covered_area = 0.0;
        for item in rtree.locate_in_envelope_intersecting(query) {
            candidates_tested += 1;
            let square = &squares[item.index];
            // The expensive geometric step: computed ONCE per candidate,
            // regardless of how many columns we're about to weight by it.
            let overlap_area = hex_polygon.intersection(&square.polygon).geodesic_area_unsigned();
            if overlap_area <= 0.0 {
                continue;
            }
            let weight = overlap_area / square.polygon.geodesic_area_unsigned();
            covered_area += overlap_area;

            // Cheap part: reuse that one weight across all 135 columns.
            for (acc, value) in per_hex.iter_mut().zip(square.values.iter()) {
                if let Some(v) = value {
                    *acc += v * weight;
                }
            }
        }
        for (total, hex_val) in grand_totals.iter_mut().zip(per_hex.iter()) {
            *total += hex_val;
        }
        overlay_time += ostart.elapsed();

        let exported: Vec<f64> = export_indices.iter().map(|&i| per_hex[i]).collect();
        hex_results.push((
            id.clone(),
            hex_polygon.exterior().0.clone(),
            exported,
            covered_area / hex_area,
        ));
    }
    let total_time = t3.elapsed();
    let n = h3_ids.len() as f64;

    println!("\n=== Timing ({} hexes x {} columns) ===", h3_ids.len(), columns.len());
    println!(
        "H3 hydration (geoplegma):     {hydrate_time:>10.2?}  ({:>7.1} us/hex)",
        hydrate_time.as_micros() as f64 / n
    );
    println!(
        "Overlay + {}-column accumulation: {overlay_time:>10.2?}  ({:>7.1} us/hex, avg {:.1} candidates/hex)",
        columns.len(),
        overlay_time.as_micros() as f64 / n,
        candidates_tested as f64 / n
    );
    println!(
        "Total processing time:        {total_time:>10.2?}  ({:.0} hexes/sec, {:.1}M cell-column values/sec)",
        n / total_time.as_secs_f64(),
        (n * columns.len() as f64) / total_time.as_secs_f64() / 1e6
    );

    println!("\n=== Sample of column totals (province-wide, area-weighted) ===");
    let mut indexed: Vec<(usize, f64)> = grand_totals.iter().copied().enumerate().collect();
    indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
    for &(idx, total) in indexed.iter().take(10) {
        println!("  {:<55} {total:>14.0}", columns[idx]);
    }

    // Per-hex export for visualization: geometry + a handful of highlight
    // columns + CBS coverage fraction, one JSON object per line.
    let export_path = std::env::args()
        .nth(4)
        .unwrap_or_else(|| "own_data/province_overlay_allcols_result.ndjson".to_string());
    let mut out = String::new();
    for (id, exterior, values, coverage) in &hex_results {
        let coords: Vec<String> = exterior
            .iter()
            .map(|c| format!("[{:.5},{:.5}]", c.x, c.y))
            .collect();
        let vals: Vec<String> = values.iter().map(|v| format!("{v:.2}")).collect();
        out.push_str(&format!(
            "{{\"id\":\"{id}\",\"geom\":[{}],\"v\":[{}],\"cov\":{coverage:.4}}}\n",
            coords.join(","),
            vals.join(",")
        ));
    }
    std::fs::write(&export_path, out)?;
    println!(
        "\nWrote per-hex export ({} hexes, columns={:?}) to {export_path}",
        hex_results.len(),
        EXPORT_COLUMNS
    );

    Ok(())
}
