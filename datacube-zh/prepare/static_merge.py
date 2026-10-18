# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=17"]
# ///
"""Merge <work>/layers/static_partial.parquet (`datacube-zh static --only a,b`) into <work>/layers/static.parquet.

A full nl `datacube-zh static` run holds every layer at once and no longer fits in 12 GB, so new or refetched layers
are aggregated with --only and merged here: columns of the partial file replace the same-named columns in place, new
ones are appended. Rows must be the same hexes in the same order (both come from grid.parquet).

Usage: uv run datacube-zh/prepare/static_merge.py [--data own_data] [--region nl]
"""
import argparse
import sys
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    args = ap.parse_args()
    layers = Path(args.data) / regions.load(args.region)["work"] / "layers"
    full, part = pq.read_table(layers / "static.parquet"), pq.read_table(layers / "static_partial.parquet")
    if full["h3_id"] != part["h3_id"]:
        raise SystemExit("static.parquet and static_partial.parquet have different hexes or row order")
    replaced, added = [], []
    for name in part.column_names:
        if name == "h3_id":
            continue
        if name in full.column_names:
            full = full.set_column(full.column_names.index(name), name, part[name])
            replaced.append(name)
        else:
            full = full.append_column(name, part[name])
            added.append(name)
    pq.write_table(full.replace_schema_metadata(full.schema.metadata), layers / "static.parquet", compression="zstd")
    (layers / "static_partial.parquet").unlink()
    print(f"static.parquet: replaced {replaced or 'none'}, added {added or 'none'} ({full.num_columns} columns)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
