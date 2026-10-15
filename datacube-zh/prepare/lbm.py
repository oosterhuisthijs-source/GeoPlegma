#!/usr/bin/env python3
"""Leefbaarometer 3.0 (BZK), 100 m grid -> work/lbm/lbm_<year>.parquet.

Source: https://www.leefbaarometer.nl/page/Opendata, "Meting 2024" open data
(one consistent model run for 2002-2024), file
"Leefbaarometer-scores grids 2002-2024.csv": per 100 m RD cell and
measurement year the total score `lbm` (absolute), `afw` (deviation from the
national mean) and the five dimension scores (deviations): fys (fysieke
omgeving), onv (overlast & onveiligheid), soc (sociale samenhang), vrz
(voorzieningen), won (woningvoorraad).

The cell polygon is built from grid_id ("E0136N3714" = lower-left corner
E 13600, N 371400 in EPSG:28992, 100 m). Aggregated by
`datacube-zh cbs --layer lbm` (area-weighted average per hex).

Usage: datacube-zh/prepare/lbm.py [--data own_data] [--region zh]
"""
import argparse
import csv
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

YEARS = [2018, 2020, 2022, 2024]  # measurement years within the cube
URL = "https://www.leefbaarometer.nl/resources/open-data-leefbaarometer-meting-2024.zip"
CSV_NAME = "Leefbaarometer 3.0 - Meting 2024 - open data/Leefbaarometer-scores grids 2002-2024.csv"
COLUMNS = {
    "lbm": "lbm_score",
    "afw": "lbm_afwijking",
    "fys": "lbm_fysieke_omgeving",
    "onv": "lbm_overlast_onveiligheid",
    "soc": "lbm_sociale_samenhang",
    "vrz": "lbm_voorzieningen",
    "won": "lbm_woningvoorraad",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    args = ap.parse_args()
    reg = regions.load(args.region)
    xmin, ymin, xmax, ymax = reg["bbox_rd"]
    src = Path(args.data) / "sources/leefbaarometer"
    out_dir = Path(args.data) / reg["work"] / "lbm"
    out_dir.mkdir(parents=True, exist_ok=True)

    scores = src / CSV_NAME
    if not scores.exists():
        src.mkdir(parents=True, exist_ok=True)
        zip_path = src / Path(URL).name
        subprocess.run(["curl", "-sSfL", "-A", "Mozilla/5.0", "-o", str(zip_path), URL], check=True)
        subprocess.run(["unzip", "-o", "-q", str(zip_path), "-d", str(src), "-x", "__MACOSX/*"], check=True)

    writers, files = {}, {}
    for y in YEARS:
        f = open(out_dir / f"lbm_{y}.csv", "w", newline="")
        files[y] = f
        writers[y] = csv.writer(f)
        writers[y].writerow(["wkt", *COLUMNS.values()])
    counts = dict.fromkeys(YEARS, 0)
    with open(scores, newline="") as f:
        for row in csv.DictReader(f):
            y = int(row["jaar"])
            if y not in writers:
                continue
            gid = row["grid_id"]  # E0136N3714
            x0 = int(gid[1:5]) * 100
            y0 = int(gid[6:10]) * 100
            if not (xmin <= x0 <= xmax and ymin <= y0 <= ymax):
                continue
            x1, y1 = x0 + 100, y0 + 100
            wkt = f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"
            writers[y].writerow([wkt, *("" if row[k] == "NA" else row[k] for k in COLUMNS)])
            counts[y] += 1
    for f in files.values():
        f.close()

    for y in YEARS:
        csv_path = out_dir / f"lbm_{y}.csv"
        out = out_dir / f"lbm_{y}.parquet"
        subprocess.run(
            ["ogr2ogr", "-f", "Parquet", str(out), str(csv_path), "-overwrite",
             "-oo", "GEOM_POSSIBLE_NAMES=wkt", "-oo", "KEEP_GEOM_COLUMNS=NO", "-oo", "AUTODETECT_TYPE=YES",
             "-s_srs", "EPSG:28992", "-t_srs", "OGC:CRS84", "-nln", f"lbm_{y}",
             "-lco", "GEOMETRY_ENCODING=WKB", "-lco", "COMPRESSION=ZSTD", "-lco", "GEOMETRY_NAME=geom"],
            check=True,
        )
        csv_path.unlink()
        log(f"lbm {y}: {counts[y]} cells -> {out}")


if __name__ == "__main__":
    main()
