# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "h3>=4", "pyarrow>=17", "shapely>=2"]
# ///
"""Housing of permit holders (taakstelling huisvesting vergunninghouders) per gemeente and year -> side table.

Source: "Overzicht huisvesting vergunninghouders 1 januari <Y+1>" (Ministerie van JenV / COA, monthly PDF on
open.overheid.nl, one table per province, every gemeente; Rijksoverheid open data). The 1 January file of year Y+1
closes year Y and uses year Y's gemeenten. The ministry sets a target (taakstelling) per gemeente per half year,
proportional to population; gemeenten house that many permit holders (statushouders) or carry the difference over.

  vergunninghouders.py download   -> <data>/sources/vergunninghouders/overzicht_<Y+1>0101_<published>.pdf
                                     (open.overheid.nl search API; a re-issued file wins over the first)

Per gemeente and year (persons; stand: + = achterstand/backlog, - = voorsprong/ahead):
  vh_stand_begin          stand on 1 January (carried over from earlier years)
  vh_taakstelling_h1/_h2  target for the first / second half year
  vh_realisatie_h1/_h2    permit holders housed in the first / second half year
  vh_taakstelling         h1 + h2;   vh_realisatie   h1 + h2
  vh_correcties           corrections the ministry applied during the year (listed from 2023; before, the 1 July
                          stand minus begin + target h1 - housed h1; 2018: corrected minus reported 1 July stand)
  vh_stand_eind           stand on 1 January of the next year (= begin + taakstelling + correcties - realisatie)

Not hexes: the cube carries one static gemeente per hex (Wijk- en Buurtkaart 2024), so earlier gemeenten are mapped
onto their 2024 gemeente by hex centres (year-Y gemeente polygon -> hex -> 2024 gemeentecode), weighted by that
year's inhabitants (CBS 100 m squares in the cube) when a gemeente was split (Haaren 2021). All columns are sums, so
merged gemeenten simply add. Written to <work>/layers/vergunninghouders_gemeente.parquet; assemble.py ships it as
datacube_<region>_gemeente.parquet (zh: its gemeenten only). Join on gemeentecode.

Usage: uv run datacube-zh/prepare/vergunninghouders.py download [--data own_data]
       uv run datacube-zh/prepare/vergunninghouders.py [--data own_data] [--region nl] [--years 2018-2025]
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402
import vee  # noqa: E402  (yearly gemeente polygons)

SEARCH = "https://open.overheid.nl/overheid/openbaarmakingen/api/v0/zoek"
COLUMNS = ["vh_stand_begin", "vh_taakstelling_h1", "vh_realisatie_h1", "vh_taakstelling_h2", "vh_realisatie_h2",
           "vh_taakstelling", "vh_realisatie", "vh_correcties", "vh_stand_eind"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def download(src: Path):
    src.mkdir(parents=True, exist_ok=True)
    hits, start = [], 0
    while True:
        q = urllib.parse.urlencode({"zoektekst": '"Overzicht huisvesting vergunninghouders"', "start": start,
                                    "aantalResultaten": 50})
        with urllib.request.urlopen(f"{SEARCH}?{q}", timeout=60) as r:
            d = json.load(r)
        for res in d["resultaten"]:
            doc = res["document"]
            m = re.search(r"vergunninghouders\s+1\s+januari\s+(20\d\d)", doc["titel"], re.I)
            if m:
                hits.append((m.group(1), doc["openbaarmakingsdatum"], doc["id"]))
        start += 50
        if start >= d["totaal"]:
            break
    for year, published, doc_id in sorted(hits):
        f = src / f"overzicht_{year}0101_{published}.pdf"
        if f.exists():
            continue
        url = "https://open.overheid.nl/documenten/" + re.sub(r"_\d+$", "", doc_id) + "/pdf"
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        if not data.startswith(b"%PDF"):
            raise SystemExit(f"{url}: not a PDF ({data[:80]!r})")
        f.write_bytes(data)
        log(f"{f.name}: {len(data) / 1e6:.1f} MB")


# overview spelling -> CBS spelling (both normalised by norm())
ALIASES = {"dantumadeel": "dantumadiel", "ferwerderadeel": "ferwerderadiel", "kollumerland": "kollumerlandennieuwkruisland",
           "tytjerksteradiel": "tytsjerksteradiel", "nuenenca": "nuenengerwenennederwetten",
           "haarlemmerliedeca": "haarlemmerliedeenspaarnwoude", "bodegravenreewijk": "bodegravenreeuwijk",
           "nordeastfryslan": "noardeastfryslan", "vijherenlanden": "vijfheerenlanden",
           "vijheerenlanden": "vijfheerenlanden",  # typos in the 2019 and 2023 files
           # 2024 and 2025 files drop the province suffix CBS uses for same-named gemeenten
           "beek": "beekl", "stein": "steinl", "laren": "larennh", "hengelo": "hengeloo", "middelburg": "middelburgz",
           "rijswijk": "rijswijkzh"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace(" a/d ", " aan den ")
    s = re.sub(r"[^a-z0-9]+", "", s)  # Bergen (L.) == Bergen L
    s = s.replace("aanden", "aande")  # a/d IJssel, a/d Rijn, a/d Geul
    return ALIASES.get(s, s)


ROW = re.compile(r"^(\S.*?)\s{2,}(-?\d+(?:\s+-?\d+)+)\s*$")


def parse(pdf: Path) -> dict[str, dict]:
    """{gemeente name: COLUMNS} from one 1 January overview; checks the table's own sums on every row."""
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    if "Gecorrigeerde" in text:     # 2018: taak1 real1 stand_jul gecorr_jul taak2 incl m1-6 real_tot av
        layout = "2018"
    elif "Correcties" in text:      # 2023-: stand_jan taak1 real1 stand_jul taak2 corr incl m1-6 real_tot av
        layout = "corr"
    else:                           # 2019-2022: stand_jan taak1 real1 stand_jul taak2 incl m1-6 real_tot av
        layout = "plain"
    width = {"2018": 14, "corr": 15, "plain": 14}[layout]
    out = {}
    for line in text.splitlines():
        m = ROW.match(line)
        if not m or m.group(1).lower().startswith(("totaal", "aantal", "pagina")):
            continue
        v = [int(x) for x in m.group(2).split()]
        if len(v) < width:
            continue
        v = v[:width]  # the last 1-2 numbers repeat achter/voor as achterstand or voorsprong
        if layout == "2018":
            t1, r1, sj, sjc, t2, incl, *months, rtot, av = v
            corr = sjc - sj
            sb = sj - t1 + r1
        elif layout == "corr":
            sb, t1, r1, sj, t2, corr, incl, *months, rtot, av = v
        else:
            sb, t1, r1, sj, t2, incl, *months, rtot, av = v
            corr = 0
        name = m.group(1).strip()
        if incl != sj + corr + t2 or rtot != sum(months):
            raise SystemExit(f"{pdf.name}: {name}: the row's own sums don't add up {v}")
        # unlisted corrections (2019-2022): a 1 July stand that is not begin + target h1 - housed h1, or a year-end
        # stand that is not target incl. stand - housed (2021: 5 gemeenten, e.g. Rotterdam -41). The published
        # stands are what the ministry carries over, so they are kept and the difference counts as a correction.
        corr += (sj - (sb + t1 - r1)) + (av - (incl - rtot))
        if av != incl - rtot:
            log(f"  {pdf.name}: {name}: unlisted year-end correction {av - (incl - rtot):+d}")
        out[name] = dict(zip(COLUMNS, [sb, t1, r1, t2, rtot, t1 + t2, r1 + rtot, corr, av]))
    return out


def names(data: Path, year: int) -> dict[str, str]:
    """{normalised gemeente name: code} from the year's Wijk- en Buurtkaart, cached as JSON."""
    src = data / "sources/wijkbuurt"
    cache = src / f"gemeentenamen_{year}_{vee.VERSIONS[year]}.json"
    if not cache.exists():
        gpkg = sorted(src.glob(f"**/*{year}_{vee.VERSIONS[year]}*.gpkg"), key=lambda p: len(str(p)))[0]
        layers = subprocess.run(["ogrinfo", "-q", str(gpkg)], capture_output=True, text=True, check=True).stdout
        layer = next(line.split()[1] for line in layers.splitlines() if "gemeente" in line.lower())
        rows = subprocess.run(["ogr2ogr", "-f", "CSV", "/vsistdout/", "-dialect", "sqlite", "-sql",
                               f'SELECT DISTINCT gemeentecode, gemeentenaam FROM "{layer}"', str(gpkg)],
                              capture_output=True, text=True, check=True).stdout.splitlines()[1:]
        cache.write_text(json.dumps(dict(r.split(",", 1) for r in rows), ensure_ascii=False, indent=0))
    return {norm(n.strip('"')): c for c, n in json.loads(cache.read_text()).items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["download", "build"], default="build")
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    ap.add_argument("--years", default="2018-2025")
    args = ap.parse_args()
    data = Path(args.data)
    src = data / "sources/vergunninghouders"
    if args.step == "download":
        return download(src)
    a, _, b = args.years.partition("-")
    years = list(range(int(a), int(b or a) + 1))

    work = data / regions.load(args.region)["work"]
    admin = pq.read_table(work / "layers/admin.parquet", columns=["h3_id", "gemeentecode", "gemeentenaam"])
    ids = admin["h3_id"].to_pylist()
    code24 = np.array(admin["gemeentecode"].to_pylist(), object)
    name24 = dict(zip(admin["gemeentecode"].to_pylist(), admin["gemeentenaam"].to_pylist()))
    lat, lon = np.array([h3.cell_to_latlng(c) for c in ids]).T
    pts = shapely.points(lon, lat)

    rows = []
    for year in years:
        files = sorted(src.glob(f"overzicht_{year + 1}0101_*.pdf"))
        if not files:
            log(f"{year}: no 1 January {year + 1} overview, left out")
            continue
        table = parse(files[-1])  # latest publication (a re-issue corrects the first)
        lookup = names(data, year)
        missing = [n for n in table if norm(n) not in lookup]
        if missing:
            raise SystemExit(f"{year}: gemeenten not in the Wijk- en Buurtkaart: {missing}")
        # year-Y gemeente -> 2024 gemeente, weighted by inhabitants of year Y (hex count where there are none)
        codes, polys = vee.gemeenten(data, year)
        hit_pt, hit_poly = shapely.STRtree(polys).query(pts, predicate="within")
        cbs = pq.read_table(work / f"layers/cbs_{year}.parquet", columns=["h3_id", "aantal_inwoners_sum"])
        inw = dict(zip(cbs["h3_id"].to_pylist(), cbs["aantal_inwoners_sum"].to_pylist()))
        pop, cnt = {}, {}
        for p, g in zip(hit_pt, hit_poly):
            key = (codes[g], code24[p])
            pop[key] = pop.get(key, 0.0) + (inw.get(ids[p]) or 0.0)
            cnt[key] = cnt.get(key, 0) + 1
        to24: dict[str, dict[str, float]] = {}
        for (cy, c24), n in cnt.items():
            to24.setdefault(cy, {})[c24] = pop[(cy, c24)]
        for cy in to24:
            parts = {c24: p for c24, p in to24[cy].items() if c24 != "GM0998"}  # Buitenland: border slivers
            tot = sum(parts.values())
            w = {c: p / tot if tot > 0 else cnt[(cy, c)] / sum(cnt[(cy, k)] for k in parts) for c, p in parts.items()}
            w = {c: x for c, x in w.items() if x >= 0.005}  # boundary slivers between neighbours
            to24[cy] = {c: x / sum(w.values()) for c, x in w.items()}  # totals per gemeente kept exactly
        acc: dict[str, np.ndarray] = {}
        for name, vals in table.items():
            cy = lookup[norm(name)]
            parts = to24.get(cy)
            if not parts:
                raise SystemExit(f"{year}: {name} ({cy}) has no hex centre in the region grid")
            for c24, w in parts.items():
                acc.setdefault(c24, np.zeros(len(COLUMNS)))
                acc[c24] += w * np.array([vals[c] for c in COLUMNS], float)
        for c24, v in acc.items():
            rows.append((year, c24, name24[c24], *np.round(v, 2)))
        tot = {c: sum(t[c] for t in table.values()) for c in ("vh_taakstelling", "vh_realisatie")}
        log(f"{year}: {files[-1].name}: {len(table)} gemeenten -> {len(acc)} of 2024; target {tot['vh_taakstelling']:,}"
            f", housed {tot['vh_realisatie']:,}, backlog end {sum(t['vh_stand_eind'] for t in table.values()):,}")

    cols = list(zip(*rows))
    out = pa.table({"year_int": pa.array(cols[0], pa.int32()), "gemeentecode": pa.array(cols[1], pa.string()),
                    "gemeentenaam": pa.array(cols[2], pa.string()),
                    **{c: pa.array(cols[3 + i], pa.float64()) for i, c in enumerate(COLUMNS)}})
    source = {"source": "Ministerie van JenV / COA, Overzicht huisvesting vergunninghouders 1 januari <jaar+1> "
                        "(open.overheid.nl); gemeenten mapped to Wijk- en Buurtkaart 2024 (inhabitant-weighted)",
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    dest = work / "layers/vergunninghouders_gemeente.parquet"
    pq.write_table(out.replace_schema_metadata({"datacube": json.dumps(source)}), dest)
    log(f"-> {dest} ({out.num_rows} rows)")


if __name__ == "__main__":
    main()
