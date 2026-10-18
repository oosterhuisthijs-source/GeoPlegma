# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pyarrow>=17", "rasterio>=1.4"]
# ///
"""Water depth after a cloudburst (Klimaateffectatlas "waterdiepte bij hevige neerslag") -> per hex.

Source: Klimaateffectatlas (Stichting CAS), national 2026 maps, CC BY 4.0 ("Klimaateffectatlas, 2026"): modelled water
depth (m) on the ground after a heavy shower, 0.5 m pixels, EPSG:28992, as 5x5 km Cloud-Optimized GeoTIFF tiles in a
public object store (~1,675 tiles, ~122 GB per scenario):
  https://kea-nl-wateroverlast.nbg1.your-objectstorage.com/waterdiepte_hevige_neerslag/waterdiepte_<scenario>/waterdiepte/
Scenarios: 70mm_1h, 90mm_1h, 155mm_2h (the older 70 / 140 mm in 2 hours maps are no longer published). Nodata (-9999)
= no water on the ground: counted as depth 0. Static: a model of the current terrain and sewers, not an event.

Read at full resolution (the file's overviews are averages that smear water into dry pixels: +15% mean depth in a
test window), tile by tile: every tile is reduced to 10 m blocks (sum, max, pixels > 10 cm / > 30 cm), and the blocks
go to hexes through the 10 m hex index raster (`datacube-zh --region nl hexindex`, <work>/s2/hexindex_10m.u32).
Each tile's result is cached (<work>/regenwater/<scenario>/<tile>.npz), so an interrupted run resumes.

Per hex (null where no tile covers the hex):
  regen_<scenario>_diepte_gem_m    mean water depth over the hex (dry = 0)
  regen_<scenario>_diepte_max_m    deepest pixel
  regen_<scenario>_aandeel_10cm    share of the hex with more than 10 cm of water
  regen_<scenario>_aandeel_30cm    share of the hex with more than 30 cm of water (roads impassable, water at doors)

Usage: uv run datacube-zh/prepare/regenwater.py [--data own_data] [--region nl] [--scenario 70mm_1h] [--workers 4]
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

BUCKET = "https://kea-nl-wateroverlast.nbg1.your-objectstorage.com"
PIX, BLOCK = 0.5, 10.0
K = int(BLOCK / PIX)  # 20 pixels per block side


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def tiles(scenario: str) -> list[str]:
    keys, token = [], None
    while True:
        q = {"list-type": "2", "prefix": f"waterdiepte_hevige_neerslag/waterdiepte_{scenario}/waterdiepte/",
             "max-keys": "1000"}
        if token:
            q["continuation-token"] = token
        with urllib.request.urlopen(f"{BUCKET}/?{urllib.parse.urlencode(q)}", timeout=120) as r:
            s = r.read().decode()
        keys += [k for k in re.findall(r"<Key>(.*?)</Key>", s) if k.endswith(".tif")]
        t = re.findall(r"<NextContinuationToken>(.*?)</NextContinuationToken>", s)
        if not t:
            return keys
        token = t[0]


def process(key: str, index_path: str, meta: dict, cache: str):
    """One tile -> per-hex sums (hex position, pixels, depth sum, max, > 10 cm, > 30 cm), cached as npz."""
    import rasterio
    out = Path(cache)
    if out.exists():
        return key, 0.0
    t0 = time.time()
    for attempt in range(4):
        try:
            with rasterio.open(f"/vsicurl/{BUCKET}/{key}") as src:
                a = src.read(1)
                x0, y0 = src.transform.c, src.transform.f
                assert abs(src.transform.a - PIX) < 1e-9 and abs(src.transform.e + PIX) < 1e-9, src.transform
                nodata = src.nodata
            break
        except Exception as e:  # the object store drops connections now and then
            if attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))
    inside = np.ones(a.shape, bool)
    depth = np.where((a == nodata) | ~np.isfinite(a), 0.0, np.maximum(a, 0.0)).astype(np.float32)
    # pad so the array starts and ends on the 10 m hex-index grid
    gx0, gy0 = meta["x0"], meta["y0"]
    left = int(round(((x0 - gx0) % BLOCK) / PIX))
    top = int(round(((gy0 - y0) % BLOCK) / PIX))
    h, w = a.shape
    H, W = -(-(h + top) // K) * K, -(-(w + left) // K) * K
    pd = np.zeros((H, W), np.float32)
    pi = np.zeros((H, W), bool)
    pd[top:top + h, left:left + w] = depth
    pi[top:top + h, left:left + w] = inside
    del a, depth, inside

    def blocks(arr, how):
        return getattr(arr.reshape(H // K, K, W // K, K), how)(axis=(1, 3))
    n = blocks(pi, "sum").astype(np.int32)
    s = blocks(pd, "sum")
    mx = blocks(pd, "max")
    c10 = blocks(pd > 0.1, "sum").astype(np.int32)
    c30 = blocks(pd > 0.3, "sum").astype(np.int32)
    # hex index window for these blocks
    col0 = int(round((x0 - left * PIX - gx0) / BLOCK))
    row0 = int(round((gy0 - (y0 + top * PIX)) / BLOCK))
    idx = np.memmap(index_path, dtype="<u4", mode="r", shape=(meta["rows"], meta["cols"]))
    r1, c1 = row0 + H // K, col0 + W // K
    win = np.zeros((H // K, W // K), np.uint32)
    rr0, cc0 = max(row0, 0), max(col0, 0)
    rr1, cc1 = min(r1, meta["rows"]), min(c1, meta["cols"])
    if rr1 > rr0 and cc1 > cc0:
        win[rr0 - row0:rr1 - row0, cc0 - col0:cc1 - col0] = idx[rr0:rr1, cc0:cc1]
    keep = (win > 0) & (n > 0)
    hexpos = win[keep].astype(np.int64) - 1
    uniq, inv = np.unique(hexpos, return_inverse=True)
    res = {"hex": uniq,
           "n": np.bincount(inv, n[keep], len(uniq)), "sum": np.bincount(inv, s[keep], len(uniq)),
           "c10": np.bincount(inv, c10[keep], len(uniq)), "c30": np.bincount(inv, c30[keep], len(uniq)),
           "max": np.zeros(len(uniq), np.float32)}
    np.maximum.at(res["max"], inv, mx[keep])
    tmp = out.with_suffix(".tmp.npz")
    np.savez(tmp, **res)
    tmp.rename(out)
    return key, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="nl")
    ap.add_argument("--scenario", default="70mm_1h", choices=["70mm_1h", "90mm_1h", "155mm_2h"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="only the first N tiles still to do (testing)")
    args = ap.parse_args()
    work = Path(args.data) / regions.load(args.region)["work"]
    meta = json.loads((work / "s2/hexindex_10m.json").read_text())
    cache = work / "regenwater" / args.scenario
    cache.mkdir(parents=True, exist_ok=True)

    keys = tiles(args.scenario)
    todo = [k for k in keys if not (cache / f"{Path(k).stem}.npz").exists()]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{args.scenario}: {len(keys)} tiles, {len(todo)} to do")
    t0, done = time.time(), 0
    with ProcessPoolExecutor(args.workers) as pool:
        futs = [pool.submit(process, k, str(work / "s2/hexindex_10m.u32"), meta, str(cache / f"{Path(k).stem}.npz"))
                for k in todo]
        for f in as_completed(futs):
            f.result()
            done += 1
            if done % 25 == 0 or done == len(todo):
                rate = done / (time.time() - t0)
                log(f"  {done}/{len(todo)} tiles, {rate * 3600:.0f}/h, ~{(len(todo) - done) / rate / 3600:.1f} h left")

    n_hex = meta["hexes"]
    n, s, c10, c30 = (np.zeros(n_hex) for _ in range(4))
    mx = np.full(n_hex, -1.0)
    for f in sorted(cache.glob("*.npz")):
        if f.name.endswith(".tmp.npz"):
            continue
        r = np.load(f)
        n[r["hex"]] += r["n"]
        s[r["hex"]] += r["sum"]
        c10[r["hex"]] += r["c10"]
        c30[r["hex"]] += r["c30"]
        np.maximum.at(mx, r["hex"], r["max"])
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    assert len(ids) == n_hex
    has = n > 0

    def col(v, digits):
        return pa.array(np.where(has, np.round(v, digits), np.nan), pa.float64(), mask=~has)
    p = f"regen_{args.scenario}"
    with np.errstate(invalid="ignore", divide="ignore"):
        table = pa.table({"h3_id": ids, f"{p}_diepte_gem_m": col(s / n, 4), f"{p}_diepte_max_m": col(mx, 3),
                          f"{p}_aandeel_10cm": col(c10 / n, 4), f"{p}_aandeel_30cm": col(c30 / n, 4)})
    source = {"source": f"Klimaateffectatlas 2026, waterdiepte bij hevige neerslag {args.scenario} (0.5 m), CC BY 4.0",
              "tiles": len(keys), "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    dest = work / f"layers/regen_{args.scenario}.parquet"
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), dest, compression="zstd")
    log(f"{has.sum():,} of {n_hex:,} hexes covered; mean depth {np.nanmean(np.where(has, s / np.maximum(n, 1), np.nan)):.3f} m,"
        f" share > 10 cm {c10.sum() / n.sum():.2%}, > 30 cm {c30.sum() / n.sum():.2%}")
    log(f"-> {dest}")


if __name__ == "__main__":
    main()
