# /// script
# requires-python = ">=3.11"
# dependencies = ["osmium>=4", "shapely>=2", "pyproj>=3.6", "h3>=4.2", "pyarrow>=17", "numpy"]
# ///
"""OpenStreetMap high-voltage grid, solar parks and marinas -> per-hex columns for the cube.

Same source as prepare/osm.py (Geofabrik `netherlands-latest.osm.pbf`, latest version only, its
timestamp is recorded). Read in passes (multipolygon relations, ways, then only the nodes those need),
so no node cache for the whole country is needed.

What is read, and why only this:
- power=line / power=cable with a voltage >= 50 kV: the TenneT and regional grid (380/220/150/110/50 kV),
  well mapped. Lower voltages are mostly missing in OSM.
- power=substation with a voltage >= 50 kV or substation=transmission (~780 in NL). The ~100k
  neighbourhood transformers are mostly missing (OSM has ~2,400), so they are left out.
- power=plant + plant:source=solar (areas) with plant:output:electricity (MW/MWp). About half the parks
  have no power tag; they get their area x the median kW/ha of the tagged parks.
- leisure=marina (nodes and areas).

Writes <data>/<work>/layers/osm_extra.parquet, per hex:
  osm_hoogspanning_lengte_m         overhead line >= 50 kV inside the cell (m; each way once)
  osm_hoogspanning_kabel_lengte_m   underground/underwater cable >= 50 kV inside the cell (m)
  osm_hoogspanning_afstand_min_km   cell centre to the nearest overhead line >= 50 kV (all of NL, km)
  osm_onderstation_aantal           substations >= 50 kV whose centre lies in the cell
  osm_zonnepark_vermogen_kw         rated power of solar parks, split over cells by area share
  osm_jachthaven_aantal             marinas whose centre lies in the cell

Lengths and areas are exact (geometries and hex boundaries intersected in RD, EPSG:28992).

Usage: uv run datacube-zh/prepare/osm_extra.py [--data own_data] [--region zh] [--pbf PATH]
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
MIN_KV = 50
TAGS = [("power", "line"), ("power", "cable"), ("power", "substation"), ("power", "plant"), ("leisure", "marina")]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def max_kv(v: str | None) -> int | None:
    """'380000;150000' -> 380"""
    kv = [int(x) // 1000 for x in re.findall(r"\d+", v or "")]
    return max(kv) if kv else None


def output_kw(v: str | None) -> float | None:
    """'12.5 MWp', '9 MW', '500 kWp' -> kW; 'yes' -> None"""
    m = re.match(r"\s*([\d.,]+)\s*([kMG]?)W", v or "")
    if not m:
        return None
    return float(m[1].replace(",", ".")) * {"": 1e-3, "k": 1, "M": 1e3, "G": 1e6}[m[2]]


def kind_of(t: dict) -> str | None:
    p, kv = t.get("power"), max_kv(t.get("voltage"))
    if p in ("line", "cable"):
        if not kv or kv < MIN_KV:
            return None
        return "kabel" if p == "cable" or t.get("location") in ("underground", "underwater") else "lijn"
    if p == "substation":
        return "onderstation" if (kv and kv >= MIN_KV) or t.get("substation") == "transmission" else None
    if p == "plant":
        return "zonnepark" if t.get("plant:source") == "solar" else None
    return "jachthaven" if t.get("leisure") == "marina" else None


def read(pbf: Path):
    """Features (kind, tags, node ids or relation parts) and the locations of the nodes they use."""
    feats, rel_ways = [], {}
    for r in osmium.FileProcessor(str(pbf), osmium.osm.RELATION).with_filter(osmium.filter.TagFilter(*TAGS)):
        t = dict(r.tags)
        kind = kind_of(t)
        if kind and t.get("type") == "multipolygon":
            members = [(m.ref, m.role) for m in r.members if m.type == "w"]
            feats.append({"kind": kind, "type": "r", "tags": t, "members": members})
            rel_ways.update({ref: None for ref, _ in members})
    for w in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.TagFilter(*TAGS)):
        t = dict(w.tags)
        kind = kind_of(t)
        if kind:
            feats.append({"kind": kind, "type": "w", "tags": t, "nodes": [n.ref for n in w.nodes]})
    if rel_ways:
        for w in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(rel_ways.keys())):
            rel_ways[w.id] = [n.ref for n in w.nodes]
    for f in feats:
        if f["type"] == "r":
            f["parts"] = [(rel_ways.get(ref) or [], role) for ref, role in f.pop("members")]
    need = {n for f in feats for n in f.get("nodes", [])} | {n for f in feats for p, _ in f.get("parts", []) for n in p}
    loc = {}
    fp = osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(need))
    for n in fp:
        loc[n.id] = (n.location.lon, n.location.lat)
    for n in osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(
            osmium.filter.TagFilter(("power", "substation"), ("leisure", "marina"))):
        kind = kind_of(dict(n.tags))
        if kind:
            feats.append({"kind": kind, "type": "n", "tags": dict(n.tags), "point": (n.location.lon, n.location.lat)})
    return feats, loc, fp.header.get("osmosis_replication_timestamp", "")


def geometry(f: dict, loc: dict):
    """lon/lat shapely geometry: Point, LineString (lines/cables) or (Multi)Polygon (areas)."""
    if "point" in f:
        return shapely.Point(f["point"])
    if f["type"] == "w":
        pts = [loc[n] for n in f["nodes"] if n in loc]
        if f["kind"] in ("lijn", "kabel"):
            return shapely.LineString(pts) if len(pts) >= 2 else None
        if len(pts) >= 4 and f["nodes"][0] == f["nodes"][-1]:
            return shapely.make_valid(shapely.Polygon(pts))
        return shapely.Point(pts[0]) if pts and f["kind"] != "zonnepark" else None  # unclosed marina outline
    rings = {"outer": [], "inner": []}
    for nodes, role in f["parts"]:
        pts = [loc[n] for n in nodes if n in loc]
        if len(pts) >= 2:
            rings["inner" if role == "inner" else "outer"].append(shapely.LineString(pts))
    if not rings["outer"]:
        return None
    area = shapely.union_all(list(shapely.polygonize(rings["outer"]).geoms))
    if rings["inner"]:
        area = area.difference(shapely.union_all(list(shapely.polygonize(rings["inner"]).geoms)))
    return None if area.is_empty else shapely.make_valid(area)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--pbf", default=None, help="default <data>/sources/osm/netherlands-latest.osm.pbf")
    args = ap.parse_args()
    data = Path(args.data)
    work = data / regions.load(args.region)["work"]
    pbf = Path(args.pbf) if args.pbf else data / "sources/osm/netherlands-latest.osm.pbf"

    t0 = time.time()
    feats, loc, stamp = read(pbf)
    to_rd = Transformer.from_crs(4326, 28992, always_xy=True)
    to_wgs = Transformer.from_crs(28992, 4326, always_xy=True)
    by_kind = {}
    for f in feats:
        g = geometry(f, loc)
        if g is not None and not g.is_empty:
            f["geo"], f["rd"] = g, shapely.transform(g, lambda xy: np.column_stack(to_rd.transform(xy[:, 0], xy[:, 1])))
            by_kind.setdefault(f["kind"], []).append(f)
    log(f"{stamp}: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(by_kind.items())) + f" in {time.time() - t0:.0f}s")

    grid = pq.read_table(work / "grid.parquet", columns=["h3_id", "lat", "lon"])
    ids = grid["h3_id"].to_pylist()
    index = {c: k for k, c in enumerate(ids)}
    hex_rd = {}

    def hexagon(c):
        if c not in hex_rd:
            lat, lon = np.array(h3.cell_to_boundary(c)).T
            hex_rd[c] = shapely.Polygon(np.column_stack(to_rd.transform(lon, lat)))
        return hex_rd[c]

    def line_cells(line_rd):
        d = np.append(np.arange(0, line_rd.length, SAMPLE_M), line_rd.length)
        xy = shapely.get_coordinates(shapely.line_interpolate_point(line_rd, d))
        lon, lat = to_wgs.transform(xy[:, 0], xy[:, 1])
        cells = {h3.latlng_to_cell(la, lo, H3_RES) for lo, la in zip(lon, lat)}
        return sorted({n for c in cells for n in h3.grid_disk(c, 1)} & index.keys())

    def area_cells(poly):
        cells = set()
        for p in getattr(poly, "geoms", [poly]):
            if isinstance(p, shapely.Polygon):
                cells |= set(h3.h3shape_to_cells_experimental(h3.geo_to_h3shape(p), H3_RES, contain="overlap"))
        return cells & index.keys()

    def centre_cell(f):
        p = f["geo"] if f["geo"].geom_type == "Point" else f["geo"].representative_point()
        return index.get(h3.latlng_to_cell(p.y, p.x, H3_RES))

    cols = {k: np.zeros(len(ids)) for k in ("osm_hoogspanning_lengte_m", "osm_hoogspanning_kabel_lengte_m",
                                            "osm_onderstation_aantal", "osm_zonnepark_vermogen_kw", "osm_jachthaven_aantal")}

    # lines and cables: exact length per cell
    for kind, col in (("lijn", "osm_hoogspanning_lengte_m"), ("kabel", "osm_hoogspanning_kabel_lengte_m")):
        for f in by_kind.get(kind, []):
            cand = line_cells(f["rd"])
            if cand:
                for c, ln in zip(cand, shapely.length(shapely.intersection(f["rd"], [hexagon(c) for c in cand]))):
                    cols[col][index[c]] += ln

    # solar parks: power split by area share; untagged parks get area x median kW/ha of the tagged ones
    parks = [f for f in by_kind.get("zonnepark", []) if f["rd"].area > 0]
    for f in parks:
        f["kw"] = output_kw(f["tags"].get("plant:output:electricity"))
    density = np.median([f["kw"] / f["rd"].area for f in parks if f["kw"] and 0.01 <= f["kw"] / f["rd"].area <= 0.3])
    estimated = 0.0
    for f in parks:
        for c in area_cells(f["geo"]):
            a = shapely.intersection(f["rd"], hexagon(c)).area
            if a > 0:
                kw = f["kw"] * a / f["rd"].area if f["kw"] else density * a
                cols["osm_zonnepark_vermogen_kw"][index[c]] += kw
                estimated += 0 if f["kw"] else kw

    for kind, col in (("onderstation", "osm_onderstation_aantal"), ("jachthaven", "osm_jachthaven_aantal")):
        for f in by_kind.get(kind, []):
            k = centre_cell(f)
            if k is not None:
                cols[col][k] += 1

    # distance from cell centres (RD) to the nearest overhead line in all of NL
    cx, cy = to_rd.transform(grid["lon"].to_numpy(), grid["lat"].to_numpy())
    _, dist = shapely.STRtree([f["rd"] for f in by_kind["lijn"]]).query_nearest(
        shapely.points(cx, cy), return_distance=True, all_matches=False)

    source = {"source": "OpenStreetMap (Geofabrik netherlands-latest.osm.pbf), © OpenStreetMap contributors, ODbL",
              "osm_timestamp": stamp, "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
              "zonnepark_kw_per_ha_mediaan": round(density * 1e4)}
    per_hex = pa.table({
        "h3_id": ids,
        "osm_hoogspanning_lengte_m": np.round(cols["osm_hoogspanning_lengte_m"], 1),
        "osm_hoogspanning_kabel_lengte_m": np.round(cols["osm_hoogspanning_kabel_lengte_m"], 1),
        "osm_hoogspanning_afstand_min_km": np.round(dist / 1000, 3),
        "osm_onderstation_aantal": pa.array(cols["osm_onderstation_aantal"].astype(np.int32)),
        "osm_zonnepark_vermogen_kw": np.round(cols["osm_zonnepark_vermogen_kw"], 1),
        "osm_jachthaven_aantal": pa.array(cols["osm_jachthaven_aantal"].astype(np.int32)),
    }).replace_schema_metadata({"datacube": json.dumps(source)})
    pq.write_table(per_hex, work / "layers/osm_extra.parquet", compression="zstd")

    log(f"overhead {cols['osm_hoogspanning_lengte_m'].sum() / 1000:.0f} km, cable "
        f"{cols['osm_hoogspanning_kabel_lengte_m'].sum() / 1000:.0f} km, substations "
        f"{int(cols['osm_onderstation_aantal'].sum())}, marinas {int(cols['osm_jachthaven_aantal'].sum())}")
    log(f"solar parks {cols['osm_zonnepark_vermogen_kw'].sum() / 1000:.0f} MW ({estimated / 1000:.0f} MW estimated at "
        f"{density * 1e4:.0f} kW/ha; {sum(1 for f in parks if f['kw'])} of {len(parks)} parks in NL have a power tag)")
    log(f"-> {work / 'layers/osm_extra.parquet'}")


if __name__ == "__main__":
    main()
