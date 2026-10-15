//! Raster -> hex aggregation: every pixel centre is mapped to its H3 cell with
//! geoplegma's `zone_from_point`, and pixel values are accumulated per cell.
//!
//! Input is a raw Float32 EHdr raster (`.bil` + `.hdr`) in lon/lat, as written
//! by `prepare/static.py` (NaN = no data). Rows are streamed in blocks so even
//! a province-wide 5m AHN (~1 GB) never has to sit in memory at once.

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result, anyhow, bail};
use geoplegma::api::{DggrsApi, DggrsApiConfig};
use geoplegma::types::{Point, RefinementLevel};
use rayon::prelude::*;

use crate::grid::H3_RESOLUTION;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RasterAgg {
    Mean,
    Min,
    Max,
    Mode,
}

struct Header {
    nrows: usize,
    ncols: usize,
    ulx: f64,
    uly: f64,
    xdim: f64,
    ydim: f64,
}

fn read_header(bil: &Path) -> Result<Header> {
    let hdr = bil.with_extension("hdr");
    let text = std::fs::read_to_string(&hdr).with_context(|| format!("reading {}", hdr.display()))?;
    let kv: HashMap<String, String> = text
        .lines()
        .filter_map(|l| {
            let mut it = l.split_whitespace();
            Some((it.next()?.to_uppercase(), it.next()?.to_string()))
        })
        .collect();
    let get = |k: &str| kv.get(k).ok_or_else(|| anyhow!("{} has no {k}", hdr.display()));
    if get("NBITS")? != "32" || kv.get("PIXELTYPE").map(String::as_str) != Some("FLOAT") {
        bail!("{}: expected Float32", hdr.display());
    }
    if get("BYTEORDER")? != "I" {
        bail!("{}: expected little-endian (BYTEORDER I)", hdr.display());
    }
    Ok(Header {
        nrows: get("NROWS")?.parse()?,
        ncols: get("NCOLS")?.parse()?,
        ulx: get("ULXMAP")?.parse()?,
        uly: get("ULYMAP")?.parse()?,
        xdim: get("XDIM")?.parse()?,
        ydim: get("YDIM")?.parse()?,
    })
}

#[derive(Clone, Default)]
struct Acc {
    sum: f64,
    n: u64,
    min: f64,
    max: f64,
    classes: HashMap<i64, u64>,
}

impl Acc {
    fn add(&mut self, v: f64, mode: bool) {
        if self.n == 0 {
            self.min = v;
            self.max = v;
        } else {
            self.min = self.min.min(v);
            self.max = self.max.max(v);
        }
        self.sum += v;
        self.n += 1;
        if mode {
            *self.classes.entry(v.round() as i64).or_default() += 1;
        }
    }
    fn merge(&mut self, o: Acc) {
        if o.n == 0 {
            return;
        }
        if self.n == 0 {
            *self = o;
            return;
        }
        self.sum += o.sum;
        self.n += o.n;
        self.min = self.min.min(o.min);
        self.max = self.max.max(o.max);
        for (k, c) in o.classes {
            *self.classes.entry(k).or_default() += c;
        }
    }
}

pub struct RasterResult {
    pub values: Vec<Option<f64>>,
    /// Pixels that landed in a grid hex (nodata ones only if `nodata_fill` is set).
    pub pixels: u64,
    /// ...of which had real (non-nodata) values.
    pub valid_pixels: u64,
    /// Hexes without any pixel centre that were filled from the pixel under the hex centre.
    pub centre_sampled: usize,
}

/// Aggregate `bil` onto the hexes (identified by `index`: h3 id -> position).
pub fn aggregate(
    h3: &Arc<dyn DggrsApi>,
    index: &HashMap<String, usize>,
    bil: &Path,
    agg: RasterAgg,
    nodata: &[f64],
    nodata_fill: Option<f64>,
    centres: &[(f64, f64)],
) -> Result<RasterResult> {
    let h = read_header(bil)?;
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
    let n_hex = index.len();
    let is_mode = agg == RasterAgg::Mode;
    let mut totals = vec![Acc::default(); n_hex];
    let (mut pixels, mut valid) = (0u64, 0u64);

    let mut reader = BufReader::with_capacity(1 << 22, File::open(bil)?);
    const BLOCK_ROWS: usize = 512;
    let mut buf = vec![0u8; h.ncols * 4 * BLOCK_ROWS];
    let mut row0 = 0;
    while row0 < h.nrows {
        let rows = BLOCK_ROWS.min(h.nrows - row0);
        let bytes = &mut buf[..h.ncols * 4 * rows];
        reader.read_exact(bytes)?;

        let (block, block_px, block_valid) = bytes
            .par_chunks(h.ncols * 4)
            .enumerate()
            .fold(
                || (HashMap::<usize, Acc>::new(), 0u64, 0u64),
                |(mut acc, mut px, mut ok), (r, row)| {
                    let lat = h.uly - (row0 + r) as f64 * h.ydim;
                    for (c, b) in row.chunks_exact(4).enumerate() {
                        let raw = f32::from_le_bytes(b.try_into().unwrap()) as f64;
                        let is_nodata = raw.is_nan() || nodata.contains(&raw) || raw.abs() > 1e30;
                        if is_nodata && nodata_fill.is_none() {
                            continue;
                        }
                        let lon = h.ulx + c as f64 * h.xdim;
                        let Ok(zones) = h3.zone_from_point(level, Point::new(lat, lon), Some(id_only)) else {
                            continue;
                        };
                        let Some(&i) = index.get(&zones.zones[0].id.to_string()) else {
                            continue;
                        };
                        px += 1;
                        if !is_nodata {
                            ok += 1;
                        }
                        let v = if is_nodata { nodata_fill.unwrap() } else { raw };
                        acc.entry(i).or_default().add(v, is_mode);
                    }
                    (acc, px, ok)
                },
            )
            .reduce(
                || (HashMap::new(), 0, 0),
                |(mut a, pa, oa), (b, pb, ob)| {
                    for (k, v) in b {
                        a.entry(k).or_default().merge(v);
                    }
                    (a, pa + pb, oa + ob)
                },
            );
        for (i, a) in block {
            totals[i].merge(a);
        }
        pixels += block_px;
        valid += block_valid;
        row0 += rows;
    }

    // Hexes that no pixel centre fell into (coarse rasters): sample the pixel
    // under the hex centre (lat, lon) instead.
    let mut file = File::open(bil)?;
    let mut sample = |lat: f64, lon: f64| -> Result<Option<f64>> {
        let row = ((h.uly - lat) / h.ydim).round();
        let col = ((lon - h.ulx) / h.xdim).round();
        if row < 0.0 || col < 0.0 || row as usize >= h.nrows || col as usize >= h.ncols {
            return Ok(None);
        }
        file.seek(SeekFrom::Start(((row as usize * h.ncols + col as usize) * 4) as u64))?;
        let mut b = [0u8; 4];
        file.read_exact(&mut b)?;
        let raw = f32::from_le_bytes(b) as f64;
        Ok(if raw.is_nan() || nodata.contains(&raw) || raw.abs() > 1e30 { nodata_fill } else { Some(raw) })
    };
    let mut centre_sampled = 0;
    for (i, a) in totals.iter_mut().enumerate() {
        if a.n == 0 {
            if let Some(v) = sample(centres[i].0, centres[i].1)? {
                a.add(v, is_mode);
                centre_sampled += 1;
            }
        }
    }

    let values = totals
        .into_iter()
        .map(|a| {
            (a.n > 0).then(|| match agg {
                RasterAgg::Mean => a.sum / a.n as f64,
                RasterAgg::Min => a.min,
                RasterAgg::Max => a.max,
                RasterAgg::Mode => a
                    .classes
                    .iter()
                    .max_by(|x, y| x.1.cmp(y.1).then(y.0.cmp(x.0)))
                    .map(|(k, _)| *k as f64)
                    .unwrap(),
            })
        })
        .collect();
    Ok(RasterResult { values, pixels, valid_pixels: valid, centre_sampled })
}
