# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17", "pyproj>=3.6"]
# ///
"""RIVM nitrogen deposition (GDN, historical series) and air quality (GCN) -> per hex and year.

Sources (1 km ASCII grids in RD, public domain), downloaded once to <data>/sources/rivm_gdn/ and
<data>/sources/rivm_gcn/ (shared by all regions; `--download` fetches what is missing):

- GDN historical series, edition 2026-10-06 (data.rivm.nl/data/stikstof/Stikstof Natuur/2026/): total nitrogen
  and potential acid deposition 2005-2025, recalculated with ONE method for the whole series, so suitable
  for trends (unlike the yearly GDN rounds).
- GCN large-scale concentration maps (data.rivm.nl/data/gcn/conc_<stof>_<jaar>.zip), one file per substance
  and calculation round. RIVM: rounds can NOT be strung into a series, the calibration on measurements
  changed over the years; there is no historical concentration series.

Per hex and year (value of the 1 km cell under the hex centre, null outside the map):
  gdn_stikstof_mol_ha    total nitrogen deposition (mol N/ha/yr)
  gdn_verzuring_mol_ha   potential acid deposition (mol H+/ha/yr)
  gcn_no2_ug_m3, gcn_pm10_ug_m3, gcn_pm25_ug_m3, gcn_nh3_ug_m3   yearly mean background concentration (ug/m3)

Writes <data>/<work>/layers/rivm.parquet (h3_id, year_int, ...).

Usage: uv run datacube-zh/prepare/rivm.py [--data own_data] [--region zh] [--years 2018-2025] [--download]
"""
import argparse
import datetime as dt
import io
import json
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

GDN_EDITION = "20261006"
GDN_URL = "https://data.rivm.nl/data/stikstof/Stikstof%20Natuur/2026/RIVM-MIL_HistorischeReeks{what}-ASCIIGrid_{ed}.zip"
GDN = {"gdn_stikstof_mol_ha": ("Stikstofdepositie", "ndep"), "gdn_verzuring_mol_ha": ("VerzurendeDepositie", "potz")}
GCN_URL = "https://data.rivm.nl/data/gcn/conc_{s}_{y}.zip"
GCN = {"gcn_no2_ug_m3": "NO2", "gcn_pm10_ug_m3": "PM10", "gcn_pm25_ug_m3": "PM25", "gcn_nh3_ug_m3": "NH3"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            dest.write_bytes(r.read())
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def gcn_zip(src: Path, s: str, y: int, download: bool) -> Path | None:
    """The zip of one substance and year; RIVM's URLs are lower case up to 2023, upper case after."""
    dest = src / f"conc_{s}_{y}.zip"
    if not dest.exists() and download:
        if not (fetch(GCN_URL.format(s=s, y=y), dest) or fetch(GCN_URL.format(s=s.lower(), y=y), dest)):
            log(f"  GCN {s} {y}: not published")
    return dest if dest.exists() else None


def read_asc(text: str) -> tuple[dict, np.ndarray]:
    lines = text.splitlines()
    head = {}
    for i, line in enumerate(lines):
        k, *v = line.split()
        if not re.match(r"[A-Za-z]", k):
            break
        head[k.lower()] = float(v[0])
    a = np.loadtxt(lines[i:], dtype=np.float32, ndmin=2)
    a[a == head["nodata_value"]] = np.nan
    return head, a


def sample(head: dict, a: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Value of the grid cell containing each RD point (row 0 is the top of the grid)."""
    cs, nrows, ncols = head["cellsize"], int(head["nrows"]), int(head["ncols"])
    col = np.floor((x - head["xllcorner"]) / cs).astype(int)
    row = nrows - 1 - np.floor((y - head["yllcorner"]) / cs).astype(int)
    ok = (col >= 0) & (col < ncols) & (row >= 0) & (row < nrows)
    out = np.full(len(x), np.nan, np.float32)
    out[ok] = a[row[ok], col[ok]]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", default="2018-2025")
    ap.add_argument("--download", action="store_true", help="fetch missing source files")
    args = ap.parse_args()
    data = Path(args.data)
    a, _, b = args.years.partition("-")
    years = list(range(int(a), int(b or a) + 1))
    work = data / regions.load(args.region)["work"]
    gdn_src, gcn_src = data / "sources/rivm_gdn", data / "sources/rivm_gcn"
    gdn_src.mkdir(parents=True, exist_ok=True)
    gcn_src.mkdir(parents=True, exist_ok=True)

    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    lat, lon = np.array([h3.cell_to_latlng(c) for c in ids]).T
    x, y = Transformer.from_crs(4326, 28992, always_xy=True).transform(lon, lat)

    cols = {c: [] for c in [*GDN, *GCN]}
    for col, (what, stem) in GDN.items():
        z = gdn_src / f"RIVM-MIL_HistorischeReeks{what}-ASCIIGrid_{GDN_EDITION}.zip"
        if not z.exists():
            if not args.download:
                sys.exit(f"missing {z}: run with --download")
            fetch(GDN_URL.format(what=what, ed=GDN_EDITION), z)
        with zipfile.ZipFile(z) as zf:
            for yr in years:
                name = f"{stem}_historisch_{yr}.asc"
                cols[col].append(sample(*read_asc(zf.read(name).decode()), x, y) if name in zf.namelist()
                                 else np.full(len(ids), np.nan, np.float32))
    for col, s in GCN.items():
        for yr in years:
            z = gcn_zip(gcn_src, s, yr, args.download)
            v = np.full(len(ids), np.nan, np.float32)
            if z:
                with zipfile.ZipFile(z) as zf:
                    # the background concentration map (not rwc_*: the road-contribution input), e.g.
                    # conc_no2_2018.asc, conc_nh3_2022/conc_nh3_2022.asc; for NH3 2019 nh3_conc_2019.asc is the
                    # calibrated map of April 2021 (conc_nh3_2019_1.0.asc was not calibrated, see its _README.txt)
                    member = next(m for m in zf.namelist()
                                  if re.fullmatch(rf"(.*/)?(conc_{s}_{yr}|{s}_conc_{yr})\.asc", m, re.I))
                    v = sample(*read_asc(io.TextIOWrapper(zf.open(member)).read()), x, y)
            cols[col].append(v)
    for col, per_year in cols.items():
        log(f"{col}: " + ", ".join(f"{yr} {np.nanmean(v):.1f}" for yr, v in zip(years, per_year)))

    n = len(ids)
    table = pa.table({"h3_id": ids * len(years), "year_int": np.repeat(np.array(years, np.int32), n),
                      **{c: pa.array(np.round(np.concatenate(v).astype(np.float64), 2), from_pandas=True)
                         for c, v in cols.items()}})
    source = {"source": f"RIVM GDN historical series (edition {GDN_EDITION}) and GCN concentration maps (per round)",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers/rivm.parquet",
                   compression="zstd")
    log(f"-> {work / 'layers/rivm.parquet'} ({table.num_rows} rows)")


if __name__ == "__main__":
    main()
