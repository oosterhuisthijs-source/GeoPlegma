//! The base grid: every H3 cell (via geoplegma) that overlaps the province.

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result, anyhow};
use geo::algorithm::orient::{Direction, Orient};
use geo::{BoundingRect, MultiPolygon, Polygon};
use geoplegma::api::{DggrsApi, DggrsApiConfig};
use geoplegma::types::{BoundingBox, DggrsUid, RefinementLevel, ZoneId};
use rayon::prelude::*;

use crate::geoparquet::{OutColumn, write_table};
use crate::overlay::{hex_overlaps, poly_area_m2};

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

/// Load provinces from the PDOK `provinciegebied` GeoJSON: the given codes
/// (e.g. ["28"] = Zuid-Holland), or all provinces when `codes` is empty.
pub fn load_provinces(path: &Path, codes: &[String]) -> Result<Vec<(String, String, MultiPolygon<f64>)>> {
    let raw = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let fc: geojson::FeatureCollection = raw.parse()?;
    let mut out = Vec::new();
    for f in fc.features {
        let code = f.property("code").and_then(|c| c.as_str()).unwrap_or_default().to_string();
        if !codes.is_empty() && !codes.contains(&code) {
            continue;
        }
        let name = f.property("naam").and_then(|n| n.as_str()).unwrap_or_default().to_string();
        let geom: geo::Geometry<f64> = f.geometry.ok_or_else(|| anyhow!("province {code} has no geometry"))?.try_into()?;
        let mp = match geom {
            geo::Geometry::MultiPolygon(mp) => mp,
            geo::Geometry::Polygon(p) => MultiPolygon(vec![p]),
            other => return Err(anyhow!("unexpected province geometry {other:?}")),
        };
        out.push((code, name, mp.orient(Direction::Default)));
    }
    if out.is_empty() {
        return Err(anyhow!("no provinces {codes:?} in {}", path.display()));
    }
    Ok(out)
}

/// Build the grid: every H3 cell (geoplegma `zones_from_bbox`) overlapping
/// the region's provinces, with the fraction of each cell inside the region
/// and the province covering most of it (`code`, `naam`).
pub fn build(boundary: &Path, codes: &[String], out: &Path) -> Result<()> {
    let provinces = load_provinces(boundary, codes)?;
    let names: Vec<&str> = provinces.iter().map(|p| p.1.as_str()).collect();
    let mut rect = provinces[0].2.bounding_rect().ok_or_else(|| anyhow!("empty province"))?;
    for p in &provinces[1..] {
        if let Some(r) = p.2.bounding_rect() {
            rect = geo::Rect::new(
                geo::coord! { x: rect.min().x.min(r.min().x), y: rect.min().y.min(r.min().y) },
                geo::coord! { x: rect.max().x.max(r.max().x), y: rect.max().y.max(r.max().y) },
            );
        }
    }
    let bbox = BoundingBox::new(rect.min().x, rect.min().y, rect.max().x, rect.max().y);

    let h3 = h3()?;
    let zones = h3.zones_from_bbox(RefinementLevel::new(H3_RESOLUTION)?, Some(bbox), Some(region_only()))?;
    eprintln!("{} cells cover the bbox of {}", zones.zones.len(), names.join(", "));
    let candidates: Vec<Hex> = zones
        .zones
        .par_iter()
        .filter_map(|z| {
            let polygon = z.region.as_ref()?.to_geo_polygon().orient(Direction::Default);
            let area_m2 = poly_area_m2(&polygon);
            Some(Hex { id: z.id.to_string(), polygon, area_m2 })
        })
        .collect();

    // One overlay pass (provinces are cut into small pieces internally, so
    // this stays fast even for detailed national coastlines).
    let geoms: Vec<MultiPolygon<f64>> = provinces.iter().map(|p| p.2.clone()).collect();
    let overlaps = hex_overlaps(&candidates, &geoms);

    let mut rows: Vec<(String, f64, f64, f64, f64, usize)> = candidates
        .iter()
        .zip(&overlaps)
        .filter_map(|(hex, hits)| {
            let mut per = vec![0.0; provinces.len()];
            for &(i, ov) in hits {
                per[i] += ov;
            }
            let mut fraction = (per.iter().sum::<f64>() / hex.area_m2).min(1.0);
            // Summing the province pieces leaves ~1e-6 rounding on cells that
            // are fully inside; snap those to exactly 1.
            if fraction > 1.0 - 1e-5 {
                fraction = 1.0;
            }
            if fraction <= 1e-9 {
                return None;
            }
            let main = (0..per.len()).max_by(|a, b| per[*a].total_cmp(&per[*b]))?;
            let c = geo::Centroid::centroid(&hex.polygon)?;
            Some((hex.id.clone(), c.y(), c.x(), hex.area_m2, fraction, main))
        })
        .collect();
    rows.sort_by(|a, b| a.0.cmp(&b.0));
    eprintln!(
        "{} cells overlap the region ({} fully inside)",
        rows.len(),
        rows.iter().filter(|r| r.4 >= 1.0).count()
    );

    write_table(
        out,
        vec![
            ("h3_id".into(), OutColumn::Str(rows.iter().map(|r| Some(r.0.clone())).collect())),
            ("lat".into(), OutColumn::F64(rows.iter().map(|r| Some(r.1)).collect())),
            ("lon".into(), OutColumn::F64(rows.iter().map(|r| Some(r.2)).collect())),
            ("hex_area_m2".into(), OutColumn::F64(rows.iter().map(|r| Some(r.3)).collect())),
            ("province_fraction".into(), OutColumn::F64(rows.iter().map(|r| Some(r.4)).collect())),
            ("code".into(), OutColumn::Str(rows.iter().map(|r| Some(provinces[r.5].0.clone())).collect())),
            ("naam".into(), OutColumn::Str(rows.iter().map(|r| Some(provinces[r.5].1.clone())).collect())),
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
