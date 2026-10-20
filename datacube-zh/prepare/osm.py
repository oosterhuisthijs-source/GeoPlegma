# /// script
# requires-python = ">=3.11"
# dependencies = ["osmium>=4", "shapely>=2", "pyproj>=3.6", "h3>=4", "pyarrow>=17", "numpy"]
# ///
"""OpenStreetMap main roads and railways -> a line file for the games + per-hex lengths for the cube.

Source: Geofabrik `netherlands-latest.osm.pbf` (downloaded by hand to <data>/sources/osm/, shared by
all regions; only the latest version is used, its timestamp is recorded). Ways with one of the tags
in KINDS are read in two passes (ways, then only their nodes), so no node cache for the whole country
is needed.

Writes, under <data>/<work>/layers/:

- osm_lijnen.parquet  one row per OSM way that crosses a hex of the region (GeoParquet, EPSG:4326):
                      whole ways, not clipped, so vehicles can move along them across cells.
                      Geometry runs in the direction of travel for one-way roads (oneway=-1 reversed).
                      `cellen` lists the res-9 cells of the region the way passes through.
- osm.parquet         per hex: length (m) per group (LENGTH_COLUMNS), 0 if none; tunnels and bridges
                      included; a dual carriageway counts twice, every track separately.

Lengths are exact: lines and hex boundaries (h3) are intersected in RD (EPSG:28992).

Usage: uv run datacube-zh/prepare/osm.py [--data own_data] [--region zh] [--pbf PATH]
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import h3
import numpy as np
import osmium
import osmium.filter
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

H3_RES = 9
SAMPLE_M = 10  # spacing of the points that find a line's candidate cells (+ their neighbours)

# (key, value) -> soort in the line file
KINDS = {
    ("highway", "motorway"): "snelweg", ("highway", "motorway_link"): "snelweg_afrit",
    ("highway", "trunk"): "autoweg", ("highway", "trunk_link"): "autoweg_afrit",
    ("highway", "primary"): "provinciaal", ("highway", "primary_link"): "provinciaal_afrit",
    ("highway", "secondary"): "regionaal", ("highway", "secondary_link"): "regionaal_afrit",
    ("highway", "tertiary"): "lokaal", ("highway", "tertiary_link"): "lokaal_afrit",
    ("railway", "rail"): "spoor", ("railway", "light_rail"): "lightrail",
    ("railway", "subway"): "metro", ("railway", "tram"): "tram",
}
# per-hex length columns -> soorten (spoor split on the `service` tag: yards, sidings, spurs)
LENGTH_COLUMNS = {
    "osm_snelweg_lengte_m": ["snelweg", "snelweg_afrit"],
    "osm_autoweg_lengte_m": ["autoweg", "autoweg_afrit"],
    "osm_provinciaal_lengte_m": ["provinciaal", "provinciaal_afrit"],
    "osm_regionaal_lengte_m": ["regionaal", "regionaal_afrit"],
    "osm_lokaal_lengte_m": ["lokaal", "lokaal_afrit"],
    "osm_spoor_lengte_m": ["spoor"],
    "osm_spoor_overig_lengte_m": ["spoor_dienst"],
    "osm_metro_tram_lengte_m": ["lightrail", "metro", "tram"],
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def to_int(v: str | None) -> int | None:
    m = re.match(r"\s*(-?\d+)", v or "")
    return int(m[1]) if m else None


def read_ways(pbf: Path) -> tuple[list[dict], str]:
    ways = []
    fp = (osmium.FileProcessor(str(pbf), osmium.osm.WAY)
          .with_filter(osmium.filter.TagFilter(*KINDS)))
    stamp = fp.header.get("osmosis_replication_timestamp", "")
    for w in fp:
        t = dict(w.tags)
        soort = next(s for (k, v), s in KINDS.items() if t.get(k) == v)
        ways.append({"id": w.id, "nodes": [n.ref for n in w.nodes], "soort": soort, "tags": t})
    return ways, stamp


def read_nodes(pbf: Path, ids: set[int]) -> dict[int, tuple[float, float]]:
    loc = {}
    fp = osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(ids))
    for n in fp:
        loc[n.id] = (n.location.lon, n.location.lat)
    return loc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--pbf", default=None, help="default <data>/sources/osm/netherlands-latest.osm.pbf")
    args = ap.parse_args()
    data = Path(args.data)
    reg = regions.load(args.region)
    work = data / reg["work"]
    pbf = Path(args.pbf) if args.pbf else data / "sources/osm/netherlands-latest.osm.pbf"

    t0 = time.time()
    ways, stamp = read_ways(pbf)
    log(f"{len(ways)} ways ({stamp}) in {time.time() - t0:.0f}s")
    t0 = time.time()
    loc = read_nodes(pbf, {n for w in ways for n in w["nodes"]})
    log(f"{len(loc)} nodes in {time.time() - t0:.0f}s")

    # geometries in lon/lat and RD; one-way roads in the direction of travel
    lonlat, keep = [], []
    for w in ways:
        pts = [loc[n] for n in w["nodes"] if n in loc]
        if len(pts) < 2:
            continue
        t = w["tags"]
        oneway = t.get("oneway")
        if oneway == "-1":
            pts = pts[::-1]
        w["eenrichting"] = (oneway in ("yes", "-1", "true", "1") or
                            (w["soort"].startswith("snelweg") and oneway != "no")) if "highway" in t else None
        if w["soort"] == "spoor" and t.get("service"):
            w["groep"] = "spoor_dienst"
        else:
            w["groep"] = w["soort"]
        lonlat.append(pts)
        keep.append(w)
    ways = keep
    to_rd = Transformer.from_crs(4326, 28992, always_xy=True)
    to_wgs = Transformer.from_crs(28992, 4326, always_xy=True)
    geo = [shapely.LineString(p) for p in lonlat]
    rd = [shapely.LineString(np.column_stack(to_rd.transform(*np.array(p).T))) for p in lonlat]

    grid = set(pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist())

    # candidate (way, cell) pairs: cells of points every SAMPLE_M along the line, plus their neighbours
    pair_way, pair_cell = [], []
    for i, line in enumerate(rd):
        d = np.append(np.arange(0, line.length, SAMPLE_M), line.length)
        xy = shapely.get_coordinates(shapely.line_interpolate_point(line, d))
        lon, lat = to_wgs.transform(xy[:, 0], xy[:, 1])
        cells = {h3.latlng_to_cell(la, lo, H3_RES) for lo, la in zip(lon, lat)}
        cand = {n for c in cells for n in h3.grid_disk(c, 1)} & grid
        pair_way += [i] * len(cand)
        pair_cell += sorted(cand)
    log(f"{len(pair_way)} candidate (way, cell) pairs")

    # exact length of each way inside each candidate hex (hex boundaries projected to RD)
    uniq = sorted(set(pair_cell))
    hexes = {}
    for c in uniq:
        lat, lon = np.array(h3.cell_to_boundary(c)).T
        hexes[c] = shapely.Polygon(np.column_stack(to_rd.transform(lon, lat)))
    lengths = shapely.length(shapely.intersection(np.array(rd, dtype=object)[pair_way],
                                                  np.array([hexes[c] for c in pair_cell], dtype=object)))
    hit = lengths > 0
    pw, pc, pl = np.array(pair_way)[hit], np.array(pair_cell)[hit], lengths[hit]

    cells_of = [[] for _ in ways]
    for i, c in zip(pw, pc):
        cells_of[i].append(c)
    inside = [i for i in range(len(ways)) if cells_of[i]]
    log(f"{len(inside)} of {len(ways)} ways cross the region")

    def tag(i, k):
        return ways[i]["tags"].get(k)

    lines = pa.table({
        "osm_id": pa.array([ways[i]["id"] for i in inside], pa.int64()),
        "soort": [ways[i]["soort"] for i in inside],
        "naam": [tag(i, "name") for i in inside],
        "ref": [tag(i, "ref") for i in inside],
        "rijstroken": pa.array([to_int(tag(i, "lanes")) for i in inside], pa.int32()),
        "maxsnelheid_kmh": pa.array([to_int(tag(i, "maxspeed")) for i in inside], pa.int32()),
        "eenrichting": pa.array([ways[i]["eenrichting"] for i in inside], pa.bool_()),
        "tunnel": [tag(i, "tunnel") not in (None, "no") for i in inside],
        "brug": [tag(i, "bridge") not in (None, "no") for i in inside],
        "laag": pa.array([to_int(tag(i, "layer")) or 0 for i in inside], pa.int32()),
        "geelektrificeerd": [tag(i, "electrified") for i in inside],
        "gebruik": [tag(i, "usage") for i in inside],
        "dienst": [tag(i, "service") for i in inside],
        "lengte_m": [round(rd[i].length, 1) for i in inside],
        "cellen": [sorted(cells_of[i]) for i in inside],
        "geometry": pa.array([shapely.to_wkb(geo[i]) for i in inside], pa.binary()),
    })
    geo_meta = {"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": {
        "encoding": "WKB", "geometry_types": ["LineString"], "edges": "planar"}}}  # no crs key = OGC:CRS84
    source = {"source": "OpenStreetMap (Geofabrik netherlands-latest.osm.pbf), © OpenStreetMap contributors, ODbL",
              "osm_timestamp": stamp, "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    lines = lines.replace_schema_metadata({"geo": json.dumps(geo_meta), "datacube": json.dumps(source)})
    pq.write_table(lines, work / "layers/osm_lijnen.parquet", compression="zstd")

    # per-hex lengths
    ids = sorted(grid)
    index = {c: k for k, c in enumerate(ids)}
    cols = {name: np.zeros(len(ids)) for name in LENGTH_COLUMNS}
    group_col = {g: name for name, gs in LENGTH_COLUMNS.items() for g in gs}
    for i, c, length in zip(pw, pc, pl):
        cols[group_col[ways[i]["groep"]]][index[c]] += length
    per_hex = pa.table({"h3_id": ids, **{k: pa.array(np.round(v, 1)) for k, v in cols.items()}})
    per_hex = per_hex.replace_schema_metadata({"datacube": json.dumps(source)})
    pq.write_table(per_hex, work / "layers/osm.parquet", compression="zstd")
    log(", ".join(f"{k} {v.sum() / 1000:.0f} km" for k, v in cols.items()))
    log(f"-> {work / 'layers/osm_lijnen.parquet'} ({lines.num_rows} ways), {work / 'layers/osm.parquet'}")


if __name__ == "__main__":
    main()
