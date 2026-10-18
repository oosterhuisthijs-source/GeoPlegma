# /// script
# requires-python = ">=3.11"
# dependencies = ["shapely>=2", "pyproj>=3.6", "h3>=4", "pyarrow>=17", "numpy"]
# ///
"""Primary flood defences (primaire waterkeringen): norms, safety assessment and HWBP reinforcements -> per hex
lengths and attributes + a whole-line file.

Source: Rijkswaterstaat Waterveiligheidsportaal WFS (geo.rijkswaterstaat.nl/services/ogc/wvp/ows; NGR: CC0 / Public
Domain), downloaded once to <data>/sources/waterkeringen/<layer>.geojson (EPSG:28992):
  nbpw_dijktrajecten_formeel             the 237 dyke sections (dijktrajecten) with their legal standard (Omgevingswet):
                                         NORM_SW signaleringswaarde, NORM_OG ondergrens, as return period N (1/N per year)
  lbo1_veiligheidsoordeel_dijktrajecten  first national assessment LBO-1 (to 2023): veiligheidsoordeel A+ (much safer
                                         than required) .. D (much less safe than the lower limit) per section
  hwbp_actuele_projecten_dijkvakken      current Hoogwaterbeschermingsprogramma projects (dyke stretches) with planned
                                         years (verkenning / onderzoek / realisatie start and end; strings, often null)
  hwbp_afgesloten_projecten_dijkvakken   completed HWBP projects (status DYKE_SAFE, no years)
Static: the current state, not a series.

Per hex (lengths exact: lines x hex boundaries in RD; 0 = none; attributes null where there is no such line):
  wk_primair_lengte_m           length of primary dyke sections
  wk_norm_signalering_jaar      strictest signaleringswaarde of the sections in the hex (return period N, years)
  wk_norm_ondergrens_jaar       strictest ondergrens (N, years)
  wk_lbo1_oordeel               worst LBO-1 safety category in the hex (A+ < A < B < C < D)
  wk_hwbp_lengte_m              length of stretches in a current HWBP project
  wk_hwbp_realisatie_start      earliest planned start of construction among those (year)
  wk_hwbp_realisatie_eind       latest planned end of construction among those (year)
  wk_hwbp_afgerond_lengte_m     length of stretches of completed HWBP projects
Also <work>/layers/waterkeringen_lijnen.parquet (GeoParquet, EPSG:4326): one row per section or HWBP stretch, whole
(not clipped), with its attributes and the region cells it crosses (`cellen`); shipped as
datacube_<region>_waterkeringen.parquet.

Usage: uv run datacube-zh/prepare/waterkeringen.py download [--data own_data]
       uv run datacube-zh/prepare/waterkeringen.py [--data own_data] [--region nl]
"""
import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

WFS = "https://geo.rijkswaterstaat.nl/services/ogc/wvp/ows"
LAYERS = ["nbpw:nbpw_dijktrajecten_formeel", "lbo1:lbo1_veiligheidsoordeel_dijktrajecten",
          "hwbp:hwbp_actuele_projecten_dijkvakken", "hwbp:hwbp_afgesloten_projecten_dijkvakken"]
CATEGORIES = ["A+", "A", "B", "C", "D"]
SAMPLE_M = 10
COLUMNS = ["wk_primair_lengte_m", "wk_norm_signalering_jaar", "wk_norm_ondergrens_jaar", "wk_lbo1_oordeel",
           "wk_hwbp_lengte_m", "wk_hwbp_realisatie_start", "wk_hwbp_realisatie_eind", "wk_hwbp_afgerond_lengte_m"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def download(src: Path):
    src.mkdir(parents=True, exist_ok=True)
    for layer in LAYERS:
        q = urllib.parse.urlencode({"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": layer,
                                    "outputFormat": "application/json", "srsName": "EPSG:28992"})
        with urllib.request.urlopen(f"{WFS}?{q}", timeout=600) as r:
            d = json.load(r)
        if d.get("numberMatched") not in (None, len(d["features"])):
            raise SystemExit(f"{layer}: {len(d['features'])} of {d['numberMatched']} features (paging needed)")
        (src / f"{layer.split(':')[1]}.geojson").write_text(json.dumps(d))
        log(f"{layer}: {len(d['features'])} features")


def year(v) -> int | None:
    return int(v) if v not in (None, "") and str(v).isdigit() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    args = ap.parse_args()
    src = Path(args.data) / "sources/waterkeringen"
    if args.step == "download":
        return download(src)

    def features(name):
        return json.loads((src / f"{name}.geojson").read_text())["features"]

    lbo1 = {f["properties"]["dijktraject"]: f["properties"]["veiligheidsoordeelcategorie"] or None
            for f in features("lbo1_veiligheidsoordeel_dijktrajecten")}
    rows = []  # (soort, properties for the line file, RD geometry)
    for f in features("nbpw_dijktrajecten_formeel"):
        p = f["properties"]
        rows.append(("dijktraject", {
            "traject_id": p["TRAJECT_ID"], "naam": None,
            "norm_signalering_jaar": p["NORM_SW"] or None, "norm_ondergrens_jaar": p["NORM_OG"] or None,
            "lbo1_oordeel": lbo1.get(p["TRAJECT_ID"]), "beheerder": None,
            "realisatie_start": None, "realisatie_eind": None}, shapely.geometry.shape(f["geometry"])))
    for kind, name in (("hwbp_project", "hwbp_actuele_projecten_dijkvakken"),
                       ("hwbp_afgerond", "hwbp_afgesloten_projecten_dijkvakken")):
        for f in features(name):
            p = f["properties"]
            rows.append((kind, {
                "traject_id": p.get("traject_id"), "naam": p.get("name"), "norm_signalering_jaar": None,
                "norm_ondergrens_jaar": None, "lbo1_oordeel": None,
                "beheerder": p.get("description") if kind == "hwbp_project" else p.get("waterbeheerder"),
                "realisatie_start": year(p.get("realisatie_start_jaar")),
                "realisatie_eind": year(p.get("realisatie_eind_jaar"))}, shapely.geometry.shape(f["geometry"])))
    log(", ".join(f"{k}: {sum(r[0] == k for r in rows)}" for k in ("dijktraject", "hwbp_project", "hwbp_afgerond")))

    work = Path(args.data) / regions.load(args.region)["work"]
    grid = set(pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist())
    to_rd = Transformer.from_crs(4326, 28992, always_xy=True)
    to_wgs = Transformer.from_crs(28992, 4326, always_xy=True)

    # candidate (line, cell) pairs from points every SAMPLE_M (+ neighbours), then exact lengths (as prepare/osm.py)
    pair_line, pair_cell = [], []
    for i, (_, _, g) in enumerate(rows):
        for part in shapely.get_parts(g):
            d = np.append(np.arange(0, part.length, SAMPLE_M), part.length)
            xy = shapely.get_coordinates(shapely.line_interpolate_point(part, d))
            lon, lat = to_wgs.transform(xy[:, 0], xy[:, 1])
            cand = {n for lo, la in zip(lon, lat) for n in h3.grid_disk(h3.latlng_to_cell(la, lo, 9), 1)} & grid
            pair_line += [i] * len(cand)
            pair_cell += sorted(cand)
    pairs = sorted(set(zip(pair_line, pair_cell)))
    pair_line, pair_cell = [p[0] for p in pairs], [p[1] for p in pairs]
    hexes = {}
    for c in set(pair_cell):
        lat, lon = np.array(h3.cell_to_boundary(c)).T
        hexes[c] = shapely.Polygon(np.column_stack(to_rd.transform(lon, lat)))
    lengths = shapely.length(shapely.intersection(np.array([rows[i][2] for i in pair_line], dtype=object),
                                                  np.array([hexes[c] for c in pair_cell], dtype=object)))

    ids = sorted(grid)
    index = {c: k for k, c in enumerate(ids)}
    n = len(ids)
    length = {k: np.zeros(n) for k in ("dijktraject", "hwbp_project", "hwbp_afgerond")}
    norm_sw, norm_og = np.full(n, np.nan), np.full(n, np.nan)
    worst = np.full(n, -1)
    start, end = np.full(n, np.nan), np.full(n, np.nan)
    cells_of = [[] for _ in rows]
    for i, c, ln in zip(pair_line, pair_cell, lengths):
        if ln <= 0:
            continue
        kind, p, _ = rows[i]
        k = index[c]
        cells_of[i].append(c)
        length[kind][k] += ln
        if kind == "dijktraject":
            for arr, v in ((norm_sw, p["norm_signalering_jaar"]), (norm_og, p["norm_ondergrens_jaar"])):
                if v:
                    arr[k] = np.fmax(arr[k], v)
            if p["lbo1_oordeel"] in CATEGORIES:
                worst[k] = max(worst[k], CATEGORIES.index(p["lbo1_oordeel"]))
        elif kind == "hwbp_project":
            if p["realisatie_start"]:
                start[k] = np.fmin(start[k], p["realisatie_start"])
            if p["realisatie_eind"]:
                end[k] = np.fmax(end[k], p["realisatie_eind"])

    def as_int(a):
        return pa.array([None if np.isnan(v) else int(v) for v in a], pa.int32())

    per_hex = pa.table({
        "h3_id": ids,
        "wk_primair_lengte_m": np.round(length["dijktraject"], 1),
        "wk_norm_signalering_jaar": as_int(norm_sw), "wk_norm_ondergrens_jaar": as_int(norm_og),
        "wk_lbo1_oordeel": pa.array([CATEGORIES[w] if w >= 0 else None for w in worst], pa.string()),
        "wk_hwbp_lengte_m": np.round(length["hwbp_project"], 1),
        "wk_hwbp_realisatie_start": as_int(start), "wk_hwbp_realisatie_eind": as_int(end),
        "wk_hwbp_afgerond_lengte_m": np.round(length["hwbp_afgerond"], 1)})
    source = {"source": "Rijkswaterstaat Waterveiligheidsportaal WFS: nbpw_dijktrajecten_formeel, "
                        "lbo1_veiligheidsoordeel_dijktrajecten, hwbp_actuele/afgesloten_projecten_dijkvakken (CC0)",
              "downloaded": dt.datetime.fromtimestamp((src / "nbpw_dijktrajecten_formeel.geojson").stat().st_mtime)
              .date().isoformat(),
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(per_hex.replace_schema_metadata({"datacube": json.dumps(source)}),
                   work / "layers/waterkeringen.parquet", compression="zstd")

    inside = [i for i in range(len(rows)) if cells_of[i]]
    geo = []
    for i in inside:
        g = rows[i][2]
        geo.append(shapely.transform(g, lambda xy: np.column_stack(to_wgs.transform(xy[:, 0], xy[:, 1]))))
    keys = ["traject_id", "naam", "beheerder", "lbo1_oordeel"]
    ints = ["norm_signalering_jaar", "norm_ondergrens_jaar", "realisatie_start", "realisatie_eind"]
    lines = pa.table({
        "soort": [rows[i][0] for i in inside],
        **{k: pa.array([rows[i][1][k] for i in inside], pa.string()) for k in keys},
        **{k: pa.array([rows[i][1][k] for i in inside], pa.int32()) for k in ints},
        "lengte_m": [round(rows[i][2].length, 1) for i in inside],
        "cellen": [sorted(cells_of[i]) for i in inside],
        "geometry": pa.array([shapely.to_wkb(g) for g in geo], pa.binary())})
    geo_meta = {"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": {
        "encoding": "WKB", "geometry_types": ["MultiLineString", "LineString"], "edges": "planar"}}}
    lines = lines.replace_schema_metadata({"geo": json.dumps(geo_meta), "datacube": json.dumps(source)})
    pq.write_table(lines, work / "layers/waterkeringen_lijnen.parquet", compression="zstd")
    log(f"dyke sections {length['dijktraject'].sum() / 1000:.0f} km, HWBP current "
        f"{length['hwbp_project'].sum() / 1000:.0f} km, completed {length['hwbp_afgerond'].sum() / 1000:.0f} km; "
        f"{(length['dijktraject'] > 0).sum()} hexes on a primary dyke")
    log(f"-> {work / 'layers/waterkeringen.parquet'}, {work / 'layers/waterkeringen_lijnen.parquet'} "
        f"({lines.num_rows} lines)")


if __name__ == "__main__":
    main()
