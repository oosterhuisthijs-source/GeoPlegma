#!/usr/bin/env python3
"""Fetch the static layers listed in datacube-zh/config/static.toml.

Rasters: WCS 2.0.1 GetCoverage in EPSG:28992, tiled over the Zuid-Holland
bbox (optionally downscaled server-side to `resolution`), mosaicked and warped
to lon/lat as a raw Float32 EHdr file (`<name>.bil` + `.hdr`), which is what
`datacube-zh static` reads.

Vectors: WFS layers (or a GeoPackage + SQL) clipped to the bbox and written
as GeoParquet in OGC:CRS84, one file per source layer (`<name>__<i>.parquet`).

Only needs the Python standard library and the GDAL command-line tools.

Usage: datacube-zh/prepare/static.py [--data own_data] [--region zh] [--only name1,name2]
"""
import argparse
import math
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

# Region bbox in EPSG:28992 and minimum raster resolution: set from
# config/regions.toml in main().
BBOX_RD = None
MIN_RES = 0
MAX_TILE_PX = 4000
LAT = 52.0  # for metres -> degrees when picking the lon/lat output resolution


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url, out, retries=4):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=600) as r, open(out, "wb") as f:
                ctype = r.headers.get("Content-Type", "")
                shutil.copyfileobj(r, f)
            if "xml" in ctype or "text" in ctype:
                raise RuntimeError(f"service error for {url}: {Path(out).read_text()[:500]}")
            return
        except Exception:
            if attempt == retries - 1:
                raise


def describe(wcs, coverage):
    q = urllib.parse.urlencode(
        {"service": "WCS", "version": "2.0.1", "request": "DescribeCoverage", "coverageId": coverage}
    )
    xml = urllib.request.urlopen(f"{wcs}?{q}", timeout=120).read().decode()
    axes = re.search(r'axisLabels="([^"]+)"', xml).group(1).split()
    offsets = re.findall(r"<gml:offsetVector[^>]*>([^<]+)<", xml)
    native = max(abs(float(v)) for o in offsets for v in o.split())
    return axes, native


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def prepare_raster(layer, src_dir, out_dir):
    name = layer["name"]
    out = out_dir / f"{name}.bil"
    if out.exists():
        log(f"skip {name} (exists)")
        return
    axes, native = describe(layer["wcs"], layer["coverage"])
    res = max(layer.get("resolution", native), MIN_RES, native)
    scale = native / res
    tile_m = MAX_TILE_PX * res
    xmin, ymin, xmax, ymax = BBOX_RD
    tiles = []
    tile_dir = src_dir / name
    tile_dir.mkdir(parents=True, exist_ok=True)
    nx, ny = math.ceil((xmax - xmin) / tile_m), math.ceil((ymax - ymin) / tile_m)
    for i in range(nx):
        for j in range(ny):
            x0, y0 = xmin + i * tile_m, ymin + j * tile_m
            x1, y1 = min(x0 + tile_m, xmax), min(y0 + tile_m, ymax)
            params = [
                ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
                ("coverageId", layer["coverage"]), ("format", "image/tiff"),
                ("subset", f"{axes[0]}({x0},{x1})"), ("subset", f"{axes[1]}({y0},{y1})"),
            ]
            if abs(scale - 1) > 1e-9:
                params.append(("SCALEFACTOR", f"{scale:.6f}"))
            tiles.append((f"{layer['wcs']}?{urllib.parse.urlencode(params)}", tile_dir / f"{i}_{j}.tif"))

    log(f"{name}: {len(tiles)} tiles at {res} m (native {native} m)")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda t: t[1].exists() or fetch(*t), tiles))

    vrt = tile_dir / "mosaic.vrt"
    run(["gdalbuildvrt", "-q", "-a_srs", "EPSG:28992", str(vrt)] + [str(t[1]) for t in tiles])
    dx = res / (111320 * math.cos(math.radians(LAT)))
    dy = res / 111320
    nodata = layer.get("nodata", [])
    warp = ["gdalwarp", "-q", "-overwrite", "-t_srs", "OGC:CRS84", "-tr", f"{dx:.10f}", f"{dy:.10f}",
            "-r", "near", "-ot", "Float32", "-of", "EHdr", "-dstnodata", "nan", "-multi", "-wo", "NUM_THREADS=ALL_CPUS"]
    if nodata:
        warp += ["-srcnodata", " ".join(str(v) for v in nodata)]
    run(warp + [str(vrt), str(out)])
    log(f"{name}: -> {out}")


def prepare_vector(layer, src_dir, out_dir):
    name = layer["name"]
    xmin, ymin, xmax, ymax = BBOX_RD
    common = ["-t_srs", "OGC:CRS84", "-nlt", "MULTIPOLYGON", "-makevalid",
              "-lco", "GEOMETRY_ENCODING=WKB", "-lco", "COMPRESSION=ZSTD", "-overwrite"]
    if "wfs" in layer:
        for i, wfs_layer in enumerate(layer["layers"]):
            out = out_dir / f"{name}__{i}.parquet"
            if out.exists():
                continue
            run(["ogr2ogr", "-f", "Parquet", str(out), f"WFS:{layer['wfs']}", wfs_layer,
                 "-spat", *map(str, BBOX_RD), "-spat_srs", "EPSG:28992"] + common)
            log(f"{name}: {wfs_layer} -> {out}")
    else:
        gpkg = src_dir / Path(urllib.parse.urlparse(layer["gpkg"]).path).name
        if not gpkg.exists():
            fetch(layer["gpkg"], gpkg)
        out = out_dir / f"{name}__0.parquet"
        if not out.exists():
            # -spat is in the source CRS (EPSG:28992); -spat_srs can't be combined with -sql.
            run(["ogr2ogr", "-f", "Parquet", str(out), str(gpkg), "-sql", layer["sql"],
                 "-spat", *map(str, BBOX_RD)] + common)
            log(f"{name}: {gpkg.name} -> {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--config", default="datacube-zh/config/static.toml")
    ap.add_argument("--only", default="")
    ap.add_argument("--region", default="zh")
    args = ap.parse_args()

    global BBOX_RD, MIN_RES
    reg = regions.load(args.region)
    BBOX_RD = tuple(reg["bbox_rd"])
    MIN_RES = reg.get("raster_min_resolution", 0)
    cfg = tomllib.loads(Path(args.config).read_text())
    only = set(filter(None, args.only.split(",")))
    # Downloaded GeoPackages are shared; WCS tiles depend on bbox/resolution,
    # so they are cached per region (zh keeps the original location).
    src_dir = Path(args.data) / "sources/static"
    tile_dir = src_dir if args.region == "zh" else src_dir / args.region
    out_dir = Path(args.data) / reg["work"] / "static"
    src_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(prepare_raster, l) for l in cfg.get("raster", [])] + [(prepare_vector, l) for l in cfg.get("vector", [])]
    jobs = [(f, l) for f, l in jobs if not only or l["name"] in only]
    failed = []

    def go(job):
        f, layer = job
        try:
            f(layer, tile_dir if f is prepare_raster else src_dir, out_dir)
        except Exception as e:
            failed.append(layer["name"])
            log(f"FAILED {layer['name']}: {e}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(go, jobs))
    if failed:
        sys.exit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
