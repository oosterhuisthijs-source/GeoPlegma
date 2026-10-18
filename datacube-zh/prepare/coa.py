# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17"]
# ///
"""COA asylum reception capacity per gemeente and snapshot date, 2022-2026 -> side table.

COA publishes no dated series of its locations. What exists:
  - the location map on coa.nl/nl/locatiezoeker: a Leaflet map whose page embeds every location as a point with
    name, "<n> opvangplaatsen" and a marker colour (legend in the same page: oker = Reguliere opvanglocatie,
    petrol = Tijdelijke opvanglocatie). The Internet Archive (Wayback Machine) holds ~30 copies from January 2022
    on (one per month at most is used); earlier copies have names only, so 2018-2021 is not built.
  - the location export coa.nl/nl/locatie-overzicht/export (CSV: name, type, capacity, address), archived once
    (20 July 2026). No coordinates: placed by postcode + house number with PDOK Locatieserver (open, no key).
Both are COA's own figures; no licence is stated on coa.nl. CAPACITY (design places), not occupancy. Crisis
emergency reception run by gemeenten / veiligheidsregio and the gemeentelijke opvang of Ukrainians are not COA
locations and are missing throughout.

  coa.py download   -> <data>/sources/coa/wayback/locatiezoeker_<timestamp>.html, export_<timestamp>.csv

Per gemeente (the cube's static 2024 gemeenten, by the hex holding the point) and snapshot (every gemeente, 0 when
it has no location):
  datum                     date of the archived copy;  bron: "kaart" (map) or "export" (July 2026 CSV)
  coa_locaties, coa_plaatsen                          all locations / places
  coa_locaties_regulier, coa_plaatsen_regulier        regular locations (azc)
  coa_locaties_tijdelijk, coa_plaatsen_tijdelijk      temporary locations (noodopvang, hotels, boats, ...)
  coa_locaties_zonder_aantal                          locations listed without a number of places (not in plaatsen)

Written to <work>/layers/coa_gemeente.parquet; assemble.py ships it as datacube_<region>_coa.parquet (zh: its
gemeenten only). Snapshots are irregular, so a location's opening/closing is known to within months.

Usage: uv run datacube-zh/prepare/coa.py download [--data own_data]
       uv run datacube-zh/prepare/coa.py [--data own_data] [--region nl]
"""
import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import h3
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

CDX = "https://web.archive.org/cdx/search/cdx"
MAP = "https://www.coa.nl/nl/locatiezoeker"
EXPORT = "https://www.coa.nl/nl/locatie-overzicht/export"
PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
COLUMNS = ["coa_locaties", "coa_plaatsen", "coa_locaties_regulier", "coa_plaatsen_regulier", "coa_locaties_tijdelijk",
           "coa_plaatsen_tijdelijk", "coa_locaties_zonder_aantal"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url: str, tries: int = 4) -> bytes:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "datacube-zh"}),
                                        timeout=120) as r:
                return r.read()
        except OSError as e:  # the Wayback Machine drops connections now and then
            if i == tries - 1:
                raise
            log(f"  retry {url}: {e}")
            time.sleep(10 * (i + 1))


def snapshots(url: str, start: str) -> list[str]:
    q = urllib.parse.urlencode({"url": url, "from": start, "output": "json", "filter": "statuscode:200",
                                "collapse": "timestamp:6"})  # at most one per month
    return [r[1] for r in json.loads(get(f"{CDX}?{q}"))[1:]]


def download(src: Path):
    (src / "wayback").mkdir(parents=True, exist_ok=True)
    for name, url, start in (("locatiezoeker", MAP, "2022"), ("export", EXPORT, "2022")):
        ext = "csv" if name == "export" else "html"
        for ts in snapshots(url, start):
            f = src / "wayback" / f"{name}_{ts}.{ext}"
            if not f.exists():
                f.write_bytes(get(f"https://web.archive.org/web/{ts}id_/{url}"))
                log(f"{f.name}: {f.stat().st_size / 1e3:.0f} kB")


def map_locations(f: Path) -> list[dict]:
    """Points from the map page's Drupal settings JSON."""
    s = f.read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>', s, re.S)
    settings = json.loads(m.group(1))
    legend = {}
    for item in re.findall(r'\{"name":"([^"]+)","icon":"[^"]*marker-(\w+)-', m.group(1)):
        legend[item[1]] = "regulier" if item[0].startswith("Reguliere") else "tijdelijk"
    out, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("features"), list):
                for ft in o["features"]:
                    if "lat" not in ft or ft.get("entity_id") in seen:
                        continue
                    seen.add(ft.get("entity_id"))
                    popup = ft.get("popup")
                    popup = popup.get("value", "") if isinstance(popup, dict) else popup or ""
                    n = re.search(r"(\d[\d.]*)\s+opvangplaatsen", popup)
                    colour = re.search(r"marker-(\w+)-", json.dumps(ft.get("icon", "")))
                    out.append({"lat": float(ft["lat"]), "lon": float(ft["lon"]),
                                "naam": html.unescape(re.sub(r"<[^>]+>", "", popup.split("<br")[0])).strip(" -"),
                                "plaatsen": int(n.group(1).replace(".", "")) if n else None,
                                "type": legend.get(colour.group(1)) if colour else None})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(settings)
    return out


def export_locations(f: Path, cache: Path) -> list[dict]:
    """Rows of the export CSV, placed by postcode + house number (PDOK Locatieserver, cached)."""
    geo = json.loads(cache.read_text()) if cache.exists() else {}
    out = []
    with open(f, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            lines = [x.strip() for x in r["Adres"].splitlines() if x.strip()]
            pc = re.match(r"(\d{4})\s?([A-Z]{2})", lines[1]) if len(lines) > 1 else None
            nr = re.search(r"\s(\d+)", lines[0])
            key = f"{pc.group(1)}{pc.group(2)} {nr.group(1)}" if pc and nr else lines[0]
            if key not in geo:
                q = (f"postcode:{pc.group(1)}{pc.group(2)} and huisnummer:{nr.group(1)}" if pc and nr
                     else " ".join(lines[:2]))
                docs = json.loads(get(f"{PDOK}?" + urllib.parse.urlencode(
                    {"q": q, "fq": "type:adres", "fl": "centroide_ll", "rows": 1})))["response"]["docs"]
                if not docs and pc:  # house number not found: the postcode's centre
                    docs = json.loads(get(f"{PDOK}?" + urllib.parse.urlencode(
                        {"q": f"postcode:{pc.group(1)}{pc.group(2)}", "fq": "type:postcode", "fl": "centroide_ll",
                         "rows": 1})))["response"]["docs"]
                geo[key] = docs[0]["centroide_ll"] if docs else None
            if geo[key] is None:
                log(f"  export: {r['Locatienaam']}: address not found ({key}), skipped")
                continue
            lon, lat = map(float, re.findall(r"[-\d.]+", geo[key]))
            n = r["Aantal opvangplaatsen"].strip()
            out.append({"lat": lat, "lon": lon, "naam": r["Locatienaam"], "plaatsen": int(n) if n.isdigit() else None,
                        "type": {"Regulier": "regulier", "Tijdelijk": "tijdelijk"}.get(r["Type locatie"].strip())})
    cache.write_text(json.dumps(geo, indent=0))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    args = ap.parse_args()
    src = Path(args.data) / "sources/coa"
    if args.step == "download":
        return download(src)

    work = Path(args.data) / regions.load(args.region)["work"]
    admin = pq.read_table(work / "layers/admin.parquet", columns=["h3_id", "gemeentecode", "gemeentenaam"])
    gem = dict(zip(admin["h3_id"].to_pylist(), admin["gemeentecode"].to_pylist()))
    names = {c: n for c, n in zip(admin["gemeentecode"].to_pylist(), admin["gemeentenaam"].to_pylist())
             if c != "GM0998"}

    def gemeente(lat, lon):
        cell = h3.latlng_to_cell(lat, lon, 9)
        for k in range(4):  # a boat or a point at the edge: nearest hex with a gemeente
            hits = [gem[c] for c in h3.grid_ring(cell, k) if gem.get(c) not in (None, "GM0998")]
            if hits:
                return max(set(hits), key=hits.count)
        return None

    rows = []
    files = [(f, "kaart") for f in sorted((src / "wayback").glob("locatiezoeker_*.html"))]
    files += [(f, "export") for f in sorted((src / "wayback").glob("export_*.csv"))]
    for f, bron in files:
        ts = f.stem.split("_")[-1]
        locs = map_locations(f) if bron == "kaart" else export_locations(f, src / "pdok_geocode.json")
        if not locs:
            log(f"{f.name}: no locations with coordinates, skipped")
            continue
        acc = {c: dict.fromkeys(COLUMNS, 0) for c in names}
        outside = 0
        for loc in locs:
            c = gemeente(loc["lat"], loc["lon"])
            if c is None:
                outside += 1
                continue
            a = acc[c]
            a["coa_locaties"] += 1
            if loc["plaatsen"] is None:
                a["coa_locaties_zonder_aantal"] += 1
            else:
                a["coa_plaatsen"] += loc["plaatsen"]
            if loc["type"]:
                a[f"coa_locaties_{loc['type']}"] += 1
                a[f"coa_plaatsen_{loc['type']}"] += loc["plaatsen"] or 0
        datum = dt.date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
        for c, a in acc.items():
            rows.append((datum, bron, c, names[c], *a.values()))
        tot = {k: sum(a[k] for a in acc.values()) for k in COLUMNS}
        log(f"{datum} {bron}: {tot['coa_locaties']} locations ({tot['coa_locaties_regulier']} regulier, "
            f"{tot['coa_locaties_tijdelijk']} tijdelijk, {tot['coa_locaties_zonder_aantal']} without a number), "
            f"{tot['coa_plaatsen']:,} places, {sum(a['coa_locaties'] > 0 for a in acc.values())} gemeenten"
            + (f"; {outside} outside the grid" if outside else ""))

    cols = list(zip(*rows))
    out = pa.table({"datum": pa.array(cols[0], pa.date32()), "bron": pa.array(cols[1], pa.string()),
                    "gemeentecode": pa.array(cols[2], pa.string()), "gemeentenaam": pa.array(cols[3], pa.string()),
                    **{c: pa.array(cols[4 + i], pa.int32()) for i, c in enumerate(COLUMNS)}})
    source = {"source": "COA locatiezoeker (Wayback Machine copies, 2022-) + locatie-overzicht export (2026-07-20), "
                        "export addresses placed with PDOK Locatieserver; no licence stated by COA",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    dest = work / "layers/coa_gemeente.parquet"
    pq.write_table(out.replace_schema_metadata({"datacube": json.dumps(source)}), dest)
    log(f"-> {dest} ({out.num_rows} rows)")


if __name__ == "__main__":
    main()
