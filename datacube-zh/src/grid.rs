//! The base grid: every H3 cell (via geoplegma) that overlaps the province.

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use geo::algorithm::orient::{Direction, Orient};
use geo::{BooleanOps, BoundingRect, Contains, Intersects, MultiPolygon, Polygon};
use geoplegma::api::{DggrsApi, DggrsApiConfig};
use geoplegma::types::{BoundingBox, DggrsUid, RefinementLevel, ZoneId};
use rayon::prelude::*;

use crate::geoparquet::{OutColumn, write_table};
use crate::overlay::{area_m2, poly_area_m2};

pub const H3_RESOLUTION: i32 = 9;

/// One cell of the base grid, with its boundary hydrated through geoplegma.
pub struct Hex {
    pub id: String,
    pub polygon: Polygon<f64>,
    pub area_m2: f64,
}

fn region_only() -> DggrsApiConfig {
    DggrsApiConfig {
        region: true,
        center: false,
        vertex_count: false,
        children: false,
        neighbors: false,
        area_sqm: false,
        densify: false,
    }
}

pub fn h3() -> Result<Arc<dyn DggrsApi>> {
    Ok(geoplegma::get(DggrsUid::H3)?)
}

/// Load one province (by CBS/Kadaster province code, e.g. "28" for
/// Zuid-Holland) from the PDOK `provinciegebied` GeoJSON.
pub fn load_province(path: &Path, code: &str) -> Result<(MultiPolygon<f64>, String)> {
    let raw = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let fc: geojson::FeatureCollection = raw.parse()?;
    let feature = fc
        .features
        .into_iter()
        .find(|f| f.property("code").and_then(|c| c.as_str()) == Some(code))
        .ok_or_else(|| anyhow!("province code {code} not found in {}", path.display()))?;
    let name = feature
        .property("naam")
        .and_then(|n| n.as_str())
        .unwrap_or_default()
        .to_string();
    let geom: geo::Geometry<f64> = feature
        .geometry
        .ok_or_else(|| anyhow!("province feature has no geometry"))?
        .try_into()?;
    let mp = match geom {
        geo::Geometry::MultiPolygon(mp) => mp,
        geo::Geometry::Polygon(p) => MultiPolygon(vec![p]),
        other => return Err(anyhow!("unexpected province geometry {other:?}")),
    };
    Ok((mp.orient(Direction::Default), name))
}

/// Build the grid: all H3 cells overlapping `province`, with the fraction of
/// each cell's area that lies inside it.
pub fn build(boundary: &Path, code: &str, out: &Path) -> Result<()> {
    let (province, name) = load_province(boundary, code)?;
    let rect = province.bounding_rect().ok_or_else(|| anyhow!("empty province"))?;
    let bbox = BoundingBox::new(rect.min().x, rect.min().y, rect.max().x, rect.max().y);

    let h3 = h3()?;
    let zones = h3.zones_from_bbox(RefinementLevel::new(H3_RESOLUTION)?, Some(bbox), Some(region_only()))?;
    eprintln!("{} cells cover the bbox of {name}", zones.zones.len());

    let mut rows: Vec<(String, f64, f64, f64, f64)> = zones
        .zones
        .par_iter()
        .filter_map(|z| {
            let poly = z.region.as_ref()?.to_geo_polygon().orient(Direction::Default);
            if !province.intersects(&poly) {
                return None;
            }
            let area = poly_area_m2(&poly);
            let fraction = if province.contains(&poly) {
                1.0
            } else {
                (area_m2(&province.intersection(&poly)) / area).min(1.0)
            };
            if fraction <= 0.0 {
                return None;
            }
            let c = geo::Centroid::centroid(&poly)?;
            Some((z.id.to_string(), c.y(), c.x(), area, fraction))
        })
        .collect();
    rows.sort_by(|a, b| a.0.cmp(&b.0));
    eprintln!(
        "{} cells overlap {name} ({} fully inside)",
        rows.len(),
        rows.iter().filter(|r| r.4 >= 1.0).count()
    );

    let n = rows.len();
    write_table(
        out,
        vec![
            ("h3_id".into(), OutColumn::Str(rows.iter().map(|r| Some(r.0.clone())).collect())),
            ("lat".into(), OutColumn::F64(rows.iter().map(|r| Some(r.1)).collect())),
            ("lon".into(), OutColumn::F64(rows.iter().map(|r| Some(r.2)).collect())),
            ("hex_area_m2".into(), OutColumn::F64(rows.iter().map(|r| Some(r.3)).collect())),
            ("province_fraction".into(), OutColumn::F64(rows.iter().map(|r| Some(r.4)).collect())),
            ("code".into(), OutColumn::Str(vec![Some(code.to_string()); n])),
            ("naam".into(), OutColumn::Str(vec![Some(name.clone()); n])),
        ],
    )?;
    eprintln!("wrote {}", out.display());
    Ok(())
}

/// Load the grid ids from `grid.parquet` and hydrate each cell's boundary
/// through geoplegma (`zone_from_id`).
pub fn load_hexes(grid: &Path) -> Result<Vec<Hex>> {
    let (_, strings) = crate::geoparquet::read_table(grid)?;
    let ids = strings
        .get("h3_id")
        .ok_or_else(|| anyhow!("{} has no h3_id column", grid.display()))?;
    let h3 = h3()?;
    ids.par_iter()
        .map(|id| {
            let id = id.as_deref().ok_or_else(|| anyhow!("null h3_id"))?;
            let zones = h3.zone_from_id(ZoneId::new_hex(id)?, Some(region_only()))?;
            let polygon = zones.zones[0]
                .region
                .as_ref()
                .ok_or_else(|| anyhow!("no region for {id}"))?
                .to_geo_polygon()
                .orient(Direction::Default);
            let area = poly_area_m2(&polygon);
            Ok(Hex { id: id.to_string(), polygon, area_m2: area })
        })
        .collect()
}
