# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pyarrow>=17"]
# ///
"""KNMI daily station wind -> one national row per year (not per hex).

Wind per hex per year says little: the spatial pattern is the same every year (coast windy, inland calm) and what
changes between years is one national signal. So this is a small table, datacube_<region>_jaar.parquet, to name
weather years next to the cube, e.g. as a year covariate.

Source: KNMI daily station data (www.daggegevens.knmi.nl/klimatologie/daggegevens, no key; CC BY 4.0):
  FG     daily mean wind speed (0.1 m/s)
  FHX    highest hourly mean wind speed (0.1 m/s)
  DDVEC  vector mean wind direction (degrees; 360 = north, 0 = calm/variable)
Downloaded once to <data>/sources/knmi_stations/daggegevens_<year>.txt (one request per year).

Station set: stations change over the years, which would make fake jumps, so only stations with FG, FHX and DDVEC
on >= 95% of the days in EVERY year are used (fixed set, logged); that includes coastal and IJsselmeer stations.

Per year:
  wind_gem_ms             mean daily wind speed over station-days (m/s)
  wind_<sector>_aandeel   share of station-days with the vector mean direction FROM that 45-degree sector
                          (n, no, o, zo, z, zw, w, nw; calm/variable days left out); the 8 add up to 1
  wind_zw_w_aandeel       zw + w (the usual Atlantic wind)
  wind_o_no_aandeel       o + no (continental wind: dry, and it carries German/Belgian air and our own ammonia west)
  wind_stille_dagen       days with the station-mean wind speed < 2 m/s (stagnant air: ammonia and NO2 build up)
  wind_storm_dagen        days with an hourly mean >= 20.8 m/s (Beaufort 9) at one or more stations of the set
  wind_stations           number of stations in the set (meta)

Usage: uv run datacube-zh/prepare/knmi_wind.py [--data own_data] [--region nl] [--years 2018-2025]
"""
import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

URL = "https://www.daggegevens.knmi.nl/klimatologie/daggegevens"
SECTORS = ["n", "no", "o", "zo", "z", "zw", "w", "nw"]
CALM_MS, STORM_MS, MIN_COVER = 2.0, 20.8, 0.95


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(src: Path, years: list[int]) -> list[Path]:
    """One file per year: the service answers a multi-year request for all stations with an HTML page."""
    src.mkdir(parents=True, exist_ok=True)
    files = []
    for y in years:
        f = src / f"daggegevens_{y}.txt"
        if not f.exists():
            body = urllib.parse.urlencode({"start": f"{y}0101", "end": f"{y}1231",
                                           "vars": "FG:FHX:DDVEC", "stns": "ALL"}).encode()
            with urllib.request.urlopen(urllib.request.Request(URL, data=body), timeout=600) as r:
                data = r.read()
            if not data.startswith(b"#"):
                raise SystemExit(f"{y}: KNMI did not return station data ({data[:80]!r})")
            f.write_bytes(data)
            log(f"{f}: {len(data) / 1e6:.1f} MB")
        files.append(f)
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    ap.add_argument("--years", default="2018-2025")
    args = ap.parse_args()
    a, _, b = args.years.partition("-")
    years = list(range(int(a), int(b or a) + 1))
    files = fetch(Path(args.data) / "sources/knmi_stations", years)

    names = {}
    rows = []  # station, date, fg, fhx, ddvec (nan = missing)
    for line in (ln for f in files for ln in f.read_text(encoding="latin-1").splitlines()):
        if line.startswith("# ") and line[2:5].strip().isdigit():
            parts = line[2:].split()
            names[int(parts[0])] = " ".join(parts[4:])
        elif line and not line.startswith("#"):
            p = [x.strip() for x in line.split(",")]
            rows.append((int(p[0]), p[1], *[float(x) if x else np.nan for x in p[2:5]]))
    stn = np.array([r[0] for r in rows])
    year = np.array([int(r[1][:4]) for r in rows])
    date = np.array([r[1] for r in rows])
    fg, fhx, dd = (np.array([r[i] for r in rows]) for i in (2, 3, 4))
    fg, fhx = fg / 10, fhx / 10

    # fixed station set: complete enough in every year
    ok = np.isfinite(fg) & np.isfinite(fhx) & np.isfinite(dd)
    keep = []
    for s in np.unique(stn):
        cover = [ok[(stn == s) & (year == y)].sum() / (366 if y % 4 == 0 else 365) for y in years]
        if min(cover) >= MIN_COVER:
            keep.append(s)
    log(f"{len(keep)} of {len(np.unique(stn))} stations complete in every year: "
        + ", ".join(names.get(s, str(s)) for s in keep))
    sel = np.isin(stn, keep) & ok

    out = {c: [] for c in ["wind_gem_ms", *[f"wind_{s}_aandeel" for s in SECTORS], "wind_zw_w_aandeel",
                           "wind_o_no_aandeel", "wind_stille_dagen", "wind_storm_dagen", "wind_stations"]}
    for y in years:
        m = sel & (year == y)
        out["wind_gem_ms"].append(round(float(fg[m].mean()), 2))
        d = dd[m & (dd > 0)]  # 0 = calm / variable
        sector = ((d % 360 + 22.5) // 45).astype(int) % 8
        shares = np.bincount(sector, minlength=8) / len(sector)
        for s, v in zip(SECTORS, shares):
            out[f"wind_{s}_aandeel"].append(round(float(v), 4))
        out["wind_zw_w_aandeel"].append(round(float(shares[5] + shares[6]), 4))
        out["wind_o_no_aandeel"].append(round(float(shares[1] + shares[2]), 4))
        days = np.unique(date[m])
        day_mean = {dday: fg[m & (date == dday)].mean() for dday in days}
        out["wind_stille_dagen"].append(int(sum(v < CALM_MS for v in day_mean.values())))
        out["wind_storm_dagen"].append(int(len(np.unique(date[m & (fhx >= STORM_MS)]))))
        out["wind_stations"].append(len(keep))
        log(f"{y}: {out['wind_gem_ms'][-1]} m/s, zw+w {out['wind_zw_w_aandeel'][-1]:.0%}, "
            f"o+no {out['wind_o_no_aandeel'][-1]:.0%}, calm {out['wind_stille_dagen'][-1]}, "
            f"storm {out['wind_storm_dagen'][-1]}")

    table = pa.table({"year_int": np.array(years, np.int32), **out})
    source = {"source": "KNMI daily station data (FG, FHX, DDVEC), fixed station set: "
                        + ", ".join(f"{s} {names.get(s, '')}" for s in keep),
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    work = Path(args.data) / regions.load(args.region)["work"]
    dest = work / "layers/knmi_wind_jaar.parquet"
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), dest)
    log(f"-> {dest} ({table.num_rows} rows)")


if __name__ == "__main__":
    main()
