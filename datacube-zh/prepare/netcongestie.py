# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17", "shapely>=2"]
# ///
"""Electricity grid congestion (Netbeheer Nederland capaciteitskaart) -> per hex (current) + per gemeente (history).

Source: capaciteitskaart.netbeheernederland.nl, the grid operators' joint map; no licence stated. The CSV exports
(';', decimal comma; column meanings in brondata_documentatie.txt) are fetched by
  netcongestie.py download   -> <data>/sources/netcongestie/<yyyymmdd>/{congestie_pc6,voedingsgebieden,...}.csv
Run it monthly (systemd timer datacube-netcongestie.timer) to build a history: the site keeps only the current state.
Earlier states: Internet Archive copies of the old PC6 export (dashboard/download, `postcode,invoeding,afname`) in
<data>/sources/netcongestie/wayback/pc6_<timestamp>.csv: 2023-03 .. 2024-05, 7 copies; until 2023-12 they list only
part of the country (70-95k of ~465k postcodes, the operators that published then), from 2024-01 all of it.

Colour codes per 6-digit postcode, for afname (offtake) and opwek/invoeding (feed-in): 0 transparent (no
congestion), 1 yellow (limited capacity), 2 orange (waiting list / research), 3 red (no capacity: waiting list),
-1 no information.

Postcodes -> hexes with the CBS PC6 polygons (2024 v2, <data>/sources/cbs_pc6/): each hex is sampled at the centres
of its 7 res-10 children.

Per hex (current snapshot; null where no sample point lies in a postcode with information):
  net_afname_kleur_max     worst offtake colour (0-3) among the sample points
  net_afname_rood_aandeel  share of the sample points in a red offtake postcode
  net_opwek_kleur_max, net_opwek_rood_aandeel    the same for feed-in
  net_voedingsgebied_id    supply area of the regional grid operator (most sample points); MW figures per area in
                           datacube_<region>_netgebieden.parquet (one row per area: available / needed transport
                           capacity, waiting list, requests, expected year solved), not spread over hexes

Per gemeente (static 2024 gemeenten; a postcode goes to the gemeente of its polygon's inner point) and snapshot:
  datum, bron ("archief" = Internet Archive copy, "netbeheer" = own download), net_pc6 (CBS postcodes in the
  gemeente), net_pc6_gemeld (postcodes in the snapshot with information), and for afname and opwek the share of the
  listed postcodes per colour: net_<afname|opwek>_<rood|oranje|geel|geen>_aandeel. Compare snapshots only where
  net_pc6_gemeld / net_pc6 is near 1.

Usage: uv run datacube-zh/prepare/netcongestie.py download [--data own_data]
       uv run datacube-zh/prepare/netcongestie.py [--data own_data] [--region nl]
"""
import argparse
import csv
import datetime as dt
import io
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

API = "https://capaciteitskaart.netbeheernederland.nl/api/download/"
FILES = ["congestie_pc6.csv", "voedingsgebieden.csv", "tennetgebieden.csv", "tennetcongestie.csv", "projecten.csv",
         "voedingsgebieden_year_granularity.csv", "brondata_documentatie.txt"]
PC6_ZIP = "https://download.cbs.nl/postcode/2026-cbs_pc6_2024_v2.zip"
COLOURS = {3: "rood", 2: "oranje", 1: "geel", 0: "geen"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "datacube-zh"})
    with urllib.request.urlopen(req, timeout=600) as r:  # the API redirects to a short-lived S3 link
        return r.read()


def download(src: Path):
    day = src / dt.date.today().strftime("%Y%m%d")
    day.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        (day / f).write_bytes(get(API + f))
        log(f"{day / f}: {(day / f).stat().st_size / 1e6:.1f} MB")
    pc6 = src.parent / "cbs_pc6"
    if not (pc6 / "cbs_pc6_2024_v2.gpkg").exists():
        pc6.mkdir(parents=True, exist_ok=True)
        (pc6 / "pc6.zip").write_bytes(get(PC6_ZIP))
        subprocess.run(["unzip", "-o", "-q", str(pc6 / "pc6.zip"), "-d", str(pc6)], check=True)


def read_pc6_codes(f: Path) -> dict[str, tuple[int, int]]:
    """{postcode: (afname, opwek)} from a current export (';') or an archived one (',', 'invoeding')."""
    raw = f.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    rows = csv.DictReader(io.StringIO(text), delimiter=";" if text.split("\n", 1)[0].count(";") else ",")
    out = {}
    for r in rows:
        pc = (r.get("postcode") or "").strip().replace(" ", "")
        if len(pc) != 6:
            continue
        def code(v):
            try:
                c = int(float((v or "").replace(",", ".")))
            except ValueError:
                return -1
            return c if c in COLOURS else -1
        out[pc] = (code(r.get("afname")), code(r.get("opwek", r.get("invoeding"))))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    args = ap.parse_args()
    src = Path(args.data) / "sources/netcongestie"
    if args.step == "download":
        return download(src)

    # CBS PC6 polygons (RD), cached as GeoParquet
    pc6_dir = Path(args.data) / "sources/cbs_pc6"
    cache = pc6_dir / "pc6_2024_v2.parquet"
    if not cache.exists():
        subprocess.run(["ogr2ogr", "-f", "Parquet", str(cache), str(pc6_dir / "cbs_pc6_2024_v2.gpkg"), "-t_srs",
                        "OGC:CRS84", "-sql", "SELECT postcode6, geom FROM cbs_pc6_2024", "-lco",
                        "GEOMETRY_ENCODING=WKB", "-lco", "GEOMETRY_NAME=geom"], check=True)
    t = pq.read_table(cache)
    pcs = t["postcode6"].to_pylist()
    polys = shapely.from_wkb(t["geom"].to_pylist())
    tree = shapely.STRtree(polys)

    work = Path(args.data) / regions.load(args.region)["work"]
    admin = pq.read_table(work / "layers/admin.parquet", columns=["h3_id", "gemeentecode", "gemeentenaam"])
    ids = admin["h3_id"].to_pylist()
    gem_of_hex = dict(zip(ids, admin["gemeentecode"].to_pylist()))
    names = {c: n for c, n in zip(admin["gemeentecode"].to_pylist(), admin["gemeentenaam"].to_pylist())
             if c and c != "GM0998"}

    # hex samples: centres of the 7 res-10 children -> postcode index
    sample_hex, sample_xy = [], []
    for k, c in enumerate(ids):
        for ch in h3.cell_to_children(c, 10):
            la, lo = h3.cell_to_latlng(ch)
            sample_hex.append(k)
            sample_xy.append((lo, la))
    sample_hex = np.array(sample_hex)
    pts = shapely.points(np.array(sample_xy))
    hit_pt, hit_poly = tree.query(pts, predicate="within")
    sample_pc = np.full(len(pts), -1)
    sample_pc[hit_pt] = hit_poly
    log(f"{len(pts):,} sample points, {(sample_pc >= 0).mean():.1%} inside a postcode")

    # postcode -> gemeente (inner point -> hex -> admin)
    inner = shapely.point_on_surface(polys)
    xy = shapely.get_coordinates(inner)
    pc_gem = [gem_of_hex.get(h3.latlng_to_cell(y, x, 9)) for x, y in xy]
    n_pc = {}
    for g in pc_gem:
        n_pc[g] = n_pc.get(g, 0) + 1

    # snapshots: archived copies, then own downloads (newest = current)
    snaps = [(p.stem.split("_")[1][:8], "archief", p) for p in sorted((src / "wayback").glob("pc6_*.csv"))]
    snaps += [(d.name, "netbeheer", d / "congestie_pc6.csv") for d in sorted(src.glob("[0-9]" * 8))
              if (d / "congestie_pc6.csv").exists()]
    if not snaps or snaps[-1][1] != "netbeheer":
        raise SystemExit("no own download of the current state: run `netcongestie.py download` first")

    rows = []
    for day, bron, f in snaps:
        codes = read_pc6_codes(f)
        acc = {g: {"n": 0, **{f"{m}_{c}": 0 for m in ("afname", "opwek") for c in COLOURS.values()}} for g in names}
        for pc, g in zip(pcs, pc_gem):
            if g in acc and pc in codes:
                a, o = codes[pc]
                if a < 0 and o < 0:
                    continue
                acc[g]["n"] += 1
                for m, v in (("afname", a), ("opwek", o)):
                    if v >= 0:
                        acc[g][f"{m}_{COLOURS[v]}"] += 1
        datum = dt.date(int(day[:4]), int(day[4:6]), int(day[6:8]))
        for g, a in acc.items():
            row = {"datum": datum, "bron": bron, "gemeentecode": g, "gemeentenaam": names[g], "net_pc6": n_pc.get(g, 0),
                   "net_pc6_gemeld": a["n"]}
            for m in ("afname", "opwek"):
                listed = sum(a[f"{m}_{c}"] for c in COLOURS.values())
                for c in COLOURS.values():
                    row[f"net_{m}_{c}_aandeel"] = round(a[f"{m}_{c}"] / listed, 4) if listed else None
            rows.append(row)
        tot = sum(a["n"] for a in acc.values())
        red = sum(a["afname_rood"] for a in acc.values())
        log(f"{datum} {bron}: {len(codes):,} postcodes in the file, {tot:,} matched ({tot / len(pcs):.0%} of CBS); "
            f"afname red {red / max(tot, 1):.0%}")
    gem_table = pa.Table.from_pylist(rows)

    # per hex: the current snapshot
    day, _, f = snaps[-1]
    codes = read_pc6_codes(f)
    pc_code = np.array([codes.get(p, (-1, -1)) for p in pcs])  # by polygon index
    n = len(ids)
    cols = {}
    for j, m in enumerate(("afname", "opwek")):
        v = np.where(sample_pc >= 0, pc_code[np.maximum(sample_pc, 0), j], -1)
        known = v >= 0
        cnt = np.bincount(sample_hex[known], minlength=n)
        red = np.bincount(sample_hex[known & (v == 3)], minlength=n)
        mx = np.full(n, -1)
        np.maximum.at(mx, sample_hex[known], v[known])
        cols[f"net_{m}_kleur_max"] = pa.array([int(x) if x >= 0 else None for x in mx], pa.int32())
        cols[f"net_{m}_rood_aandeel"] = pa.array([round(r / c, 3) if c else None for r, c in zip(red, cnt)], pa.float64())
    # supply area: most sample points
    area_of_pc = {}
    with open(f, encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            area_of_pc[r["postcode"]] = r["voedingsgebied_id"] or None
    area_names = {}
    with open(f, encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["voedingsgebied_id"]:
                area_names[r["voedingsgebied_id"]] = r["voedingsgebied_naam"]
    votes: dict[int, dict[str, int]] = {}
    for k, p in zip(sample_hex, sample_pc):
        if p >= 0 and (a := area_of_pc.get(pcs[p])):
            votes.setdefault(k, {})[a] = votes.get(k, {}).get(a, 0) + 1
    cols["net_voedingsgebied_id"] = pa.array([max(votes[k], key=votes[k].get) if k in votes else None
                                              for k in range(n)], pa.string())
    source = {"source": "Netbeheer Nederland capaciteitskaart (congestie_pc6.csv), postcodes via CBS PC6 2024 v2; "
                        "no licence stated", "stand": day,
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    per_hex = pa.table({"h3_id": ids, **cols}).replace_schema_metadata({"datacube": json.dumps(source)})
    pq.write_table(per_hex, work / "layers/netcongestie.parquet", compression="zstd")
    pq.write_table(gem_table.replace_schema_metadata({"datacube": json.dumps(source)}),
                   work / "layers/netcongestie_gemeente.parquet")

    # supply areas (MW), current download, one row per area
    def num(v):
        try:
            return float(v.replace(",", ".")) if v not in (None, "") else None
        except ValueError:
            return None
    areas = []
    with open(f.parent / "voedingsgebieden.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            areas.append({
                "voedingsgebied_id": r["voedingsgebied_id"], "naam": area_names.get(r["voedingsgebied_id"]),
                "netbeheerder": r["RNB"], "provincie": r["provincie"], "jaar": int(r["jaar"]) if r["jaar"] else None,
                **{k: num(r[k]) for k in ("aanwezige_transportcapaciteit_afname", "aanwezige_transportcapaciteit_invoeding",
                                          "benodigde_transportcapaciteit_afname", "benodigde_transportcapaciteit_invoeding",
                                          "wachtrij_afname", "wachtrij_invoeding", "unieke_verzoeken_afname",
                                          "unieke_verzoeken_invoeding")},
                "jaartal_opgelost_afname": r["jaartal_opgelost_afname"] or None,
                "jaartal_opgelost_invoeding": r["jaartal_opgelost_invoeding"] or None,
                "stand": day})
    present = set(cols["net_voedingsgebied_id"].to_pylist())
    areas = [a for a in areas if a["voedingsgebied_id"] in present]
    pq.write_table(pa.Table.from_pylist(areas).replace_schema_metadata({"datacube": json.dumps(source)}),
                   work / "layers/netgebieden.parquet")
    known = np.array([x is not None for x in cols["net_afname_kleur_max"].to_pylist()])
    log(f"current {day}: {known.mean():.0%} of hexes with offtake information; red (max) "
        f"{np.mean([x == 3 for x in cols['net_afname_kleur_max'].to_pylist()]):.0%} of hexes; "
        f"{len(areas)} supply areas")
    log(f"-> {work / 'layers/netcongestie.parquet'}, netcongestie_gemeente.parquet ({gem_table.num_rows} rows), "
        f"netgebieden.parquet")


if __name__ == "__main__":
    main()
