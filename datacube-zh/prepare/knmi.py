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

  knmi.py download [--years 2017-2025]   fetch the daily files to <data>/sources/knmi/<dataset>/ (shared by
                                         all regions; resumable, backs off on the API's rate limit). Uses
                                         $KNMI_API_KEY or ~/.config/knmi_api_key, else KNMI's public
                                         anonymous key (shared quota, mostly rate-limited).
  knmi.py [--region zh]                  build <work>/layers/knmi.parquet

Per hex and calendar year (value of the 1 km grid cell under the hex centre; a centre on a cell without data,
along the coast, takes the nearest cell with data within 3 km):
  knmi_temp_gem_c             mean daily mean temperature (°C)
  knmi_graaddagen             heating degree days: sum over the days of max(0, 18 °C - daily mean), unweighted
  knmi_zomerse_dagen          days with maximum >= 25 °C
  knmi_tropische_dagen        days with maximum >= 30 °C
  knmi_vorstdagen             days with minimum < 0 °C
  knmi_neerslag_mm            precipitation total (days starting 08 UT in the year)
  knmi_zware_neerslag_dagen   days with >= 20 mm
  knmi_verdamping_mm          Makkink reference evaporation total
  knmi_neerslagtekort_max_mm  largest potential precipitation deficit of the growing season: cumulative
                              (evaporation - precipitation) from 1 April, maximum up to 30 September
                              (KNMI's definition; not floored at 0, so a wet summer can be negative)
  knmi_neerslagtekort_31aug_mm  the same cumulative deficit on 31 August (end of the NDVI summer window)
Years before 2018 (--years 2017-2025) go to <work>/layers/knmi_voor.parquet, a side file: last summer's
deficit for the cube's first year.
A year with missing days gets null for the indicators of that variable (logged); so does a grid cell missing on
any day of the year (only Rd1 2017: 1,393 cells, mostly a block on 30 July).

Usage: uv run datacube-zh/prepare/knmi.py download [--data own_data] [--years 2017-2025]
       uv run datacube-zh/prepare/knmi.py [--data own_data] [--region zh] [--years 2017-2025]
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
import warnings
from pathlib import Path

import h3
import netCDF4
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
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
COLUMNS = ["knmi_temp_gem_c", "knmi_graaddagen", "knmi_zomerse_dagen", "knmi_tropische_dagen", "knmi_vorstdagen", "knmi_neerslag_mm",
           "knmi_zware_neerslag_dagen", "knmi_verdamping_mm", "knmi_neerslagtekort_max_mm",
           "knmi_neerslagtekort_31aug_mm"]
# daily variables each indicator is computed from (a grid cell without data in any of them has no value)
SOURCES = {"knmi_temp_gem_c": ["tg"], "knmi_graaddagen": ["tg"], "knmi_zomerse_dagen": ["tx"], "knmi_tropische_dagen": ["tx"],
           "knmi_vorstdagen": ["tn"], "knmi_neerslag_mm": ["rd"], "knmi_zware_neerslag_dagen": ["rd"],
           "knmi_verdamping_mm": ["ev"], "knmi_neerslagtekort_max_mm": ["ev", "rd"],
           "knmi_neerslagtekort_31aug_mm": ["ev", "rd"]}
CUBE_START = 2018  # earlier years (asked for as last summer's deficit) go to layers/knmi_voor.parquet


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def parse_years(s: str) -> list[int]:
    a, _, b = s.partition("-")
    return list(range(int(a), int(b or a) + 1))


def limited(e: urllib.error.HTTPError) -> bool:
    """Rate limit (429) or quota used up (403 'Quota exceeded': a registered key has ~1000 requests per hour)."""
    if e.code == 429:
        return True
    return e.code == 403 and b"Quota exceeded" in e.read()


def api_get(url: str, keys: list[str]) -> dict:
    """GET a JSON API url. On a rate limit or exhausted quota, try the next key; when all keys are limited,
    wait (doubling up to 15 min). Transient server and network errors are retried too."""
    wait, k = 30, 0
    while True:
        for _ in keys:
            try:
                req = urllib.request.Request(url, headers={"Authorization": keys[k]})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if not limited(e) and e.code not in (500, 502, 503, 504):
                    raise
                k = (k + 1) % len(keys)
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"  {e}")
                break
        log(f"  all keys limited, waiting {wait}s ({time.strftime('%H:%M')})")
        time.sleep(wait)
        wait = min(wait * 2, 900)


def list_files(name: str, version: int, key: list[str], years: list[int]) -> dict[str, str]:
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
    key = [own, ANON_KEY] if own else [ANON_KEY]  # anonymous key fills in while the own quota is used up
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


def sample(g: np.ndarray, iy: np.ndarray, ix: np.ndarray, max_r: int = 3) -> np.ndarray:
    """Grid value under each hex centre. A centre on a cell without data (coast, IJsselmeer edge) takes the
    nearest cell with data within max_r cells (km); further out it stays NaN."""
    v = g[iy, ix]
    for i in np.flatnonzero(np.isnan(v)):
        y0, y1 = max(iy[i] - max_r, 0), min(iy[i] + max_r + 1, g.shape[0])
        x0, x1 = max(ix[i] - max_r, 0), min(ix[i] + max_r + 1, g.shape[1])
        yy, xx = np.nonzero(~np.isnan(g[y0:y1, x0:x1]))
        if len(yy):
            k = np.argmin((yy + y0 - iy[i]) ** 2 + (xx + x0 - ix[i]) ** 2)
            v[i] = g[yy[k] + y0, xx[k] + x0]
    return v


def grid_coords(data: Path) -> tuple[np.ndarray, np.ndarray]:
    f = next((data / "sources/knmi/Tg1").glob("*.nc"))
    with netCDF4.Dataset(f) as ds:
        return np.asarray(ds["x"][:]), np.asarray(ds["y"][:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", default="2017-2025")
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
            with warnings.catch_warnings():  # cells without data (sea) are all-NaN
                warnings.simplefilter("ignore", RuntimeWarning)
                grids["knmi_temp_gem_c"] = np.nanmean(st["tg"], axis=0)
            grids["knmi_graaddagen"] = np.nansum(np.maximum(18 - st["tg"], 0), axis=0)
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
            grids["knmi_neerslagtekort_31aug_mm"] = deficit[(dt.date(year, 8, 31) - dt.date(year, 4, 1)).days]
        # counts and nansums turn a cell without data into 0: set those back to NaN before sampling. A cell with
        # data on some days but not all (Rd1 2017: single cells on ~29 days, a 1,390 km2 block on 30 July) would
        # get a silently low sum: null too
        nodata = {v: np.isnan(a).any(axis=0) for v, a in st.items() if a is not None}
        for c, g in grids.items():
            g[np.logical_or.reduce([nodata[v] for v in SOURCES[c]])] = np.nan
        for c in COLUMNS:
            g = grids.get(c)
            v = sample(g, iy, ix) if g is not None else np.full(len(ids), np.nan, np.float32)
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
    early = pc.less(table["year_int"], CUBE_START)
    for part, f in [(table.filter(pc.invert(early)), "knmi.parquet"), (table.filter(early), "knmi_voor.parquet")]:
        if part.num_rows:
            pq.write_table(part.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers" / f,
                           compression="zstd")
            log(f"-> {work / 'layers' / f} ({part.num_rows} rows)")


if __name__ == "__main__":
    main()
