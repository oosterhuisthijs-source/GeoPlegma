#!/usr/bin/env python3
"""NDW public charging points -> per-hex number of charge points and fast chargers.

Source: NDW open data `charging_point_locations.geojson.gz` (https://opendata.ndw.nu/), the national
register of public charging locations (OCPI feeds of the operators, updated daily). One point per
location with `availabilities` (number of charge points per power type and maximum power). The file
is a snapshot, so the result is static: the same values in every cube year. OSM has only ~25% of
these points, which is why this layer does not come from OSM.

Downloaded once to <data>/sources/ndw/ (shared by all regions; delete it or pass --refresh for a
newer snapshot). Per hex (H3 res 9 of the location point):
  laad_punten_aantal       public charge points
  laad_punten_snel_aantal  of which fast chargers (>= 50 kW)

Writes <data>/<work>/layers/laadpalen.parquet. Needs numpy, pyarrow and h3 (the system python3 here).

Usage: datacube-zh/prepare/laadpalen.py [--data own_data] [--region zh] [--refresh]
"""
import argparse
import email.utils
import gzip
import json
import sys
import urllib.request
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

URL = "https://opendata.ndw.nu/charging_point_locations.geojson.gz"
FAST_W = 50_000


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(data: Path, refresh: bool) -> tuple[Path, str]:
    """Local copy of the NDW file and its snapshot date (server Last-Modified, kept in a sidecar)."""
    path, stamp_file = data / "sources/ndw/charging_point_locations.geojson.gz", data / "sources/ndw/last_modified.txt"
    if refresh or not path.exists() or not stamp_file.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=300) as r:
            path.write_bytes(r.read())
            stamp = email.utils.parsedate_to_datetime(r.headers["Last-Modified"]).isoformat()
        stamp_file.write_text(stamp)
        log(f"downloaded {URL} ({stamp})")
    return path, stamp_file.read_text().strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--refresh", action="store_true", help="download a new snapshot")
    args = ap.parse_args()
    data = Path(args.data)
    work = data / regions.load(args.region)["work"]
    path, stamp = fetch(data, args.refresh)

    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    index = {c: k for k, c in enumerate(ids)}
    points, fast = np.zeros(len(ids), np.int32), np.zeros(len(ids), np.int32)
    features = json.load(gzip.open(path))["features"]
    for f in features:
        lon, lat = f["geometry"]["coordinates"][:2]
        k = index.get(h3.latlng_to_cell(lat, lon, 9))
        if k is None:
            continue
        for a in f["properties"].get("availabilities") or []:
            n = a.get("total") or 0
            points[k] += n
            fast[k] += n if (a.get("power_max") or 0) >= FAST_W else 0

    source = {"source": f"NDW open data, {URL}", "snapshot": stamp}
    table = pa.table({"h3_id": ids, "laad_punten_aantal": points, "laad_punten_snel_aantal": fast})
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}),
                   work / "layers/laadpalen.parquet", compression="zstd")
    log(f"{len(features)} locations in NL; region: {points.sum()} charge points, {fast.sum()} fast (snapshot {stamp})")
    log(f"-> {work / 'layers/laadpalen.parquet'}")


if __name__ == "__main__":
    main()
