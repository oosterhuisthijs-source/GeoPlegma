# /// script
# requires-python = ">=3.11"
# dependencies = ["openpyxl", "pandas", "pyarrow>=17"]
# ///
"""Data centres per gemeente and year from RVO's energy-efficiency reports -> side table.

Source: RVO "Rapportages energie-efficiëntie van datacentra" (EU Energy Efficiency Directive art. 12; data year 2025,
published 5 October 2026), one row per data centre >= 500 kW IT power that had to report: gemeente code (lauCode),
type, operational start year, and, where the operator did not mark them confidential, installed IT power (kW, 48 of
104), floor area (m2, 69) and energy use 2025 (kWh, 47). No coordinates (locations: osm_datacentrum_* per cell, from
OSM). No licence stated by RVO. Earlier editions are not online, so the history is only the start year:
  rvo_datacentra_2025.xlsx  https://www.rvo.nl/sites/default/files/2026-10/Rapportages%20energie-effici%C3%ABntie%20van%20datacentra.xlsx

Per gemeente (the cube's static 2024 gemeenten; lauCode is that code) and year 2018-2025:
  dc_rvo_in_bedrijf          reporting data centres operating that year (start year <= year); centres that closed
                             before 2025 never reported, so earlier years are a lower bound
  dc_rvo_it_vermogen_kw      their installed IT power as reported for 2025 (sum over those that report it)
  dc_rvo_it_vermogen_gemeld  how many of them report it
  dc_rvo_vloer_m2            their total floor area as reported for 2025; dc_rvo_vloer_gemeld: how many report it
  dc_rvo_energie_kwh         energy use 2025 (2025 row only); dc_rvo_energie_gemeld: how many report it

Written to <work>/layers/datacentra_gemeente.parquet; assemble.py ships it as datacube_<region>_datacentra.parquet.

Usage: uv run datacube-zh/prepare/datacentra.py download [--data own_data]
       uv run datacube-zh/prepare/datacentra.py [--data own_data] [--region nl]
"""
import argparse
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

URL = "https://www.rvo.nl/sites/default/files/2026-10/Rapportages%20energie-effici%C3%ABntie%20van%20datacentra.xlsx"
FILE = "rvo_datacentra_2025.xlsx"
YEARS = range(2018, 2026)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    args = ap.parse_args()
    src = Path(args.data) / "sources/datacentra"
    if args.step == "download":
        src.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=120) as r:
            (src / FILE).write_bytes(r.read())
        return log(f"-> {src / FILE}")

    d = pd.read_excel(src / FILE, header=1)
    units = {"itPowerDemand Installed_unit": "KW", "totalFloorArea unit": "M2", "totalEnergy Consumption_unit": "KWH"}
    for col, unit in units.items():
        bad = d[col].dropna()[lambda s: s != unit]
        if len(bad):
            raise SystemExit(f"{col}: unexpected units {set(bad)}")
    d = d.rename(columns={"lauCode": "gemeentecode", "operational StartYear": "start",
                          "itPowerDemand Installed": "kw", "totalFloorArea": "m2", "totalEnergy Consumption": "kwh"})

    work = Path(args.data) / regions.load(args.region)["work"]
    admin = pq.read_table(work / "layers/admin.parquet", columns=["gemeentecode", "gemeentenaam"]).to_pandas()
    names = dict(admin.drop_duplicates("gemeentecode").values)
    unknown = set(d["gemeentecode"]) - names.keys()
    if unknown:
        raise SystemExit(f"lauCodes not among the cube's 2024 gemeenten: {sorted(unknown)}")

    rows = []
    for code in sorted(names):
        if code == "GM0998":
            continue
        g = d[d["gemeentecode"] == code]
        for y in YEARS:
            op = g[g["start"] <= y]
            rows.append({"year_int": y, "gemeentecode": code, "gemeentenaam": names[code],
                         "dc_rvo_in_bedrijf": len(op),
                         "dc_rvo_it_vermogen_kw": float(op["kw"].sum()), "dc_rvo_it_vermogen_gemeld": int(op["kw"].notna().sum()),
                         "dc_rvo_vloer_m2": float(op["m2"].sum()), "dc_rvo_vloer_gemeld": int(op["m2"].notna().sum()),
                         "dc_rvo_energie_kwh": float(op["kwh"].sum()) if y == 2025 else None,
                         "dc_rvo_energie_gemeld": int(op["kwh"].notna().sum()) if y == 2025 else None})
    t = pa.Table.from_pylist(rows)
    t = t.cast(pa.schema([("year_int", pa.int32()), ("gemeentecode", pa.string()), ("gemeentenaam", pa.string()),
                          ("dc_rvo_in_bedrijf", pa.int32()), ("dc_rvo_it_vermogen_kw", pa.float64()),
                          ("dc_rvo_it_vermogen_gemeld", pa.int32()), ("dc_rvo_vloer_m2", pa.float64()),
                          ("dc_rvo_vloer_gemeld", pa.int32()), ("dc_rvo_energie_kwh", pa.float64()),
                          ("dc_rvo_energie_gemeld", pa.int32())]))
    source = {"source": "RVO, Rapportages energie-efficiëntie van datacentra 2026 (gegevens 2025), 5-10-2026",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    dest = work / "layers/datacentra_gemeente.parquet"
    pq.write_table(t.replace_schema_metadata({"datacube": json.dumps(source)}), dest)
    last = d[d["start"] <= 2025]
    log(f"{len(d)} data centres in {d['gemeentecode'].nunique()} gemeenten; operating 2018 {(d['start'] <= 2018).sum()}, "
        f"2025 {len(last)}; reported IT power {last['kw'].sum() / 1000:.0f} MW ({last['kw'].notna().sum()} centres), "
        f"energy 2025 {last['kwh'].sum() / 1e6:.0f} GWh ({last['kwh'].notna().sum()})")
    log(f"-> {dest} ({t.num_rows} rows)")


if __name__ == "__main__":
    main()
