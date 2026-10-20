# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "rasterio", "pyarrow>=17"]
# ///
"""Sentinel-2 NDVI per hex, year AND land cover of the pixel (LGN group): a long side table.

A cell's s2_zomer_gem mixes covers (grass browns in a drought, trees hardly, pavement not at all). This splits it:
every 10 m pixel gets that year's LGN group from the 4 LGN 5 m pixels inside it (config/lgn.toml [groups]):
  - the group most of the 4 pixels have (LGN nodata ignored); "zuiver" when all 4 agree
  - gemengd     a tie between two groups (2-2, or 1-1-1-1)
  - geen_lgn    no LGN pixel at all (east of LGN's 280 km edge)
Same inputs, water mask and pixel rules as s2_metrics.py, so per cell and year the pixels of all groups add up
exactly to the cube's s2_zomer_pixels / s2_land_pixels / s2_anomalie_pixels, and their pixel-weighted mean is
the cube's value.

Per h3_id, year_int, groep (only rows with pixels):
  s2_zomer_gem, s2_zomer_pixels                mean NDVI July-August and its pixels
  s2_zomer_gem_zuiver, s2_zomer_pixels_zuiver  the same over pure pixels only (all 4 LGN pixels in the group)
  s2_piek_gem, s2_land_pixels                  mean peak NDVI April-October and its pixels
  s2_zomer_anomalie, s2_anomalie_pixels        July-August minus the pixel's own median over the years
Writes <data>/<work>/layers/s2_dekking.parquet (national grid; zh is a slice of nl). coarsen.py makes res 8/7.

Usage: uv run datacube-zh/prepare/s2_cover.py [--data own_data] [--rows N --row0 R (test)]
"""
import argparse
import datetime as dt
import json
import sys
import time
import tomllib
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from rasterio.windows import Window

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402
from s2_metrics import NODATA, vrt_for, water_mask  # noqa: E402

BLOCK = 256


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def lgn_groups(d, r0: int, h: int, cols: int, x0: float, y0: float, lut: np.ndarray, gemengd: int, geen: int,
               odd: set):
    """Group and purity of each 10 m pixel in rows r0..r0+h from one LGN 5 m raster."""
    c_off, r_off = int((x0 - d.transform.c) / 5), int((d.transform.f - y0) / 5) + 2 * r0
    a = d.read(1, window=Window(c_off, r_off, 2 * cols, 2 * h), boundless=True, fill_value=0).astype(np.int64)
    a[a == 65535] = 0  # fill value in some LGN tiles (declared nodata is 0); lgn.py ignores it too
    g = lut[np.where((a < 0) | (a >= len(lut)), len(lut) - 1, a)]  # out-of-range codes -> nodata slot
    if ((g < 0) & (a != 0)).any():
        odd |= set(np.unique(a[(g < 0) & (a != 0)]).tolist())
    g = g.reshape(h, 2, cols, 2).transpose(1, 3, 0, 2).reshape(4, -1)
    valid = g >= 0
    cnt = np.stack([((g == g[k]) & valid).sum(axis=0) * valid[k] for k in range(4)])
    m = cnt.max(axis=0)
    win = g[cnt.argmax(axis=0), np.arange(g.shape[1])]
    tie = ((cnt == m) & (g != win) & valid).any(axis=0)
    out = np.where(m == 0, geen, np.where(tie, gemengd, win)).astype(np.int16)
    pure = (m == 4)
    return out, pure


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--rows", type=int, default=None, help="process only N rows (testing)")
    ap.add_argument("--row0", type=int, default=0, help="first row to process (testing)")
    args = ap.parse_args()
    data = Path(args.data)
    work = data / regions.load("nl")["work"]
    src = data / "sources/s2_ndvi"
    meta = json.loads((work / "s2/hexindex_10m.json").read_text())
    rows_total, cols, x0, y0 = meta["rows"], meta["cols"], meta["x0"], meta["y0"]
    row0 = args.row0
    rows = min(args.rows or rows_total - row0, rows_total - row0)
    hexidx = np.memmap(work / "s2/hexindex_10m.u32", dtype="<u4", mode="r", shape=(rows_total, cols))
    lgnwater = water_mask(data, work, rows_total, cols, x0, y0)
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    n = len(ids) + 1

    groups = tomllib.loads((Path(__file__).resolve().parent.parent / "config/lgn.toml").read_text())["groups"]
    names = list(groups) + ["gemengd", "geen_lgn"]
    G, gemengd, geen = len(names), len(groups), len(groups) + 1
    lut = np.full(max(c for v in groups.values() for c in v) + 2, -1, np.int16)  # last slot: out of range
    for i, codes in enumerate(groups.values()):
        lut[codes] = i

    years = [y for y in range(2018, 2026) if vrt_for(src, y)]
    ds = {y: rasterio.open(vrt_for(src, y)) for y in years}
    lgn = {y: rasterio.open(data / f"lgn/tif/LGN{y}.tif") for y in years}
    for y, d in lgn.items():
        t = d.transform
        assert (round(t.a), round(t.e)) == (5, -5) and (t.c - x0) % 10 == 0 and (y0 - t.f) % 10 == 0, (y, t)
    ny = len(years)
    log(f"years {years}, {G} groups, {rows} of {rows_total} rows")

    K = n * G
    cnt = {k: np.zeros((ny, K), np.int32) for k in ["zomer_n", "zuiver_n", "land_n", "anom_n"]}
    tot = {k: np.zeros((ny, K)) for k in ["zomer", "zuiver", "piek", "anom"]}
    odd = set()  # LGN codes in no group: nodata for the vote (logged)
    t0 = time.time()
    for r0 in range(row0, row0 + rows, BLOCK):
        h = min(BLOCK, row0 + rows - r0)
        win = Window(0, r0, cols, h)
        idx = np.asarray(hexidx[r0:r0 + h]).ravel().astype(np.int64) * G
        stack = {y: ds[y].read(indexes=[1, 2], window=win, boundless=True, fill_value=NODATA).reshape(2, -1)
                 for y in years}
        piek = np.stack([stack[y][0] for y in years]).astype(np.int32)
        zomer = np.stack([stack[y][1] for y in years]).astype(np.int32)
        del stack
        has, zhas = piek != NODATA, zomer != NODATA
        nvalid = has.sum(axis=0)
        ndvi_water = (nvalid > 0) & ((has & (piek < 0)).sum(axis=0) >= np.ceil(0.75 * np.maximum(nvalid, 1)))
        water = (np.asarray(lgnwater[r0:r0 + h]).ravel() == 1) | ndvi_water
        zf = np.where(zhas & ~water, zomer, np.nan).astype(np.float32)
        enough = np.isfinite(zf).sum(axis=0) >= 4
        base = np.full(zf.shape[1], np.nan, np.float32)
        if enough.any():
            base[enough] = np.nanmedian(zf[:, enough], axis=0)
        del zf
        for k, y in enumerate(years):
            g, pure = lgn_groups(lgn[y], r0, h, cols, x0, y0, lut, gemengd, geen, odd)
            key = idx + g
            land = has[k] & ~water
            zl = zhas[k] & ~water
            an = zl & np.isfinite(base)
            zp = zl & pure
            for name, mask, val in [("zomer", zl, zomer[k]), ("zuiver", zp, zomer[k]), ("piek", land, piek[k])]:
                kk = key[mask]
                cnt[{"zomer": "zomer_n", "zuiver": "zuiver_n", "piek": "land_n"}[name]][k] += \
                    np.bincount(kk, minlength=K).astype(np.int32)
                tot[name][k] += np.bincount(kk, val[mask] / 10000, minlength=K)
            kk = key[an]
            cnt["anom_n"][k] += np.bincount(kk, minlength=K).astype(np.int32)
            tot["anom"][k] += np.bincount(kk, (zomer[k][an] - base[an]) / 10000, minlength=K)
        if (r0 // BLOCK) % 16 == 0:
            done = (r0 + h - row0) / rows
            log(f"  row {r0 + h - row0}/{rows} ({done:.0%}, {time.time() - t0:.0f}s, ~{(time.time() - t0) / done / 60:.0f} min total)")

    if odd:
        log(f"LGN codes outside config/lgn.toml groups (counted as no LGN): {sorted(odd)}")

    hex_of = np.repeat(np.arange(n), G)
    grp_of = np.tile(np.arange(G), n)
    parts = []
    for k, y in enumerate(years):
        keep = (cnt["land_n"][k] > 0) | (cnt["zomer_n"][k] > 0)
        keep[:G] = False  # index 0 = no hex
        sel = np.nonzero(keep)[0]

        def mean(name, nk):
            c = cnt[nk][k][sel]
            with np.errstate(invalid="ignore", divide="ignore"):
                return pa.array(np.round(np.where(c > 0, tot[name][k][sel] / c, np.nan), 4), from_pandas=True)
        parts.append(pa.table({
            "h3_id": pa.array([ids[i - 1] for i in hex_of[sel]]),
            "year_int": pa.array(np.full(len(sel), y, np.int32)),
            "groep": pa.array([names[i] for i in grp_of[sel]]).dictionary_encode(),
            "s2_zomer_gem": mean("zomer", "zomer_n"), "s2_zomer_pixels": pa.array(cnt["zomer_n"][k][sel]),
            "s2_zomer_gem_zuiver": mean("zuiver", "zuiver_n"), "s2_zomer_pixels_zuiver": pa.array(cnt["zuiver_n"][k][sel]),
            "s2_piek_gem": mean("piek", "land_n"), "s2_land_pixels": pa.array(cnt["land_n"][k][sel]),
            "s2_zomer_anomalie": mean("anom", "anom_n"), "s2_anomalie_pixels": pa.array(cnt["anom_n"][k][sel]),
        }))
    table = pa.concat_tables(parts)
    source = {"source": "Sentinel-2 NDVI composites (prepare/s2_ndvi.py) per pixel, split by the pixel's LGN group of "
                        "that year (majority of 4 LGN 5 m pixels, config/lgn.toml); prepare/s2_cover.py",
              "years": years, "groups": names, "rows_processed": rows, "row0": row0,
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    dest = work / ("layers/s2_dekking.parquet" if rows == rows_total else "s2/s2_dekking_test.parquet")
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), dest, compression="zstd")
    log(f"-> {dest} ({table.num_rows} rows, {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
