# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=17", "deltalake>=0.22"]
# ///
"""Step 6: join the per-layer outputs into the final datacube.

Writes two Delta tables under <data>/out/:

- datacube_zh/                 real values, one row per (h3_id, year_int),
                               every rebuilt column (the actual product)
- volledige_tijdreeks_delta/   drop-in replacement for ruimtelijke-assistent:
                               the original's exact columns, order and types,
                               CBS columns min-max scaled to 0-255 over all years
                               combined (as the original was), columns this
                               pipeline doesn't produce yet left null

Usage: uv run datacube-zh/assemble.py [--data own_data] [--original PATH]
"""
import argparse
import glob
import shutil
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from deltalake import write_deltalake

YEARS = range(2018, 2024)
DEFAULT_ORIGINAL = "/home/thijsradijs/ruimtelijke-assistent/src/backend/data/volledige_tijdreeks_delta"
CBS_SUFFIXES = ("_sum", "_area_weighted_average", "_min", "_max")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def left_join(left: pa.Table, right: pa.Table, keys) -> pa.Table:
    return left.join(right, keys=keys, join_type="left outer", coalesce_keys=True)


def build_real(work: Path) -> pa.Table:
    grid = pq.read_table(work / "grid.parquet")
    admin = pq.read_table(work / "layers/admin.parquet")
    static = pq.read_table(work / "layers/static.parquet")
    base = left_join(left_join(grid, admin, "h3_id"), static, "h3_id")

    years = []
    for y in YEARS:
        cbs = pq.read_table(work / f"layers/cbs_{y}.parquet")
        years.append(cbs)
    # Columns can differ per year only by nulls (same config), but be safe.
    cbs_all = pa.concat_tables(years, promote_options="default")

    cube = left_join(cbs_all, base, "h3_id")
    # Leading key columns first, then everything else in layer order.
    lead = ["h3_id", "year_int"]
    cube = cube.select(lead + [c for c in cube.column_names if c not in lead])
    return cube.sort_by([("year_int", "ascending"), ("h3_id", "ascending")])


def scale_0_255(col: pa.ChunkedArray) -> pa.ChunkedArray:
    mm = pc.min_max(col)
    lo, hi = mm["min"].as_py(), mm["max"].as_py()
    if lo is None or hi is None or hi == lo:
        return pc.cast(pc.multiply(pc.is_valid(col), 0), pa.int16())
    scaled = pc.round(pc.multiply(pc.divide(pc.subtract(col, lo), hi - lo), 255.0))
    return pc.cast(scaled, pa.int16())


def build_compat(real: pa.Table, original_schema: pa.Schema) -> pa.Table:
    n = real.num_rows
    year = real["year_int"]
    derived = {
        "year": pc.cast(year, pa.float64()),
        "year_label": pc.cast(year, pa.float64()),
        "year_date": pa.chunked_array([pa.array([f"{y}-01-01" for y in year.to_pylist()])]),
        "year_ms": pa.chunked_array(
            [pa.array([float(pa.scalar(f"{y}-01-01").cast(pa.timestamp("ms")).value) for y in year.to_pylist()])]
        ),
        # The original's static `aantal_inwoners` was a real (unscaled) count
        # matching CBS ~2020/2021; here it is the real CBS sum of that row's year.
        "aantal_inwoners": real["aantal_inwoners_sum"],
        # Stood for "has Atlas Natuurlijk Kapitaal data"; approximated by
        # whether the ANK heat-island layer has a value for the hex.
        "heeft_atlas_data": pc.is_valid(real["hitte"]),
    }
    cols, missing = [], []
    for field in original_schema:
        name = field.name
        if name in derived:
            arr = derived[name]
        elif name in real.column_names and name.endswith(CBS_SUFFIXES):
            arr = scale_0_255(pc.cast(real[name], pa.float64()))
        elif name == "natura2000_id":
            arr = pc.cast(real["natura2000_id"], pa.int16())
        elif name in real.column_names:
            arr = real[name]
        else:
            arr = pa.nulls(n, field.type)
            missing.append(name)
        cols.append(pc.cast(arr, field.type))
    log(f"compat: {len(missing)} original columns not produced (null): {', '.join(missing)}")
    return pa.Table.from_arrays(cols, schema=pa.schema([f.remove_metadata() for f in original_schema]))


def write_delta(table: pa.Table, path: Path):
    if path.exists():
        shutil.rmtree(path)
    write_deltalake(str(path), table, mode="overwrite")
    log(f"wrote {path} ({table.num_rows} rows x {table.num_columns} cols)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--original", default=DEFAULT_ORIGINAL, help="original Delta table (for the compat schema)")
    args = ap.parse_args()
    work = Path(args.data) / "work"
    out = Path(args.data) / "out"
    out.mkdir(parents=True, exist_ok=True)

    real = build_real(work)
    write_delta(real, out / "datacube_zh")
    pq.write_table(real, out / "datacube_zh.parquet", compression="zstd")

    original_schema = pq.read_schema(glob.glob(f"{args.original}/*.parquet")[0])
    compat = build_compat(real, original_schema)
    write_delta(compat, out / "volledige_tijdreeks_delta")


if __name__ == "__main__":
    main()
