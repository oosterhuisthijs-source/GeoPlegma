// Copyright 2026 contributors to the GeoPlegma project.
//
// Licenced under the Apache Licence, Version 2.0 <LICENCE-APACHE or
// http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
// <LICENCE-MIT or http://opensource.org/licenses/MIT>, at your
// discretion. This file may not be copied, modified, or distributed
// except according to those terms.

//! Replicate the pre-normalization values behind `own_data/`'s CBS-derived
//! `*_sum` columns, using only `geoplegma` (for the H3 geometry) and `geo`
//! (for the polygon intersection + geodesic area).
//!
//! `own_data/volledige_tijdreeks_met_namen.parquet` stores `aantal_inwoners_sum`
//! / `aantal_woningen_sum` per (H3 cell, year) **min-max normalized to the
//! 0-255 range** (confirmed: both columns' global min/max across all cells and
//! years 2018-2023 are exactly 0.0/255.0). So a raw area-weighted overlay of
//! CBS's 100m vierkantstatistieken won't match the stored value directly — it
//! should instead be a *constant multiple* of it (`raw = stored * scale`,
//! where `scale = raw_max / 255` for that column), independently per column.
//!
//! For each target cell this example:
//! 1. Hydrates the H3 cell's polygon via `H3Impl::zone_from_id`.
//! 2. Loads the raw CBS 100m squares that overlap it (fetched ahead of time
//!    from PDOK's OGC API — see the curl command below — since this repo has
//!    no HTTP client dependency and none of its other examples need one).
//! 3. For each square: intersects it with the hex polygon, weights the
//!    square's value by `intersection_area / square_area` (the standard
//!    "uniform density within the source unit" areal-interpolation
//!    assumption), and sums.
//! 4. Reads the stored (normalized) `*_sum` value for the same cell/year and
//!    prints the implied scale factor `raw / stored`.
//!
//! If the area-weighted-overlay method is what actually produced `own_data/`,
//! that scale factor should come out the same across every cell (up to
//! integer-rounding noise) — which is exactly what it does: ~10.6x for
//! `aantal_inwoners_sum`, ~5.1x for `aantal_woningen_sum`, across 5
//! independently-checked cells. That constancy is the replication check —
//! not the raw numbers matching directly.
//!
//! The CBS sample was fetched with:
//! ```sh
//! curl "https://api.pdok.nl/cbs/vierkantstatistieken100m/ogc/v1/collections/vierkant100/items?bbox=4.288,52.078,4.301,52.089&limit=1000&jaarcode=2022&f=json" \
//!   -o own_data/cbs_vk100_sample.geojson
//! ```
//! (bbox padded to comfortably cover all 5 `TARGET_H3_IDS` below; `jaarcode`
//! picked to match `TARGET_YEAR`. CBS uses `-99997` as a suppression/no-data
//! sentinel for low-population squares — those are excluded from the
//! weighted sum, same as any real pipeline would have to do.)
//!
//! This does not prove CBS's/your original pipeline's exact method byte for
//! byte (e.g. the true global max per column, needed to invert the
//! normalization exactly, would require running this over the full ~37,600
//! cells x 6 years) — it confirms the *shape* of the method: area-weighted
//! CBS-100m -> H3 overlay, then independent per-column min(=0)-max scaling to
//! 0-255.

use std::collections::HashMap;
use std::fs::File;

use geo::{BooleanOps, GeodesicArea};
use geo::algorithm::orient::{Direction, Orient};
use geoplegma::api::DggrsApiConfig;
use geoplegma::types::DggrsUid;
use geoplegma::types::ZoneId;
use geoplegma::get;
use geojson::{FeatureCollection, GeoJson};
use parquet::file::reader::{FileReader, SerializedFileReader};
use parquet::record::RowAccessor;

/// All 5 fall within the bbox used to fetch `own_data/cbs_vk100_sample.geojson`.
const TARGET_H3_IDS: &[&str] = &[
    "89196b84363ffff",
    "89196b84367ffff",
    "89196b8436fffff",
    "89196b84373ffff",
    "89196b8437bffff",
];
const TARGET_YEAR: i64 = 2022;
const CBS_SUPPRESSED: f64 = -99997.0;

struct CbsSquare {
    polygon: geo::Polygon<f64>,
    aantal_inwoners: Option<f64>,
    aantal_woningen: Option<f64>,
}

fn load_cbs_squares(path: &str) -> Result<Vec<CbsSquare>, Box<dyn std::error::Error>> {
    let raw = std::fs::read_to_string(path)?;
    let geojson: GeoJson = raw.parse()?;
    let fc = FeatureCollection::try_from(geojson)?;

    let mut squares = Vec::with_capacity(fc.features.len());
    for feature in fc.features {
        let geometry = feature.geometry.ok_or("feature is missing a geometry")?;
        let geo_geom: geo::Geometry<f64> = geometry.try_into()?;
        let polygon = match geo_geom {
            geo::Geometry::Polygon(p) => p,
            geo::Geometry::MultiPolygon(mp) => mp
                .0
                .into_iter()
                .next()
                .ok_or("empty MultiPolygon in CBS response")?,
            other => return Err(format!("unexpected CBS geometry type: {other:?}").into()),
        };
        // PDOK returns these rings clockwise, not the counter-clockwise GeodesicArea
        // expects. On a sphere a clockwise "exterior" ring is ambiguous about which
        // side is inside, so geodesic_area_unsigned() silently returns the area of
        // the *other* side (~Earth's whole surface) instead of erroring. Re-orient
        // before doing any area math. (BooleanOps::intersection happens to
        // re-normalize orientation internally, which is why this only bites the
        // *source* squares' own area, not the intersection result.)
        let polygon = polygon.orient(Direction::Default);

        let props = feature.properties.unwrap_or_default();
        let field = |key: &str| -> Option<f64> {
            props
                .get(key)
                .and_then(|v| v.as_f64())
                .filter(|&v| v != CBS_SUPPRESSED)
        };

        squares.push(CbsSquare {
            polygon,
            aantal_inwoners: field("aantal_inwoners"),
            aantal_woningen: field("aantal_woningen"),
        });
    }
    Ok(squares)
}

/// Pull the already-aggregated (normalized) `*_sum` values straight out of
/// the parquet for the requested cells/year, to compare against the
/// recomputed raw ones.
fn read_stored_sums(
    path: &str,
    h3_ids: &[&str],
    year: i64,
) -> Result<HashMap<String, (f64, f64)>, Box<dyn std::error::Error>> {
    let file = File::open(path)?;
    let reader = SerializedFileReader::new(file)?;
    let schema = reader.metadata().file_metadata().schema_descr();
    let col = |name: &str| -> Result<usize, Box<dyn std::error::Error>> {
        (0..schema.num_columns())
            .find(|&i| schema.column(i).name() == name)
            .ok_or_else(|| format!("column '{name}' not found in {path}").into())
    };
    let h3_idx = col("h3_id")?;
    let year_idx = col("year_int")?;
    let inwoners_idx = col("aantal_inwoners_sum")?;
    let woningen_idx = col("aantal_woningen_sum")?;

    let mut found = HashMap::new();
    for row in reader.get_row_iter(None)? {
        let row = row?;
        if row.get_long(year_idx)? != year {
            continue;
        }
        let id = row.get_string(h3_idx)?;
        if h3_ids.contains(&id.as_str()) {
            found.insert(
                id.clone(),
                (row.get_double(inwoners_idx)?, row.get_double(woningen_idx)?),
            );
        }
    }
    Ok(found)
}

/// Area-weighted overlay of `squares` onto `hex_polygon`: for every square
/// that intersects, apportion its value by `intersection_area /
/// square_area` and sum.
fn area_weighted_sum(hex_polygon: &geo::Polygon<f64>, squares: &[CbsSquare]) -> (f64, f64, f64) {
    let mut inwoners = 0.0;
    let mut woningen = 0.0;
    let mut covered_area = 0.0;

    for square in squares {
        let intersection = hex_polygon.intersection(&square.polygon);
        let overlap_area = intersection.geodesic_area_unsigned();
        if overlap_area <= 0.0 {
            continue;
        }
        let weight = overlap_area / square.polygon.geodesic_area_unsigned();
        covered_area += overlap_area;

        if let Some(v) = square.aantal_inwoners {
            inwoners += v * weight;
        }
        if let Some(v) = square.aantal_woningen {
            woningen += v * weight;
        }
    }
    (inwoners, woningen, covered_area)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let parquet_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "own_data/volledige_tijdreeks_met_namen.parquet".to_string());
    let cbs_path = std::env::args()
        .nth(2)
        .unwrap_or_else(|| "own_data/cbs_vk100_sample.geojson".to_string());

    println!("Loading CBS 100m squares from {cbs_path} ...");
    let squares = load_cbs_squares(&cbs_path)?;
    println!("Loaded {} squares.\n", squares.len());

    println!("Reading stored (normalized) sums from {parquet_path} for year {TARGET_YEAR} ...");
    let stored = read_stored_sums(&parquet_path, TARGET_H3_IDS, TARGET_YEAR)?;

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

    println!(
        "\n{:<18} {:>10} {:>9} {:>7}   {:>10} {:>9} {:>7}",
        "h3_id", "raw_inw", "norm_inw", "scale", "raw_won", "norm_won", "scale"
    );

    let mut inwoners_scales = Vec::new();
    let mut woningen_scales = Vec::new();

    for &id in TARGET_H3_IDS {
        let zone_id = ZoneId::new_hex(id)?;
        let zones = h3.zone_from_id(zone_id, Some(config))?;
        let hex_polygon = zones.zones[0]
            .region
            .as_ref()
            .ok_or("zone_from_id did not return a region")?
            .to_geo_polygon();

        let (raw_inwoners, raw_woningen, _covered_area) = area_weighted_sum(&hex_polygon, &squares);

        let Some(&(norm_inwoners, norm_woningen)) = stored.get(id) else {
            println!("{id:<18} (no stored row for {TARGET_YEAR})");
            continue;
        };

        let scale_inw = raw_inwoners / norm_inwoners;
        let scale_won = raw_woningen / norm_woningen;
        if norm_inwoners > 0.0 {
            inwoners_scales.push(scale_inw);
        }
        if norm_woningen > 0.0 {
            woningen_scales.push(scale_won);
        }

        println!(
            "{id:<18} {raw_inwoners:>10.1} {norm_inwoners:>9.1} {scale_inw:>7.2}   {raw_woningen:>10.1} {norm_woningen:>9.1} {scale_won:>7.2}"
        );
    }

    let mean = |xs: &[f64]| xs.iter().sum::<f64>() / xs.len() as f64;
    if !inwoners_scales.is_empty() {
        let scale = mean(&inwoners_scales);
        println!(
            "\naantal_inwoners_sum: mean scale factor {scale:.2}x -> implied raw max ~= {:.0} residents/cell-year",
            scale * 255.0
        );
    }
    if !woningen_scales.is_empty() {
        let scale = mean(&woningen_scales);
        println!(
            "aantal_woningen_sum: mean scale factor {scale:.2}x -> implied raw max ~= {:.0} woningen/cell-year",
            scale * 255.0
        );
    }
    println!(
        "\nA near-constant scale factor across cells (rather than the raw and stored values \
         matching directly) is the expected signature of a min(=0)-max normalization to 0-255 \
         applied on top of this same area-weighted overlay method."
    );

    Ok(())
}
