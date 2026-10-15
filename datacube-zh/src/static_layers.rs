//! Static (non-yearly) layers from `config/static.toml` -> one table keyed by
//! `h3_id`. Rasters go through `raster::aggregate` (pixel centre ->
//! `zone_from_point`), vectors through the polygon overlay engine.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result, anyhow, bail};
use geo::MultiPolygon;

use crate::geoparquet::{OutColumn, read_polygon_table, write_table};
use crate::grid::{Hex, h3};
use crate::overlay::overlay;
use crate::raster::{RasterAgg, aggregate};

#[derive(serde::Deserialize)]
pub struct StaticConfig {
    #[serde(default)]
    pub raster: Vec<RasterLayer>,
    #[serde(default)]
    pub vector: Vec<VectorLayer>,
}

#[derive(serde::Deserialize)]
pub struct RasterLayer {
    pub name: String,
    pub agg: RasterAgg,
    #[serde(default)]
    pub nodata: Vec<f64>,
    pub nodata_fill: Option<f64>,
}

#[derive(serde::Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum VectorAgg {
    Any,
    Mode,
}

#[derive(serde::Deserialize)]
pub struct VectorLayer {
    pub name: String,
    pub agg: VectorAgg,
    pub column: Option<String>,
    pub label: Option<String>,
    pub label_column: Option<String>,
}

impl StaticConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        Ok(toml::from_str(&raw)?)
    }
}

/// All `<name>__<i>.parquet` parts of a vector layer, concatenated.
fn read_vector_parts(dir: &Path, name: &str) -> Result<(Vec<MultiPolygon<f64>>, HashMap<String, Vec<Option<String>>>)> {
    let mut parts: Vec<PathBuf> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.extension().is_some_and(|e| e == "parquet")
                && p.file_stem()
                    .and_then(|s| s.to_str())
                    .is_some_and(|s| s.strip_prefix(name).is_some_and(|r| r.starts_with("__")))
        })
        .collect();
    parts.sort();
    if parts.is_empty() {
        bail!("no {name}__*.parquet in {} (run prepare/static.py)", dir.display());
    }
    let mut geoms = Vec::new();
    let mut cols: HashMap<String, Vec<Option<String>>> = HashMap::new();
    for p in parts {
        let t = read_polygon_table(&p)?;
        let n0 = geoms.len();
        let n = t.geometries.len();
        // Numeric attributes are stringified (integers without a decimal point)
        // so `mode` works uniformly.
        let mut attrs: HashMap<String, Vec<Option<String>>> = t.strings;
        for (k, v) in t.numeric {
            attrs.insert(
                k,
                v.into_iter()
                    .map(|x| x.map(|x| if x.fract() == 0.0 { format!("{}", x as i64) } else { x.to_string() }))
                    .collect(),
            );
        }
        for (k, v) in attrs {
            let col = cols.entry(k).or_insert_with(|| vec![None; n0]);
            col.extend(v);
        }
        geoms.extend(t.geometries);
        for col in cols.values_mut() {
            col.resize(n0 + n, None);
        }
    }
    Ok((geoms, cols))
}

pub fn run(config: &StaticConfig, hexes: &[Hex], static_dir: &Path, out: &Path, only: &[String]) -> Result<()> {
    let wanted = |name: &str| only.is_empty() || only.iter().any(|o| o == name);
    let index: HashMap<String, usize> = hexes.iter().enumerate().map(|(i, h)| (h.id.clone(), i)).collect();
    let h3 = h3()?;
    let centres: Vec<(f64, f64)> = hexes
        .iter()
        .map(|h| geo::Centroid::centroid(&h.polygon).map_or((f64::NAN, f64::NAN), |c| (c.y(), c.x())))
        .collect();
    let mut columns = vec![("h3_id".to_string(), OutColumn::Str(hexes.iter().map(|h| Some(h.id.clone())).collect()))];

    for layer in config.raster.iter().filter(|l| wanted(&l.name)) {
        let t = Instant::now();
        let bil = static_dir.join(format!("{}.bil", layer.name));
        if !bil.exists() {
            eprintln!("raster {}: {} missing, skipped", layer.name, bil.display());
            continue;
        }
        let r = aggregate(&h3, &index, &bil, layer.agg, &layer.nodata, layer.nodata_fill, &centres)?;
        let filled = r.values.iter().filter(|v| v.is_some()).count();
        eprintln!(
            "raster {}: {:.1}M pixels in grid ({:.1}% with data) -> {filled}/{} hexes ({} from hex centre) in {:.2?}",
            layer.name,
            r.pixels as f64 / 1e6,
            100.0 * r.valid_pixels as f64 / r.pixels.max(1) as f64,
            hexes.len(),
            r.centre_sampled,
            t.elapsed()
        );
        columns.push((layer.name.clone(), OutColumn::F64(r.values)));
    }

    for layer in config.vector.iter().filter(|l| wanted(&l.name)) {
        let t = Instant::now();
        let (geoms, attrs) = read_vector_parts(static_dir, &layer.name)?;
        match layer.agg {
            VectorAgg::Any => {
                let r = overlay(hexes, &geoms, &[], &[], false);
                let flags: Vec<Option<bool>> = r.coverage.iter().map(|&c| Some(c > 0.0)).collect();
                let n_true = flags.iter().filter(|f| **f == Some(true)).count();
                eprintln!(
                    "vector {}: {} polygons -> {n_true}/{} hexes true in {:.2?}",
                    layer.name,
                    geoms.len(),
                    hexes.len(),
                    t.elapsed()
                );
                columns.push((layer.name.clone(), OutColumn::Bool(flags)));
            }
            VectorAgg::Mode => {
                let col_name = layer.column.as_deref().ok_or_else(|| anyhow!("{}: mode needs `column`", layer.name))?;
                let col = attrs.get(col_name).ok_or_else(|| anyhow!("{}: no column {col_name}", layer.name))?;
                let r = overlay(hexes, &geoms, &[], &[col.as_slice()], false);
                let modes = &r.modes[0];
                eprintln!(
                    "vector {}: {} polygons -> {}/{} hexes assigned in {:.2?}",
                    layer.name,
                    geoms.len(),
                    modes.iter().filter(|m| m.is_some()).count(),
                    hexes.len(),
                    t.elapsed()
                );
                if let (Some(label), Some(label_col)) = (&layer.label, &layer.label_column) {
                    let labels = attrs.get(label_col).ok_or_else(|| anyhow!("{}: no column {label_col}", layer.name))?;
                    let by_code: HashMap<&str, &str> = col
                        .iter()
                        .zip(labels)
                        .filter_map(|(c, l)| Some((c.as_deref()?, l.as_deref()?)))
                        .collect();
                    columns.push((layer.name.clone(), OutColumn::Str(modes.clone())));
                    columns.push((
                        label.clone(),
                        OutColumn::Str(
                            modes
                                .iter()
                                .map(|m| m.as_deref().and_then(|m| by_code.get(m)).map(|s| s.to_string()))
                                .collect(),
                        ),
                    ));
                } else {
                    columns.push((layer.name.clone(), OutColumn::Str(modes.clone())));
                }
            }
        }
    }

    write_table(out, columns)?;
    eprintln!("wrote {}", out.display());
    Ok(())
}
