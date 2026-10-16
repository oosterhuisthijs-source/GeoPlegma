# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17"]
# ///
"""TROPOMI (Sentinel-5P) tropospheric NO2 column -> yearly mean per hex.

Source: KNMI TEMIS, "Monthly mean tropospheric NO2 from TROPOMI, version 1.0" (www.temis.nl/airpollution/
no2col/no2month_tropomi.php): global monthly grids at 0.125 degree (~14 x 9 km here), from the official
reprocessed TROPOMI data, May 2018 on. Downloaded once to <data>/sources/temis/ (shared by all regions;
missing months are fetched).

A satellite MEASUREMENT of the whole tropospheric column (not ground-level concentration; that is
gcn_no2_ug_m3, a model calibrated on stations). Only cloud-free overpasses (~13:30 local time) count,
so months with much cloud rest on few days, and winter months can be missing at a pixel.

Per hex and year (0.125 degree cell containing the hex centre):
  tropomi_no2_1e15_cm2   mean of the monthly means (10^15 molecules/cm2); null for 2018 (no Jan-Apr, and
                         NO2 is highest in winter, so a May-Dec mean is not comparable) and for years
                         with fewer than 10 months
  tropomi_no2_maanden    months with a value in that year (meta)

Writes <data>/<work>/layers/tropomi.parquet (h3_id, year_int, ...).

Usage: uv run datacube-zh/prepare/tropomi.py [--data own_data] [--region zh] [--years 2018-2025]
"""
import argparse
import datetime as dt
import gzip
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

URL = "https://d1qb6yzwaaq4he.cloudfront.net/tropomi/no2/{y}/{m:02d}/no2_{y}{m:02d}.asc.gz"
STEP, NLON, NLAT = 0.125, 2880, 1440
FIRST = (2018, 5)  # first month of the official reprocessed record
MIN_MONTHS = 10


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(src: Path, y: int, m: int) -> Path | None:
    f = src / f"no2_{y}{m:02d}.asc.gz"
    if not f.exists():
        try:
            with urllib.request.urlopen(URL.format(y=y, m=m), timeout=300) as r:
                f.write_bytes(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            raise
    return f


def read_month(path: Path, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """Values (1e13 molec/cm2, NaN = undefined) of the grid cells (lat row, lon col) asked for. Format: 4 header
    lines, then per latitude (south to north) a 'lat=' line and 2880 values as 4-character fields."""
    want = set(rows.tolist())
    grid = {}
    with gzip.open(path, "rt") as f:
        lines = f.read().splitlines()
    i, row = 4, 0
    while row < NLAT:
        assert lines[i].startswith("lat="), lines[i]
        j, vals = i + 1, []
        while sum(len(v) for v in vals) < NLON * 4:
            vals.append(lines[j])
            j += 1
        if row in want:
            s = "".join(vals)
            grid[row] = np.array([int(s[k:k + 4]) for k in range(0, NLON * 4, 4)], np.float32)
        i, row = j, row + 1
    out = np.array([grid[r][c] for r, c in zip(rows, cols)], np.float32)
    out[out == -999] = np.nan
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", default="2018-2025")
    args = ap.parse_args()
    data = Path(args.data)
    a, _, b = args.years.partition("-")
    years = list(range(int(a), int(b or a) + 1))
    work = data / regions.load(args.region)["work"]
    src = data / "sources/temis"
    src.mkdir(parents=True, exist_ok=True)

    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    lat, lon = np.array([h3.cell_to_latlng(c) for c in ids]).T
    rows = np.floor((lat + 90) / STEP).astype(int)
    cols = np.floor((lon + 180) / STEP).astype(int)
    log(f"{len(ids)} hexes in {len(set(zip(rows, cols)))} TROPOMI cells")

    mean_col, n_col = [], []
    for y in years:
        months = []
        for m in range(1, 13):
            if (y, m) < FIRST:
                continue
            f = fetch(src, y, m)
            if f is not None:
                months.append(read_month(f, rows, cols))
        stack = np.stack(months) if months else np.full((1, len(ids)), np.nan, np.float32)
        n = np.isfinite(stack).sum(axis=0)
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(stack, axis=0) / 100  # 1e13 -> 1e15 molecules/cm2
        mean[(n < MIN_MONTHS) | (y <= FIRST[0])] = np.nan
        mean_col.append(np.round(mean.astype(np.float64), 3))
        n_col.append(n.astype(np.int32))
        log(f"{y}: {len(months)} months, mean {np.nanmean(mean) if np.isfinite(mean).any() else float('nan'):.2f} "
            f"x1e15, months per hex {n.min()}-{n.max()}")

    table = pa.table({"h3_id": ids * len(years), "year_int": np.repeat(np.array(years, np.int32), len(ids)),
                      "tropomi_no2_1e15_cm2": pa.array(np.concatenate(mean_col), from_pandas=True),
                      "tropomi_no2_maanden": np.concatenate(n_col)})
    source = {"source": "KNMI TEMIS monthly mean tropospheric NO2 from TROPOMI v1.0, 0.125 degree",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers/tropomi.parquet",
                   compression="zstd")
    log(f"-> {work / 'layers/tropomi.parquet'} ({table.num_rows} rows)")


if __name__ == "__main__":
    main()
