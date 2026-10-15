//! Area-weighted overlay of a polygon layer onto the hex grid.
//!
//! For every (hex, source polygon) pair the intersection is computed once,
//! giving two weights that every column then reuses:
//!
//! - `source_fraction = overlap / source_area` — the share of the source
//!   polygon that falls in this hex. Used for **sum**: a count spread over a
//!   polygon is apportioned to hexes by area, so a polygon's value is
//!   conserved across the hexes it touches (partition of unity).
//! - `overlap` (m²) — used for **area-weighted average** and **mode**: a
//!   per-area quantity (percentage, mean, category) contributes in proportion
//!   to how much of the hex it covers. Divided by the overlap of the polygons
//!   that actually have a value, so suppressed/null polygons don't drag the
//!   average towards zero.
//!
//! (The Databricks `cbs_integration_to_consumption.py` used `overlap /
//! hex_area` for both sum and average — correct for the average, but not a
//! sum; see the GeoPlegma CLAUDE.md for the details.)

use std::collections::HashMap;

use geo::algorithm::orient::{Direction, Orient};
use geo::{BooleanOps, BoundingRect, GeodesicArea, MultiPolygon};
use rayon::prelude::*;
use rstar::{AABB, RTree, RTreeObject};

use crate::grid::Hex;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NumericAgg {
    Sum,
    AreaWeightedAverage,
    Min,
    Max,
}

impl NumericAgg {
    pub fn suffix(self) -> &'static str {
        match self {
            NumericAgg::Sum => "sum",
            NumericAgg::AreaWeightedAverage => "area_weighted_average",
            NumericAgg::Min => "min",
            NumericAgg::Max => "max",
        }
    }
}

pub struct NumericInput<'a> {
    pub values: &'a [Option<f64>],
    pub agg: NumericAgg,
}

pub struct OverlayResult {
    /// One output vector per numeric input, indexed like `hexes`.
    pub numeric: Vec<Vec<Option<f64>>>,
    /// One output vector per string (mode) input.
    pub modes: Vec<Vec<Option<String>>>,
    /// Fraction of each hex covered by any source polygon.
    pub coverage: Vec<f64>,
}

struct Indexed {
    index: usize,
    env: AABB<[f64; 2]>,
}

impl RTreeObject for Indexed {
    type Envelope = AABB<[f64; 2]>;
    fn envelope(&self) -> Self::Envelope {
        self.env
    }
}

/// Polygons with more vertices than this are cut into pieces first: a single
/// province-sized land-use polygon (NBBG has some with >1M vertices) would
/// otherwise be intersected in full with every hex it touches.
const MAX_PIECE_VERTICES: usize = 512;

/// Recursively halve `mp` along the longer side of its bounding box until
/// every piece has at most `MAX_PIECE_VERTICES` vertices (like PostGIS
/// `ST_Subdivide`). The pieces tile the original exactly.
fn subdivide(mp: MultiPolygon<f64>, depth: u32, out: &mut Vec<MultiPolygon<f64>>) {
    use geo::CoordsIter;
    if mp.0.is_empty() {
        return;
    }
    if mp.coords_count() <= MAX_PIECE_VERTICES || depth >= 24 {
        out.push(mp);
        return;
    }
    let Some(r) = mp.bounding_rect() else { return };
    let (min, max) = (r.min(), r.max());
    let halves = if r.width() >= r.height() {
        let mid = (min.x + max.x) / 2.0;
        [geo::Rect::new(min, geo::coord! { x: mid, y: max.y }), geo::Rect::new(geo::coord! { x: mid, y: min.y }, max)]
    } else {
        let mid = (min.y + max.y) / 2.0;
        [geo::Rect::new(min, geo::coord! { x: max.x, y: mid }), geo::Rect::new(geo::coord! { x: min.x, y: mid }, max)]
    };
    for half in halves {
        subdivide(mp.intersection(&half.to_polygon()), depth + 1, out);
    }
}

/// Source polygons cut into small pieces, each remembering the index of the
/// polygon it came from, in an R-tree.
struct PieceIndex {
    pieces: Vec<MultiPolygon<f64>>,
    owner: Vec<usize>,
    tree: RTree<Indexed>,
}

impl PieceIndex {
    fn build(geoms: &[MultiPolygon<f64>]) -> Self {
        let split: Vec<(usize, Vec<MultiPolygon<f64>>)> = geoms
            .par_iter()
            .enumerate()
            .map(|(i, g)| {
                let mut out = Vec::new();
                subdivide(g.clone().orient(Direction::Default), 0, &mut out);
                (i, out)
            })
            .collect();
        let (mut pieces, mut owner) = (Vec::new(), Vec::new());
        for (i, ps) in split {
            for p in ps {
                owner.push(i);
                pieces.push(p);
            }
        }
        let tree = RTree::bulk_load(
            pieces
                .iter()
                .enumerate()
                .filter_map(|(index, g)| {
                    let r = g.bounding_rect()?;
                    Some(Indexed { index, env: AABB::from_corners([r.min().x, r.min().y], [r.max().x, r.max().y]) })
                })
                .collect(),
        );
        PieceIndex { pieces, owner, tree }
    }

    /// (owner polygon index, overlap m²) for every piece overlapping `hex`.
    /// A polygon split into several pieces can appear more than once; the
    /// overlaps of its pieces add up to the polygon's overlap.
    fn hits(&self, hex: &Hex) -> Vec<(usize, f64)> {
        let r = hex.polygon.bounding_rect().expect("hex has a bounding rect");
        let query = AABB::from_corners([r.min().x, r.min().y], [r.max().x, r.max().y]);
        self.tree
            .locate_in_envelope_intersecting(query)
            .filter_map(|item| {
                let overlap = hex.polygon.intersection(&self.pieces[item.index]).geodesic_area_unsigned();
                (overlap > 0.0).then_some((self.owner[item.index], overlap))
            })
            .collect()
    }
}

/// Overlay `geoms` (+ attribute columns) onto `hexes`.
///
/// `empty_sum_is_zero`: what a `Sum` column reports for a hex no source
/// polygon touches. True for exhaustive count layers (CBS only publishes
/// squares that have residents/dwellings, so "no square" means zero), false
/// when absence should mean "unknown".
pub fn overlay(
    hexes: &[Hex],
    geoms: &[MultiPolygon<f64>],
    numeric: &[NumericInput],
    strings: &[&[Option<String>]],
    empty_sum_is_zero: bool,
) -> OverlayResult {
    let areas: Vec<f64> = geoms.par_iter().map(|g| g.clone().orient(Direction::Default).geodesic_area_unsigned()).collect();
    let index = PieceIndex::build(geoms);

    struct HexOut {
        numeric: Vec<Option<f64>>,
        modes: Vec<Option<String>>,
        coverage: f64,
    }

    let per_hex: Vec<HexOut> = hexes
        .par_iter()
        .map(|hex| {
            // (source index, overlap m², overlap / source area)
            let hits: Vec<(usize, f64, f64)> = index
                .hits(hex)
                .into_iter()
                .filter(|&(i, _)| areas[i] > 0.0)
                .map(|(i, overlap)| (i, overlap, (overlap / areas[i]).min(1.0)))
                .collect();
            let covered: f64 = hits.iter().map(|h| h.1).sum();

            let numeric_out = numeric
                .iter()
                .map(|input| {
                    let vals = hits.iter().filter_map(|&(i, ov, frac)| input.values[i].map(|v| (v, ov, frac)));
                    match input.agg {
                        NumericAgg::Sum => {
                            let mut any = false;
                            let s: f64 = vals.inspect(|_| any = true).map(|(v, _, frac)| v * frac).sum();
                            if any {
                                Some(s)
                            } else if hits.is_empty() && empty_sum_is_zero {
                                Some(0.0)
                            } else {
                                None
                            }
                        }
                        NumericAgg::AreaWeightedAverage => {
                            let (num, den) = vals.fold((0.0, 0.0), |(n, d), (v, ov, _)| (n + v * ov, d + ov));
                            (den > 0.0).then(|| num / den)
                        }
                        NumericAgg::Min => vals.map(|x| x.0).reduce(f64::min),
                        NumericAgg::Max => vals.map(|x| x.0).reduce(f64::max),
                    }
                })
                .collect();

            let modes_out = strings
                .iter()
                .map(|col| {
                    let mut weights: HashMap<&str, f64> = HashMap::new();
                    for &(i, ov, _) in &hits {
                        if let Some(s) = col[i].as_deref() {
                            *weights.entry(s).or_default() += ov;
                        }
                    }
                    weights
                        .into_iter()
                        .max_by(|a, b| a.1.total_cmp(&b.1).then_with(|| b.0.cmp(a.0)))
                        .map(|(s, _)| s.to_string())
                })
                .collect();

            HexOut { numeric: numeric_out, modes: modes_out, coverage: (covered / hex.area_m2).min(1.0) }
        })
        .collect();

    // Transpose hex-major -> column-major.
    let mut result = OverlayResult {
        numeric: vec![Vec::with_capacity(hexes.len()); numeric.len()],
        modes: vec![Vec::with_capacity(hexes.len()); strings.len()],
        coverage: Vec::with_capacity(hexes.len()),
    };
    for h in per_hex {
        for (col, v) in result.numeric.iter_mut().zip(h.numeric) {
            col.push(v);
        }
        for (col, v) in result.modes.iter_mut().zip(h.modes) {
            col.push(v);
        }
        result.coverage.push(h.coverage);
    }
    result
}

/// For every hex, the source polygons it overlaps and the overlap area (m²).
/// The geometric core of `overlay`, exposed for callers that aggregate
/// several attributes of the same polygons differently (e.g. NBBG, where one
/// polygon carries both its 2020 and 2022 category).
pub fn hex_overlaps(hexes: &[Hex], geoms: &[MultiPolygon<f64>]) -> Vec<Vec<(usize, f64)>> {
    let index = PieceIndex::build(geoms);
    hexes.par_iter().map(|hex| index.hits(hex)).collect()
}
