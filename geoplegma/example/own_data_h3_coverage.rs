// Copyright 2026 contributors to the GeoPlegma project.
//
// Licenced under the Apache Licence, Version 2.0 <LICENCE-APACHE or
// http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
// <LICENCE-MIT or http://opensource.org/licenses/MIT>, at your
// discretion. This file may not be copied, modified, or distributed
// except according to those terms.

//! Hydrates H3 geometry for an already-aggregated dataset (`own_data/`) and
//! cross-checks it for coverage gaps against the theoretical H3 tiling of the
//! dataset's own bounding box.
//!
//! The input is a Parquet file with one row per (H3 cell, year) and an
//! `h3_id` column holding standard H3 hex strings at resolution 9. This
//! example:
//!
//! 1. Reads the unique `h3_id` values (only that column, via Parquet's
//!    row-group projection).
//! 2. Hydrates each one via `H3Impl::zone_from_id`, writing the polygon
//!    boundaries out as GeoJSON so the dataset can be mapped without
//!    depending on a Python H3 binding.
//! 3. Builds a bounding box from the hydrated polygons' vertices (not cell
//!    centers, so every dataset cell is fully enclosed) and asks the H3
//!    adapter for the full theoretical tiling of that box via
//!    `zones_from_bbox`, then diffs the two ID sets.
//!
//! `dataset ⊆ theoretical` is expected to hold exactly (it's a sanity check
//! on the round-trip). `theoretical \ dataset` is *not* a list of bugs — the
//! bounding box is a rectangle, so it will include neighbouring provinces,
//! the North Sea, and anything CBS excluded (e.g. cells with no residents).
//! It's a starting point for finding real gaps, not a final answer; a proper
//! check would clip the theoretical tiling to the actual province boundary.

use std::collections::HashSet;
use std::fs::File;

use geoplegma::api::DggrsApiConfig;
use geoplegma::types::{BoundingBox, DggrsUid, RefinementLevel, ZoneId};
use geoplegma::{get, types::Point};
use parquet::file::reader::{FileReader, SerializedFileReader};
use parquet::record::RowAccessor;

const H3_RESOLUTION: i32 = 9;

fn read_unique_h3_ids(path: &str) -> Result<HashSet<String>, Box<dyn std::error::Error>> {
    let file = File::open(path)?;
    let reader = SerializedFileReader::new(file)?;

    let schema = reader.metadata().file_metadata().schema_descr();
    let h3_id_idx = (0..schema.num_columns())
        .find(|&i| schema.column(i).name() == "h3_id")
        .ok_or("column 'h3_id' not found in input Parquet schema")?;

    let mut ids = HashSet::new();
    for row in reader.get_row_iter(None)? {
        let row = row?;
        ids.insert(row.get_string(h3_id_idx)?.clone());
    }
    Ok(ids)
}

fn geojson_polygon_feature(h3_id: &str, exterior: &[Point]) -> String {
    let coords: Vec<String> = exterior
        .iter()
        .map(|p| format!("[{},{}]", p.lon, p.lat))
        .collect();
    format!(
        "{{\"type\":\"Feature\",\"properties\":{{\"h3_id\":\"{h3_id}\"}},\
         \"geometry\":{{\"type\":\"Polygon\",\"coordinates\":[[{}]]}}}}",
        coords.join(",")
    )
}

fn geojson_point_feature(h3_id: &str, center: Point) -> String {
    format!(
        "{{\"type\":\"Feature\",\"properties\":{{\"h3_id\":\"{h3_id}\"}},\
         \"geometry\":{{\"type\":\"Point\",\"coordinates\":[{},{}]}}}}",
        center.lon, center.lat
    )
}

fn write_geojson(path: &str, features: &[String]) -> std::io::Result<()> {
    let mut body = String::from("{\"type\":\"FeatureCollection\",\"features\":[");
    body.push_str(&features.join(","));
    body.push_str("]}");
    std::fs::write(path, body)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let input_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "own_data/volledige_tijdreeks_met_namen.parquet".to_string());

    println!("Reading unique H3 cell IDs from {input_path} ...");
    let dataset_ids = read_unique_h3_ids(&input_path)?;
    println!("Found {} unique H3 cells in the dataset.", dataset_ids.len());

    let h3 = get(DggrsUid::H3)?;
    let level = RefinementLevel::new(H3_RESOLUTION)?;

    // --- Step 1: hydrate geometry for every dataset cell ---------------
    let hydrate_config = DggrsApiConfig {
        region: true,
        center: false,
        vertex_count: false,
        children: false,
        neighbors: false,
        area_sqm: false,
        densify: false,
    };

    let (mut min_lon, mut min_lat, mut max_lon, mut max_lat) =
        (f64::MAX, f64::MAX, f64::MIN, f64::MIN);
    let mut features = Vec::with_capacity(dataset_ids.len());

    for id in &dataset_ids {
        let zone_id = ZoneId::new_hex(id)?;
        let zones = h3.zone_from_id(zone_id, Some(hydrate_config))?;
        let zone = zones
            .zones
            .first()
            .ok_or_else(|| format!("zone_from_id returned no zone for '{id}'"))?;
        let region = zone
            .region
            .as_ref()
            .ok_or("zone_from_id did not populate region despite config.region=true")?;

        for p in &region.exterior {
            min_lon = min_lon.min(p.lon);
            max_lon = max_lon.max(p.lon);
            min_lat = min_lat.min(p.lat);
            max_lat = max_lat.max(p.lat);
        }

        features.push(geojson_polygon_feature(id, &region.exterior));
    }

    let hydrated_path = "own_data/h3_cells_hydrated.geojson";
    write_geojson(hydrated_path, &features)?;
    println!(
        "Wrote hydrated polygons for {} cells to {hydrated_path}",
        dataset_ids.len()
    );

    let bbox = BoundingBox::new(min_lon, min_lat, max_lon, max_lat);
    println!(
        "Dataset bounding box: lon [{min_lon:.5}, {max_lon:.5}], lat [{min_lat:.5}, {max_lat:.5}]"
    );

    // --- Step 2: theoretical H3-9 tiling of that bounding box -----------
    let coverage_config = DggrsApiConfig {
        region: false,
        center: true,
        vertex_count: false,
        children: false,
        neighbors: false,
        area_sqm: false,
        densify: false,
    };

    let theoretical = h3.zones_from_bbox(level, Some(bbox), Some(coverage_config))?;
    println!(
        "Theoretical H3-{H3_RESOLUTION} tiling of the bounding box: {} cells.",
        theoretical.zones.len()
    );

    let theoretical_by_id: std::collections::HashMap<String, Point> = theoretical
        .zones
        .iter()
        .map(|z| (z.id.to_string(), z.center.expect("center requested")))
        .collect();
    let theoretical_ids: HashSet<&String> = theoretical_by_id.keys().collect();
    let dataset_id_refs: HashSet<&String> = dataset_ids.iter().collect();

    // --- Step 3: diff --------------------------------------------------
    let unexpected: Vec<&&String> = dataset_id_refs.difference(&theoretical_ids).collect();
    if unexpected.is_empty() {
        println!(
            "Sanity check OK: every dataset cell falls inside its own derived bounding box tiling."
        );
    } else {
        println!(
            "WARNING: {} dataset cells were NOT found in the theoretical tiling (this should not happen): {:?}",
            unexpected.len(),
            unexpected.iter().take(5).collect::<Vec<_>>()
        );
    }

    let gap: Vec<&&String> = theoretical_ids.difference(&dataset_id_refs).collect();
    let gap_pct = 100.0 * gap.len() as f64 / theoretical_ids.len() as f64;
    println!(
        "{} of {} theoretical cells ({:.1}%) are in the bounding box but absent from the dataset.",
        gap.len(),
        theoretical_ids.len(),
        gap_pct
    );
    println!(
        "This includes real gaps AND expected exclusions (water, neighbouring provinces \
         caught by the rectangular bbox, CBS-suppressed cells) -- inspect the output file \
         against a province boundary before treating it as a data-quality issue."
    );

    let gap_features: Vec<String> = gap
        .iter()
        .map(|id| geojson_point_feature(id, theoretical_by_id[**id]))
        .collect();
    let gap_path = "own_data/h3_coverage_gap_centers.geojson";
    write_geojson(gap_path, &gap_features)?;
    println!("Wrote {} candidate gap-cell centers to {gap_path}", gap_features.len());

    Ok(())
}
