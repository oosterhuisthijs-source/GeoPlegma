# /// script
# requires-python = ">=3.11"
# dependencies = ["earthengine-api>=1.4", "numpy", "rasterio"]
# ///
"""Sentinel-2 NDVI composites per year (Earth Engine) -> GeoTIFFs on GCS -> local download.

Earth Engine does only the cloud masking and compositing; all per-pixel metrics and the hex aggregation
happen locally (prepare/s2_metrics.py, with geoplegma's hex index). Per year, on the RD grid at 10 m
(EPSG:28992, origin 0/625000, NL box 0-300 km x 300-625 km), one int16 GeoTIFF set with 4 bands:
  piek        90th percentile of NDVI over 1 April - 31 October  (x 10000): green at some point in the season,
              so harvest and mowing don't count as bare
  laatzomer   median NDVI over 1 July - 31 August (x 10000): late-summer state, where drought browning shows
  n_piek      clear observations in the April-October window
  n_zomer     clear observations in the July-August window
NDVI = (B8 - B4) / (B8 + B4) from COPERNICUS/S2_SR_HARMONIZED (offset of the 2022 processing baseline removed);
a pixel observation is clear when Google's Cloud Score+ cs_cdf >= 0.60 (GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED,
also catches cloud shadow). Nodata -32768.

Auth: Application Default Credentials (gcloud auth application-default login with the earthengine scope,
quota project som-scenario); ~/.config/earthengine/credentials must not hold a stale token.

Steps:
  s2_ndvi.py test [--year 2022]        compute a 5 x 5 km sample around Utrecht locally and print statistics
  s2_ndvi.py export --years 2018-2025  submit one export task per year to gs://<bucket>/<prefix>/<year>/
  s2_ndvi.py status                    task states (from <data>/sources/s2_ndvi/tasks.json)
  s2_ndvi.py download                  gsutil -m cp finished years to <data>/sources/s2_ndvi/<year>/
  s2_ndvi.py watch                     download as tasks finish, then run s2_metrics.py once (overnight)

Usage: uv run datacube-zh/prepare/s2_ndvi.py <step> [--data own_data] [...]
"""
import argparse
import datetime as dt
import io
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import ee
import numpy as np

PROJECT = "som-scenario"
BUCKET = "som-scenario"
PREFIX = "datacube/ndvi"
BOX_RD = [0, 300000, 300000, 625000]  # config/regions.toml nl bbox_rd
SCALE = 10
CLEAR = 0.60
BANDS = ["piek", "laatzomer", "n_piek", "n_zomer"]
NODATA = -32768


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def init():
    ee.Initialize(project=PROJECT)


def composite(year: int) -> ee.Image:
    box = ee.Geometry.Rectangle(BOX_RD, proj="EPSG:28992", geodesic=False)
    cs = ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED")

    def ndvi(img):
        clear = img.select("cs_cdf").gte(CLEAR)
        # normalizedDifference drops the properties; keep the timestamp for the summer filter below
        return (img.normalizedDifference(["B8", "B4"]).rename("ndvi").updateMask(clear)
                .copyProperties(img, ["system:time_start"]))

    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(box)
          .filterDate(f"{year}-04-01", f"{year}-11-01").linkCollection(cs, ["cs_cdf"]).map(ndvi))
    summer = s2.filterDate(f"{year}-07-01", f"{year}-09-01")
    piek = s2.reduce(ee.Reducer.percentile([90])).rename("piek")
    zomer = summer.median().rename("laatzomer")
    img = ee.Image.cat([piek.multiply(10000).round(), zomer.multiply(10000).round(),
                        s2.count().rename("n_piek"), summer.count().rename("n_zomer")])
    return img.unmask(NODATA).toInt16().rename(BANDS)


def test(year: int):
    """5 x 5 km around Utrecht (RD 130-135 km / 452-457 km): fetched directly, no export."""
    init()
    region = ee.Geometry.Rectangle([130000, 452000, 135000, 457000], proj="EPSG:28992", geodesic=False)
    url = composite(year).getDownloadURL({"region": region, "crs": "EPSG:28992",
                                          "crsTransform": [SCALE, 0, 130000, 0, -SCALE, 457000],
                                          "dimensions": "500x500", "format": "NPY"})
    a = np.load(io.BytesIO(urllib.request.urlopen(url, timeout=600).read()))
    for b in BANDS:
        v = a[b].astype(float)
        v[v == NODATA] = np.nan
        if b in ("piek", "laatzomer"):
            v /= 10000
        print(f"{year} {b:10s} valid {np.isfinite(v).mean():.1%}  p5 {np.nanpercentile(v, 5):.3f}  "
              f"median {np.nanmedian(v):.3f}  p95 {np.nanpercentile(v, 95):.3f}")
    p = a["piek"].astype(float) / 10000
    print(f"{year} share never green (piek < 0.3): {(p < 0.3).mean():.1%}, dense green (piek >= 0.6): "
          f"{(p >= 0.6).mean():.1%}, water-like (piek < 0): {(p < 0).mean():.1%}")


def export(data: Path, years: list[int]):
    init()
    tasks_file = data / "sources/s2_ndvi/tasks.json"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks = json.loads(tasks_file.read_text()) if tasks_file.exists() else {}
    w, h = (BOX_RD[2] - BOX_RD[0]) // SCALE, (BOX_RD[3] - BOX_RD[1]) // SCALE
    for y in years:
        if str(y) in tasks:
            log(f"{y}: already submitted ({tasks[str(y)]['id']}), skipped")
            continue
        task = ee.batch.Export.image.toCloudStorage(
            image=composite(y), description=f"datacube_ndvi_{y}", bucket=BUCKET,
            fileNamePrefix=f"{PREFIX}/{y}/ndvi_{y}", crs="EPSG:28992",
            crsTransform=[SCALE, 0, BOX_RD[0], 0, -SCALE, BOX_RD[3]], dimensions=f"{w}x{h}",
            maxPixels=int(2e9), fileDimensions=[16384, 16384], formatOptions={"cloudOptimized": True},
            skipEmptyTiles=True)
        task.start()
        tasks[str(y)] = {"id": task.id, "submitted": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        tasks_file.write_text(json.dumps(tasks, indent=1))
        log(f"{y}: submitted {task.id}")


def status(data: Path) -> dict:
    init()
    tasks = json.loads((data / "sources/s2_ndvi/tasks.json").read_text())
    out = {}
    for y, t in sorted(tasks.items()):
        s = ee.data.getTaskStatus(t["id"])[0]
        out[y] = s["state"]
        print(f"{y}: {s['state']}" + (f" ({s.get('error_message')})" if s.get("error_message") else ""))
    return out


def download(data: Path):
    states = status(data)
    for y, state in states.items():
        dest = data / f"sources/s2_ndvi/{y}"
        if state != "COMPLETED" or (dest / ".complete").exists():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gsutil", "-m", "-q", "cp", "-n", f"gs://{BUCKET}/{PREFIX}/{y}/*.tif", str(dest)], check=True)
        (dest / ".complete").write_text(dt.datetime.now().astimezone().isoformat(timespec="seconds"))
        log(f"{y}: downloaded to {dest}")


def watch(data: Path, interval: int = 900):
    """Overnight: every `interval` s, download finished years; when all are in, run prepare/s2_metrics.py (under a
    memory cap) once and stop. Never cuts a release. Run it as a systemd user service so it survives the session."""
    while True:
        try:
            download(data)
        except Exception as e:  # noqa: BLE001 - network hiccups: try again next round
            log(f"{dt.datetime.now():%H:%M} download round failed: {e}")
        tasks = json.loads((data / "sources/s2_ndvi/tasks.json").read_text())
        done = [y for y in tasks if (data / f"sources/s2_ndvi/{y}/.complete").exists()]
        log(f"{dt.datetime.now():%H:%M} {len(done)}/{len(tasks)} years downloaded")
        if len(done) == len(tasks):
            here = Path(__file__).resolve().parent
            cmd = ["systemd-run", "--user", "--scope", "-p", "MemoryMax=10G", "-p", "MemorySwapMax=0",
                   "uv", "run", str(here / "s2_metrics.py"), "--data", str(data)]
            log(f"all years in, running: {' '.join(cmd)}")
            r = subprocess.run(cmd)
            log(f"s2_metrics.py finished with exit code {r.returncode}")
            return
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["test", "export", "status", "download", "watch"])
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--year", type=int, default=2022)
    ap.add_argument("--years", default="2018-2025")
    args = ap.parse_args()
    data = Path(args.data)
    a, _, b = args.years.partition("-")
    years = list(range(int(a), int(b or a) + 1))
    if args.step == "test":
        test(args.year)
    elif args.step == "export":
        export(data, years)
    elif args.step == "status":
        status(data)
    elif args.step == "watch":
        watch(data)
    else:
        download(data)


if __name__ == "__main__":
    main()
