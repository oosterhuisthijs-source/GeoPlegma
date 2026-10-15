#!/usr/bin/env python3
"""PILOT: split grassland into agricultural vs natural with TESSERA embeddings (2022, Zuid-Holland).

Question: can annual 10 m TESSERA embeddings tell intensively farmed grassland from natural / extensive
grassland, and where would that change the per-hex split compared to the registrations (BRP, NBBG)?
In SimCityZH that split matters: `Agrarisch grasland` is LivestockArea (the biggest nitrogen term) and
`Natuurlijk grasland` counts as NatureArea.

Labels (BRP 2022 gewaspercelen, 10 m inward buffer so mixed edge pixels are not used for training):
    0 = agricultural grass: 265 Grasland blijvend, 266 Grasland tijdelijk
    1 = natural grass:      331 Grasland natuurlijk (hoofdfunctie landbouw), 332 (hoofdfunctie natuur),
                            1926 Agrarisch natuurmengsel
Prediction domain: BRP grass parcels (above) plus NBBG 2022 classes 53 (Agrarisch grasland),
61 (Open droog natuurlijk terrein) and 62 (Open nat natuurlijk terrein).

Stages (each caches its output under --work):
    sample   per tile: rasterize labels, draw training pixels           -> samples.npz
    train    LightGBM, spatial CV grouped by tile (out-of-fold metrics) -> model.txt, cv_report.json
    predict  per tile: P(natural) for every domain pixel, per-hex counts -> hex_grass_2022.parquet,
                                                                            parcels_2022.parquet, prob/*.tif

Hex assignment uses the `h3` library (res 9, the same cells as geoplegma's h3o backend). If the pilot is
kept, the production path is a lon/lat raster through datacube-zh's `raster.rs` (geoplegma), like the
other static rasters.

Embeddings: geotessera v1.1 NPY tiles (the `cambridge` variant; the default `dclimate` run is Zarr-only),
downloaded with
    geotessera download --bbox 3.83,51.65,5.15,52.35 --year 2022 -f npy -o <work>/tiles_2022

Usage (run with the pilot venv, which has rasterio/lightgbm/h3):
    own_data/tessera/.venv/bin/python datacube-zh/prepare/tessera_grass.py sample|train|predict|all
"""
import argparse
import glob
import json
import os
import re
import time

import duckdb
import geopandas as gpd
import numpy as np
import pyproj
import rasterio
from rasterio.features import rasterize
from shapely import from_wkb

YEAR = 2022
AGRI, NATURAL = (265, 266), (331, 332, 1926)
NBBG_DOMAIN = (53, 61, 62)
TILE_DEG = 0.1
SAMPLES_PER_CLASS_PER_TILE = 3000
CRS_TILES = "EPSG:32631"
SEED = 0


def load_vectors(data):
    """BRP grass parcels and NBBG domain polygons, in the tiles' CRS."""
    con = duckdb.connect()
    con.sql("INSTALL spatial; LOAD spatial")
    codes = ",".join(map(str, AGRI + NATURAL))
    brp = con.sql(
        f"SELECT gewascode, ST_AsWKB(geom) AS wkb FROM '{data}/work/brp/brp_{YEAR}.parquet' WHERE gewascode IN ({codes})"
    ).fetchnumpy()
    brp = gpd.GeoDataFrame(
        {"gewascode": brp["gewascode"], "label": np.isin(brp["gewascode"], NATURAL).astype(np.uint8)},
        geometry=from_wkb([bytes(b) for b in brp["wkb"]]), crs="EPSG:4326",
    ).to_crs(CRS_TILES)
    brp["parcel"] = np.arange(len(brp), dtype=np.int32)
    nb = con.sql(
        f"SELECT NBBG22 AS nbbg, ST_AsWKB(Shape) AS wkb FROM '{data}/work/bbg/nbbg_2020_2022.parquet' "
        f"WHERE NBBG22 IN ({','.join(map(str, NBBG_DOMAIN))})"
    ).fetchnumpy()
    nbbg = gpd.GeoDataFrame({"nbbg": nb["nbbg"]}, geometry=from_wkb([bytes(b) for b in nb["wkb"]]), crs="EPSG:4326").to_crs(CRS_TILES)
    return brp, nbbg


def tiles(work):
    """(name, lon0, lat0, emb_path, scales_path, tiff_path) per tile; lon0/lat0 = the tile's 0.1° cell corner."""
    out = []
    for npy in sorted(glob.glob(f"{work}/tiles_{YEAR}/global_0.1_degree_representation/{YEAR}/*/*.npy")):
        if npy.endswith("_scales.npy"):
            continue
        name = os.path.basename(npy)[:-4]
        lon_c, lat_c = map(float, re.match(r"grid_(-?[\d.]+)_(-?[\d.]+)", name).groups())
        tif = f"{work}/tiles_{YEAR}/global_0.1_degree_tiff_all/{name}.tiff"
        scales = npy[:-4] + "_scales.npy"
        if os.path.exists(tif) and os.path.exists(scales):
            out.append((name, round(lon_c - TILE_DEG / 2, 2), round(lat_c - TILE_DEG / 2, 2), npy, scales, tif))
    return out


TO_WGS = pyproj.Transformer.from_crs(CRS_TILES, "EPSG:4326", always_xy=True)


def tile_rasters(tile, brp, nbbg):
    """Everything per pixel for one tile: embeddings (int8 + scales), labels, domain, own-cell mask, lon/lat."""
    name, lon0, lat0, npy, scales_path, tif = tile
    with rasterio.open(tif) as r:
        transform, shape, bounds = r.transform, r.shape, r.bounds
    emb = np.load(npy, mmap_mode="r")
    scales = np.load(scales_path)
    assert emb.shape[:2] == shape, (name, emb.shape, shape)

    box = (bounds.left, bounds.bottom, bounds.right, bounds.top)
    b = brp.iloc[brp.sindex.query(gpd.GeoSeries.from_xy([box[0], box[2]], [box[1], box[3]]).union_all().envelope)]
    n = nbbg.iloc[nbbg.sindex.query(gpd.GeoSeries.from_xy([box[0], box[2]], [box[1], box[3]]).union_all().envelope)]

    def burn(geoms, values, fill, dtype):
        pairs = [(g, v) for g, v in zip(geoms, values) if g is not None and not g.is_empty]
        if not pairs:
            return np.full(shape, fill, dtype)
        return rasterize(pairs, out_shape=shape, transform=transform, fill=fill, dtype=dtype)

    parcel = burn(b.geometry, b.parcel, -1, "int32")                    # full parcels (domain + parcel id)
    train = burn(b.geometry.buffer(-10), b.label + 1, 0, "uint8")       # 0 none, 1 agri, 2 natural (interior)
    nbbg_code = burn(n.geometry, n.nbbg, 0, "uint8")

    # pixel centres -> lon/lat; keep only the tile's own 0.1° cell (tiles overlap their neighbours by ~80 m)
    cols, rows = np.meshgrid(np.arange(shape[1]), np.arange(shape[0]))
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    lon, lat = TO_WGS.transform(x, y)
    own = (lon >= lon0) & (lon < lon0 + TILE_DEG) & (lat >= lat0) & (lat < lat0 + TILE_DEG)
    valid = own & np.isfinite(scales) & (scales > 0)
    return dict(emb=emb, scales=scales, parcel=parcel, train=train, nbbg=nbbg_code, own=valid,
                lon=lon, lat=lat, transform=transform, shape=shape)


def features(t, mask):
    return np.asarray(t["emb"][mask], dtype=np.float32) * t["scales"][mask][:, None]


def stage_sample(args, brp, nbbg):
    rng = np.random.default_rng(SEED)
    X, y, grp, par = [], [], [], []
    for k, tile in enumerate(tiles(args.work)):
        t = tile_rasters(tile, brp, nbbg)
        for cls in (1, 2):
            idx = np.flatnonzero((t["train"] == cls).ravel() & t["own"].ravel())
            if len(idx) == 0:
                continue
            idx = rng.choice(idx, min(len(idx), SAMPLES_PER_CLASS_PER_TILE), replace=False)
            m = np.zeros(t["shape"], bool)
            m.ravel()[idx] = True
            X.append(features(t, m))
            y.append(np.full(m.sum(), cls - 1, np.uint8))
            grp.append(np.full(m.sum(), k, np.int16))
            par.append(t["parcel"][m])
        print(f"sample {tile[0]}: {sum(map(len, y))} total", flush=True)
    np.savez(f"{args.work}/samples.npz", X=np.concatenate(X), y=np.concatenate(y),
             group=np.concatenate(grp), parcel=np.concatenate(par))


def stage_train(args, brp):
    import lightgbm as lgb
    from sklearn.metrics import balanced_accuracy_score, confusion_matrix, f1_score, roc_auc_score
    from sklearn.model_selection import GroupKFold

    d = np.load(f"{args.work}/samples.npz")
    X, y, g, parcel = d["X"], d["y"], d["group"], d["parcel"]
    params = dict(objective="binary", learning_rate=0.05, num_leaves=63, min_child_samples=50,
                  feature_fraction=0.5, bagging_fraction=0.8, bagging_freq=1, verbose=-1, seed=SEED)
    oof = np.zeros(len(y))
    for f, (tr, te) in enumerate(GroupKFold(n_splits=min(5, len(np.unique(g)))).split(X, y, g)):
        w = np.where(y[tr] == 1, (y[tr] == 0).sum() / max((y[tr] == 1).sum(), 1), 1.0)
        m = lgb.train(params, lgb.Dataset(X[tr], y[tr], weight=w), num_boost_round=400)
        oof[te] = m.predict(X[te])
        print(f"fold {f}: AUC {roc_auc_score(y[te], oof[te]):.3f}", flush=True)

    pred = (oof >= 0.5).astype(int)
    # parcel level: mean out-of-fold probability over the sampled pixels of each parcel
    codes = brp.set_index("parcel").gewascode
    pp = {}
    for p, pr in zip(parcel, oof):
        pp.setdefault(p, []).append(pr)
    p_ids = np.array(list(pp))
    p_prob = np.array([np.mean(v) for v in pp.values()])
    p_true = np.isin(codes.loc[p_ids].values, NATURAL).astype(int)
    per_code = {int(c): {"parcels": int((codes.loc[p_ids].values == c).sum()),
                         "share_predicted_natural": round(float((p_prob[codes.loc[p_ids].values == c] >= 0.5).mean()), 3)}
                for c in AGRI + NATURAL if (codes.loc[p_ids].values == c).any()}
    report = {
        "samples": int(len(y)), "natural_samples": int(y.sum()), "tiles": int(len(np.unique(g))),
        "pixel": {"auc": round(roc_auc_score(y, oof), 3), "balanced_accuracy": round(balanced_accuracy_score(y, pred), 3),
                  "f1_natural": round(f1_score(y, pred), 3), "confusion[true][pred]": confusion_matrix(y, pred).tolist()},
        "parcel": {"parcels": int(len(p_ids)), "auc": round(roc_auc_score(p_true, p_prob), 3),
                   "balanced_accuracy": round(balanced_accuracy_score(p_true, p_prob >= 0.5), 3),
                   "f1_natural": round(f1_score(p_true, p_prob >= 0.5), 3),
                   "confusion[true][pred]": confusion_matrix(p_true, p_prob >= 0.5).tolist()},
        "per_gewascode": per_code,
        "note": "out-of-fold, 5-fold GroupKFold by 0.1° tile (spatial CV); class-balanced weights",
    }
    json.dump(report, open(f"{args.work}/cv_report.json", "w"), indent=1)
    print(json.dumps(report, indent=1))
    w = np.where(y == 1, (y == 0).sum() / max(y.sum(), 1), 1.0)
    lgb.train(params, lgb.Dataset(X, y, weight=w), num_boost_round=400).save_model(f"{args.work}/model.txt")


def stage_predict(args, brp, nbbg):
    import h3
    import lightgbm as lgb
    import pandas as pd

    model = lgb.Booster(model_file=f"{args.work}/model.txt")
    os.makedirs(f"{args.work}/prob", exist_ok=True)
    hex_rows, parcel_sum, parcel_n = [], np.zeros(len(brp)), np.zeros(len(brp))
    for tile in tiles(args.work):
        t = tile_rasters(tile, brp, nbbg)
        dom = t["own"] & ((t["parcel"] >= 0) | (t["nbbg"] > 0))
        prob = np.full(t["shape"], 255, np.uint8)
        if dom.any():
            p = model.predict(features(t, dom))
            prob[dom] = np.round(p * 100).astype(np.uint8)
            pid = t["parcel"][dom]
            np.add.at(parcel_sum, pid[pid >= 0], p[pid >= 0])
            np.add.at(parcel_n, pid[pid >= 0], 1)
            lab = np.where(pid >= 0, brp.label.values[np.maximum(pid, 0)], 255)
            cells = [h3.latlng_to_cell(la, lo, 9) for la, lo in zip(t["lat"][dom], t["lon"][dom])]
            hex_rows.append(pd.DataFrame({
                "h3_id": cells, "p_natural": p,
                "brp_agri": lab == 0, "brp_natural": lab == 1, "nbbg": t["nbbg"][dom],
            }))
        with rasterio.open(f"{args.work}/prob/{tile[0]}.tif", "w", driver="GTiff", height=t["shape"][0],
                           width=t["shape"][1], count=1, dtype="uint8", crs=CRS_TILES, transform=t["transform"],
                           nodata=255, compress="deflate") as dst:
            dst.write(prob, 1)
        print(f"predict {tile[0]}: {int(dom.sum())} domain px", flush=True)

    px = pd.concat(hex_rows, ignore_index=True)
    px["pred_natural"] = px.p_natural >= 0.5
    area = 100.0  # m2 per 10 m pixel
    g = px.groupby("h3_id")
    hexes = pd.DataFrame({
        "grass_domain_m2": g.size() * area,
        "tessera_natural_m2": g.pred_natural.sum() * area,
        "tessera_p_natural_mean": g.p_natural.mean(),
        "brp_agri_m2": g.brp_agri.sum() * area,
        "brp_natural_m2": g.brp_natural.sum() * area,
        "nbbg53_m2": g.nbbg.apply(lambda s: (s == 53).sum()) * area,
        "nbbg61_62_m2": g.nbbg.apply(lambda s: s.isin((61, 62)).sum()) * area,
    }).reset_index()
    hexes["tessera_agri_m2"] = hexes.grass_domain_m2 - hexes.tessera_natural_m2
    hexes.to_parquet(f"{args.work}/hex_grass_{YEAR}.parquet", index=False)

    parcels = brp[["parcel", "gewascode", "label"]].copy()
    parcels["p_natural"] = np.where(parcel_n > 0, parcel_sum / np.maximum(parcel_n, 1), np.nan)
    parcels["pixels"] = parcel_n.astype(int)
    parcels["geometry"] = brp.geometry.to_crs("EPSG:4326").to_wkb()
    parcels.to_parquet(f"{args.work}/parcels_{YEAR}.parquet", index=False)

    s = hexes.sum(numeric_only=True) / 1e6
    print(f"km2: domain {s.grass_domain_m2:.1f} | tessera natural {s.tessera_natural_m2:.1f} agri {s.tessera_agri_m2:.1f}"
          f" | BRP natural {s.brp_natural_m2:.1f} agri {s.brp_agri_m2:.1f} | NBBG 53 {s.nbbg53_m2:.1f} 61/62 {s.nbbg61_62_m2:.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["sample", "train", "predict", "all"])
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--work", default="own_data/tessera")
    args = ap.parse_args()
    t0 = time.time()
    brp, nbbg = load_vectors(args.data)
    print(f"vectors: {len(brp)} BRP grass parcels, {len(nbbg)} NBBG polygons ({time.time() - t0:.0f}s)", flush=True)
    if args.stage in ("sample", "all"):
        stage_sample(args, brp, nbbg)
    if args.stage in ("train", "all"):
        stage_train(args, brp)
    if args.stage in ("predict", "all"):
        stage_predict(args, brp, nbbg)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
