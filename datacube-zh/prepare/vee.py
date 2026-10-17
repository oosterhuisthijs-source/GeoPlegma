# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pyarrow>=17", "shapely>=2"]
# ///
"""Livestock per gemeente (CBS agricultural census) spread over the farmland of each gemeente -> per hex and year.

Source: CBS StatLine 80781ned "Landbouw; gewassen, dieren en grondgebruik naar gemeente" (CC BY 4.0; animals on
1 April; each year in that year's municipal boundaries; grazing animals and pigs rounded to 10, poultry to 100).
Farm-level animal counts (RVO I&R, WUR GIAB) are not open data, so this is the finest open level. CBS counts a
farm's animals in the gemeente of its registered seat.

Spreading (a model, not a measurement): each hex goes to the gemeente of that year containing its centre (Wijk- en
Buurtkaart of the year). Within a gemeente the count is divided over its hexes in proportion to
  grazing animals (cattle, dairy cows, sheep, goats, horses) and their farms: grassland + maize + fodder crops (BRP)
  housed animals (pigs, chickens, other poultry) and their farms: all registered farmland (BRP)
of that year; a gemeente with animals but no BRP land spreads over its whole area. Totals per gemeente are
kept exactly. Housed animals live in barns, not on fields: their cell values are a density, not barn locations.
Always computed on the national grid (work_nl), so zh == nl on shared hexes; other regions take their hexes.

Per hex and year (number, fractional):
  vee_rundvee, vee_melkkoeien, vee_schapen, vee_geiten, vee_paarden, vee_varkens, vee_kippen, vee_overig_pluimvee
  vee_bedrijven_graasdieren, vee_bedrijven_rundvee, vee_bedrijven_hokdieren, vee_bedrijven_varkens, vee_bedrijven_kippen
  (farms with that kind of animal; a farm with both is in both)

Writes <data>/<work>/layers/vee.parquet (h3_id, year_int, ...).

Usage: uv run datacube-zh/prepare/vee.py [--data own_data] [--region zh] [--years 2018-2025]
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

TABLE = "80781ned"
API = f"https://opendata.cbs.nl/ODataApi/odata/{TABLE}"
VERSIONS = {2018: "v3", 2019: "v3", 2020: "v3", 2021: "v3", 2022: "v3", 2023: "v3", 2024: "v2", 2025: "v1"}
# output column -> (CBS keys summed, spreading weight)
MEASURES = {
    "vee_rundvee": (["RundveeTotaal_84"], "graze"),
    "vee_melkkoeien": (["MelkEnKalfkoeien2Jaar_88"], "graze"),
    "vee_schapen": (["Schapen_95"], "graze"),
    "vee_geiten": (["GeitenTotaal_96"], "graze"),
    "vee_paarden": (["PaardenEnPonySTotaal_99"], "graze"),
    "vee_varkens": (["VarkensTotaal_121"], "housed"),
    "vee_kippen": (["KippenTotaal_125"], "housed"),
    "vee_overig_pluimvee": (["Kalkoenen_130", "Slachteenden_131", "OverigPluimvee_132"], "housed"),
    "vee_bedrijven_graasdieren": (["GraasdierenTotaal_102"], "graze"),
    "vee_bedrijven_rundvee": (["RundveeTotaal_103"], "graze"),
    "vee_bedrijven_hokdieren": (["HokdierenTotaal_135"], "housed"),
    "vee_bedrijven_varkens": (["VarkensTotaal_136"], "housed"),
    "vee_bedrijven_kippen": (["KippenTotaal_140"], "housed"),
}
# farm counts and the animals they count (a 0 next to animals > 0 is a missing value, not no farms)
FARM_ANIMALS = {"vee_bedrijven_graasdieren": ["vee_rundvee", "vee_schapen", "vee_geiten", "vee_paarden"],
                "vee_bedrijven_rundvee": ["vee_rundvee"],
                "vee_bedrijven_hokdieren": ["vee_varkens", "vee_kippen", "vee_overig_pluimvee"],
                "vee_bedrijven_varkens": ["vee_varkens"], "vee_bedrijven_kippen": ["vee_kippen"]}
GRAZE = ["brp_grasland_fraction", "brp_natuurlijk_grasland_fraction", "brp_mais_fraction", "brp_voedergewassen_fraction"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch_cbs(src: Path, years: list[int]) -> dict[tuple[str, int], dict]:
    """{(GMxxxx, year): {key: value}} for all gemeenten; cached per table modification date."""
    try:
        info = json.load(urllib.request.urlopen(f"{API}/TableInfos?$format=json", timeout=120))["value"][0]
        cache = src / f"{TABLE}_{info['Modified'][:10]}.json"
    except OSError as e:  # CBS unreachable (503s happen): use the newest cached copy
        cached = sorted(src.glob(f"{TABLE}_*.json"))
        if not cached:
            raise
        cache = cached[-1]
        log(f"CBS unreachable ({e}); using {cache.name}")
    if not cache.exists():
        keys = sorted({k for ks, _ in MEASURES.values() for k in ks})
        rows, url = [], (f"{API}/TypedDataSet?$format=json&$select=RegioS,Perioden,{','.join(keys)}&$filter="
                         + urllib.parse.quote(f"substringof('GM',RegioS) and Perioden ge '{years[0]}JJ00'"))
        while url:
            page = json.load(urllib.request.urlopen(url, timeout=300))
            rows += page["value"]
            url = page.get("odata.nextLink")
        cache.write_text(json.dumps({"modified": info["Modified"], "rows": rows}))
        log(f"fetched {len(rows)} rows of {TABLE} (modified {info['Modified'][:10]})")
    d = json.loads(cache.read_text())
    out = {(r["RegioS"].strip(), int(r["Perioden"][:4])): r for r in d["rows"]}
    return out, d["modified"], cache.name


def gemeenten(data: Path, year: int) -> tuple[list[str], list]:
    """Gemeente codes + polygons (lon/lat) of the year's Wijk- en Buurtkaart, cached as GeoJSON."""
    src = data / "sources/wijkbuurt"
    cache = src / f"gemeenten_{year}_{VERSIONS[year]}.geojson"
    if not cache.exists():
        gpkg = sorted(src.glob(f"**/*{year}_{VERSIONS[year]}*.gpkg"), key=lambda p: len(str(p)))[0]
        layers = subprocess.run(["ogrinfo", "-q", str(gpkg)], capture_output=True, text=True, check=True).stdout
        layer = next(line.split()[1] for line in layers.splitlines() if "gemeente" in line.lower())
        subprocess.run(["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-dialect", "sqlite", "-sql",
                        f'SELECT gemeentecode, geom FROM "{layer}"', str(cache), str(gpkg)], check=True)
    feats = json.loads(cache.read_text())["features"]
    # one gemeente can have several features (land and water parts): one entry per code
    parts: dict[str, list] = {}
    for f in feats:
        parts.setdefault(f["properties"]["gemeentecode"], []).append(shapely.geometry.shape(f["geometry"]))
    return list(parts), [shapely.union_all(p) if len(p) > 1 else p[0] for p in parts.values()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", default="2018-2025")
    args = ap.parse_args()
    data = Path(args.data)
    a, _, b = args.years.partition("-")
    years = list(range(int(a), int(b or a) + 1))
    work = data / regions.load(args.region)["work"]
    nl = data / regions.load("nl")["work"]
    src = data / "sources/cbs_landbouw"
    src.mkdir(parents=True, exist_ok=True)

    cbs, modified, cache_name = fetch_cbs(src, years)
    grid = pq.read_table(nl / "grid.parquet", columns=["h3_id", "lat", "lon", "hex_area_m2"])
    ids = grid["h3_id"].to_pylist()
    n = len(ids)
    area = grid["hex_area_m2"].to_numpy()
    pts = shapely.points(grid["lon"].to_numpy(), grid["lat"].to_numpy())
    region_ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    pos = {c: k for k, c in enumerate(ids)}
    sel = np.array([pos[c] for c in region_ids])

    out = {c: [] for c in MEASURES}
    for yr in years:
        codes, polys = gemeenten(data, yr)
        tree = shapely.STRtree(polys)
        hit_pt, hit_poly = tree.query(pts, predicate="within")
        gm = np.full(n, -1)
        gm[hit_pt] = hit_poly  # a centre on a shared border takes the last match
        brp = pq.read_table(nl / f"layers/brp_{yr}.parquet").to_pydict()
        bpos = np.array([pos[c] for c in brp["h3_id"]])
        frac = {c: np.zeros(n) for c in ["brp_coverage", *GRAZE]}
        for c in frac:
            frac[c][bpos] = np.nan_to_num(np.array(brp[c], dtype=float))
        weights = {"graze": area * sum(frac[c] for c in GRAZE), "housed": area * frac["brp_coverage"]}
        inside = gm >= 0
        count, known = {}, {}
        for col, (keys, _) in MEASURES.items():
            count[col], known[col] = np.zeros(len(codes)), np.zeros(len(codes), bool)
            for g, code in enumerate(codes):
                r = cbs.get((code, yr))
                v = [r.get(k) for k in keys] if r else [None]
                if r and any(x is not None for x in v):
                    count[col][g], known[col][g] = sum(x or 0 for x in v), True
        for col in MEASURES:
            # CBS publishes 0 instead of "no data" for a whole year (horses 2022): unknown, not none
            if known[col].any() and count[col][known[col]].sum() == 0:
                known[col][:] = False
                log(f"  {yr}: {col} is 0 everywhere, set to null")
        for col, animals in FARM_ANIMALS.items():
            # from 2024 CBS's farm count per animal is 0 in gemeenten that still have those animals: unknown
            bad = known[col] & (count[col] == 0) & (sum(count[a] for a in animals) > 0)
            known[col] &= ~bad
            if bad.any():
                log(f"  {yr}: {col} 0 with animals present in {bad.sum()} gemeenten, set to null")
        totals = {}
        for col, (keys, wkind) in MEASURES.items():
            w = weights[wkind]
            wsum = np.bincount(gm[inside], w[inside], len(codes))
            asum = np.bincount(gm[inside], area[inside], len(codes))
            vals = np.full(n, np.nan)
            vals[inside] = 0.0
            count_c, known_c = count[col], known[col]
            gi = gm[inside]
            use_area = wsum[gi] <= 0
            share = np.where(use_area, area[inside] / np.maximum(asum[gi], 1), w[inside] / np.maximum(wsum[gi], 1e-9))
            vals[inside] = np.where(known_c[gi], count_c[gi] * share, np.nan)
            out[col].append(vals[sel])
            totals[col] = count_c[known_c].sum()
        missing = [c for g, c in enumerate(codes) if (c, yr) not in cbs]
        empty = [c for g, c in enumerate(codes) if not (gm == g).any() and cbs.get((c, yr), {}).get("RundveeTotaal_84")]
        if empty:
            log(f"  {yr}: gemeenten with animals but no hex centre (animals lost): {empty}")
        log(f"{yr}: {len(codes)} gemeenten ({len(missing)} not in CBS table), NL cattle {totals['vee_rundvee']:,.0f}, "
            f"pigs {totals['vee_varkens']:,.0f}, chickens {totals['vee_kippen']:,.0f}; region cattle "
            f"{np.nansum(out['vee_rundvee'][-1]):,.0f}")

    table = pa.table({"h3_id": region_ids * len(years),
                      "year_int": np.repeat(np.array(years, np.int32), len(region_ids)),
                      **{c: pa.array(np.round(np.concatenate(v), 3), from_pandas=True) for c, v in out.items()}})
    source = {"source": f"CBS StatLine {TABLE} (modified {modified}, {cache_name}), spread over BRP farmland per gemeente",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), work / "layers/vee.parquet",
                   compression="zstd")
    log(f"-> {work / 'layers/vee.parquet'} ({table.num_rows} rows)")


if __name__ == "__main__":
    main()
