# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "netcdf4", "h3>=4", "pyarrow>=17", "pyproj>=3.6"]
# ///
"""KNMI daily 1 km climate grids -> yearly climate indicators per hex.

Source: KNMI Data Platform (CC BY 4.0), daily grids on the KNMI RD grid (1 km, 300 x 350):
  Tg1 v5   daily mean temperature (IDW of ~35 automatic stations)
  Tx1 v2   daily maximum temperature
  Tn1 v2   daily minimum temperature
  Rd1 v5   daily precipitation 08-08 UT (kriging of ~300 voluntary stations; ~4 weeks delay)
  EV24 v2  daily Makkink reference evaporation (thin plate spline of ~32 stations)

Two steps:

  knmi.py download [--years 2018-2025]   fetch the daily files to <data>/sources/knmi/<dataset>/ (shared by
                                         all regions; resumable, backs off on the API's rate limit). Uses
                                         $KNMI_API_KEY or ~/.config/knmi_api_key, else KNMI's public
                                         anonymous key (shared quota, mostly rate-limited).
  knmi.py [--region zh]                  build <work>/layers/knmi.parquet

Per hex and calendar year (value of the 1 km grid cell under the hex centre):
  knmi_temp_gem_c             mean daily mean temperature (°C)
  knmi_zomerse_dagen          days with maximum >= 25 °C
  knmi_tropische_dagen        days with maximum >= 30 °C
  knmi_vorstdagen             days with minimum < 0 °C
  knmi_neerslag_mm            precipitation total (days starting 08 UT in the year)
  knmi_zware_neerslag_dagen   days with >= 20 mm
  knmi_verdamping_mm          Makkink reference evaporation total
  knmi_neerslagtekort_max_mm  largest potential precipitation deficit of the growing season: cumulative
                              (evaporation - precipitation) from 1 April, maximum up to 30 September
                              (KNMI's definition; not floored at 0, so a wet summer can be negative)
A year with missing days gets null for the indicators of that variable (logged).

Usage: uv run datacube-zh/prepare/knmi.py download [--data own_data] [--years 2018-2025]
       uv run datacube-zh/prepare/knmi.py [--data own_data] [--region zh] [--years 2018-2025]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import h3
import netCDF4
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

API = "https://api.dataplatform.knmi.nl/open-data/v1/datasets"
# KNMI's public anonymous key (https://developer.dataplatform.knmi.nl/open-data-api, valid to 2027-08-01)
ANON_KEY = ("eyJvcmciOiI1ZTU1NGUxOTI3NGE5NjAwMDEyYTNlYjEiLCJpZCI6IjUzYTg1ZDBhMmQ5YzRkYzJiYWNlNzQ4NTQ2Zjk4ODExIiwiaCI6"
            "Im11cm11cjEyOCJ9")
DATASETS = {"tg": ("Tg1", 5), "tx": ("Tx1", 2), "tn": ("Tn1", 2), "rd": ("Rd1", 5), "ev": ("EV24", 2)}
FNAME = re.compile(r"_L3__(\d{8})T\d{6}_\d{8}T\d{6}_(\d{4})\.nc$")
COLUMNS = ["knmi_temp_gem_c", "knmi_zomerse_dagen", "knmi_tropische_dagen", "knmi_vorstdagen", "knmi_neerslag_mm",
           "knmi_zware_neerslag_dagen", "knmi_verdamping_mm", "knmi_neerslagtekort_max_mm"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def parse_years(s: str) -> list[int]:
    a, _, b = s.partition("-")
    return list(range(int(a), int(b or a) + 1))


def api_get(url: str, key: str) -> dict:
    """GET a JSON API url, waiting out rate limits (429) and transient errors."""
    wait = 30
    while True:
        try:
            req = urllib.request.Request(url, headers={"Authorization": key})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            log(f"  HTTP {e.code}, waiting {wait}s")
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"  {e}, waiting {wait}s")
        time.sleep(wait)
        wait = min(wait * 2, 900)


def list_files(name: str, version: int, key: str, years: list[int]) -> dict[str, str]:
    """{yyyymmdd: filename} for the days of `years`, the highest file version per day. Lists by filename from
    the day before the first year (`begin` = list after this filename) and pages with nextPageToken."""
    prefix = f"INTER_OPER_R___{name.upper().ljust(8, '_')}L3__"  # e.g. INTER_OPER_R___TG1_____L3__
    q = {"maxKeys": 1000, "orderBy": "filename", "sorting": "asc", "begin": f"{prefix}{years[0] - 1}1231"}
    out = {}
    while True:
        page = api_get(f"{API}/{name}/versions/{version}/files?{urllib.parse.urlencode(q)}", key)
        files = page.get("files", [])
        past_end = False
        for f in files:
            m = FNAME.search(f["filename"])
            if not m:
                continue
            if int(m[1][:4]) > years[-1]:
                past_end = True
            elif int(m[1][:4]) in years and (m[1] not in out or out[m[1]] < f["filename"]):
                out[m[1]] = f["filename"]
        if past_end or not files or not page.get("nextPageToken"):
            return out
        q["nextPageToken"] = page["nextPageToken"]


KEY_FILE = Path.home() / ".config/knmi_api_key"


def download(data: Path, years: list[int]):
    own = os.environ.get("KNMI_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else None)
    key = own or ANON_KEY
    log("key:", "registered (KNMI_API_KEY or ~/.config/knmi_api_key)" if own else "public anonymous key (shared quota)")
    for var, (name, version) in DATASETS.items():
        dest = data / "sources/knmi" / name
        dest.mkdir(parents=True, exist_ok=True)
        files = list_files(name, version, key, years)
        todo = [f for f in sorted(files.values()) if not (dest / f).exists()]
        log(f"{name}: {len(files)} days in {years[0]}-{years[-1]}, {len(todo)} to download")
        for i, f in enumerate(todo):
            url = api_get(f"{API}/{name}/versions/{version}/files/{f}/url", key)["temporaryDownloadUrl"]
            tmp = dest / f".{f}.part"
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(dest / f)
            if (i + 1) % 100 == 0:
                log(f"  {name}: {i + 1}/{len(todo)}")


def read_day(path: Path) -> np.ndarray:
    with netCDF4.Dataset(path) as ds:
        a = ds["prediction"][0]
        return np.ma.filled(a.astype(np.float32), np.nan)


def year_stack(data: Path, var: str, year: int) -> np.ndarray | None:
    """(days, y, x) for one variable and year, or None if a day is missing."""
    name = DATASETS[var][0]
    by_day = {}
    for f in (data / "sources/knmi" / name).glob(f"*_L3__{year}*.nc"):
        m = FNAME.search(f.name)
        if m and (m[1] not in by_day or by_day[m[1]].name < f.name):
            by_day[m[1]] = f
    n_days = 366 if year % 4 == 0 else 365
    if len(by_day) < n_days:
        log(f"  {name} {year}: {len(by_day)} of {n_days} days, indicators left empty")
        return None
    return np.stack([read_day(by_day[d]) for d in sorted(by_day)])


def grid_coords(data: Path) -> tuple[np.ndarray, np.ndarray]:
    f = next((data / "sources/knmi/Tg1").glob("*.nc"))
    with netCDF4.Dataset(f) as ds:
        return np.asarray(ds["x"][:]), np.asarray(ds["y"][:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", default="2018-2025")
    args = ap.parse_args()
    data, years = Path(args.data), parse_years(args.years)
    if args.step == "download":
        return download(data, years)

    work = data / regions.load(args.region)["work"]
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    # grid cell under each hex centre (RD); the KNMI grid has 1 km cells centred on x.5 km
    xs, ys = grid_coords(data)
    lat, lon = np.array([h3.cell_to_latlng(c) for c in ids]).T
    x, y = Transformer.from_crs(4326, 28992, always_xy=True).transform(lon, lat)
    ix = np.clip(np.round((x - xs[0]) / (xs[1] - xs[0])).astype(int), 0, len(xs) - 1)
    iy = np.clip(np.round((y - ys[0]) / (ys[1] - ys[0])).astype(int), 0, len(ys) - 1)

    rows = {c: [] for c in ["h3_id", "year_int"] + COLUMNS}
    for year in years:
        t0 = time.time()
        st = {v: year_stack(data, v, year) for v in DATASETS}
        grids = {}
        if st["tg"] is not None:
            grids["knmi_temp_gem_c"] = np.nanmean(st["tg"], axis=0)
        if st["tx"] is not None:
            grids["knmi_zomerse_dagen"] = (st["tx"] >= 25).sum(axis=0).astype(np.float32)
            grids["knmi_tropische_dagen"] = (st["tx"] >= 30).sum(axis=0).astype(np.float32)
        if st["tn"] is not None:
            grids["knmi_vorstdagen"] = (st["tn"] < 0).sum(axis=0).astype(np.float32)
        if st["rd"] is not None:
            grids["knmi_neerslag_mm"] = np.nansum(st["rd"], axis=0)
            grids["knmi_zware_neerslag_dagen"] = (st["rd"] >= 20).sum(axis=0).astype(np.float32)
        if st["ev"] is not None:
            grids["knmi_verdamping_mm"] = np.nansum(st["ev"], axis=0)
        if st["ev"] is not None and st["rd"] is not None:
            d0 = (dt.date(year, 4, 1) - dt.date(year, 1, 1)).days
            d1 = (dt.date(year, 9, 30) - dt.date(year, 1, 1)).days + 1
            deficit = np.cumsum(st["ev"][d0:d1] - st["rd"][d0:d1], axis=0)
            grids["knmi_neerslagtekort_max_mm"] = deficit.max(axis=0)
        # a grid cell that is NaN (outside the interpolation area) stays null for its hexes
        for c in COLUMNS:
            g = grids.get(c)
            v = g[iy, ix] if g is not None else np.full(len(ids), np.nan, np.float32)
            rows[c].append(np.round(v.astype(np.float64), 1))
        rows["h3_id"] += ids
        rows["year_int"].append(np.full(len(ids), year, np.int32))
        del st
        log(f"{year}: {', '.join(f'{c.removeprefix('knmi_')} {np.nanmean(grids[c]):.1f}' for c in COLUMNS if c in grids)} "
            f"({time.time() - t0:.0f}s)")

    table = pa.table({"h3_id": rows["h3_id"], "year_int": np.concatenate(rows["year_int"]),
                      **{c: pa.array(np.concatenate(rows[c]), from_pandas=True) for c in COLUMNS}})
    source = {"source": "KNMI Data Platform daily 1 km grids (Tg1 v5, Tx1 v2, Tn1 v2, Rd1 v5, EV24 v2), CC BY 4.0",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers/knmi.parquet",
                   compression="zstd")
    log(f"-> {work / 'layers/knmi.parquet'} ({table.num_rows} rows)")


if __name__ == "__main__":
    main()
