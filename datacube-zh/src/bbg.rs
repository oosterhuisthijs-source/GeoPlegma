//! CBS Nieuw Bestand Bodemgebruik (NBBG) -> per-hex fraction of every
//! land-use category, for each reference year in the mutatiebestand.

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result, anyhow};

use crate::geoparquet::{OutColumn, read_polygon_table, write_table};
use crate::grid::Hex;
use crate::overlay::hex_overlaps;

#[derive(serde::Deserialize)]
pub struct BbgConfig {
    /// Source code column -> reference year, e.g. NBBG20 = 2020.
    pub years: BTreeMap<String, i64>,
    /// Category code -> label.
    pub categories: BTreeMap<String, String>,
}

impl BbgConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        Ok(toml::from_str(&raw)?)
    }
}

/// "Detailhandel en horeca" -> "detailhandel_en_horeca".
pub fn slug(label: &str) -> String {
    let s: String = label
        .to_lowercase()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect();
    s.split('_').filter(|p| !p.is_empty()).collect::<Vec<_>>().join("_")
}

/// Per-class area fractions of every hex: `fractions[class][hex]`, plus the
/// covered share of each hex (sum over classes). `class_of(polygon)` maps a
/// source polygon to a class index (None = ignore). Shared by NBBG and BRP.
pub fn class_fractions(
    hexes: &[Hex],
    overlaps: &[Vec<(usize, f64)>],
    n_classes: usize,
    class_of: impl Fn(usize) -> Option<usize>,
) -> (Vec<Vec<f64>>, Vec<f64>) {
    let mut fractions = vec![vec![0.0; hexes.len()]; n_classes];
    for (h, (hex, hits)) in hexes.iter().zip(overlaps).enumerate() {
        for &(i, overlap) in hits {
            if let Some(k) = class_of(i) {
                fractions[k][h] += overlap / hex.area_m2;
            }
        }
    }
    let coverage = (0..hexes.len()).map(|h| fractions.iter().map(|f| f[h]).sum::<f64>().min(1.0)).collect();
    (fractions, coverage)
}

/// Name of the class with the largest fraction per hex (None if all zero).
pub fn dominant(fractions: &[Vec<f64>], labels: &[String], n_hexes: usize) -> Vec<Option<String>> {
    (0..n_hexes)
        .map(|h| {
            let (k, f) = fractions.iter().enumerate().map(|(k, f)| (k, f[h])).max_by(|a, b| a.1.total_cmp(&b.1))?;
            (f > 0.0).then(|| labels[k].clone())
        })
        .collect()
}

pub fn run(config: &BbgConfig, hexes: &[Hex], input: &Path, out_dir: &Path) -> Result<()> {
    let t0 = Instant::now();
    let table = read_polygon_table(input)?;
    let overlaps = hex_overlaps(hexes, &table.geometries);
    eprintln!(
        "bbg: {} polygons, {} hex/polygon overlaps in {:.2?}",
        table.geometries.len(),
        overlaps.iter().map(Vec::len).sum::<usize>(),
        t0.elapsed()
    );

    let codes: Vec<(i64, String)> = config
        .categories
        .iter()
        .map(|(c, l)| Ok((c.parse::<i64>()?, l.clone())))
        .collect::<Result<_>>()?;

    for (column, year) in &config.years {
        let src = table
            .numeric
            .get(column)
            .ok_or_else(|| anyhow!("{} has no column {column}", input.display()))?;
        let mut unknown = 0usize;
        for &(i, _) in overlaps.iter().flatten() {
            if src[i].is_some_and(|c| !codes.iter().any(|(k, _)| *k == c as i64)) {
                unknown += 1;
            }
        }
        let (fractions, coverage) = class_fractions(hexes, &overlaps, codes.len(), |i| {
            let c = src[i]? as i64;
            codes.iter().position(|(k, _)| *k == c)
        });
        let labels: Vec<String> = codes.iter().map(|(_, l)| l.clone()).collect();
        let dominant = dominant(&fractions, &labels, hexes.len());

        let mut columns = vec![
            ("h3_id".to_string(), OutColumn::Str(hexes.iter().map(|h| Some(h.id.clone())).collect())),
            ("bbg_source_year".to_string(), OutColumn::I64(vec![Some(*year); hexes.len()])),
            ("bbg_coverage".to_string(), OutColumn::F64(coverage.iter().map(|&c| Some(c)).collect())),
            ("bbg_dominant".to_string(), OutColumn::Str(dominant)),
        ];
        for ((_, label), f) in codes.iter().zip(fractions) {
            // Hexes the map doesn't reach at all get null rather than 0.
            let vals = f
                .iter()
                .zip(&coverage)
                .map(|(&v, &c)| (c > 0.0).then_some(v.min(1.0)))
                .collect();
            columns.push((format!("bbg_{}_fraction", slug(label)), OutColumn::F64(vals)));
        }
        let out = out_dir.join(format!("bbg_{year}.parquet"));
        write_table(&out, columns)?;
        let mut cov_sorted = coverage.clone();
        cov_sorted.sort_by(f64::total_cmp);
        eprintln!(
            "bbg {year} ({column}): coverage median {:.3}, min {:.3}; {unknown} overlaps with unmapped codes -> {}",
            cov_sorted[cov_sorted.len() / 2],
            cov_sorted[0],
            out.display()
        );
    }
    Ok(())
}
