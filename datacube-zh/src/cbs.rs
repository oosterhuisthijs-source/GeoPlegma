//! CBS vierkantstatistieken (100m squares) -> hex grid, one output per year.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result};

use crate::geoparquet::{OutColumn, read_polygon_table, write_table};
use crate::grid::Hex;
use crate::overlay::{NumericAgg, NumericInput, overlay};

#[derive(serde::Deserialize)]
pub struct CbsConfig {
    pub sentinel_threshold: f64,
    pub percentage_max: Option<f64>,
    #[serde(default)]
    pub complements: Vec<[String; 2]>,
    #[serde(default)]
    pub null_strings: Vec<String>,
    #[serde(default)]
    pub rename: HashMap<String, String>,
    pub aggregation: Aggregation,
}

#[derive(serde::Deserialize)]
pub struct Aggregation {
    #[serde(default)]
    pub sum: Vec<String>,
    #[serde(default)]
    pub area_weighted_average: Vec<String>,
    #[serde(default)]
    pub min: Vec<String>,
    #[serde(default)]
    pub max: Vec<String>,
    #[serde(default)]
    pub mode: Vec<String>,
}

impl CbsConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        Ok(toml::from_str(&raw)?)
    }

    /// Every configured numeric output, in a stable order.
    fn numeric_columns(&self) -> Vec<(&str, NumericAgg)> {
        let a = &self.aggregation;
        [
            (&a.sum, NumericAgg::Sum),
            (&a.area_weighted_average, NumericAgg::AreaWeightedAverage),
            (&a.min, NumericAgg::Min),
            (&a.max, NumericAgg::Max),
        ]
        .into_iter()
        .flat_map(|(cols, agg)| cols.iter().map(move |c| (c.as_str(), agg)))
        .collect()
    }
}

/// Columns in the source that are neither configured nor known identifiers.
const IGNORED: &[&str] = &["crs28992res100m", "c28992r100", "jaarcode", "data_year", "geom_bbox"];

pub fn run(config: &CbsConfig, hexes: &[Hex], input: &Path, year: i64, out: &Path) -> Result<()> {
    let t0 = Instant::now();
    let mut table = read_polygon_table(input)?;
    let n_src = table.geometries.len();

    // Canonical names + sentinel/null cleanup.
    let mut numeric: HashMap<String, Vec<Option<f64>>> = table
        .numeric
        .drain()
        .map(|(name, mut vals)| {
            for v in vals.iter_mut() {
                if v.is_some_and(|x| x <= config.sentinel_threshold) {
                    *v = None;
                }
            }
            (config.rename.get(&name).cloned().unwrap_or(name), vals)
        })
        .collect();
    if let Some(max) = config.percentage_max {
        let mut invalid = 0;
        for (name, vals) in numeric.iter_mut().filter(|(n, _)| n.starts_with("percentage_")) {
            for v in vals.iter_mut() {
                if v.is_some_and(|x| x > max) {
                    *v = None;
                    invalid += 1;
                }
            }
            let _ = name;
        }
        if invalid > 0 {
            eprintln!("  {invalid} percentage values > {max} set to null");
        }
    }
    for [a, b] in &config.complements {
        if let (Some(va), Some(vb)) = (numeric.get(a).cloned(), numeric.get(b).cloned()) {
            let fill = |x: &Vec<Option<f64>>, y: &Vec<Option<f64>>| -> (Vec<Option<f64>>, usize) {
                let mut n = 0;
                let out = x
                    .iter()
                    .zip(y)
                    .map(|(x, y)| match (x, y) {
                        (None, Some(y)) => {
                            n += 1;
                            Some(100.0 - y)
                        }
                        _ => *x,
                    })
                    .collect();
                (out, n)
            };
            let (fa, na) = fill(&va, &vb);
            let (fb, nb) = fill(&vb, &va);
            numeric.insert(a.clone(), fa);
            numeric.insert(b.clone(), fb);
            eprintln!("  complement {a}/{b}: filled {na} + {nb} suppressed values");
        }
    }
    let strings: HashMap<String, Vec<Option<String>>> = table
        .strings
        .drain()
        .map(|(name, mut vals)| {
            for v in vals.iter_mut() {
                if v.as_ref().is_some_and(|s| config.null_strings.contains(s)) {
                    *v = None;
                }
            }
            (config.rename.get(&name).cloned().unwrap_or(name), vals)
        })
        .collect();

    let wanted = config.numeric_columns();
    let configured: BTreeSet<&str> =
        wanted.iter().map(|w| w.0).chain(config.aggregation.mode.iter().map(String::as_str)).collect();
    let unconfigured: Vec<&String> = numeric
        .keys()
        .chain(strings.keys())
        .filter(|k| !configured.contains(k.as_str()) && !IGNORED.contains(&k.as_str()))
        .collect();
    let missing: Vec<&str> = configured
        .iter()
        .copied()
        .filter(|c| !numeric.contains_key(*c) && !strings.contains_key(*c))
        .collect();

    let all_null = vec![None; n_src];
    let all_null_str: Vec<Option<String>> = vec![None; n_src];
    let inputs: Vec<NumericInput> = wanted
        .iter()
        .map(|&(name, agg)| NumericInput { values: numeric.get(name).unwrap_or(&all_null), agg })
        .collect();
    let mode_inputs: Vec<&[Option<String>]> = config
        .aggregation
        .mode
        .iter()
        .map(|name| strings.get(name).unwrap_or(&all_null_str).as_slice())
        .collect();
    let t_read = t0.elapsed();

    let t1 = Instant::now();
    let result = overlay(hexes, &table.geometries, &inputs, &mode_inputs, true);
    let t_overlay = t1.elapsed();

    let n = hexes.len();
    let mut columns = vec![
        ("h3_id".to_string(), OutColumn::Str(hexes.iter().map(|h| Some(h.id.clone())).collect())),
        ("year_int".to_string(), OutColumn::I64(vec![Some(year); n])),
        ("cbs_coverage".to_string(), OutColumn::F64(result.coverage.iter().map(|&c| Some(c)).collect())),
    ];
    for ((name, agg), vals) in wanted.iter().zip(result.numeric) {
        columns.push((format!("{name}_{}", agg.suffix()), OutColumn::F64(vals)));
    }
    for (name, vals) in config.aggregation.mode.iter().zip(result.modes) {
        columns.push((format!("{name}_mode"), OutColumn::Str(vals)));
    }
    write_table(out, columns)?;

    eprintln!(
        "cbs {year}: {n_src} squares -> {n} hexes | read {t_read:.2?}, overlay {t_overlay:.2?} -> {}",
        out.display()
    );
    if !missing.is_empty() {
        eprintln!("  not in source this year (null): {}", missing.join(", "));
    }
    if !unconfigured.is_empty() {
        eprintln!("  in source but not configured (dropped): {unconfigured:?}");
    }
    Ok(())
}
