# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17", "shapely>=2"]
# ///
"""RIVM Meetnet Ammoniak Nederland (MAN) -> measured yearly ammonia concentration per hex.

Source: man.rivm.nl (RIVM with volunteers; until 2026 "Meetnet Ammoniak in Natuurgebieden"): monthly passive
samplers, yearly means per measuring point 2005-2025, in ~96 nature areas (mostly Natura 2000) plus, since 2021,
17 points outside them (cities, farmland). Downloaded once to <data>/sources/rivm_man/ (shared by all regions):
  Alle-Gebieden-jaar.csv        https://man.rivm.nl/data_alle_jaar (yearly mean per point; no coordinates)
  Natura2000.geojson.js,        the site's area polygons, with MAN_naam = the csv's Gebied
  NietNatura2000.geojson.js
  MANmeetpunten.geojson.js      the extra points, with coordinates

MEASURED, unlike gcn_nh3_ug_m3 (a model calibrated on these and LML stations) and nh3_* (modelled emissions). But
the points are few: an area value is the mean of its 1-15 points spread over the whole area polygon, and the
points sit where RIVM chose to measure (often near the edge facing farmland).

Per hex and year:
  man_nh3_ug_m3     measured yearly mean NH3 (ug/m3): for a hex whose centre lies in a MAN area, the mean of that
                    area's points with a value that year; for a hex holding one of the extra points, that point.
                    Null elsewhere and in years the area was not measured.
  man_meetpunten    number of measuring points behind the value (meta)
  man_gebied        MAN area name (static; also set in years without a measurement)
The Veluwe cross-section points ("Veluwe Doorsnede") have neither coordinates nor a polygon and are skipped; the
national series ("Nederland") is not a place.

Usage: uv run datacube-zh/prepare/man.py download [--data own_data]
       uv run datacube-zh/prepare/man.py [--data own_data] [--region zh] [--years 2018-2025]
"""
import argparse
import csv
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

BASE = "https://man.rivm.nl"
FILES = {"Alle-Gebieden-jaar.csv": "/data_alle_jaar",
         **{f"{n}.geojson.js": f"/javascript/shapes/{n}.geojson.js"
            for n in ("Natura2000", "NietNatura2000", "MANmeetpunten")}}
SKIP = {"Nederland", "Veluwe Doorsnede"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def download(src: Path):
    src.mkdir(parents=True, exist_ok=True)
    for name, path in FILES.items():
        with urllib.request.urlopen(BASE + path, timeout=300) as r:
            (src / name).write_bytes(r.read())
        log(f"{name}: {(src / name).stat().st_size / 1e3:.0f} kB")


def load_js(f: Path) -> dict:
    """The site's shape files are `manApp.features.x = {geojson};` with // comment lines."""
    s = re.sub(r"^\s*//.*$", "", f.read_text(), flags=re.M)
    return json.loads(s[s.index("{"):s.rindex("}") + 1])


def measurements(src: Path) -> dict[str, dict[str, dict[int, float]]]:
    """{area: {point: {year: ug/m3}}} from the csv (decimal comma, empty = not measured)."""
    out = {}
    with open(src / "Alle-Gebieden-jaar.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            area = r["Gebied"].strip()
            if area in SKIP:
                continue
            vals = {int(k): float(v.replace(",", ".")) for k, v in r.items() if k and k.isdigit() and v}
            out.setdefault(area, {})[r["Locatienaam"].strip()] = vals
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", default="2018-2025")
    args = ap.parse_args()
    src = Path(args.data) / "sources/rivm_man"
    if args.step == "download":
        return download(src)
    a, _, b = args.years.partition("-")
    years = list(range(int(a), int(b or a) + 1))

    meas = measurements(src)
    work = Path(args.data) / regions.load(args.region)["work"]
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    lat, lon = np.array([h3.cell_to_latlng(c) for c in ids]).T
    pts = shapely.points(lon, lat)

    # areas: a hex centre in several polygons takes the smallest (the more specific area)
    areas = [(f["properties"]["MAN_naam"].strip(), shapely.geometry.shape(f["geometry"]))
             for n in ("Natura2000", "NietNatura2000") for f in load_js(src / f"{n}.geojson.js")["features"]
             if f["properties"].get("MAN_naam") and f["properties"]["MAN_naam"].strip() in meas]
    log(f"{len(areas)} MAN area polygons, {len({n for n, _ in areas})} of {len(meas)} csv areas")
    gebied = np.full(len(ids), None, object)
    best = np.full(len(ids), np.inf)
    hit_pt, hit_poly = shapely.STRtree([g for _, g in areas]).query(pts, predicate="within")
    for p, a_ in zip(hit_pt, hit_poly):
        if areas[a_][1].area < best[p]:
            best[p], gebied[p] = areas[a_][1].area, areas[a_][0]

    # extra points (coordinates known): the hex holding the point takes that point, not an area mean
    point_hex = {}
    for f in load_js(src / "MANmeetpunten.geojson.js")["features"]:
        pr = f["properties"]
        area, name = pr["MAN_naam"].strip(), pr["Meetpunt_naam"].strip()
        series = next((v for k, v in meas.get(area, {}).items() if k.split(" (")[0] == name.split(" (")[0]), None)
        if series is None:
            log(f"  extra point {area} / {name}: not in the csv, skipped")
            continue
        lon_, lat_ = f["geometry"]["coordinates"]
        point_hex[h3.latlng_to_cell(lat_, lon_, 9)] = (area, series)
    pos = {h: i for i, h in enumerate(ids)}
    inside = {h: v for h, v in point_hex.items() if h in pos}
    log(f"{len(inside)} of {len(point_hex)} extra points on region hexes")
    for h, (area, _) in inside.items():
        gebied[pos[h]] = area

    val_col, n_col = [], []
    for y in years:
        area_mean = {}
        for area, points in meas.items():
            v = [s[y] for s in points.values() if y in s]
            if v:
                area_mean[area] = (float(np.mean(v)), len(v))
        val, n = np.full(len(ids), np.nan), np.zeros(len(ids), np.int32)
        for i, g in enumerate(gebied):
            if g in area_mean:
                val[i], n[i] = area_mean[g]
        for h, (area, series) in inside.items():
            i = pos[h]
            val[i], n[i] = (series[y], 1) if y in series else (np.nan, 0)
        val_col.append(np.round(val, 2))
        n_col.append(n)
        log(f"{y}: {len(area_mean)} areas measured, {np.isfinite(val).sum()} hexes with a value, "
            f"mean {np.nanmean(val) if np.isfinite(val).any() else float('nan'):.2f} ug/m3")

    table = pa.table({"h3_id": ids * len(years), "year_int": np.repeat(np.array(years, np.int32), len(ids)),
                      "man_nh3_ug_m3": pa.array(np.concatenate(val_col), from_pandas=True),
                      "man_meetpunten": np.concatenate(n_col),
                      "man_gebied": pa.array(list(gebied) * len(years), pa.string())})
    source = {"source": "RIVM Meetnet Ammoniak Nederland (man.rivm.nl), yearly means per point + the site's area shapes",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers/man.parquet",
                   compression="zstd")
    log(f"-> {work / 'layers/man.parquet'} ({table.num_rows} rows)")


if __name__ == "__main__":
    main()
