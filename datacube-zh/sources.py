#!/usr/bin/env python3
"""Check every datacube source for a newer version than the one the cube is built from.

The version in use is read from where the pipeline itself pins it (prepare scripts and configs),
so there is no second list to keep in sync; the version available is read from the publisher
(file listings, ATOM feeds, download pages, WCS capabilities, HTTP Last-Modified).

    source        in use (pinned in)                     available (checked at)
    cbs_vk100     prepare/cbs.sh URLS                    CBS dossier page + geodata.cbs.nl listing
    wijkbuurt     prepare/wb.py VERSIONS, wijkbuurt.sh   geodata.cbs.nl/files/Wijkenbuurtkaart/
    leefbaarometer prepare/lbm.py URL (meting-YYYY)      leefbaarometer.nl open-data page
    nbbg          config/bbg.toml (NBBGyyyy vN)          geodata.cbs.nl/files/Bodemgebruik/
    brp           assemble.py YEARS                      PDOK ATOM feed (definitief_YYYY)
    lgn           config/lgn.toml years                  lgn.nl (download list in the app bundle)
    tessera       work/layers/tessera_<year>.parquet     geotessera registry (own_data/tessera/.venv)
    bag           sources/bag/9999VBO<ddmmyyyy>.zip      PDOK extract Last-Modified
    3dbag         sources/static/3dbag-pand.parquet      source.coop mirror Last-Modified
    rivm_<layer>  config/static.toml WCS coverage id     WCS GetCapabilities: same id with newer dates
    wind          prepare/wind.py LAYER                  RIVM WFS GetCapabilities: same layer with a newer date
    osm           work/layers/osm.parquet timestamp      Geofabrik extract Last-Modified (only listed: OSM
                                                         changes daily, refresh when convenient; also
                                                         used by prepare/osm_extra.py)
    ndw           sources/ndw/last_modified.txt          NDW charging points Last-Modified (only listed:
                                                         updated daily, refresh with laadpalen.py --refresh)

Prints a table and writes <data>/sources_check.json. "NEW" = a newer version for a year the cube
has (or, for CBS vk100, a new year: that is what extends the cube); "+" = a year after the cube's
last year, listed but not alarming. Exit code 10 if anything NEW (for timers/cron), 2 if only checks
failed, 0 otherwise. Acting on NEW: re-run that prepare step, assemble, coarsen, release.

Usage: datacube-zh/sources.py [--data own_data] [--only cbs_vk100,lgn] [--json]
"""
import argparse
import datetime as dt
import email.utils
import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "datacube-zh source check (GeoPlegma)"}


def get(url: str, timeout: int = 60) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def last_modified(url: str) -> dt.datetime:
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return email.utils.parsedate_to_datetime(r.headers["Last-Modified"])


def vrank(v: str) -> int:
    """CBS version order: v0 < v1 < v2 < v3 < vol (final)."""
    return 99 if v == "vol" else int(v.lstrip("v"))


def result(in_use, available, newer: list[str], note: str = "", ahead: list[str] | None = None) -> dict:
    """`newer`: refines what the cube has (act on it); `ahead`: years after the cube's last year,
    which only matter once the cube is extended (CBS vk100 publishing that year drives that)."""
    return {"in_use": in_use, "available": available, "newer": newer, "ahead": ahead or [], "note": note}


# --- checks ---------------------------------------------------------------------------------------

def check_cbs_vk100(data: Path) -> dict:
    used = dict(re.findall(r"\[(\d{4})\]=\"[^\"]*cbs_vk100_\d{4}_(v\d|vol)", (HERE / "prepare/cbs.sh").read_text()))
    used = {int(y): v for y, v in used.items()}
    text = get("https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/"
               "kaart-van-100-meter-bij-100-meter-met-statistieken")
    root = "https://geodata.cbs.nl/files/Vierkant_statistiek/"
    for year in re.findall(r'HREF="/files/Vierkant_statistiek/(\d{4})/"', get(root)):
        text += get(f"{root}{year}/")
    avail = {}
    for y, v in re.findall(r"cbs_vk100_(\d{4})_(v\d|vol)", text):
        if int(y) >= min(used) and vrank(v) > vrank(avail.get(int(y), "v0")):
            avail[int(y)] = v
    newer = [f"{y} {v}" + (f" (in cube: {used[y]})" if y in used else " (new year)")
             for y, v in sorted(avail.items()) if y not in used or vrank(v) > vrank(used[y])]
    fmt = lambda d: ", ".join(f"{y} {v}" for y, v in sorted(d.items()) if y >= max(d) - 2)  # noqa: E731
    return result(fmt(used), fmt(avail), newer, "a new year extends the cube; a newer version of a year refines it")


def check_wijkbuurt(data: Path) -> dict:
    src = (HERE / "prepare/wb.py").read_text()
    used = {int(y): int(v) for y, v in re.findall(r"(\d{4}): \"v(\d)\"", src.split("VERSIONS", 1)[1].split("}", 1)[0])}
    admin = re.search(r'VERSION="(\d{4})_v(\d)"', (HERE / "prepare/wijkbuurt.sh").read_text()).groups()
    avail = {}
    for y, v in re.findall(r"WijkBuurtkaart_(\d{4})_v(\d)", get("https://geodata.cbs.nl/files/Wijkenbuurtkaart/")):
        if int(y) >= min(used):
            avail[int(y)] = max(avail.get(int(y), -1), int(v))
    last = max(cube_years())
    newer = [f"{y} v{v}" + (f" (in cube: v{used[y]})" if y in used else " (new year)")
             for y, v in sorted(avail.items()) if v > used.get(y, -1) and y <= last]
    ahead = [f"{y} v{v}" for y, v in sorted(avail.items()) if y > last]
    if avail.get(int(admin[0]), -1) > int(admin[1]):
        newer.append(f"admin boundaries: {admin[0]} v{avail[int(admin[0])]} (in cube: v{admin[1]})")
    fmt = lambda d: ", ".join(f"{y} v{v}" for y, v in sorted(d.items()) if y >= max(d) - 2)  # noqa: E731
    return result(fmt(used) + f"; admin {admin[0]} v{admin[1]}", fmt(avail), newer,
                  "v0/v1 carry few indicators; income arrives in later versions", ahead)


def check_leefbaarometer(data: Path) -> dict:
    used = int(re.search(r"meting-(\d{4})", (HERE / "prepare/lbm.py").read_text()).group(1))
    avail = max(int(y) for y in re.findall(r"meting-(\d{4})", get("https://www.leefbaarometer.nl/page/Opendata")))
    return result(f"meting {used}", f"meting {avail}", [f"meting {avail}"] if avail > used else [],
                  "measured every 2 years")


def check_nbbg(data: Path) -> dict:
    cfg = (HERE / "config/bbg.toml").read_text()
    edition, version = re.search(r"NBBG(\d{4})_v(\d)", cfg).groups()
    listing = get("https://geodata.cbs.nl/files/Bodemgebruik/")
    editions = [int(e) for e in re.findall(r"/Bodemgebruik/NBBG(\d{4})/", listing)]
    latest = max(editions)
    versions = [int(v) for v in re.findall(r"NBBG\d{4}_v(\d)", get(f"https://geodata.cbs.nl/files/Bodemgebruik/NBBG{latest}/"))]
    newer = []
    if latest > int(edition):
        newer.append(f"NBBG{latest} (in cube: NBBG{edition})")
    elif versions and max(versions) > int(version):
        newer.append(f"NBBG{edition} v{max(versions)} (in cube: v{version})")
    return result(f"NBBG{edition} v{version}", f"NBBG{latest} v{max(versions) if versions else '?'}", newer,
                  "new editions every ~2 years")


def cube_years() -> list[int]:
    m = re.search(r"YEARS = range\((\d{4}), (\d{4})\)", (HERE / "assemble.py").read_text())
    return list(range(int(m.group(1)), int(m.group(2))))


def check_brp(data: Path) -> dict:
    used = max(cube_years())
    feed = get("https://service.pdok.nl/rvo/gewaspercelen/atom/basisregistratie_gewaspercelen_brp.xml")
    final = max(int(y) for y in re.findall(r"brpgewaspercelen_definitief_(\d{4})", feed))
    concept = [int(y) for y in re.findall(r"gewaspercelen_concept_(\d{4})", feed)]
    note = f"concept {max(concept)} also published" if concept else ""
    return result(f"definitief to {used}", f"definitief to {final}", [], note,
                  [f"definitief {y}" for y in range(used + 1, final + 1)])


def check_lgn(data: Path) -> dict:
    used = tomllib.loads((HERE / "config/lgn.toml").read_text())["years"]
    page = get("https://lgn.nl")
    text = page + "".join(get("https://lgn.nl" + s) for s in re.findall(r'src="(/assets/[^"]+\.js)"', page))
    avail = sorted({int(y) for y in re.findall(r"LGN(20\d\d)", text)})
    last = max(cube_years())
    return result(f"LGN{min(used)}-{max(used)}", f"LGN{avail[0]}-{avail[-1]}" if avail else "?",
                  [f"LGN{y}" for y in avail if max(used) < y <= last], "download manually from lgn.nl into own_data/lgn/",
                  [f"LGN{y}" for y in avail if y > last])


def check_tessera(data: Path) -> dict:
    used = sorted(int(p.stem.split("_")[1]) for p in (data / "work/layers").glob("tessera_[0-9]*.parquet"))
    py = (data / "tessera/.venv/bin/python").absolute()
    if not py.exists():
        raise RuntimeError(f"{py} missing (geotessera venv)")
    out = subprocess.run([str(py), "-I", "-c", "from geotessera import GeoTessera; "
                          "print(GeoTessera(dataset_variant='cambridge').registry.get_available_years())"],
                         capture_output=True, text=True, timeout=300, cwd="/tmp")
    avail = json.loads(out.stdout.strip().splitlines()[-1])
    last = max(cube_years())
    return result(f"{min(used)}-{max(used)} (zh)", f"{min(avail)}-{max(avail)}",
                  [str(y) for y in avail if max(used) < y <= last], "cambridge variant, v1.1",
                  [str(y) for y in avail if y > last])


def check_bag(data: Path) -> dict:
    local = sorted((data / "sources/bag").glob("9999VBO*.zip"))
    if not local:
        raise RuntimeError("no sources/bag/9999VBO<date>.zip")
    d = re.search(r"9999VBO(\d{8})", local[-1].name).group(1)
    have = dt.date(int(d[4:]), int(d[2:4]), int(d[:2]))
    remote = last_modified("https://service.pdok.nl/kadaster/bag/atom/downloads/lvbag-extract-nl.zip").date()
    # The cube uses the state on 1 January per year: a fresher extract only adds something once it
    # covers a new 1 January that the cube's years need.
    need = max(cube_years()) + 1
    newer = [f"extract {remote} covers 1 Jan {remote.year}"] if remote.year > have.year and remote.year <= need else []
    return result(f"extract {have}", f"extract {remote}", newer, "monthly extract; only a new 1 January matters")


def check_3dbag(data: Path) -> dict:
    local = data / "sources/static/3dbag-pand.parquet"
    have = dt.datetime.fromtimestamp(local.stat().st_mtime, dt.timezone.utc)
    remote = last_modified("https://data.source.coop/cholmes/portolan-nl/tudelft/3dbag/3dbag-pand.parquet")
    return result(f"downloaded {have.date()}", f"mirror updated {remote.date()}",
                  [f"mirror updated {remote.date()}"] if remote > have else [], "Portolan NL mirror of 3DBAG")


def check_rivm(data: Path) -> dict:
    """RIVM WCS layers: coverage ids carry their dates/years, so a newer edition is a sibling id."""
    static = tomllib.loads((HERE / "config/static.toml").read_text())
    layers = [r for r in static.get("raster", []) if "data.rivm.nl" in r.get("wcs", "")]
    caps = {}
    for wcs in {r["wcs"] for r in layers}:
        caps[wcs] = re.findall(r"CoverageId>([^<]+)<", get(f"{wcs}?service=WCS&request=GetCapabilities&version=2.0.1"))
    nums = lambda s: tuple(int(n) for n in re.findall(r"\d+", s))  # noqa: E731
    stem = lambda s: re.sub(r"\d+", "#", s)  # noqa: E731
    # Several editions of one map may be in use side by side; compare against the newest of them.
    newest_used = {}
    for r in layers:
        k = (r["wcs"], stem(r["coverage"]))
        newest_used[k] = max(newest_used.get(k, r["coverage"]), r["coverage"], key=nums)
    used, avail, newer = [], [], []
    for r in layers:
        cov = r["coverage"]
        if cov != newest_used[(r["wcs"], stem(cov))]:
            continue
        sib = [c for c in caps[r["wcs"]] if stem(c) == stem(cov)]
        best = max(sib, key=nums) if sib else cov
        used.append(f"{r['name']}: {cov}")
        if nums(best) > nums(cov):
            avail.append(f"{r['name']}: {best}")
            newer.append(f"{r['name']}: {best} (in cube: {cov})")
    return result(f"{len(layers)} layers", "; ".join(avail) or "no newer editions", newer,
                  "raster layers on data.rivm.nl (noise, heat, green, water storage, light)")


def check_wind(data: Path) -> dict:
    used = re.search(r'^LAYER = "([^"]+)"', (HERE / "prepare/wind.py").read_text(), re.M)[1]
    names = re.findall(r"<Name>alo:(rivm_\d{8}_windturbines_vermogen)</Name>",
                       get("https://data.rivm.nl/geo/alo/wfs?service=WFS&request=GetCapabilities"), re.I)
    best = max(names + [used], key=lambda n: re.search(r"\d{8}", n)[0])
    return result(used, best, [best] if best != used else [], "RIVM wind turbine snapshot (static layer)")


def check_osm(data: Path) -> dict:
    import pyarrow.parquet as pq  # only this check needs it
    f = data / "work/layers/osm.parquet"
    used = json.loads(pq.read_schema(f).metadata[b"datacube"])["osm_timestamp"] if f.exists() else "not prepared"
    remote = last_modified("https://download.geofabrik.de/europe/netherlands-latest.osm.pbf")
    return result(f"extract {used[:10]}", f"extract {remote.date()}", [],
                  "OpenStreetMap changes daily; not flagged NEW, refresh with prepare/osm.py and osm_extra.py "
                  "when convenient")


def check_ndw(data: Path) -> dict:
    f = data / "sources/ndw/last_modified.txt"
    used = f.read_text().strip()[:10] if f.exists() else "not prepared"
    remote = last_modified("https://opendata.ndw.nu/charging_point_locations.geojson.gz")
    return result(f"snapshot {used}", f"snapshot {remote.date()}", [],
                  "NDW charging points change daily; not flagged NEW, refresh with prepare/laadpalen.py --refresh")


def check_gdn(data: Path) -> dict:
    """RIVM historical deposition series: a new edition (whole series recalculated) every October."""
    local = sorted(re.findall(r"HistorischeReeksStikstofdepositie-ASCIIGrid_(\d{8})",
                              " ".join(p.name for p in (data / "sources/rivm_gdn").glob("*.zip"))))
    used = local[-1] if local else "not prepared"
    base = "https://data.rivm.nl/data/stikstof/Stikstof%20Natuur/"
    editions = []
    for y in re.findall(r'href="(\d{4})/"', get(base)):
        if int(y) >= 2025:
            editions += re.findall(r"HistorischeReeksStikstofdepositie-ASCIIGrid_(\d{8})\.zip", get(f"{base}{y}/"))
    latest = max(editions) if editions else "?"
    newer = [f"edition {latest}"] if editions and latest > used else []
    return result(f"edition {used}", f"edition {latest}", newer, "new edition: update GDN_EDITION in prepare/rivm.py")


def check_gcn(data: Path) -> dict:
    """RIVM GCN concentration maps: one round per year (published around March)."""
    years = cube_years()
    have = [y for y in years if (data / f"sources/rivm_gcn/conc_NO2_{y}.zip").exists()]
    missing = [str(y) for y in years if y not in have]
    ahead = []
    for y in range(years[-1] + 1, dt.date.today().year + 1):
        try:
            last_modified(f"https://data.rivm.nl/data/gcn/conc_NO2_{y}.zip")
            ahead.append(str(y))
        except Exception:  # noqa: BLE001 - not published (404) or no Last-Modified
            pass
    return result(f"{have[0] if have else '-'}-{have[-1] if have else '-'}", f"+{','.join(ahead) or 'none'} after the cube",
                  missing, "missing cube years: run prepare/rivm.py --download", ahead)


CHECKS = {
    "cbs_vk100": check_cbs_vk100, "wijkbuurt": check_wijkbuurt, "leefbaarometer": check_leefbaarometer,
    "nbbg": check_nbbg, "brp": check_brp, "lgn": check_lgn, "tessera": check_tessera,
    "bag": check_bag, "3dbag": check_3dbag, "rivm": check_rivm, "wind": check_wind, "osm": check_osm,
    "ndw": check_ndw, "gdn": check_gdn, "gcn": check_gcn,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--only", help="comma-separated check names")
    ap.add_argument("--json", action="store_true", help="print the JSON report instead of the table")
    args = ap.parse_args()
    data = Path(args.data)
    names = args.only.split(",") if args.only else list(CHECKS)

    def run(name):
        try:
            return name, CHECKS[name](data)
        except Exception as e:  # a broken publisher page must not hide the other checks
            return name, {"error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(8) as pool:
        results = dict(pool.map(run, names))

    report = {"checked": dt.datetime.now().isoformat(timespec="seconds"), "sources": results}
    (data / "sources_check.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        for name, r in results.items():
            if "error" in r:
                print(f"?? {name:15s} check failed: {r['error']}")
                continue
            mark = "NEW" if r["newer"] else "ok "
            print(f"{mark} {name:15s} in cube: {r['in_use']}")
            print(f"    {'':15s} available: {r['available']}")
            for n in r["newer"]:
                print(f"    {'':15s} -> {n}")
            for n in r["ahead"]:
                print(f"    {'':15s} +  {n} (after the cube's last year; matters once the cube is extended)")
    if any(r.get("newer") for r in results.values()):
        sys.exit(10)
    if any("error" in r for r in results.values()):
        sys.exit(2)


if __name__ == "__main__":
    main()
