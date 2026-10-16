# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1", "h3>=4", "pyarrow>=17"]
# ///
"""Step 6b: the assembled res-9 cube aggregated to coarser H3 resolutions (default 8 and 7),
for national views and simulations that don't need ~0.1 km2 cells.

Every coarse cell is aggregated directly from its res-9 children in the region grid (res 7 from
res 9, not via res 8, so weights stay exact). Rules per column (`rule_for`):

- counts/sums (`*_sum`, `num_*`, 3D BAG counts/areas/volumes, pixel counts)  -> sum
- area shares (`*_fraction`, `*_coverage`, `lgn_groep_*`)                     -> area-weighted mean
- averages (CBS averages, wb_*, lbm_*, rasters, ndff)   -> weighted mean, weight = child area x that
                                                           layer's coverage (cbs/wb/lbm_coverage)
- 3D BAG mean height / build year                       -> mean weighted by footprint
- TESSERA embeddings / change                           -> mean weighted by its own pixel counts
- `*_min` / `*_max`                                     -> min / max
- wind turbines (`WIND_RULES`)                          -> count/power summed, heights max, distance min
- OSM line lengths (`osm_*_lengte_m`)                   -> sum
- OSM grid/solar/marinas, NDW charging (`STATIC_RULES`) -> counts and power summed, distance min
- KNMI climate, RIVM deposition/air quality, TROPOMI     -> area-weighted mean (TROPOMI month count: min)
- categories (dominant class, Natura 2000, soil, buurt)  -> area-weighted majority; buurt/wijk/gemeente
                                                           and code/name pairs come from the same child value
- flags (bool)                                          -> true if any child is true
- source years                                          -> max

Area shares are relative to the covered part of the coarse cell: a coastal or border parent only
has the children that are in the grid. `n_children` (of 7 / 49) and `covered_area_m2` say how much;
`hex_area_m2` is the full coarse cell, `province_fraction` its share inside the region.

Writes <out>/datacube_<region>_r<res>.parquet (+ .columns.json with the rule per column).

Usage: uv run datacube-zh/coarsen.py [--data own_data] [--region zh] [--res 8 7]
"""
import argparse
import json
import sys
import time
import tomllib
from pathlib import Path

import duckdb
import h3
import pyarrow as pa

HERE = Path(__file__).parent

# Category columns that belong together: the majority is taken on the key, the rest follow it.
COMPANIONS = {
    "buurtcode": ["buurtnaam", "wijkcode", "wijknaam", "gemeentecode", "gemeentenaam"],
    "natura2000_id": ["natura2000_naam"],
    "bodemklasse": ["bodemnaam"],
    "code": ["naam"],
}
TESSERA_WEIGHT = {
    "tessera_pc": "tessera_pixels",
    "tessera_change_mean": "tessera_land_pixels", "tessera_change_share": "tessera_land_pixels",
    "tessera_change_agri_mean": "tessera_agri_pixels", "tessera_change_agri_share": "tessera_agri_pixels",
    "tessera_change_nonagri_mean": "tessera_nonagri_pixels", "tessera_change_nonagri_share": "tessera_nonagri_pixels",
    "tessera_change_nonagri_share_dc": "tessera_nonagri_pixels",
}
WIND_RULES = {"wind_aantal": "sum", "wind_vermogen_kw": "sum", "wind_ashoogte_max_m": "max",
              "wind_rotordiameter_max_m": "max", "wind_tiphoogte_max_m": "max", "wind_afstand_min_km": "min"}
STATIC_RULES = {"osm_hoogspanning_afstand_min_km": "min", "osm_onderstation_aantal": "sum",
                "osm_zonnepark_vermogen_kw": "sum", "osm_jachthaven_aantal": "sum",
                "laad_punten_aantal": "sum", "laad_punten_snel_aantal": "sum"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def rule_for(name: str, meta: dict) -> tuple[str, str | None]:
    """(rule, weight column or None) for one res-9 column; raises on anything unclassified."""
    t, agg = meta["type"], (meta.get("aggregation") or "")
    companions = {c for cs in COMPANIONS.values() for c in cs}
    if name in companions:
        return "companion", None
    if t == "string":
        return "mode", None
    if t == "bool":
        return "any", None
    if name in ("lgn_source_year", "bbg_source_year", "lbm_source_year", "tessera_change_from"):
        return "max", None
    if name in WIND_RULES:
        return WIND_RULES[name], None
    if name in STATIC_RULES:
        return STATIC_RULES[name], None
    if name == "tropomi_no2_maanden":
        return "min", None  # months the yearly mean rests on: the weakest child counts
    if name.startswith("osm_") and name.endswith("_lengte_m"):
        return "sum", None
    if name.startswith("tessera_"):
        if name.endswith("pixels"):
            return "sum", None
        key = "tessera_pc" if name.startswith("tessera_pc") else name
        return "wmean", TESSERA_WEIGHT[key]
    if name.endswith(("_fraction", "_coverage")) or name.startswith("lgn_groep_"):
        return "wmean", "area"
    if name.endswith("_sum") or name.startswith("num_") or agg.startswith("som,"):
        return "sum", None
    if name.endswith("_min"):
        return "min", None
    if name.endswith("_max") or agg.startswith("maximum"):
        return "max", None
    if name in ("bag3d_hoogte_gem_m", "bag3d_bouwjaar_gem"):
        return "wmean", "bag3d_footprint_m2"
    if name.startswith("wb_"):
        return "wmean", "wb_coverage"
    if name.startswith("lbm_"):
        return "wmean", "lbm_coverage"
    if name.endswith("_area_weighted_average"):
        return "wmean", "cbs_coverage"
    if t in ("double", "float"):
        return "wmean", "area"  # rasters (mean over pixels), ndff classes
    raise SystemExit(f"coarsen: no rule for column {name} ({t}, {agg!r})")


def q(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def build_sql(columns: dict, src: str) -> tuple[str, dict]:
    """One aggregation query over `src` (res-9 rows with a `parent` column)."""
    rules = {}
    sel, ctes, joins = [], [], []
    for name, meta in columns.items():
        if name in ("h3_id", "year_int", "lat", "lon", "hex_area_m2", "province_fraction"):
            continue
        rule, w = rule_for(name, meta)
        rules[name] = (rule, w)
        c = q(name)
        if rule == "sum":
            sel.append(f"sum({c}) AS {c}")
        elif rule in ("min", "max"):
            sel.append(f"{rule}({c}) AS {c}")
        elif rule == "any":
            sel.append(f"bool_or({c}) AS {c}")
        elif rule == "wmean":
            wexpr = "hex_area_m2" if w == "area" else (
                f"hex_area_m2 * coalesce({q(w)}, 0)" if w.endswith("_coverage") else f"coalesce({q(w)}, 0)")
            sel.append(f"sum({c} * {wexpr}) / nullif(sum(CASE WHEN {c} IS NOT NULL THEN {wexpr} END), 0) AS {c}")
        elif rule == "mode":
            # area-weighted majority; ties go to the largest value, so the result is reproducible
            alias = f"m_{len(ctes)}"
            comp = COMPANIONS.get(name, [])
            inner = "".join(f", any_value({q(x)}) AS {q(x)}" for x in comp)
            outer = "".join(f", arg_max({q(x)}, (w, {c})) AS {q(x)}" for x in comp)
            ctes.append(f"""{alias} AS (
  SELECT parent, year_int, arg_max({c}, (w, {c})) AS {c}{outer}
  FROM (SELECT parent, year_int, {c}{inner}, sum(hex_area_m2) AS w
        FROM {src} WHERE {c} IS NOT NULL GROUP BY parent, year_int, {c})
  GROUP BY parent, year_int)""")
            joins.append((alias, [name] + comp))
    base = f"""base AS (
  SELECT parent, year_int, count(*) AS n_children, sum(hex_area_m2) AS covered_area_m2,
         sum(hex_area_m2 * province_fraction) AS _inside_m2,
         {', '.join(sel)}
  FROM {src} GROUP BY parent, year_int)"""
    out_cols = ["base.*"] + [f"{a}.{q(c)}" for a, cs in joins for c in cs]
    join_sql = "".join(f" LEFT JOIN {a} USING (parent, year_int)" for a, _ in joins)
    sql = f"WITH {base}, {', '.join(ctes)} SELECT {', '.join(out_cols)} FROM base{join_sql}" if ctes else \
        f"WITH {base} SELECT * FROM base"
    return sql, rules


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--res", type=int, nargs="*", default=[8, 7])
    ap.add_argument("--memory", default="8GB", help="DuckDB memory limit (spills to disk beyond it)")
    args = ap.parse_args()

    reg = tomllib.loads((HERE / "config/regions.toml").read_text())[args.region]
    out = Path(args.data) / reg["out"]
    name = f"datacube_{args.region}"
    cube = out / f"{name}.parquet"
    columns = json.loads((out / f"{name}.columns.json").read_text())

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory}'")
    con.execute("SET preserve_insertion_order = false")
    ids = [r[0] for r in con.sql(f"SELECT DISTINCT h3_id FROM '{cube}'").fetchall()]
    log(f"{len(ids)} res-9 cells")

    for res in args.res:
        t0 = time.time()
        parents = [h3.cell_to_parent(h, res) for h in ids]
        con.register("pmap", pa.table({"h3_id": ids, "parent": parents}))
        con.execute(f"CREATE OR REPLACE TEMP VIEW src AS SELECT c.*, p.parent FROM '{cube}' c JOIN pmap p USING (h3_id)")
        sql, rules = build_sql(columns, "src")
        uniq = sorted(set(parents))
        geo = pa.table({
            "parent": uniq,
            "lat": [h3.cell_to_latlng(p)[0] for p in uniq],
            "lon": [h3.cell_to_latlng(p)[1] for p in uniq],
            "hex_area_m2": [h3.cell_area(p, "m^2") for p in uniq],
        })
        con.register("geo", geo)
        order = [c for c in columns if c not in ("h3_id", "year_int", "lat", "lon", "hex_area_m2", "province_fraction")]
        final = f"""SELECT a.parent AS h3_id, a.year_int, g.lat, g.lon, g.hex_area_m2,
                           least(a._inside_m2 / g.hex_area_m2, 1.0) AS province_fraction,
                           a.n_children, a.covered_area_m2, {', '.join('a.' + q(c) for c in order)}
                    FROM ({sql}) a JOIN geo g USING (parent) ORDER BY h3_id, year_int"""
        dest = out / f"{name}_r{res}.parquet"
        con.execute(f"COPY ({final}) TO '{dest}' (FORMAT parquet, COMPRESSION zstd)")
        n, k = con.sql(f"SELECT count(*), count(DISTINCT h3_id) FROM '{dest}'").fetchone()
        ncol = len(con.sql(f"DESCRIBE SELECT * FROM '{dest}'").fetchall())

        manifest = {}
        for c, meta in columns.items():
            if c in rules:
                rule, w = rules[c]
                wl = None if w is None else "celoppervlak" if w == "area" else \
                    f"celoppervlak x {w}" if w.endswith("_coverage") else w
                how = {"sum": "som van de res-9 cellen", "min": "minimum van de res-9 cellen",
                       "max": "maximum van de res-9 cellen", "any": "waar als een res-9 cel waar is",
                       "mode": "oppervlaktegewogen meest voorkomende waarde van de res-9 cellen",
                       "companion": "hoort bij de meest voorkomende waarde van de sleutelkolom",
                       "wmean": f"gewogen gemiddelde van de res-9 cellen (gewicht: {wl})"}[rule]
                manifest[c] = {**{k2: v for k2, v in meta.items() if k2 != "non_null_share_per_year"},
                               "aggregation_res9": meta.get("aggregation"), "aggregation": how, "rule": rule, "weight": w}
            else:
                manifest[c] = {k2: v for k2, v in meta.items() if k2 != "non_null_share_per_year"}
        manifest["h3_id"]["aggregation"] = f"H3 resolutie {res} (ouder van de res-9 cellen)"
        manifest["hex_area_m2"] = {"source": "h3", "unit": "m2", "aggregation": "volledige oppervlakte van de res-"
                                   f"{res} cel", "type": "double"}
        manifest["province_fraction"] = {"source": "grid", "unit": "0-1", "type": "double",
                                         "aggregation": "aandeel van de cel binnen de regio"}
        manifest["n_children"] = {"source": "grid", "unit": f"0-{7 ** (9 - res)}", "type": "int64",
                                  "aggregation": f"aantal res-9 cellen uit het grid (max {7 ** (9 - res)})"}
        manifest["covered_area_m2"] = {"source": "grid", "unit": "m2", "type": "double",
                                       "aggregation": "som van de oppervlakte van die res-9 cellen; aandelen en "
                                                      "gemiddelden gelden voor dit deel van de cel"}
        for c in ("hex_area_m2", "province_fraction", "n_children", "covered_area_m2"):
            manifest[c].update({"tier": "key" if c == "hex_area_m2" else "meta", "native_resolution": f"H3 res {res}",
                                "reference_period": "fixed", "time": "static"})
        (out / f"{name}_r{res}.columns.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
        log(f"res {res}: {k} cells x {n // max(k, 1)} years, {ncol} columns -> {dest} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
