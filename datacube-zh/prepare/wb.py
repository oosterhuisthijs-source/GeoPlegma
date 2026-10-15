#!/usr/bin/env python3
"""CBS Kerncijfers wijken en buurten -> work/wb/wb_<year>.parquet.

Reads the buurten layer of each year's Wijk- en Buurtkaart (downloaded by this
script if missing; latest version per year), keeps the indicators listed in
datacube-zh/config/wb.toml (renaming per year via the candidate lists),
clips to the region and reprojects to OGC:CRS84. The polygons + columns are
then aggregated by `datacube-zh cbs --layer wb`.

Usage: datacube-zh/prepare/wb.py [--data own_data] [--region zh]
"""
import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

VERSIONS = {2018: "v3", 2019: "v3", 2020: "v3", 2021: "v3", 2022: "v3", 2023: "v3", 2024: "v2", 2025: "v1"}
URL = "https://geodata.cbs.nl/files/Wijkenbuurtkaart/WijkBuurtkaart_{year}_{v}.zip"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def gpkg_for(src: Path, year: int) -> Path:
    v = VERSIONS[year]
    hits = sorted(src.glob(f"**/*{year}_{v}*.gpkg"), key=lambda p: len(str(p)))
    if hits:
        return hits[0]
    zip_path = src / f"WijkBuurtkaart_{year}_{v}.zip"
    if not zip_path.exists():
        subprocess.run(["curl", "-sSfL", "-o", str(zip_path), URL.format(year=year, v=v)], check=True)
    subprocess.run(["unzip", "-o", "-q", str(zip_path), "-d", str(src / f"x_{year}_{v}")], check=True)
    return sorted(src.glob(f"**/*{year}_{v}*.gpkg"), key=lambda p: len(str(p)))[0]


def buurt_layer(gpkg: Path) -> tuple[str, set[str]]:
    info = subprocess.run(["ogrinfo", "-q", str(gpkg)], capture_output=True, text=True, check=True).stdout
    layer = next(m.group(1) for line in info.splitlines() if (m := re.match(r"^\d+: (\S*buurt\S*)", line)))
    schema = subprocess.run(["ogrinfo", "-so", str(gpkg), layer], capture_output=True, text=True, check=True).stdout
    fields = {m.group(1) for line in schema.splitlines() if (m := re.match(r"^(\w+): ", line))}
    return layer, fields


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--config", default="datacube-zh/config/wb.toml")
    args = ap.parse_args()
    reg = regions.load(args.region)
    cfg = tomllib.loads(Path(args.config).read_text())
    src = Path(args.data) / "sources/wijkbuurt"
    out_dir = Path(args.data) / reg["work"] / "wb"
    out_dir.mkdir(parents=True, exist_ok=True)

    for year in VERSIONS:
        out = out_dir / f"wb_{year}.parquet"
        if out.exists():
            log(f"skip {out} (exists)")
            continue
        gpkg = gpkg_for(src, year)
        layer, fields = buurt_layer(gpkg)
        select, missing = [], []
        for name, candidates in cfg["indicators"].items():
            hit = next((c for c in candidates if c in fields), None)
            if hit:
                select.append(f'CAST("{hit}" AS float) AS {name}')
            else:
                missing.append(name)
        sql = f'SELECT {", ".join(select)} FROM "{layer}"'
        subprocess.run(
            ["ogr2ogr", "-f", "Parquet", str(out), str(gpkg), "-sql", sql, "-dialect", "OGRSQL",
             "-spat", *map(str, reg["bbox_rd"]), "-t_srs", "OGC:CRS84", "-nlt", "MULTIPOLYGON",
             "-makevalid", "-lco", "GEOMETRY_ENCODING=WKB", "-lco", "COMPRESSION=ZSTD",
             "-lco", "GEOMETRY_NAME=geom", "-overwrite"],
            check=True,
        )
        log(f"wb {year}: {gpkg.name}:{layer} -> {out} ({len(select)} indicators"
            + (f"; not in this kaart: {', '.join(missing)}" if missing else "") + ")")


if __name__ == "__main__":
    main()
