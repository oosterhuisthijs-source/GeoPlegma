# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=17", "deltalake>=0.22"]
# ///
"""Step 6: join the per-layer outputs into the final datacube.

Writes, under the region's out directory (config/regions.toml):

- datacube_<region>/           Delta table partitioned by year_int: real
                               values, one row per (h3_id, year_int), every
                               rebuilt column (the actual product). Built one
                               year at a time, so the national table never
                               has to fit in memory.
- datacube_<region>.columns.json   per-column manifest
- volledige_tijdreeks_delta/   (zh only) drop-in replacement for ruimtelijke-assistent:
                               the original's exact columns, order and types,
                               CBS columns min-max scaled to 0-255 over all years
                               combined (as the original was), columns this
                               pipeline doesn't produce yet left null

Usage: uv run datacube-zh/assemble.py [--data own_data] [--region zh] [--original PATH]
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

YEARS = range(2018, 2026)
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
            # The export has rounding overshoots up to ~1.007; cap at 1.
            cols[c] = pc.min_element_wise(t[c], 1.0) if c in t.column_names else pa.nulls(n, pa.float64())
        present = [t[c] for c in classes if c in t.column_names]
        total = present[0]
        for col in present[1:]:
            total = pc.add(total, col)
        cols["lgn_coverage"] = pc.min_element_wise(total, 1.0)
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


# NBBG reference year used for each cube year (NBBG has peiljaren 2020 and 2022).
BBG_YEAR_FOR = {2018: 2020, 2019: 2020, 2020: 2020, 2021: 2020, 2022: 2022, 2023: 2022, 2024: 2022, 2025: 2022}


def load_brp(work: Path, year: int) -> pa.Table | None:
    """BRP crop-group fractions per hex for one year (datacube-zh brp)."""
    f = work / f"layers/brp_{year}.parquet"
    return pq.read_table(f) if f.exists() else None


def load_bbg(work: Path, year: int) -> pa.Table | None:
    """CBS Nieuw Bestand Bodemgebruik fractions per hex (datacube-zh bbg),
    from the NBBG peiljaar used for `year`."""
    f = work / f"layers/bbg_{BBG_YEAR_FOR[year]}.parquet"
    if not f.exists():
        return None
    t = pq.read_table(f)
    return t.append_column("year_int", pa.array([year] * t.num_rows, pa.int64()))


def load_wb(work: Path, year: int) -> pa.Table | None:
    """CBS kerncijfers wijken en buurten (datacube-zh cbs --layer wb)."""
    f = work / f"layers/wb_{year}.parquet"
    return pq.read_table(f) if f.exists() else None


def load_lbm(work: Path, year: int) -> pa.Table | None:
    """Leefbaarometer: biennial measurements; odd years take the previous one."""
    src = year if year % 2 == 0 else year - 1
    f = work / f"layers/lbm_{src}.parquet"
    if not f.exists():
        return None
    t = pq.read_table(f)
    t = t.set_column(t.schema.get_field_index("year_int"), "year_int", pa.array([year] * t.num_rows, pa.int64()))
    return t.append_column("lbm_source_year", pa.array([src] * t.num_rows, pa.int64()))


def tessera_schema(work: Path) -> pa.Schema | None:
    """Union schema of all TESSERA layers (the first year has no change columns), or None if there are none."""
    files = sorted(glob.glob(str(work / "layers/tessera_*.parquet")))
    if not files:
        return None
    fields = {}
    for f in files:
        for field in pq.read_schema(f):
            fields.setdefault(field.name, field)
    fields["h3_id"] = pa.field("h3_id", pa.string())  # pandas writes large_string; the other layers use string
    return pa.schema(list(fields.values()))


def load_tessera(work: Path, year: int, schema: pa.Schema | None, h3_ids: pa.ChunkedArray) -> pa.Table | None:
    """TESSERA PCA embedding + year-on-year change (prepare/tessera_hex.py). Years and columns that
    don't exist (before 2022; change columns in the first year) are null, so the cube schema - which
    is taken from the first year, 2018 - still carries them."""
    if schema is None:
        return None
    f = work / f"layers/tessera_{year}.parquet"
    t = pq.read_table(f) if f.exists() else pa.table({"h3_id": h3_ids})
    for field in schema:
        if field.name not in t.column_names:
            t = t.append_column(field, pa.nulls(t.num_rows, field.type))
    t = t.select(schema.names).cast(schema)
    return t.append_column("year_int", pa.array([year] * t.num_rows, pa.int64()))


def build_year(work: Path, year: int, base: pa.Table, lgn: pa.Table | None) -> pa.Table:
    """One year of the cube: CBS (+ grid/admin/static base) + BRP + NBBG + wb + lbm + TESSERA (+ LGN)."""
    cube = left_join(pq.read_table(work / f"layers/cbs_{year}.parquet"), base, "h3_id")
    tess = load_tessera(work, year, tessera_schema(work), base["h3_id"])
    for layer in (load_brp(work, year), load_bbg(work, year), load_wb(work, year), load_lbm(work, year), tess):
        if layer is not None:
            cube = left_join(cube, layer, ["h3_id", "year_int"])
    if lgn is not None:
        cube = left_join(cube, lgn.filter(pc.equal(lgn["year_int"], year)), ["h3_id", "year_int"])
    lead = ["h3_id", "year_int"]
    cube = cube.select(lead + [c for c in cube.column_names if c not in lead])
    return cube.sort_by([("h3_id", "ascending")])


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


def valid_shares(table: pa.Table) -> dict:
    """Column -> share of non-null values in `table`."""
    n = max(table.num_rows, 1)
    return {c: round(1 - table[c].null_count / n, 4) for c in table.column_names}


def build_manifest(schema: pa.Schema, shares_per_year: dict, config_dir: Path) -> dict:
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
    numeric_how = {
        "sum": "som, verdeeld naar het aandeel van elk object dat in de cel ligt",
        "area_weighted_average": "oppervlaktegewogen gemiddelde over de objecten in de cel",
        "max": "maximum over de objecten die de cel raken",
        "min": "minimum over de objecten die de cel raken",
    }
    for layer in static.get("vector", []):
        src = layer.get("description", "")
        if layer["agg"] == "numeric":
            src = f"{src} [{layer.get('parquet', '')}]"
            for o in layer.get("outputs", []):
                info[o["name"]] = (src, None, f"{numeric_how[o['agg']]} (bronkolom {o['column']})")
            if layer.get("coverage"):
                info[layer["coverage"]] = (src, "0-1", "aandeel van de cel bedekt door de objecten")
            continue
        how = "true als een polygoon de cel raakt" if layer["agg"] == "any" else "waarde van de polygoon met de grootste overlap"
        info[layer["name"]] = (src, None, how)
        if layer.get("label"):
            info[layer["label"]] = (src, None, how)

    manifest = {}
    for field in schema:
        name = field.name
        src, unit, agg = info.get(name, (None, None, None))
        if name.startswith("wb_"):
            src = "CBS Kerncijfers wijken en buurten (Wijk- en Buurtkaart van dat jaar, laatste versie); config/wb.toml"
            unit = "0-1" if name == "wb_coverage" else None
            agg = ("aandeel van de cel bedekt door buurten" if name == "wb_coverage"
                   else "oppervlaktegewogen gemiddelde van de buurten die de cel overlappen (buurtwaarde gelijk verdeeld over de buurt)")
        elif name.startswith("lbm_"):
            src = "Leefbaarometer 3.0 (BZK), Meting 2024 open data, 100m grid"
            unit = "0-1" if name == "lbm_coverage" else None
            agg = {
                "lbm_coverage": "aandeel van de cel bedekt door Leefbaarometer-gridcellen",
                "lbm_source_year": "meetjaar van de waarden (tweejaarlijks; oneven jaren = vorige meting)",
                "lbm_score": "totaalscore leefbaarheid (absoluut), oppervlaktegewogen gemiddelde van de 100m-cellen",
            }.get(name, "afwijking t.o.v. het landelijk gemiddelde (score/dimensie), oppervlaktegewogen gemiddelde van de 100m-cellen")
        elif name.startswith("tessera_"):
            src = ("TESSERA v1.1 (cambridge variant) annual 10 m embeddings, Sentinel-1/2 (Cambridge, geotessera); "
                   "prepare/tessera_hex.py, PCA + thresholds fitted on 2024+2025 (own_data/tessera/pca_threshold.npz)")
            unit = "0-1" if name.endswith("_share") else None
            if name.startswith("tessera_pc"):
                agg = ("component van het celgemiddelde embedding (alle 10m-pixels in de cel) op een vaste PCA; "
                       "PC1-3 vergelijkbaar tussen jaren, PC5/6/8 hebben jaareffecten")
            else:
                agg = {
                    "tessera_pixels": "aantal 10m-pixels in de cel (pixelcentrum)",
                    "tessera_land_pixels": "landpixels gebruikt voor de verandering (water = NBBG 2022 klassen 70-83)",
                    "tessera_agri_pixels": "landpixels in een BRP-perceel (in een van beide jaren)",
                    "tessera_nonagri_pixels": "landpixels buiten BRP-percelen",
                    "tessera_change_from": "basisjaar van de vergelijking",
                }.get(name)
                if agg is None:
                    part = ("landbouw (BRP-percelen; vooral gewasrotatie)" if "_agri_" in name
                            else "niet-landbouw (bebouwing, natuur, infrastructuur)" if "_nonagri_" in name else "alle land")
                    agg = (f"gemiddelde cosinusafstand per pixel t.o.v. het vorige jaar, {part}" if name.endswith("_mean")
                           else f"aandeel pixels boven een vaste drempel (p95 van 2024->2025), {part}")
        elif name.startswith("brp_"):
            src = "BRP gewaspercelen (RVO via PDOK), definitief, per jaar; gewascode -> gewasgroep in config/brp_groups.toml"
            unit = "0-1" if name.endswith(("_fraction", "_coverage")) else None
            agg = {
                "brp_coverage": "aandeel van de cel met geregistreerde landbouwpercelen (excl. landschapselementen)",
                "brp_dominant": "gewasgroep met het grootste oppervlak in de cel",
            }.get(name, "aandeel van het celoppervlak met deze gewasgroep (0 = geen percelen)")
        elif name.startswith("bbg_"):
            src = "CBS Nieuw Bestand Bodemgebruik, mutatiebestand NBBG2022 v1 (peiljaren 2020 en 2022)"
            unit = "0-1" if name.endswith(("_fraction", "_coverage")) else None
            agg = {
                "bbg_source_year": "NBBG-peiljaar van de waarden (2018-2021 -> 2020, 2022-2025 -> 2022)",
                "bbg_coverage": "som van alle bbg_*_fraction (1 = cel volledig gekarteerd)",
                "bbg_dominant": "categorie met het grootste oppervlak in de cel",
            }.get(name, "aandeel van het celoppervlak met deze bodemgebruikscategorie")
        elif name.endswith("_fraction") and name != "province_fraction":
            src, unit, agg = ("LGN Landelijk Grondgebruik Nederland (STOPGAP: Databricks consumption export, H3 res 9)",
                              "0-1", "aandeel van de cel met deze landgebruiksklasse")
        per_year = {y: shares.get(name) for y, shares in shares_per_year.items()}
        entry = {"source": src, "unit": unit, "aggregation": agg, "type": str(field.type), "non_null_share_per_year": per_year}
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
    ap.add_argument("--region", default="zh")
    ap.add_argument("--original", default=DEFAULT_ORIGINAL, help="original Delta table (for the zh compat schema)")
    args = ap.parse_args()
    reg = tomllib.loads((Path(__file__).parent / "config/regions.toml").read_text())[args.region]
    data = Path(args.data)
    work, out = data / reg["work"], data / reg["out"]
    out.mkdir(parents=True, exist_ok=True)
    name = f"datacube_{args.region}"

    grid = pq.read_table(work / "grid.parquet")
    base = left_join(left_join(grid, pq.read_table(work / "layers/admin.parquet"), "h3_id"),
                     pq.read_table(work / "layers/static.parquet"), "h3_id")
    # The LGN stopgap export only exists for Zuid-Holland.
    lgn = load_lgn(data) if args.region == "zh" else None

    delta_path = out / name
    if delta_path.exists():
        shutil.rmtree(delta_path)
    schema, shares, writer = None, {}, None
    for year in YEARS:
        t = build_year(work, year, base, lgn)
        if schema is None:
            schema = t.schema
            writer = pq.ParquetWriter(out / f"{name}.parquet", schema, compression="zstd")
        t = t.select(schema.names).cast(schema)
        write_deltalake(str(delta_path), t, mode="append", partition_by=["year_int"])
        writer.write_table(t)
        shares[year] = valid_shares(t)
        log(f"{year}: {t.num_rows} rows x {t.num_columns} cols")
        del t
    writer.close()
    log(f"wrote {delta_path} (partitioned by year_int) and {out / f'{name}.parquet'}")

    manifest = build_manifest(schema, shares, Path(__file__).parent / "config")
    (out / f"{name}.columns.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    log(f"wrote {out / f'{name}.columns.json'} ({len(manifest)} columns)")

    if args.region == "zh":
        real = pq.read_table(out / f"{name}.parquet")
        original_schema = pq.read_schema(glob.glob(f"{args.original}/*.parquet")[0])
        write_delta(build_compat(real, original_schema), out / "volledige_tijdreeks_delta")


if __name__ == "__main__":
    main()
