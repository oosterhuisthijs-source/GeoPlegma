//! BAG verblijfsobjecten -> per-hex counts per gebruiksdoel, per year.
//!
//! Input (`prepare/bag.py`): one row per BAG *voorkomen* (version) of a
//! verblijfsobject, with its point (lon/lat), validity period
//! (`begin`/`eind` as yyyymmdd, `eind` null = still valid), status and
//! gebruiksdoelen (comma-separated). For every year the state on 1 January
//! is counted: voorkomens with `begin <= yyyy0101 < eind` and a counted
//! status. A verblijfsobject with several gebruiksdoelen counts once for
//! each of them (and once in the total).
//!
//! Each point is mapped to its hex once, with geoplegma's `zone_from_point`.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result, anyhow};
use geoplegma::api::DggrsApiConfig;
use geoplegma::types::{Point, RefinementLevel};
use rayon::prelude::*;

use crate::bbg::slug;
use crate::geoparquet::{OutColumn, read_table, write_table};
use crate::grid::{H3_RESOLUTION, Hex, h3};

#[derive(serde::Deserialize)]
pub struct BagConfig {
    /// Statuses counted as an existing verblijfsobject.
    pub statuses: Vec<String>,
    /// Gebruiksdoelen -> output column `num_<slug>`.
    pub gebruiksdoelen: Vec<String>,
    /// Name of the column counting every verblijfsobject.
    pub total: String,
}

impl BagConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        Ok(toml::from_str(&raw)?)
    }
}

pub fn run(config: &BagConfig, hexes: &[Hex], input: &Path, years: &[i64], out_dir: &Path) -> Result<()> {
    let t0 = Instant::now();
    let (numeric, strings) = read_table(input)?;
    let col = |name: &str| numeric.get(name).ok_or_else(|| anyhow!("{} has no column {name}", input.display()));
    let scol = |name: &str| strings.get(name).ok_or_else(|| anyhow!("{} has no column {name}", input.display()));
    let (lon, lat, begin, eind) = (col("lon")?, col("lat")?, col("begin")?, col("eind")?);
    let (status, doel) = (scol("status")?, scol("gebruiksdoel")?);
    let n = lon.len();

    // Point -> hex position (None = outside the grid).
    let index: HashMap<&str, usize> = hexes.iter().enumerate().map(|(i, h)| (h.id.as_str(), i)).collect();
    let h3 = h3()?;
    let level = RefinementLevel::new(H3_RESOLUTION)?;
    let id_only = DggrsApiConfig {
        region: false,
        center: false,
        vertex_count: false,
        children: false,
        neighbors: false,
        area_sqm: false,
        densify: false,
    };
    let hex_of: Vec<Option<usize>> = (0..n)
        .into_par_iter()
        .map(|i| {
            let (Some(x), Some(y)) = (lon[i], lat[i]) else { return None };
            let zones = h3.zone_from_point(level, Point::new(y, x), Some(id_only)).ok()?;
            index.get(zones.zones[0].id.to_string().as_str()).copied()
        })
        .collect();
    let in_grid = hex_of.iter().filter(|h| h.is_some()).count();
    eprintln!("bag: {n} voorkomens, {in_grid} in the grid, read + hexed in {:.2?}", t0.elapsed());

    let counted: Vec<bool> = status.iter().map(|s| s.as_ref().is_some_and(|s| config.statuses.contains(s))).collect();
    // Gebruiksdoel indices per voorkomen.
    let doelen: Vec<Vec<usize>> = doel
        .iter()
        .map(|d| {
            d.as_deref()
                .map(|d| d.split(',').filter_map(|g| config.gebruiksdoelen.iter().position(|c| c == g.trim())).collect())
                .unwrap_or_default()
        })
        .collect();

    for &year in years {
        let t = (year * 10000 + 101) as f64;
        let mut total = vec![0i64; hexes.len()];
        let mut counts = vec![vec![0i64; hexes.len()]; config.gebruiksdoelen.len()];
        for i in 0..n {
            let Some(h) = hex_of[i] else { continue };
            let valid = begin[i].is_some_and(|b| b <= t) && eind[i].is_none_or(|e| e > t);
            if !valid || !counted[i] {
                continue;
            }
            total[h] += 1;
            for &k in &doelen[i] {
                counts[k][h] += 1;
            }
        }
        let mut columns = vec![
            ("h3_id".to_string(), OutColumn::Str(hexes.iter().map(|h| Some(h.id.clone())).collect())),
            ("year_int".to_string(), OutColumn::I64(vec![Some(year); hexes.len()])),
            (config.total.clone(), OutColumn::F64(total.iter().map(|&c| Some(c as f64)).collect())),
        ];
        for (g, c) in config.gebruiksdoelen.iter().zip(counts) {
            columns.push((format!("num_{}", slug(g)), OutColumn::F64(c.into_iter().map(|c| Some(c as f64)).collect())));
        }
        let out = out_dir.join(format!("bag_{year}.parquet"));
        write_table(&out, columns)?;
        eprintln!("bag {year}: {} verblijfsobjecten on 1 January -> {}", total.iter().sum::<i64>(), out.display());
    }
    Ok(())
}
