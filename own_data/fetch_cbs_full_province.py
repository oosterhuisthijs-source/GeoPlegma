#!/usr/bin/env python3
"""One-off fetch of CBS 100m vierkantstatistieken for the full dataset bbox,
one year, trimmed to (geometry, aantal_inwoners, aantal_woningen).

Tiles the bbox into an NxN grid and fetches tiles in parallel (each tile
paginates its own cursor sequentially -- PDOK's OGC API caps `limit` at 1000
and uses cursor-based pagination, so tiling is the only way to parallelize).

Output: newline-delimited JSON, one CBS square per line:
  {"geom": [[lon,lat], ...], "inw": <float|null>, "won": <float|null>}
"""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api.pdok.nl/cbs/vierkantstatistieken100m/ogc/v1/collections/vierkant100/items"
BBOX = (3.77448, 51.64233, 5.03357, 52.33427)  # lon_min, lat_min, lon_max, lat_max
JAARCODE = int(sys.argv[1]) if len(sys.argv) > 1 else 2022
GRID_N = 8  # 8x8 = 64 tiles
OUT_PATH = f"/home/thijsradijs/GeoPlegma/own_data/cbs_vk100_zuidholland_{JAARCODE}.ndjson"
SUPPRESSED = -99997


def fetch_json(url, retries=5):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


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

    def val(key):
        v = props.get(key)
        return None if v is None or v == SUPPRESSED else v

    return {"geom": coords, "inw": val("aantal_inwoners"), "won": val("aantal_woningen")}


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
    total_pages = 0
    done = 0
    with open(OUT_PATH, "w") as out, ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_tile, idx, bbox): idx for idx, bbox in tiles}
        for fut in as_completed(futures):
            idx, lines, pages = fut.result()
            out.write("\n".join(lines))
            if lines:
                out.write("\n")
            total_squares += len(lines)
            total_pages += pages
            done += 1
            elapsed = time.time() - start
            print(
                f"[{done}/{len(tiles)}] tile {idx}: {len(lines)} squares, {pages} pages "
                f"(total {total_squares} squares, {elapsed:.1f}s elapsed)",
                file=sys.stderr,
            )

    elapsed = time.time() - start
    print(
        f"\nDone: {total_squares} squares, {total_pages} pages, {elapsed:.1f}s wall time.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
