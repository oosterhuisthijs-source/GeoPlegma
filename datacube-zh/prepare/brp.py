#!/usr/bin/env python3
"""BRP gewaspercelen (RVO, via PDOK) -> per-year GeoParquet + crop-group mapping.

Input: the national yearly BRP downloads (PDOK ATOM "brpgewaspercelen
definitief"): 2018-2019 as ESRI FileGDB, 2020+ as GeoPackage. Their schemas
differ, so every year is normalised to (geom, category, gewascode, gewas),
clipped to the region bbox and reprojected to OGC:CRS84:
    <data>/work/brp/brp_<year>.parquet

"Landschapselement" parcels (hedges, ditches, tree rows; registered only
from ~2020 on, ~1.2M national features) are excluded so years stay
comparable.

It also (re)writes datacube-zh/config/brp_groups.toml: every gewascode seen,
assigned to a crop group by the keyword RULES below (first match wins),
with its area so the classification is easy to review.

Usage: datacube-zh/prepare/brp.py [--brp-dir DIR] [--data own_data] [--years 2018-2023]
"""
import argparse
import csv
import io
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

DEFAULT_BRP_DIR = "/home/thijsradijs/Work_Databricks_repos/Databricks/Bronnen/BRP/data"

# (group, label, keywords matched case-insensitively against the gewas name;
#  or category == ...). Order matters: first match wins.
RULES = [
    ("natuur", "Natuurterrein en bos", ["natuurterrein", "bos, blijvend", "bos zonder", "voedselbos", "heide", "bos (", "houtwal", "bosje"]),
    # Grass grown for seed/forage, before "grasland"/"riet" can grab it.
    ("voedergewassen", "Voedergewassen, graszaad en groenbemesters", ["zwenkgras", "raaigras", "beemdgras", "timothee", "festulolium", "graszaad", "luzerne", "klaver", "esparcette", "groenbemester", "facelia", "voeder", "wikke", "lupine", "serradelle", "japanse haver", "bladrammenas", "drachtplanten", "mengsel"]),
    ("natuurlijk_grasland", "Natuurlijk grasland / agrarisch natuurmengsel", ["grasland, natuurlijk", "natuurmengsel", "natuurbraak"]),
    ("grasland", "Agrarisch grasland", ["grasland"]),
    ("mais", "Maïs", ["mais", "maïs"]),
    ("aardappelen", "Aardappelen", ["aardappel"]),
    ("bieten", "Bieten", ["bieten"]),
    ("granen", "Granen", ["tarwe", "gerst", "rogge", "haver", "triticale", "spelt", "boekweit", "granen", "teff", "sorghum", "gierst"]),
    ("bloembollen", "Bloembollen en -knollen", ["bloembollen", "tulp", "lelie", "hyacint", "narcis", "gladiool", "krokus", "iris", "dahlia", "zantedeschia", "allium", "bolbloemen"]),
    ("fruit", "Fruitteelt", ["appel", "peren", "kersen", "pruimen", "bessen", "fruit", "noten", "walnoot", "hazelnoot", "druiven", "kiwi", "vruchtbomen", "kweepeer", "framboz", "bramen", "cranberry", "bosbes"]),
    ("boomkwekerij", "Boomkwekerij en vaste planten", ["laanbomen", "haagplanten", "bos- en", "sierheesters", "coniferen", "rozen", "vaste planten", "boomkwekerij", "klimplanten", "heide- en", "buxus", "rhododendron", "trek- en", "onderstammen", "vruchtbomen", "kerstbomen", "open grond", "potplanten", "bloemkwekerij", "bloemzaden", "snijbloemen", "zomerbloemen", "pioenroos", "droogbloemen", "snijgroen", "overige bloemen"]),
    ("groenten", "Groenten", ["uien", "peen", "wortel", "kool", "bonen", "erwten", "asperge", "prei", "sla ", "spinazie", "witlof", "pompoen", "courgette", "selderij", "knolvenkel", "biet, rode", "rabarber", "aardbei", "radijs", "sla;", "sla,", "bladgewassen", "schorseneren", "pastinaak", "knoflook", "sjalot", "andijvie", "groente", "pepers", "komkommer", "tomaten", "paprika", "broccoli", "spruit", "venkel", "kruiden", "peterselie", "bieslook", "boerenkool", "veldsla", "rucola", "postelein", "maggi", "dille", "zuring", "mosterd", "koolrabi", "raapstelen", "artisjok", "suikermais", "cichorei"]),
    ("braak", "Braak", ["braak"]),
    ("handelsgewassen", "Handelsgewassen (vlas, hennep, koolzaad, ...)", ["vlas", "hennep", "koolzaad", "zonnebloem", "olifantsgras", "miscanthus", "karwij", "blauwmaanzaad", "soja", "teunisbloem", "lijnzaad", "quinoa", "tabak", "hennep", "wilg", "riet", "kanariezaad", "papaver"]),
]
FALLBACK_BY_CATEGORY = {"Grasland": "grasland", "Natuurterrein": "natuur", "Braakland": "braak"}
# Field margins ("Rand, grenzend aan ...") and ditches ("Sloot, ...") end up
# here, together with genuinely rare crops.
OTHER = ("overig", "Overige gewassen, akkerranden en sloten")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def classify(gewas: str, category: str) -> str:
    g = (gewas or "").lower()
    for group, _, keys in RULES:
        if any(k in g for k in keys):
            return group
    return FALLBACK_BY_CATEGORY.get(category, OTHER[0])


def source_for(brp_dir: Path, year: int):
    """(dataset, layer, sql select list) for one year's download."""
    gpkg = brp_dir / str(year) / f"brpgewaspercelen_definitief_{year}.gpkg"
    if gpkg.exists():
        return gpkg, "brp_gewas", "category AS category, CAST(gewascode AS integer) AS gewascode, gewas AS gewas"
    gdb = next((brp_dir / str(year)).glob("unzip_*/*.gdb"), None)
    if gdb:
        layer = f"BRP_gewaspercelen_{year}"
        return gdb, layer, "CAT_GEWASCATEGORIE AS category, CAST(GWS_GEWASCODE AS integer) AS gewascode, GWS_GEWAS AS gewas"
    raise FileNotFoundError(f"no BRP download for {year} under {brp_dir / str(year)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brp-dir", default=DEFAULT_BRP_DIR)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--years", default="2018-2023")
    ap.add_argument("--config-out", default="datacube-zh/config/brp_groups.toml")
    ap.add_argument("--region", default="zh")
    args = ap.parse_args()
    reg = regions.load(args.region)
    BBOX_RD = reg["bbox_rd"]
    y0, y1 = map(int, args.years.split("-"))
    out_dir = Path(args.data) / reg["work"] / "brp"
    out_dir.mkdir(parents=True, exist_ok=True)

    codes = {}  # gewascode -> {gewas, category, km2}
    for year in range(y0, y1 + 1):
        src, layer, select = source_for(Path(args.brp_dir), year)
        out = out_dir / f"brp_{year}.parquet"
        sql = f"SELECT {select} FROM \"{layer}\" WHERE {select.split(' AS category')[0]} <> 'Landschapselement'"
        if not out.exists():
            subprocess.run(
                ["ogr2ogr", "-f", "Parquet", str(out), str(src), "-sql", sql, "-dialect", "OGRSQL",
                 "-spat", *map(str, BBOX_RD), "-t_srs", "OGC:CRS84", "-nlt", "MULTIPOLYGON", "-makevalid",
                 "-lco", "GEOMETRY_ENCODING=WKB", "-lco", "COMPRESSION=ZSTD", "-lco", "GEOMETRY_NAME=geom", "-overwrite"],
                check=True,
            )
            log(f"brp {year}: {src.name} -> {out}")
        # Distinct codes + area (in the clipped region) for the mapping file.
        stats = subprocess.run(
            ["ogr2ogr", "-f", "CSV", "/vsistdout/", str(out), "-dialect", "SQLite", "-sql",
             "SELECT gewascode, gewas, category, SUM(ST_Area(ST_Transform(geom, 28992))) / 1e6 AS km2 "
             f"FROM \"brp_{year}\" GROUP BY gewascode, gewas, category"],
            check=True, capture_output=True, text=True,
        ).stdout
        for row in csv.DictReader(io.StringIO(stats)):
            if not row["gewascode"]:
                continue
            c = int(row["gewascode"])
            e = codes.setdefault(c, {"gewas": row["gewas"].strip(), "category": row["category"], "km2": 0.0})
            e["km2"] += float(row["km2"] or 0) / (y1 - y0 + 1)

    # Keep codes from earlier runs (e.g. another region) that this run didn't
    # see, so one mapping file serves every region. Areas are this run's.
    import re as _re
    if Path(args.config_out).exists():
        for line in Path(args.config_out).read_text().splitlines():
            m = _re.match(r'^(\d+) = "[^"]*"  # (.*) \[([^\]]*)\] ([\d.]+) km2$', line)
            if m and int(m.group(1)) not in codes:
                codes[int(m.group(1))] = {"gewas": m.group(2), "category": m.group(3), "km2": 0.0}
    groups = {g: label for g, label, _ in RULES} | {OTHER[0]: OTHER[1]}
    lines = [
        "# BRP gewascode -> crop group. GENERATED by datacube-zh/prepare/brp.py",
        "# (keyword RULES there; first match wins). Re-run it after changing the",
        "# rules; hand edits here are overwritten. km2 = mean yearly area in the region.",
        "",
        "[groups]",
    ]
    lines += [f'{g} = "{label}"' for g, label in groups.items()]
    lines += ["", "[codes]"]
    for c, e in sorted(codes.items(), key=lambda kv: -kv[1]["km2"]):
        g = classify(e["gewas"], e["category"])
        lines.append(f'{c} = "{g}"  # {e["gewas"].replace(chr(34), "")} [{e["category"]}] {e["km2"]:.2f} km2')
    Path(args.config_out).write_text("\n".join(lines) + "\n")
    by_group = {}
    for c, e in codes.items():
        by_group[classify(e["gewas"], e["category"])] = by_group.get(classify(e["gewas"], e["category"]), 0) + e["km2"]
    log(f"{len(codes)} gewascodes -> {args.config_out}")
    log("mean km2 per group:", {g: round(v, 1) for g, v in sorted(by_group.items(), key=lambda kv: -kv[1])})


if __name__ == "__main__":
    main()
