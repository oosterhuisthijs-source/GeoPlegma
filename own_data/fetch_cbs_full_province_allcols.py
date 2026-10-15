#!/usr/bin/env python3
"""Like fetch_cbs_full_province.py, but keeps every numeric CBS property
(135 columns) instead of just aantal_inwoners/aantal_woningen -- used to
benchmark running the geoplegma+geo overlay across all columns at once
instead of two.

Output:
  own_data/cbs_vk100_zuidholland_<year>_allcols.ndjson
    one line per square: {"geom": [[lon,lat], ...], "v": [<f64|null>, ...]}
    (v is positional, in the same order as the sidecar column list)
  own_data/cbs_vk100_zuidholland_<year>_allcols.columns.json
    the ordered list of 135 column names "v" indexes into
"""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api.pdok.nl/cbs/vierkantstatistieken100m/ogc/v1/collections/vierkant100/items"
BBOX = (3.77448, 51.64233, 5.03357, 52.33427)
JAARCODE = int(sys.argv[1]) if len(sys.argv) > 1 else 2022
GRID_N = 8
OUT_DIR = "/home/thijsradijs/GeoPlegma/own_data"
OUT_PATH = f"{OUT_DIR}/cbs_vk100_zuidholland_{JAARCODE}_allcols.ndjson"
COLUMNS_PATH = f"{OUT_DIR}/cbs_vk100_zuidholland_{JAARCODE}_allcols.columns.json"
SUPPRESSED = -99997

# Excluded: non-numeric identifiers/dates, and 4 fields PDOK returns as
# strings (including the literal string "-99997") rather than numbers.
EXCLUDE = {
    "jaarcode", "crs28992res100m", "einddatum", "startdatum",
    "cat_percentage_nederlandse_achtergrond",
    "cat_percentage_niet_westerse_migr_achtergr",
    "cat_percentage_westerse_migr_achtergr",
    "mediaan_inkomen_huishouden",
}

COLUMNS = None  # discovered from the first feature, then fixed for the whole run


def fetch_json(url, retries=5):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def discover_columns():
    # A known-populated bbox (central Den Haag), not an arbitrary corner of
    # the province bbox which may land in open water with zero squares.
    url = f"{BASE}?bbox=4.28,52.07,4.32,52.10&limit=1&jaarcode={JAARCODE}&f=json"
    data = fetch_json(url)
    props = data["features"][0]["properties"]
    cols = sorted(
        k for k, v in props.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in EXCLUDE
    )
    return cols


def trim(feature):
    geom = feature.get("geometry")
    if geom is None:
        return None
    if geom["type"] == "MultiPolygon":
        coords = geom["coordinates"][0][0]
    elif geom["type"] == "Polygon":
        coords = geom["coordinates"][0]
    else:
        return None
    props = feature.get("properties", {})
    values = []
    for c in COLUMNS:
        v = props.get(c)
        values.append(None if v is None or v == SUPPRESSED else v)
    return {"geom": coords, "v": values}


def fetch_tile(tile_idx, bbox):
    lon_min, lat_min, lon_max, lat_max = bbox
    url = (
        f"{BASE}?bbox={lon_min},{lat_min},{lon_max},{lat_max}"
        f"&limit=1000&jaarcode={JAARCODE}&f=json"
    )
    lines = []
    pages = 0
    while url:
        data = fetch_json(url)
        for f in data.get("features", []):
            t = trim(f)
            if t is not None:
                lines.append(json.dumps(t))
        pages += 1
        url = next((l["href"] for l in data.get("links", []) if l["rel"] == "next"), None)
    return tile_idx, lines, pages


def main():
    global COLUMNS
    COLUMNS = discover_columns()
    with open(COLUMNS_PATH, "w") as f:
        json.dump(COLUMNS, f)
    print(f"{len(COLUMNS)} numeric columns -> {COLUMNS_PATH}", file=sys.stderr)

    lon_min, lat_min, lon_max, lat_max = BBOX
    lon_step = (lon_max - lon_min) / GRID_N
    lat_step = (lat_max - lat_min) / GRID_N

    tiles = []
    for i in range(GRID_N):
        for j in range(GRID_N):
            tiles.append(
                (
                    i * GRID_N + j,
                    (
                        lon_min + i * lon_step,
                        lat_min + j * lat_step,
                        lon_min + (i + 1) * lon_step,
                        lat_min + (j + 1) * lat_step,
                    ),
                )
            )

    start = time.time()
    total_squares = 0
    done = 0
    with open(OUT_PATH, "w") as out, ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_tile, idx, bbox): idx for idx, bbox in tiles}
        for fut in as_completed(futures):
            idx, lines, pages = fut.result()
            out.write("\n".join(lines))
            if lines:
                out.write("\n")
            total_squares += len(lines)
            done += 1
            elapsed = time.time() - start
            print(
                f"[{done}/{len(tiles)}] tile {idx}: {len(lines)} squares, {pages} pages "
                f"(total {total_squares}, {elapsed:.1f}s elapsed)",
                file=sys.stderr,
            )

    elapsed = time.time() - start
    print(f"\nDone: {total_squares} squares, {elapsed:.1f}s wall time -> {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
