# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17", "pyproj>=3.6"]
# ///
"""Critical deposition values (KDW) of nitrogen-sensitive Natura 2000 habitats -> per hex, plus yearly exceedance.

Source: RIVM "Koppeltabel relevante habitats" (edition 2026-10-06, data.rivm.nl/data/stikstof/Stikstof Natuur/2026/):
the 1 ha AERIUS hexagons (zoom level 1) with, per mapped habitat type, its KDW (critical deposition value,
mol N/ha/yr, 429-2399), mapped area (`surface`, m2) and coverage (share of that area that really is the habitat).
Relevant habitat area = surface x coverage, as AERIUS counts it. Downloaded once to <data>/sources/rivm_kdw/.

Each AERIUS hexagon goes to the H3 res-9 cell containing its centre (1 ha in ~10 ha cells). Per cell:
  kdw_habitat_m2              relevant nitrogen-sensitive habitat area (m2)
  kdw_min_mol_ha              lowest KDW of the habitats in the cell (the most sensitive one)
  kdw_gewogen_mol_ha          KDW weighted by relevant area
  kdw_habitat_type            habitat type with the largest relevant area (e.g. H4030 = droge heiden)
and per year, with the RIVM historical deposition series (the 1 km gdn grid sampled at each AERIUS hexagon):
  kdw_overschreden_m2         relevant habitat area where deposition > KDW
  kdw_overschreden_aandeel    the same as a share of kdw_habitat_m2
  kdw_overschrijding_mol_ha   mean exceedance over the relevant area: sum(area x max(0, deposition - KDW)) / area
Cells without relevant habitat are null. RIVM computes exceedance with its own 1 ha deposition (AERIUS); the 1 km
grid here is coarser, so local values differ; the national share is checked against RIVM's 2024 figure.

Writes <data>/<work>/layers/kdw.parquet (h3_id, year_int, ...).

Usage: uv run datacube-zh/prepare/kdw.py [--data own_data] [--region zh] [--years 2018-2025]
"""
import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402
from rivm import GDN_EDITION, read_asc, sample  # noqa: E402

EDITION = "20261006"
BASE = "https://data.rivm.nl/data/stikstof/Stikstof%20Natuur/2026/"
LAYER = "hexagons_to_relevant_habitats_zoom_level_1"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def habitats(src: Path) -> dict[str, np.ndarray]:
    """One row per (AERIUS hexagon, habitat type): centre (RD), KDW, relevant area. Cached as CSV next to the gpkg."""
    gpkg = src / f"RIVM-MIL_Koppeltabel-relevante-habitats_{EDITION}.gpkg"
    cache = src / f"habitats_{EDITION}.csv"
    if not cache.exists():
        if not gpkg.exists():
            z = src / f"RIVM-MIL_Koppeltabel-relevante-habitats_{EDITION}.zip"
            if not z.exists():
                subprocess.run(["curl", "-sfL", "-o", str(z), BASE + z.name], check=True)
            zipfile.ZipFile(z).extract(gpkg.name, src)
        sql = (f"SELECT receptor_id, habitat_type_name, critical_deposition, surface * coverage AS area, "
               f"ST_X(ST_Centroid(geom)) AS x, ST_Y(ST_Centroid(geom)) AS y FROM {LAYER} WHERE surface * coverage > 0")
        subprocess.run(["ogr2ogr", "-f", "CSV", str(cache), str(gpkg), "-dialect", "sqlite", "-sql", sql], check=True)
    with open(cache) as f:
        rows = list(csv.DictReader(f))
    return {"habitat": np.array([r["habitat_type_name"] for r in rows]),
            "kdw": np.array([float(r["critical_deposition"]) for r in rows]),
            "area": np.array([float(r["area"]) for r in rows]),
            "x": np.array([float(r["x"]) for r in rows]), "y": np.array([float(r["y"]) for r in rows])}


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
    src = data / "sources/rivm_kdw"
    src.mkdir(parents=True, exist_ok=True)

    hab = habitats(src)
    lon, lat = Transformer.from_crs(28992, 4326, always_xy=True).transform(hab["x"], hab["y"])
    cell = np.array([h3.latlng_to_cell(la, lo, 9) for la, lo in zip(lat, lon)])
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    index = {c: k for k, c in enumerate(ids)}
    k = np.array([index.get(c, -1) for c in cell])
    inside = k >= 0
    log(f"{len(k)} (hexagon, habitat) rows, {inside.sum()} in the region; relevant area "
        f"{hab['area'][inside].sum() / 1e6:.0f} of {hab['area'].sum() / 1e6:.0f} km2")
    k, area, kdw = k[inside], hab["area"][inside], hab["kdw"][inside]
    x, y, habitat = hab["x"][inside], hab["y"][inside], hab["habitat"][inside]

    n = len(ids)
    area_sum = np.bincount(k, area, n)
    kdw_w = np.bincount(k, area * kdw, n)
    kdw_min = np.full(n, np.inf)
    np.minimum.at(kdw_min, k, kdw)
    # dominant habitat type per cell (largest relevant area)
    best: dict[int, tuple[float, str]] = {}
    per = {}
    for i, h, ar in zip(k, habitat, area):
        per[(i, h)] = per.get((i, h), 0.0) + ar
    for (i, h), ar in per.items():
        if ar > best.get(i, (0.0, ""))[0]:
            best[i] = (ar, h)
    has = area_sum > 0
    static = {
        "kdw_habitat_m2": np.where(has, np.round(area_sum, 1), np.nan),
        "kdw_min_mol_ha": np.where(has, kdw_min, np.nan),
        "kdw_gewogen_mol_ha": np.where(has, np.round(kdw_w / np.where(has, area_sum, 1), 1), np.nan),
    }
    dominant = [best[i][1] if i in best else None for i in range(n)]

    gdn_zip = data / f"sources/rivm_gdn/RIVM-MIL_HistorischeReeksStikstofdepositie-ASCIIGrid_{GDN_EDITION}.zip"
    if not gdn_zip.exists():
        sys.exit(f"missing {gdn_zip}: run prepare/rivm.py --download first")
    yearly = {"kdw_overschreden_m2": [], "kdw_overschreden_aandeel": [], "kdw_overschrijding_mol_ha": []}
    with zipfile.ZipFile(gdn_zip) as zf:
        for yr in years:
            dep = sample(*read_asc(zf.read(f"ndep_historisch_{yr}.asc").decode()), x, y)
            over = np.nan_to_num(dep) > kdw
            ex = np.bincount(k, area * over, n)
            amount = np.bincount(k, area * np.maximum(0, np.nan_to_num(dep) - kdw), n)
            yearly["kdw_overschreden_m2"].append(np.where(has, np.round(ex, 1), np.nan))
            yearly["kdw_overschreden_aandeel"].append(np.where(has, np.round(ex / np.where(has, area_sum, 1), 4), np.nan))
            yearly["kdw_overschrijding_mol_ha"].append(np.where(has, np.round(amount / np.where(has, area_sum, 1), 1),
                                                                np.nan))
            log(f"{yr}: {ex.sum() / area_sum.sum():.1%} of the relevant habitat area above its KDW")

    ny = len(years)
    table = pa.table({
        "h3_id": ids * ny, "year_int": np.repeat(np.array(years, np.int32), n),
        **{c: pa.array(np.tile(v, ny), from_pandas=True) for c, v in static.items()},
        "kdw_habitat_type": dominant * ny,
        **{c: pa.array(np.concatenate(v), from_pandas=True) for c, v in yearly.items()},
    })
    source = {"source": f"RIVM Koppeltabel relevante habitats (edition {EDITION}) + GDN historical series ({GDN_EDITION})",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers/kdw.parquet",
                   compression="zstd")
    log(f"-> {work / 'layers/kdw.parquet'}: {int(has.sum())} cells with relevant habitat")


if __name__ == "__main__":
    main()
