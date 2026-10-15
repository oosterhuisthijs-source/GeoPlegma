#!/usr/bin/env python3
"""TRIAL: TESSERA embeddings -> per-hex 16-dim PCA embedding + year-on-year change index.

For each year, per H3 res 9 cell of the datacube grid:
    tessera_pc01..pc16       hex-mean embedding (dequantized, all pixels incl. water) projected on a
                             16-component PCA. The PCA is fitted once (on the hex means of the fit years,
                             pooled) and saved, so later years are projected onto the SAME axes and stay
                             comparable. Signs/order of components carry no meaning of their own.
                             Year effects (fit 2024+2025, ZH 2022-2025): PC1-PC3 have province-wide year
                             offsets <= 0.07 SD and are stable per hex (r 0.86-0.99 year to year); PC5, PC6
                             and PC8 shift by 0.2-0.6 SD per year (weather/acquisition), so for comparisons
                             across years subtract the year's province mean or use the change columns.
    tessera_pixels           number of 10 m pixels averaged (pixel centre inside the hex)
For each year after the first in --years, compared with the previous year IN THAT LIST (usually y-1), over
LAND pixels only:
    tessera_change_mean      mean per-pixel cosine distance between the two years' embeddings (0 = same)
    tessera_change_share     share of land pixels whose distance exceeds a fixed threshold T. T is the
                             95th percentile of all land-pixel distances of the first pair computed, saved
                             with the PCA and reused for later pairs, so shares compare across years
                             (for the first pair the province average is ~5% by construction).
    tessera_land_pixels      land pixels used for the change metrics
    ..._agri / ..._nonagri   the same, split by whether the pixel lies in a BRP parcel in either year
                             (tessera_change_agri_mean/_share, tessera_change_nonagri_mean/_share,
                             tessera_agri_pixels, tessera_nonagri_pixels). Agricultural change is dominated
                             by crop rotation; non-agricultural change (built-up, nature, infrastructure) has
                             its own fixed threshold (p95 of non-agricultural pixel distances, first pair).
    tessera_change_from      the base year of the comparison (a 2-year gap gives larger distances)
Water = NBBG 2022 classes 70-83 (a static mask: sea/water pixels have embeddings too, and their
year-to-year noise would otherwise read as change).

Cosine distance is invariant to the per-pixel quantization scale, so change uses the raw int8 vectors.

Embeddings: geotessera v1.1 NPY tiles (`cambridge` variant), one directory per year:
    geotessera download --bbox 3.83,51.65,5.15,52.35 --year <Y> -f npy -o own_data/tessera/tiles_<Y>
Tiles overlap ~80 m; only pixels whose centre lies in the tile's own 0.1° cell are used.
Hex assignment uses the `h3` library (res 9; same cells as geoplegma's h3o backend), cached per tile.

Writes own_data/work/layers/tessera_<year>.parquet (h3_id + the columns above) and
own_data/tessera/pca_threshold.npz. Not yet wired into assemble.py.

Usage (pilot venv: rasterio, h3, scikit-learn):
    own_data/tessera/.venv/bin/python datacube-zh/prepare/tessera_hex.py --years 2024 2025
"""
import argparse
import glob
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import rasterio
from rasterio.features import rasterize
from shapely import from_wkb

TILE_DEG = 0.1
CRS_TILES = "EPSG:32631"
WATER = tuple(range(70, 84))
N_PC = 16
DIM = 128
CHANGE_PCTL = 95


def tile_names(tess, year):
    root = f"{tess}/tiles_{year}/global_0.1_degree_representation/{year}"
    return {os.path.basename(d) for d in glob.glob(f"{root}/grid_*") if glob.glob(f"{d}/*_scales.npy")}


def tile_paths(tess, year, name):
    d = f"{tess}/tiles_{year}/global_0.1_degree_representation/{year}/{name}/{name}"
    return d + ".npy", d + "_scales.npy", f"{tess}/tiles_{year}/global_0.1_degree_tiff_all/{name}.tiff"


def pixel_index(args):
    """Per tile, cached: grid position of every pixel's hex (-1 = outside own cell/grid) and a water mask."""
    tess, year, name, grid_ints, water_wkb = args
    cache = f"{tess}/cache/{name}.npz"
    _, _, tif = tile_paths(tess, year, name)
    with rasterio.open(tif) as r:
        transform, shape = r.transform, r.shape
    if os.path.exists(cache):
        c = np.load(cache)
        if tuple(c["transform"]) == tuple(transform)[:6] and c["hexpos"].shape == shape:
            return name
    import h3

    lon_c, lat_c = map(float, re.match(r"grid_(-?[\d.]+)_(-?[\d.]+)", name).groups())
    lon0, lat0 = lon_c - TILE_DEG / 2, lat_c - TILE_DEG / 2
    cols, rows = np.meshgrid(np.arange(shape[1]), np.arange(shape[0]))
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    lon, lat = pyproj.Transformer.from_crs(CRS_TILES, "EPSG:4326", always_xy=True).transform(x, y)
    own = (lon >= lon0) & (lon < lon0 + TILE_DEG) & (lat >= lat0) & (lat < lat0 + TILE_DEG)
    hexpos = np.full(shape, -1, np.int32)
    ints = np.fromiter((h3.str_to_int(h3.latlng_to_cell(a, o, 9)) for a, o in zip(lat[own], lon[own])),
                       np.uint64, count=int(own.sum()))
    pos = np.searchsorted(grid_ints, ints)
    pos = np.minimum(pos, len(grid_ints) - 1)
    hit = grid_ints[pos] == ints
    hexpos[own] = np.where(hit, pos, -1)
    geoms = [g for g in gpd.GeoSeries(from_wkb(water_wkb), crs="EPSG:4326").to_crs(CRS_TILES)
             .clip_by_rect(transform.c, transform.f + shape[0] * transform.e, transform.c + shape[1] * transform.a, transform.f)
             if not g.is_empty]
    water = rasterize([(g, 1) for g in geoms], out_shape=shape, transform=transform, fill=0, dtype="uint8") if geoms \
        else np.zeros(shape, np.uint8)
    np.savez_compressed(cache, hexpos=hexpos, water=water, transform=np.array(tuple(transform)[:6]))
    return name


def group_sum(idx, vals, n):
    """Sum rows of `vals` per group index (0..n-1)."""
    order = np.argsort(idx, kind="stable")
    idx, vals = idx[order], vals[order]
    starts = np.flatnonzero(np.r_[True, idx[1:] != idx[:-1]])
    out = np.zeros((n,) + vals.shape[1:], np.float64)
    out[idx[starts]] = np.add.reduceat(vals.astype(np.float64), starts, axis=0)
    return out


def hex_means(tess, year, names, n_hex):
    sums, counts = np.zeros((n_hex, DIM)), np.zeros(n_hex)
    for name in names:
        npy, sc, _ = tile_paths(tess, year, name)
        c = np.load(f"{tess}/cache/{name}.npz")
        m = c["hexpos"] >= 0
        if not m.any():
            continue
        e = np.asarray(np.load(npy, mmap_mode="r")[m], np.float32) * np.load(sc)[m][:, None]
        sums += group_sum(c["hexpos"][m], e, n_hex)
        counts += np.bincount(c["hexpos"][m], minlength=n_hex)
    return sums, counts


def load_brp(data, year):
    """All BRP parcels of a year (any crop), in the tiles' CRS, with a spatial index."""
    con = duckdb.connect()
    con.sql("INSTALL spatial; LOAD spatial")
    wkb = [bytes(b) for b in con.sql(f"SELECT ST_AsWKB(geom) w FROM '{data}/work/brp/brp_{year}.parquet'").fetchnumpy()["w"]]
    return gpd.GeoSeries(from_wkb(wkb), crs="EPSG:4326").to_crs(CRS_TILES)


def pixel_change(tess, y0, y1, names, brp_sets):
    """Per land pixel: cosine distance between the years, and whether it lies in a BRP parcel (either year)."""
    hp, dist, agri = [], [], []
    for name in names:
        c = np.load(f"{tess}/cache/{name}.npz")
        m = (c["hexpos"] >= 0) & (c["water"] == 0)
        if not m.any():
            continue
        tr = rasterio.Affine(*c["transform"])
        shape = m.shape
        box = gpd.GeoSeries.from_xy([tr.c, tr.c + shape[1] * tr.a], [tr.f + shape[0] * tr.e, tr.f]).union_all().envelope
        geoms = [g for brp in brp_sets for g in brp.iloc[brp.sindex.query(box)] if g is not None and not g.is_empty]
        in_brp = rasterize([(g, 1) for g in geoms], out_shape=shape, transform=tr, fill=0, dtype="uint8") if geoms \
            else np.zeros(shape, np.uint8)
        a = np.asarray(np.load(tile_paths(tess, y0, name)[0], mmap_mode="r")[m], np.float32)
        b = np.asarray(np.load(tile_paths(tess, y1, name)[0], mmap_mode="r")[m], np.float32)
        cos = (a * b).sum(1) / np.maximum(np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-9)
        hp.append(c["hexpos"][m])
        dist.append((1.0 - cos).astype(np.float32))
        agri.append(in_brp[m].astype(bool))
    return np.concatenate(hp), np.concatenate(dist), np.concatenate(agri)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, nargs="+", required=True)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--tess", default="own_data/tessera")
    ap.add_argument("--refit", action="store_true", help="refit PCA + threshold even if pca_threshold.npz exists")
    args = ap.parse_args()
    t0 = time.time()
    years = sorted(args.years)
    import h3
    from sklearn.decomposition import PCA

    grid = duckdb.sql(f"SELECT h3_id FROM '{args.data}/work/grid.parquet'").df()
    grid["int"] = [h3.str_to_int(h) for h in grid.h3_id]
    grid = grid.sort_values("int").reset_index(drop=True)
    grid_ints = grid["int"].to_numpy(np.uint64)
    n_hex = len(grid)

    names = sorted(set.intersection(*(tile_names(args.tess, y) for y in years)))
    print(f"{len(names)} tiles present in all of {years}", flush=True)
    con = duckdb.connect()
    con.sql("INSTALL spatial; LOAD spatial")
    water = [bytes(b) for b in con.sql(
        f"SELECT ST_AsWKB(Shape) w FROM '{args.data}/work/bbg/nbbg_2020_2022.parquet' "
        f"WHERE NBBG22 IN ({','.join(map(str, WATER))})").fetchnumpy()["w"]]
    os.makedirs(f"{args.tess}/cache", exist_ok=True)
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as ex:
        list(ex.map(pixel_index, [(args.tess, years[0], n, grid_ints, water) for n in names]))
    print(f"pixel -> hex index ready ({time.time() - t0:.0f}s)", flush=True)
    # the pixel grid must be identical across years for the per-pixel change
    for y in years[1:]:
        for n in names:
            with rasterio.open(tile_paths(args.tess, y, n)[2]) as r:
                assert tuple(r.transform)[:6] == tuple(np.load(f"{args.tess}/cache/{n}.npz")["transform"]), (y, n)

    means = {}
    for y in years:
        s, cnt = hex_means(args.tess, y, names, n_hex)
        means[y] = (s / np.maximum(cnt, 1)[:, None], cnt)
        print(f"{y}: hex means done, {int((cnt > 0).sum())}/{n_hex} hexes with pixels ({time.time() - t0:.0f}s)", flush=True)

    state = f"{args.tess}/pca_threshold.npz"
    have = os.path.exists(state) and not args.refit
    if have:
        st = np.load(state)
        pca_mean, comps, thr, thr_na = st["mean"], st["components"], float(st["threshold"]), float(st["threshold_nonagri"])
        print(f"reusing PCA + threshold from {state} (fit years {st['fit_years'].tolist()})")
    else:
        fit = np.vstack([means[y][0][means[y][1] >= 100] for y in years])  # hexes with >= 1 ha of pixels
        pca = PCA(n_components=N_PC, random_state=0).fit(fit)
        pca_mean, comps = pca.mean_, pca.components_
        print("PCA explained variance:", np.round(pca.explained_variance_ratio_, 3).tolist(),
              f"cumulative {pca.explained_variance_ratio_.sum():.3f}")
        thr = None

    changes = {}
    for y0, y1 in zip(years, years[1:]):
        hp, d, agri = pixel_change(args.tess, y0, y1, names, [load_brp(args.data, y0), load_brp(args.data, y1)])
        if thr is None:
            thr = float(np.percentile(d, CHANGE_PCTL))
            thr_na = float(np.percentile(d[~agri], CHANGE_PCTL))
            print(f"change thresholds: all land T = {thr:.4f}, non-agricultural T = {thr_na:.4f} (p{CHANGE_PCTL} of {y0}->{y1})")

        def per_hex(sel, t):
            n = np.bincount(hp[sel], minlength=n_hex)
            mean = np.bincount(hp[sel], d[sel], n_hex) / np.maximum(n, 1)
            share = np.bincount(hp[sel], (d[sel] > t).astype(float), n_hex) / np.maximum(n, 1)
            return mean, share, n

        changes[y1] = {"": per_hex(np.ones(len(d), bool), thr), "_agri": per_hex(agri, thr),
                       "_nonagri": per_hex(~agri, thr_na)}
        print(f"{y0}->{y1}: median pixel distance {np.median(d):.4f} (agri {np.median(d[agri]):.4f}, "
              f"non-agri {np.median(d[~agri]):.4f}), {len(d)} land px, {agri.mean():.0%} in BRP ({time.time() - t0:.0f}s)")
    if not have:
        np.savez(state, mean=pca_mean, components=comps, threshold=thr if thr is not None else np.nan,
                 threshold_nonagri=thr_na if thr is not None else np.nan,
                 fit_years=np.array(years), change_percentile=CHANGE_PCTL)

    os.makedirs(f"{args.data}/work/layers", exist_ok=True)
    for y in years:
        m, cnt = means[y]
        pcs = (m - pca_mean) @ comps.T
        df = pd.DataFrame({"h3_id": grid.h3_id})
        for k in range(N_PC):
            df[f"tessera_pc{k + 1:02d}"] = np.where(cnt > 0, pcs[:, k], np.nan).astype(np.float32)
        df["tessera_pixels"] = cnt.astype(np.int32)
        if y in changes:
            for suffix, (cm, cs, n) in changes[y].items():
                df[f"tessera_change{suffix}_mean"] = np.where(n > 0, cm, np.nan).astype(np.float32)
                df[f"tessera_change{suffix}_share"] = np.where(n > 0, cs, np.nan).astype(np.float32)
                df[f"tessera{suffix or '_land'}_pixels"] = n.astype(np.int32)
            df["tessera_change_from"] = np.int16(years[years.index(y) - 1])
        out = f"{args.data}/work/layers/tessera_{y}.parquet"
        df.to_parquet(out, index=False)
        print(f"wrote {out}: {len(df)} rows x {df.shape[1]} cols")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
