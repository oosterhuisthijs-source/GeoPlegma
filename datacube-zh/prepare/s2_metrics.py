# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "rasterio", "pyarrow>=17"]
# ///
"""Per-pixel Sentinel-2 NDVI metrics (10 m) -> per hex and year, via geoplegma's hex index.

Inputs: the yearly composites of prepare/s2_ndvi.py (<data>/sources/s2_ndvi/<year>/*.tif: bands piek, laatzomer,
n_piek, n_zomer, NDVI x 10000, nodata -32768) and the hex index raster of the same RD 10 m grid
(`datacube-zh --region nl hexindex` -> <work>/s2/hexindex_10m.u32). Everything is computed per PIXEL first and then
summed per hex, so a hex value is a share of its 10 m pixels, not an average of mixed land covers.

Per pixel:
  water       piek < 0 in at least 75% of the years with data: excluded from all land metrics
  niet groen  piek < 0.30 that year: never green in April-October (buildings, roads, greenhouses, bare sand)
  groen       piek >= 0.60 that year: dense vegetation at some point in the season
  verstening  green (piek >= 0.50) the year before, niet groen this year AND the next: new, lasting loss of green
              (construction, paving); needs Y-1 and Y+1, so empty in the first and last year
  vergroening the reverse (niet groen the year before, piek >= 0.50 this year and the next)
  anomalie    laatzomer minus the pixel's own median laatzomer over all years (needs >= 4 years): drought
              browning or a greener summer, independent of what grows there
Per hex and year (shares of the hex's land pixels with data):
  s2_niet_groen_aandeel, s2_groen_aandeel, s2_verstening_aandeel, s2_vergroening_aandeel,
  s2_piek_gem, s2_zomer_gem, s2_zomer_anomalie   (land pixels only)
  s2_helder_piek, s2_helder_zomer                mean clear observations per pixel (Apr-Oct, Jul-Aug)
  s2_water_aandeel                               share of the hex's pixels that are water (same every year)
Writes <data>/<work>/layers/s2.parquet. Always computed on the national grid; zh is a slice of nl.

Usage: uv run datacube-zh/prepare/s2_metrics.py [--data own_data] [--rows N (test: first N rows only)]
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from rasterio.windows import Window

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

NODATA = -32768
NIET_GROEN, GROEN, GROEN_WISSEL = 3000, 6000, 5000  # NDVI x 10000
BLOCK = 512


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def vrt_for(src: Path, year: int) -> Path | None:
    d = src / str(year)
    tifs = sorted(d.glob("*.tif"))
    if not tifs or not (d / ".complete").exists():
        return None
    vrt = d / f"ndvi_{year}.vrt"
    if not vrt.exists():
        subprocess.run(["gdalbuildvrt", "-q", str(vrt), *map(str, tifs)], check=True)
    return vrt


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--rows", type=int, default=None, help="process only N rows (testing)")
    ap.add_argument("--row0", type=int, default=0, help="first row to process (testing)")
    ap.add_argument("--src", default=None, help="composites directory (default <data>/sources/s2_ndvi)")
    args = ap.parse_args()
    data = Path(args.data)
    work = data / regions.load("nl")["work"]
    src = Path(args.src) if args.src else data / "sources/s2_ndvi"
    meta = json.loads((work / "s2/hexindex_10m.json").read_text())
    rows_total, cols = meta["rows"], meta["cols"]
    row0 = args.row0
    rows = min(args.rows or rows_total - row0, rows_total - row0)
    hexidx = np.memmap(work / "s2/hexindex_10m.u32", dtype="<u4", mode="r", shape=(rows_total, cols))
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    n = len(ids) + 1  # index 0 = no hex

    years = [y for y in range(2018, 2026) if vrt_for(src, y)]
    if len(years) < 3:
        sys.exit(f"only {years} downloaded; need at least 3 years")
    ds = {y: rasterio.open(vrt_for(src, y)) for y in years}
    for y, d in ds.items():
        t = d.transform
        assert (round(t.a), round(t.e), round(t.c), round(t.f)) == (10, -10, int(meta["x0"]), int(meta["y0"])), (y, t)
    ny = len(years)
    log(f"years {years}, {rows} of {rows_total} rows")

    acc = {k: np.zeros((ny, n)) for k in ["land", "data", "niet_groen", "groen", "verst", "vergr", "wissel_basis",
                                           "piek", "zomer", "zomer_n", "anom", "anom_n", "n_piek", "n_zomer"]}
    pixels, water_px = np.zeros(n), np.zeros(n)
    t0 = time.time()
    for r0 in range(row0, row0 + rows, BLOCK):
        h = min(BLOCK, row0 + rows - r0)
        win = Window(0, r0, cols, h)
        idx = np.asarray(hexidx[r0:r0 + h]).ravel()
        stack = {y: ds[y].read(window=win, boundless=True, fill_value=NODATA).reshape(4, -1) for y in years}
        piek = np.stack([stack[y][0] for y in years]).astype(np.int32)
        zomer = np.stack([stack[y][1] for y in years]).astype(np.int32)
        has = piek != NODATA
        zhas = zomer != NODATA
        nvalid = has.sum(axis=0)
        water = (nvalid > 0) & ((has & (piek < 0)).sum(axis=0) >= np.ceil(0.75 * np.maximum(nvalid, 1)))
        pixels += np.bincount(idx, minlength=n)
        water_px += np.bincount(idx, water, minlength=n)
        land = has & ~water
        zf = np.where(zhas & ~water, zomer, np.nan).astype(np.float32)
        enough = (np.isfinite(zf).sum(axis=0) >= 4)
        base = np.full(zf.shape[1], np.nan, np.float32)
        if enough.any():
            base[enough] = np.nanmedian(zf[:, enough], axis=0)
        green_wide = has & (piek >= GROEN_WISSEL)
        notgreen = has & (piek < NIET_GROEN)
        for k, y in enumerate(years):
            add = lambda key, w: acc[key].__setitem__(k, acc[key][k] + np.bincount(idx, w, minlength=n))  # noqa: E731
            add("data", has[k])
            add("land", land[k])
            add("niet_groen", land[k] & notgreen[k])
            add("groen", land[k] & (piek[k] >= GROEN))
            add("piek", np.where(land[k], piek[k], 0) / 10000)
            zl = zhas[k] & ~water
            add("zomer", np.where(zl, zomer[k], 0) / 10000)
            add("zomer_n", zl)
            an = zl & np.isfinite(base)
            add("anom", np.where(an, zomer[k] - np.nan_to_num(base), 0) / 10000)
            add("anom_n", an)
            add("n_piek", np.where(has[k], stack[y][2], 0))
            add("n_zomer", np.where(has[k], stack[y][3], 0))
            if 0 < k < ny - 1 and years[k - 1] == years[k] - 1 and years[k + 1] == years[k] + 1:
                basis = land[k] & has[k - 1] & has[k + 1]
                add("wissel_basis", basis)
                add("verst", basis & green_wide[k - 1] & notgreen[k] & notgreen[k + 1])
                add("vergr", basis & notgreen[k - 1] & green_wide[k] & green_wide[k + 1])
        if (r0 // BLOCK) % 8 == 0:
            done = (r0 + h - row0) / rows
            log(f"  row {r0 + h - row0}/{rows} ({done:.0%}, {time.time() - t0:.0f}s, ~{(time.time() - t0) / done / 60:.0f} min total)")

    def share(num, den):
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0, num / den, np.nan)

    cols_out = {
        "s2_niet_groen_aandeel": share(acc["niet_groen"], acc["land"]),
        "s2_groen_aandeel": share(acc["groen"], acc["land"]),
        "s2_verstening_aandeel": share(acc["verst"], acc["wissel_basis"]),
        "s2_vergroening_aandeel": share(acc["vergr"], acc["wissel_basis"]),
        "s2_piek_gem": share(acc["piek"], acc["land"]),
        "s2_zomer_gem": share(acc["zomer"], acc["zomer_n"]),
        "s2_zomer_anomalie": share(acc["anom"], acc["anom_n"]),
        "s2_helder_piek": share(acc["n_piek"], acc["data"]),
        "s2_helder_zomer": share(acc["n_zomer"], acc["data"]),
    }
    water_share = share(water_px, pixels)
    all_years = list(range(2018, 2026))
    out = {c: [] for c in cols_out}
    for y in all_years:
        for c, v in cols_out.items():
            out[c].append(v[years.index(y)][1:] if y in years else np.full(n - 1, np.nan))
    m = n - 1
    table = pa.table({
        "h3_id": ids * len(all_years), "year_int": np.repeat(np.array(all_years, np.int32), m),
        **{c: pa.array(np.round(np.concatenate(v), 4), from_pandas=True) for c, v in out.items()},
        "s2_water_aandeel": pa.array(np.round(np.tile(water_share[1:], len(all_years)), 4), from_pandas=True),
    })
    source = {"source": "Sentinel-2 (COPERNICUS/S2_SR_HARMONIZED, Cloud Score+ cs_cdf >= 0.6) NDVI composites from "
                        "Earth Engine (prepare/s2_ndvi.py), per-pixel metrics, hex index by geoplegma",
              "years": years, "rows_processed": rows,
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    dest = work / ("layers/s2.parquet" if rows == rows_total else "s2/s2_test.parquet")
    source["row0"] = row0
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), dest, compression="zstd")
    log(f"-> {dest} ({time.time() - t0:.0f}s); land pixels per year: "
        + ", ".join(f"{y} {acc['land'][k].sum() / 1e6:.0f}M" for k, y in enumerate(years)))


if __name__ == "__main__":
    main()
