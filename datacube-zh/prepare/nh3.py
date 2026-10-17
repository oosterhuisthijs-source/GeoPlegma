# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17", "pyproj>=3.6", "openpyxl", "pandas"]
# ///
"""RIVM Emissieregistratie ammonia emissions per 1 km square -> yearly emission per hex.

Source: Emissieregistratie (RIVM, with PBL, CBS, WUR, Deltares), dataset "ER Reeks 1990-2024 Definitief",
regional distribution "Combinatie kaart vk5 NCP en vk1 Nederland" (1x1 km on land, 5x5 km at sea), substance
ammoniak, compartment lucht, source level subsector. Regionalised years: 2019-2024 (before that only 2015,
2010, ...; 2025 is national only until early 2027), so 2018 and 2025 stay null.

The export is the portal's own form (data.emissieregistratie.nl/export), a plain multipart POST:

  nh3.py download [--years 2019-2024]   -> <data>/sources/emissieregistratie/nh3_<year>_subsector.xlsx
                                        (~60 MB each, 1-3 min per year; existing files are kept)

MODELLED: national emissions (NEMA: animal counts x emission factors, fertiliser sales) distributed over the
country; for stables by farm location, for manure application and grazing over that year's farmland. RIVM's
deposition maps (gdn_*) are calculated from these emissions, and livestock counts are an input, so this is
not independent of vee_* or gdn_*. An animal subsector holds ALL emissions of that animal's manure: stable,
storage, grazing and application on the field (cattle 2024: 52.7 kt, against 30 kt for stable + storage only
in CBS 83982NED), so it also lands on fields far from the farm.

Per hex and year (kg NH3; each 1 km square's emission split over the hexes whose centre lies in it, by hex
area, so sums are kept; squares without a hex centre in the region are lost and logged):
  nh3_rundvee_kg          Landbouwhuisdieren - rundvee
  nh3_varkens_kg          Landbouwhuisdieren - varkens
  nh3_pluimvee_kg         Landbouwhuisdieren - pluimvee
  nh3_overige_dieren_kg   Landbouwhuisdieren - overige dieren (sheep, goats, horses, rabbits, fur animals)
  nh3_kunstmest_kg        Kunstmest (artificial fertiliser)
  nh3_landbouw_kg         whole sector Landbouw (the above + soil/crop processes, private farming, energy, ...)
  nh3_totaal_kg           all sectors (+ traffic, consumers, industry, waste, ...)

Usage: uv run datacube-zh/prepare/nh3.py download [--data own_data] [--years 2019-2024]
       uv run datacube-zh/prepare/nh3.py [--data own_data] [--region zh] [--years 2018-2025]
"""
import argparse
import datetime as dt
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

URL = "https://data.emissieregistratie.nl/api/export/v1/emissies/rapport?tz=%2B120"
# form fields of the portal: lucht, ammoniak, 1 km / 5 km combination map, subsector
FORM = {"compartiment": "1", "stoffen": "80", "gebiedsindeling": "8", "bronniveau": "3",
        "rapporttype": "jaren_onder_elkaar"}
REGIONALISED = range(2019, 2025)
SUBSECTORS = {"nh3_rundvee_kg": "Landbouwhuisdieren - rundvee", "nh3_varkens_kg": "Landbouwhuisdieren - varkens",
              "nh3_pluimvee_kg": "Landbouwhuisdieren - pluimvee",
              "nh3_overige_dieren_kg": "Landbouwhuisdieren - overige dieren", "nh3_kunstmest_kg": "Kunstmest"}
COLUMNS = [*SUBSECTORS, "nh3_landbouw_kg", "nh3_totaal_kg"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def parse_years(s: str) -> list[int]:
    a, _, b = s.partition("-")
    return list(range(int(a), int(b or a) + 1))


def download(src: Path, years: list[int]):
    src.mkdir(parents=True, exist_ok=True)
    for y in years:
        f = src / f"nh3_{y}_subsector.xlsx"
        if f.exists():
            continue
        boundary = uuid.uuid4().hex
        body = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                       for k, v in {**FORM, "jaren": str(y)}.items()) + f"--{boundary}--\r\n"
        req = urllib.request.Request(URL, data=body.encode(), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=1200) as r:
            data = r.read()
        if not data.startswith(b"PK"):
            raise SystemExit(f"{y}: not an xlsx ({data[:200]!r})")
        f.write_bytes(data)
        log(f"{y}: {len(data) / 1e6:.0f} MB ({time.time() - t0:.0f}s)")


def squares(src: Path, year: int) -> pd.DataFrame | None:
    """One row per (square code, column) with kg NH3; cached as parquet next to the xlsx."""
    cache = src / f"nh3_{year}_subsector.parquet"
    if not cache.exists():
        f = src / f"nh3_{year}_subsector.xlsx"
        if not f.exists():
            return None
        df = pd.read_excel(f, sheet_name="Emissies", usecols=["Code_gebied", "Sector", "Subsector", "Jaar", "Emissie",
                                                                "Eenheid"])
        assert (df["Jaar"] == year).all() and (df["Eenheid"] == "kg").all(), f"{f}: unexpected year or unit"
        df[["Code_gebied", "Sector", "Subsector", "Emissie"]].to_parquet(cache)
    df = pd.read_parquet(cache)
    parts = [df[df["Subsector"] == name].assign(col=c) for c, name in SUBSECTORS.items()]
    parts += [df[df["Sector"] == "Landbouw"].assign(col="nh3_landbouw_kg"), df.assign(col="nh3_totaal_kg")]
    return pd.concat(parts).groupby(["Code_gebied", "col"], as_index=False)["Emissie"].sum()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", default=None)
    args = ap.parse_args()
    src = Path(args.data) / "sources/emissieregistratie"
    if args.step == "download":
        return download(src, parse_years(args.years or "2019-2024"))
    years = parse_years(args.years or "2018-2025")

    work = Path(args.data) / regions.load(args.region)["work"]
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    lat, lon = np.array([h3.cell_to_latlng(c) for c in ids]).T
    x, y = Transformer.from_crs(4326, 28992, always_xy=True).transform(lon, lat)
    area = np.array([h3.cell_area(c, "m^2") for c in ids])
    # square codes are the lower-left corner in RD km: "014369_km1" = x 14-15 km, y 369-370 km; km5 squares
    # (sea) the same with 5 km sides. A hex takes its 1 km square, else (at sea) its 5 km square.
    kx, ky = np.floor(x / 1000).astype(int), np.floor(y / 1000).astype(int)
    km1 = pd.Series([f"{a:03d}{b:03d}_km1" for a, b in zip(kx, ky)])
    km5 = pd.Series([f"{a - a % 5:03d}{b - b % 5:03d}_km5" for a, b in zip(kx, ky)])

    out = {c: [] for c in COLUMNS}
    for year in years:
        sq = squares(src, year) if year in REGIONALISED else None
        if sq is None:
            for c in COLUMNS:
                out[c].append(np.full(len(ids), np.nan))
            log(f"{year}: not regionalised by Emissieregistratie, left empty")
            continue
        codes = set(sq["Code_gebied"])
        key = np.where(km1.isin(codes), km1, np.where(km5.isin(codes), km5, None))
        has = key != None  # noqa: E711
        # a hex's share of its square: its area over the area of all region hexes with their centre in that square
        share = np.zeros(len(ids))
        tot = pd.Series(area[has]).groupby(key[has]).transform("sum").to_numpy()
        share[has] = area[has] / tot
        for c in COLUMNS:
            s = sq[sq["col"] == c].set_index("Code_gebied")["Emissie"]
            v = np.zeros(len(ids))
            v[has] = s.reindex(key[has]).fillna(0).to_numpy() * share[has]
            out[c].append(np.round(v, 2))
            if c in ("nh3_landbouw_kg", "nh3_totaal_kg"):
                lost = s.drop(set(key[has]), errors="ignore").sum()
                log(f"  {year} {c}: {s.sum() / 1e6:.2f} kt in the country, {v.sum() / 1e6:.2f} kt on region hexes "
                    f"({lost / 1e6:.2f} kt in squares without a hex centre)")
        log(f"{year}: " + ", ".join(f"{c.removeprefix('nh3_').removesuffix('_kg')} {out[c][-1].sum() / 1e6:.1f} kt"
                                    for c in COLUMNS))

    table = pa.table({"h3_id": ids * len(years), "year_int": np.repeat(np.array(years, np.int32), len(ids)),
                      **{c: pa.array(np.concatenate(out[c]), from_pandas=True) for c in COLUMNS}})
    source = {"source": "RIVM Emissieregistratie, ER Reeks 1990-2024 Definitief, ammoniak naar lucht per subsector, "
                        "1x1 km (vk1) / 5x5 km (vk5 NCP)",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers/nh3.parquet",
                   compression="zstd")
    log(f"-> {work / 'layers/nh3.parquet'} ({table.num_rows} rows)")


if __name__ == "__main__":
    main()
