//! Hex index raster: for every pixel of a regular RD (EPSG:28992) grid, the position (+1) of the res-9 hex its
//! centre falls in, in the order of the region's grid.parquet (0 = no hex of the region). Lets a Python step
//! aggregate any number of per-pixel metrics on that grid to hexes with a plain `bincount`, while geoplegma
//! still decides which hex every pixel belongs to (Sentinel-2 NDVI, prepare/s2_metrics.py).
//!
//! Output: `<out>` (little-endian u32, row-major from the top row) + `<out>.json` (grid definition, hex count).
//! RD -> WGS84 uses the approximation formulas of Schreutelkamp & Strang van Hees (2001), accurate to well under a
//! metre in the Netherlands: far below the 10 m pixel size.

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

use anyhow::Result;
use geoplegma::api::DggrsApiConfig;
use geoplegma::types::{Point, RefinementLevel};
use rayon::prelude::*;

use crate::grid::{H3_RESOLUTION, Hex, h3};

/// RD (x, y in metres) -> (lat, lon) in degrees.
pub fn rd_to_wgs84(x: f64, y: f64) -> (f64, f64) {
    const LAT: [(i32, i32, f64); 11] = [
        (0, 1, 3235.65389), (2, 0, -32.58297), (0, 2, -0.24750), (2, 1, -0.84978), (0, 3, -0.06550),
        (2, 2, -0.01709), (1, 0, -0.00738), (4, 0, 0.00530), (2, 3, -0.00039), (4, 1, 0.00033), (1, 1, -0.00012),
    ];
    const LON: [(i32, i32, f64); 12] = [
        (1, 0, 5260.52916), (1, 1, 105.94684), (1, 2, 2.45656), (3, 0, -0.81885), (1, 3, 0.05594),
        (3, 1, -0.05607), (0, 1, 0.01199), (3, 2, -0.00256), (1, 4, 0.00128), (0, 2, 0.00022),
        (2, 0, -0.00022), (5, 0, 0.00026),
    ];
    let dx = (x - 155000.0) * 1e-5;
    let dy = (y - 463000.0) * 1e-5;
    let lat: f64 = LAT.iter().map(|&(p, q, k)| k * dx.powi(p) * dy.powi(q)).sum();
    let lon: f64 = LON.iter().map(|&(p, q, k)| k * dx.powi(p) * dy.powi(q)).sum();
    (52.15517440 + lat / 3600.0, 5.38720621 + lon / 3600.0)
}

pub struct GridDef {
    pub x0: f64,
    pub y0: f64,
    pub res: f64,
    pub cols: usize,
    pub rows: usize,
}

pub fn run(hexes: &[Hex], g: &GridDef, out: &Path) -> Result<()> {
    let index: HashMap<String, u32> = hexes.iter().enumerate().map(|(i, h)| (h.id.clone(), i as u32 + 1)).collect();
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
    let mut w = BufWriter::with_capacity(1 << 24, File::create(out)?);
    let mut hit = 0u64;
    const BLOCK: usize = 256;
    let t = std::time::Instant::now();
    for r0 in (0..g.rows).step_by(BLOCK) {
        let rows: Vec<Vec<u32>> = (r0..(r0 + BLOCK).min(g.rows))
            .into_par_iter()
            .map(|r| {
                let y = g.y0 - (r as f64 + 0.5) * g.res;
                (0..g.cols)
                    .map(|c| {
                        let (lat, lon) = rd_to_wgs84(g.x0 + (c as f64 + 0.5) * g.res, y);
                        h3.zone_from_point(level, Point::new(lat, lon), Some(id_only))
                            .ok()
                            .and_then(|z| index.get(&z.zones[0].id.to_string()).copied())
                            .unwrap_or(0)
                    })
                    .collect()
            })
            .collect();
        for row in rows {
            hit += row.iter().filter(|&&v| v > 0).count() as u64;
            for v in row {
                w.write_all(&v.to_le_bytes())?;
            }
        }
        if (r0 / BLOCK) % 16 == 0 {
            eprintln!("hexindex: row {r0}/{} ({:.0?})", g.rows, t.elapsed());
        }
    }
    w.flush()?;
    let meta = serde_json::json!({
        "crs": "EPSG:28992", "x0": g.x0, "y0": g.y0, "res": g.res, "cols": g.cols, "rows": g.rows,
        "dtype": "uint32 little-endian, row-major from the top; value = position in grid.parquet + 1, 0 = no hex",
        "hexes": hexes.len(), "pixels_in_hexes": hit,
    });
    std::fs::write(out.with_extension("json"), serde_json::to_string_pretty(&meta)?)?;
    eprintln!("hexindex: {hit} of {} pixels in {} hexes -> {} ({:.0?})", g.rows * g.cols, hexes.len(), out.display(), t.elapsed());
    Ok(())
}
