#!/usr/bin/env python3
"""RIVM wind turbine locations -> per-hex counts, power, size and distance to the nearest turbine.

Source: RIVM Atlas Leefomgeving WFS layer `rivm_<date>_windturbines_vermogen` (one point per turbine
with power, hub height and rotor diameter; NL on land, inland water and sea, plus Belgian and German
turbines near the border). The layer is a dated snapshot without a construction year, so the result
is static: the same values in every cube year.

The snapshot is downloaded once to <data>/sources/wind/<layer>.geojson (shared by all regions).
Per hex (H3 res 9 of the turbine point):
  wind_aantal                 number of turbines
  wind_vermogen_kw            summed rated power (kW)
  wind_ashoogte_max_m         highest hub height (m; empty without a turbine)
  wind_rotordiameter_max_m    largest rotor diameter (m; empty without a turbine)
  wind_tiphoogte_max_m        highest tip height = hub height + rotor diameter / 2 (m)
  wind_afstand_min_km         distance from the hex centre to the nearest turbine of any country,
                              on land or at sea (km, in RD)

Writes <data>/<work>/layers/wind.parquet. Needs GDAL's Python bindings (osr), numpy, scipy,
pyarrow and h3 (the system python3 here).

Usage: datacube-zh/prepare/wind.py [--data own_data] [--region zh] [--layer rivm_20260101_windturbines_vermogen]
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from osgeo import osr
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

WFS = "https://data.rivm.nl/geo/alo/wfs"
LAYER = "rivm_20260101_windturbines_vermogen"
H3_RES = 9
osr.UseExceptions()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(data: Path, layer: str) -> list[dict]:
    dest = data / "sources/wind" / f"{layer}.geojson"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        q = urllib.parse.urlencode({"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                                    "typeNames": f"alo:{layer}", "outputFormat": "application/json",
                                    "srsName": "EPSG:28992"})
        log(f"downloading {layer}")
        with urllib.request.urlopen(f"{WFS}?{q}", timeout=300) as r:
            dest.write_bytes(r.read())
    feats = json.loads(dest.read_text())["features"]
    log(f"{len(feats)} turbines in {dest}")
    return feats


def transformer(src: int, dst: int) -> osr.CoordinateTransformation:
    a, b = osr.SpatialReference(), osr.SpatialReference()
    for s, code in ((a, src), (b, dst)):
        s.ImportFromEPSG(code)
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(a, b)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--layer", default=LAYER)
    args = ap.parse_args()
    data = Path(args.data)
    reg = regions.load(args.region)
    work = data / reg["work"]

    feats = fetch(data, args.layer)
    xy = np.array([f["geometry"]["coordinates"][:2] for f in feats], dtype=float)
    p = [f["properties"] for f in feats]
    kw = np.array([q["kw"] or 0 for q in p], dtype=float)
    ash = np.array([q["ash"] if q["ash"] is not None else np.nan for q in p], dtype=float)
    diam = np.array([q["diam"] if q["diam"] is not None else np.nan for q in p], dtype=float)

    to_wgs = transformer(28992, 4326)
    lonlat = np.array(to_wgs.TransformPoints(xy.tolist()))[:, :2]
    cells = [h3.latlng_to_cell(lat, lon, H3_RES) for lon, lat in lonlat]

    grid = pq.read_table(work / "grid.parquet", columns=["h3_id", "lat", "lon"])
    ids = grid["h3_id"].to_pylist()
    index = {h: i for i, h in enumerate(ids)}
    n = len(ids)
    count = np.zeros(n, dtype=np.int64)
    power = np.zeros(n)
    ash_max = np.full(n, np.nan)
    diam_max = np.full(n, np.nan)
    tip_max = np.full(n, np.nan)
    for c, k, a, d in zip(cells, kw, ash, diam):
        i = index.get(c)
        if i is None:
            continue
        count[i] += 1
        power[i] += k
        ash_max[i] = np.fmax(ash_max[i], a)
        diam_max[i] = np.fmax(diam_max[i], d)
        tip_max[i] = np.fmax(tip_max[i], a + d / 2)

    to_rd = transformer(4326, 28992)
    centres = np.array(to_rd.TransformPoints(np.column_stack([grid["lon"].to_numpy(), grid["lat"].to_numpy()]).tolist()))[:, :2]
    dist, _ = cKDTree(xy).query(centres)

    def opt(a):
        return pa.array(a, pa.float64(), mask=np.isnan(a))

    out = pa.table({
        "h3_id": ids,
        "wind_aantal": pa.array(count, pa.int64()),
        "wind_vermogen_kw": pa.array(power, pa.float64()),
        "wind_ashoogte_max_m": opt(ash_max),
        "wind_rotordiameter_max_m": opt(diam_max),
        "wind_tiphoogte_max_m": opt(tip_max),
        "wind_afstand_min_km": pa.array(np.round(dist / 1000, 3), pa.float64()),
    })
    dest = work / "layers/wind.parquet"
    pq.write_table(out, dest, compression="zstd")
    log(f"{int(count.sum())} turbines in {int((count > 0).sum())} of {n} hexes "
        f"({power.sum() / 1000:.0f} MW); median distance {np.median(dist) / 1000:.1f} km -> {dest}")


if __name__ == "__main__":
    main()
