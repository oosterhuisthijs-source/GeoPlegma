#!/usr/bin/env python3
"""LGN yearly 5 m rasters -> per-hex land-use class fractions (config/lgn.toml).

All LGN years share one 5 m grid in EPSG:28992, so instead of mapping every pixel
of every year to H3 separately, the region is processed in tiles: per tile the
grid's hexes (boundaries from the `h3` lib, projected to RD) are burned into a
hex-index raster with GDAL's pixel-centre rule, and each year's pixels are then
counted per (hex, class) with one bincount. A fraction is pixels of that class /
all pixel centres in the hex, i.e. the share of the hex area.

Writes <data>/<work>/layers/lgn_<year>.parquet with h3_id, year_int,
<class>_fraction (classes in that year's legend), lgn_groep_<group> (summed
classes, comparable across years), lgn_coverage (share of the hex with an LGN
code), landgebruiksklasse (dominant class) and lgn_source_year.

Needs GDAL's Python bindings, numpy, pyarrow and h3 (the system python3 here).
The tifs are unpacked from the zips once into <data>/lgn/tif/.

Usage: datacube-zh/prepare/lgn.py [--data own_data] [--region zh] [--years 2018 2025] [--check-points 20000]
"""
import argparse
import re
import sys
import time
import tomllib
import zipfile
from pathlib import Path

import h3
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from osgeo import gdal, ogr, osr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

gdal.UseExceptions()
CONFIG = Path(__file__).resolve().parent.parent / "config" / "lgn.toml"
H3_RES = 9
TILE = 4000  # pixels (20 km at 5 m)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def unpack(data: Path, pattern: str, year: int) -> tuple[Path, set[int]]:
    """Unpack LGN<year>.tif once; return its path and the codes in the year's legend (SLD)."""
    zpath = data / pattern.format(year=year)
    out = data / "lgn" / "tif" / f"LGN{year}.tif"
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        tif = next(n for n in names if n.endswith(".tif"))
        sld = next(n for n in names if n.endswith(".sld"))
        raw = z.read(sld)
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            log(f"unpacking {tif} -> {out}")
            tmp = out.with_suffix(".part")
            with z.open(tif) as src, open(tmp, "wb") as dst:
                while chunk := src.read(1 << 24):
                    dst.write(chunk)
            tmp.rename(out)
    text = raw.decode("utf-8", errors="replace")
    legend = {int(q) for q in re.findall(r'quantity="(\d+)"', text)} - {0}
    return out, legend


def hex_polygons_rd(h3_ids: list[str]) -> tuple[list[np.ndarray], np.ndarray]:
    """Hex boundaries in EPSG:28992 (closed rings) and their bboxes (minx, miny, maxx, maxy)."""
    wgs, rd = osr.SpatialReference(), osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    rd.ImportFromEPSG(28992)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    rd.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(wgs, rd)
    rings, sizes = [], []
    for h in h3_ids:
        b = h3.cell_to_boundary(h)  # ((lat, lng), ...)
        rings.extend((lng, lat) for lat, lng in b)
        sizes.append(len(b))
    xy = np.array(tr.TransformPoints(rings))[:, :2]
    polys, bbox, i = [], np.empty((len(h3_ids), 4)), 0
    for k, n in enumerate(sizes):
        p = xy[i:i + n]
        polys.append(np.vstack([p, p[:1]]))
        bbox[k] = (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max())
        i += n
    return polys, bbox


def burn(polys: list[np.ndarray], ids: np.ndarray, x0: float, y0: float, w: int, h: int, res: float) -> np.ndarray:
    """Hex-index raster for one tile: value = position in `ids` + 1 (0 = no hex), pixel-centre rule."""
    mem = gdal.GetDriverByName("MEM").CreateVector("")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(28992)
    lyr = mem.CreateLayer("h", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    defn = lyr.GetLayerDefn()
    for k, i in enumerate(ids):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in polys[i]:
            ring.AddPoint_2D(float(x), float(y))
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        f = ogr.Feature(defn)
        f.SetField("v", k + 1)
        f.SetGeometry(poly)
        lyr.CreateFeature(f)
    ds = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_UInt32)
    ds.SetGeoTransform((x0, res, 0, y0, 0, -res))
    ds.SetProjection(srs.ExportToWkt())
    gdal.RasterizeLayer(ds, [1], lyr, options=["ATTRIBUTE=v"])
    return ds.GetRasterBand(1).ReadAsArray()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--years", type=int, nargs="*", help="default: all years in config/lgn.toml")
    ap.add_argument("--check-points", type=int, default=20000,
                    help="random pixel centres to cross-check against h3.latlng_to_cell (0 = skip)")
    args = ap.parse_args()

    cfg = tomllib.loads(CONFIG.read_text())
    classes = {int(k): v for k, v in cfg["classes"].items()}
    renamed = {int(k): v for k, v in cfg.get("renamed", {}).items()}
    groups = {g: [int(c) for c in cs] for g, cs in cfg["groups"].items()}
    grouped = [c for cs in groups.values() for c in cs]
    if sorted(grouped) != sorted(classes) or len(set(grouped)) != len(grouped):
        sys.exit("config/lgn.toml: every class must be in exactly one group")
    years = args.years or cfg["years"]

    data = Path(args.data)
    reg = regions.load(args.region)
    work = data / reg["work"]
    grid = pq.read_table(work / "grid.parquet", columns=["h3_id"])
    h3_ids = grid["h3_id"].to_pylist()
    n_hex = len(h3_ids)

    # Class index: legend codes -> 0..K-1; K = "no hex/nodata" bucket is never counted.
    codes = sorted(classes)
    K = len(codes)
    lut = np.full(65536, K, dtype=np.int64)  # unknown/nodata -> K
    lut[codes] = np.arange(K)

    tifs, legends, sets = {}, {}, {}
    for y in years:
        tifs[y], legends[y] = unpack(data, cfg["zip"], y)
        unknown = legends[y] - set(classes)
        if unknown:
            sys.exit(f"LGN{y}: legend codes not in config/lgn.toml: {sorted(unknown)}")
        sets[y] = gdal.Open(str(tifs[y]))
    ref = sets[years[0]]
    gt = ref.GetGeoTransform()
    ox, oy, res = gt[0], gt[3], gt[1]
    W, H = ref.RasterXSize, ref.RasterYSize
    for y in years:
        if sets[y].GetGeoTransform() != gt or (sets[y].RasterXSize, sets[y].RasterYSize) != (W, H):
            sys.exit(f"LGN{y}: grid differs from LGN{years[0]}")
    nodata = {y: sets[y].GetRasterBand(1).GetNoDataValue() for y in years}

    t0 = time.time()
    polys, bbox = hex_polygons_rd(h3_ids)
    log(f"{n_hex} hexes projected to RD in {time.time() - t0:.1f}s")

    # Pixel window covering all hexes, cut into tiles.
    c0 = max(0, int((bbox[:, 0].min() - ox) // res))
    c1 = min(W, int((bbox[:, 2].max() - ox) // res) + 1)
    r0 = max(0, int((oy - bbox[:, 3].max()) // res))
    r1 = min(H, int((oy - bbox[:, 1].min()) // res) + 1)
    counts = {y: np.zeros((n_hex, K), dtype=np.uint32) for y in years}
    total = np.zeros(n_hex, dtype=np.uint32)  # pixel centres per hex (any value)
    unexpected = {y: {} for y in years}
    rng = np.random.default_rng(0)
    checks, check_bad = 0, 0

    tiles = [(r, c) for r in range(r0, r1, TILE) for c in range(c0, c1, TILE)]
    for ti, (r, c) in enumerate(tiles):
        h, w = min(TILE, r1 - r), min(TILE, c1 - c)
        x0, y0 = ox + c * res, oy - r * res
        x1, y1 = x0 + w * res, y0 - h * res
        sel = np.nonzero((bbox[:, 2] > x0) & (bbox[:, 0] < x1) & (bbox[:, 3] > y1) & (bbox[:, 1] < y0))[0]
        if len(sel) == 0:
            continue
        idx = burn(polys, sel, x0, y0, w, h, res)
        inside = idx > 0
        if not inside.any():
            continue
        local = idx[inside].astype(np.int64) - 1
        total[sel] += np.bincount(local, minlength=len(sel)).astype(np.uint32)

        if args.check_points:
            # Cross-check the burn against H3 itself at random pixel centres.
            n = max(1, args.check_points // len(tiles))
            pr, pc = np.nonzero(inside)
            pick = rng.integers(0, len(pr), size=min(n, len(pr)))
            pts = [(x0 + (pc[k] + 0.5) * res, y0 - (pr[k] + 0.5) * res) for k in pick]
            wgs, rd = osr.SpatialReference(), osr.SpatialReference()
            wgs.ImportFromEPSG(4326)
            rd.ImportFromEPSG(28992)
            wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            rd.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            ll = osr.CoordinateTransformation(rd, wgs).TransformPoints(pts)
            for (lng, lat, _), k in zip(ll, pick):
                checks += 1
                if h3.latlng_to_cell(lat, lng, H3_RES) != h3_ids[sel[idx[pr[k], pc[k]] - 1]]:
                    check_bad += 1

        for y in years:
            a = sets[y].GetRasterBand(1).ReadAsArray(c, r, w, h)[inside]
            cls = lut[a]
            bad = (cls == K) & (a != nodata[y])
            if bad.any():
                for v, m in zip(*np.unique(a[bad], return_counts=True)):
                    unexpected[y][int(v)] = unexpected[y].get(int(v), 0) + int(m)
            keep = cls < K
            binc = np.bincount(local[keep] * K + cls[keep], minlength=len(sel) * K).reshape(len(sel), K)
            counts[y][sel] += binc.astype(np.uint32)
        if ti % 10 == 0 or ti == len(tiles) - 1:
            log(f"tile {ti + 1}/{len(tiles)} ({len(sel)} hexes) {time.time() - t0:.0f}s")

    if checks:
        log(f"burn vs h3.latlng_to_cell: {check_bad}/{checks} pixel centres disagree ({100 * check_bad / checks:.3f}%)")
    for y in years:
        if unexpected[y]:
            log(f"LGN{y}: codes outside the legend counted as no data: {unexpected[y]}")

    out_dir = work / "layers"
    out_dir.mkdir(parents=True, exist_ok=True)
    tot = total.astype(np.float64)
    no_pixels = total == 0
    for y in years:
        cnt = counts[y].astype(np.float64)
        frac = np.divide(cnt, tot[:, None], out=np.zeros_like(cnt), where=tot[:, None] > 0)
        valid = cnt.sum(axis=1)
        # Fractions are shares of the hex area, so a hex whose pixels are all nodata (sea before
        # 2021) gets 0 everywhere and lgn_coverage 0; only hexes outside the raster are null.
        empty = no_pixels
        cols = {"h3_id": pa.array(h3_ids), "year_int": pa.array(np.full(n_hex, y, dtype=np.int64))}
        names = {}
        for j, code in enumerate(codes):
            name = renamed[code]["name"] if code in renamed and y <= renamed[code]["until"] else classes[code]
            names[j] = name
            if code in legends[y]:
                cols[f"{name}_fraction"] = pa.array(frac[:, j], mask=empty)
        for g, cs in groups.items():
            js = [codes.index(cc) for cc in cs]
            cols[f"lgn_groep_{g}"] = pa.array(frac[:, js].sum(axis=1), mask=empty)
        cols["lgn_coverage"] = pa.array(valid / np.maximum(tot, 1), mask=no_pixels)
        dom = cnt.argmax(axis=1)
        cols["landgebruiksklasse"] = pa.array([None if v == 0 else names[d].replace("_", " ") for d, v in zip(dom, valid)],
                                               pa.string())
        cols["lgn_source_year"] = pa.array(np.full(n_hex, y, dtype=np.int64))
        t = pa.table(cols)
        pq.write_table(t, out_dir / f"lgn_{y}.parquet", compression="zstd")
        log(f"lgn_{y}: {t.num_rows} hexes, {sum(1 for c in cols if c.endswith('_fraction'))} classes, "
            f"outside raster {int(empty.sum())}, all nodata {int(((valid == 0) & ~empty).sum())}, "
            f"median coverage {np.median(valid[~empty] / tot[~empty]):.4f}")
    log(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
