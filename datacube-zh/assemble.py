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
import json
import shutil
import sys
import tomllib
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


LGN_EXPORT = "sources/lgn/lgn_parquet_files/lgn{year}_pzh_vectorized_consumption"
LGN_YEARS = range(2018, 2023)  # 2023 is carried forward from 2022
# In the 2022 export these two classes collapse (6.2% -> 0.2% and 1.8% ->
# 0.06% of the province vs 2021): LGN2022 codes that the Databricks
# config did not map. Kept, but flagged in the manifest.
LGN_SUSPECT_2022 = ["natuurlijk_beheerde_agrarische_graslanden_fraction", "hoofdinfrastructuur_en_spoorbaanlichamen_fraction"]


def load_lgn(data: Path) -> pa.Table | None:
    """STOPGAP: LGN land-use fractions from the Databricks consumption export
    (already on H3 res 9), until the raw LGN rasters are available again.

    Known issues (see README): ~3,000 open-water hexes missing; polygons
    dropped inside hexes (`lgn_coverage` < 1); 2022 code-mapping bug; no 2023
    (2023 = copy of 2022, `lgn_source_year` = 2022).
    """
    tables = []
    for y in LGN_YEARS:
        files = glob.glob(str(data / LGN_EXPORT.format(year=y) / "*.parquet"))
        if not files:
            log(f"lgn: no export for {y}, skipped")
            continue
        t = pq.read_table(files[0])
        t = t.set_column(0, "h3_id", pc.utf8_lower(t["h3_id"]))
        tables.append((y, t))
    if not tables:
        return None
    last_year, last = tables[-1]
    tables.append((2023, last))

    classes = sorted({c for _, t in tables for c in t.column_names if c.endswith("_fraction")})
    out = []
    for year, t in tables:
        n = t.num_rows
        cols = {"h3_id": t["h3_id"], "year_int": pa.array([year] * n, pa.int64())}
        for c in classes:
            cols[c] = t[c] if c in t.column_names else pa.nulls(n, pa.float64())
        present = [t[c] for c in classes if c in t.column_names]
        total = present[0]
        for col in present[1:]:
            total = pc.add(total, col)
        cols["lgn_coverage"] = total
        # Dominant class, written like the original ("zout water").
        names = [c.removesuffix("_fraction").replace("_", " ") for c in classes if c in t.column_names]
        rows = list(zip(*[t[c].to_pylist() for c in classes if c in t.column_names]))
        cols["landgebruiksklasse"] = pa.array(
            [names[max(range(len(r)), key=lambda i: r[i] or 0)] if any(r) else None for r in rows]
        )
        cols["lgn_source_year"] = pa.array([min(year, last_year)] * n, pa.int64())
        out.append(pa.table(cols))
    lgn = pa.concat_tables(out)
    log(f"lgn (stopgap): {lgn.num_rows} rows, {len(classes)} classes, years {[y for y, _ in tables]}")
    return lgn


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
    lgn = load_lgn(work.parent)
    if lgn is not None:
        cube = left_join(cube, lgn, ["h3_id", "year_int"])
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
        elif name in real.column_names and name.endswith(CBS_SUFFIXES + ("_fraction",)):
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


def build_manifest(real: pa.Table, config_dir: Path) -> dict:
    """Per-column source/aggregation/coverage, for downstream docs (e.g. the
    SimCityZH explanation page)."""
    cbs = tomllib.loads((config_dir / "cbs.toml").read_text())
    static = tomllib.loads((config_dir / "static.toml").read_text())
    cbs_src = "CBS Kaart van 100m x 100m met statistieken (vierkantstatistieken), per jaar"
    cbs_aggs = {
        "sum": "som, verdeeld naar oppervlakteaandeel van het CBS-vierkant (overlap / vierkantoppervlak)",
        "area_weighted_average": "oppervlaktegewogen gemiddelde over de vierkanten met waarde",
        "min": "minimum over overlappende vierkanten",
        "max": "maximum over overlappende vierkanten",
    }
    info = {
        "h3_id": ("H3 res 9 cel (geoplegma)", "sleutel", None),
        "year_int": ("jaar", "sleutel", None),
        "lat": ("H3 celcentrum", "graden", None),
        "lon": ("H3 celcentrum", "graden", None),
        "hex_area_m2": ("H3 celoppervlak (geodetisch)", "m2", None),
        "province_fraction": ("aandeel van de cel binnen de PDOK provinciegrens Zuid-Holland", "0-1", None),
        "code": ("provinciecode (PDOK bestuurlijke gebieden)", None, None),
        "naam": ("provincienaam (PDOK bestuurlijke gebieden)", None, None),
        "cbs_coverage": (cbs_src, "0-1", "aandeel van de cel bedekt door CBS-vierkanten"),
        "mediaan_inkomen_huishouden_mode": (cbs_src, "klasse", "oppervlaktegewogen meest voorkomende inkomensklasse"),
        "lgn_coverage": ("LGN (stopgap: Databricks export)", "0-1", "som van alle _fraction kolommen; < 1 = data ontbreekt in de export"),
        "landgebruiksklasse": ("LGN (stopgap: Databricks export)", "klasse", "dominante LGN-klasse"),
        "lgn_source_year": ("LGN (stopgap: Databricks export)", "jaar", "LGN-jaar waar de waarden vandaan komen (2023 = kopie van 2022)"),
    }
    for col in ["buurtcode", "buurtnaam", "wijkcode", "wijknaam", "gemeentecode", "gemeentenaam"]:
        info[col] = ("CBS Wijk- en Buurtkaart 2024 v2", None, "buurt met de grootste overlap; wijk/gemeente afgeleid van die buurt")
    for agg, cols in cbs["aggregation"].items():
        for c in cols:
            info[f"{c}_{agg}"] = (cbs_src, None, cbs_aggs.get(agg, agg))
    for layer in static.get("raster", []):
        src = layer.get("description", "")
        info[layer["name"]] = (f"{src} [{layer['wcs']} {layer['coverage']}]", None,
                               f"{layer['agg']} over alle pixels in de cel (pixelcentrum -> H3 via geoplegma zone_from_point)")
    for layer in static.get("vector", []):
        src = layer.get("description", "")
        how = "true als een polygoon de cel raakt" if layer["agg"] == "any" else "waarde van de polygoon met de grootste overlap"
        info[layer["name"]] = (src, None, how)
        if layer.get("label"):
            info[layer["label"]] = (src, None, how)

    years = real["year_int"]
    manifest = {}
    for name in real.column_names:
        col = real[name]
        src, unit, agg = info.get(name, (None, None, None))
        if name.endswith("_fraction") and name != "province_fraction":
            src, unit, agg = ("LGN Landelijk Grondgebruik Nederland (STOPGAP: Databricks consumption export, H3 res 9)",
                              "0-1", "aandeel van de cel met deze landgebruiksklasse")
        valid = pc.is_valid(col)
        per_year = {}
        for y in sorted(set(years.to_pylist())):
            m = pc.equal(years, y)
            per_year[y] = round(pc.sum(pc.and_(valid, m)).as_py() / max(pc.sum(m).as_py(), 1), 4)
        entry = {"source": src, "unit": unit, "aggregation": agg, "type": str(col.type), "non_null_share_per_year": per_year}
        if name in LGN_SUSPECT_2022:
            entry["warning"] = "2022 (en 2023) waarden te laag: LGN2022-codes niet gemapt in de Databricks-export"
        manifest[name] = entry
    return manifest


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
    manifest = build_manifest(real, Path(__file__).parent / "config")
    (out / "datacube_zh.columns.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    log(f"wrote {out / 'datacube_zh.columns.json'} ({len(manifest)} columns)")

    original_schema = pq.read_schema(glob.glob(f"{args.original}/*.parquet")[0])
    compat = build_compat(real, original_schema)
    write_delta(compat, out / "volledige_tijdreeks_delta")


if __name__ == "__main__":
    main()
