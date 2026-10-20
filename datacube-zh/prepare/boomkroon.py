# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pyarrow>=17", "rasterio>=1.4", "scipy"]
# ///
"""Tree canopy cover per AHN epoch (Klimaateffectatlas "boomkroonbedekking", AHN3 / AHN4 / AHN5) -> per hex.

Source: Klimaateffectatlas (Stichting CAS), Rijksmonitor Klimaatadaptatie maps RMK_BKB_AHN3/4/5, CC BY 4.0
("Klimaateffectatlas, 2026"): 0/1 masks (1 = tree crown) at 0.25 m, EPSG:28992, derived from the national height
models. AHN3 was flown 2014-2019, AHN4 2020-2022, AHN5 2023-2025 (AHN5 is not complete yet). Served only via the
public WCS (cas.cloud.sogelink.com, kea_public), which refuses requests that read more than ~98 MB of source, so the
country is fetched in 800 m tiles, nearest-neighbour sampled at 1 m (1 pixel in 16; in test windows the share is
equal to the full 0.25 m read to 0.0003). Samples go to 10 m blocks and through the 10 m hex index raster
(`datacube-zh --region nl hexindex`, <work>/s2/hexindex_10m.u32) to hexes. Each tile is cached
(<work>/boomkroon/<epoch>/<x>_<y>.npz), so an interrupted run resumes.

AHN5 coverage: the mask has no nodata (0 = no crown AND not flown), so a tile counts as not flown when AHN5 has no
crown pixel at all while AHN4 has >= 0.5% crown, or when the WCS says it lies outside AHN5. Tiles with (almost) no
trees in AHN4 cannot tell; they take the status of the nearest decided tile. Not flown -> null.

Per hex (null where the epoch does not cover the hex):
  boomkroon_ahn3_aandeel, boomkroon_ahn4_aandeel, boomkroon_ahn5_aandeel   share of the hex under tree crowns

Usage: uv run datacube-zh/prepare/boomkroon.py [--data own_data] [--workers 6] [--epochs ahn3,ahn4,ahn5]
       [--limit N (test)]
"""
import argparse
import datetime as dt
import io
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

WCS = "https://cas.cloud.sogelink.com/public/data/org/gws/YWFMLMWERURF/kea_public/ows"
AXIS = "http://www.opengis.net/def/axis/OGC/1"
NN = "http://www.opengis.net/def/interpolation/OGC/1/nearest-neighbor"
EPOCHS = {"ahn3": "RMK_BKB_AHN3", "ahn4": "RMK_BKB_AHN4", "ahn5": "RMK_BKB_AHN5"}
TILE, SAMPLE, BLOCK = 800, 1.0, 10.0
K = int(BLOCK / SAMPLE)  # samples per block side
NB = int(TILE / BLOCK)   # blocks per tile side
MIN_AHN4 = 0.005         # AHN4 crown share above which an all-zero AHN5 tile means "not flown"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def tile_list(index, meta) -> list[tuple[int, int]]:
    """(row, col) of every 800 m tile (in 10 m blocks) that holds at least one hex."""
    out = []
    for r in range(0, meta["rows"], NB):
        band = np.asarray(index[r:r + NB]) > 0
        h = band.shape[0]
        w = -(-meta["cols"] // NB) * NB
        pad = np.zeros((NB, w), bool)
        pad[:h, :band.shape[1]] = band
        for c in np.nonzero(pad.reshape(NB, w // NB, NB).any(axis=(0, 2)))[0]:
            out.append((r, int(c) * NB))
    return out


def fetch(coverage: str, x: float, y: float):
    """0/1 samples (TILE/SAMPLE square, top row first) or None when the tile lies outside the coverage."""
    import rasterio
    n = int(TILE / SAMPLE)
    url = (f"{WCS}?service=WCS&version=2.0.1&request=GetCoverage&coverageId=kea_public__{coverage}"
           f"&subset=X({x:.0f},{x + TILE:.0f})&subset=Y({y - TILE:.0f},{y:.0f})&format=image/tiff"
           f"&SCALESIZE={AXIS}/i({n}),{AXIS}/j({n})&INTERPOLATION={NN}")
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "datacube-zh research (CC BY 4.0 reuse)"})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    body = r.read()
            except urllib.error.HTTPError as e:  # outside the coverage: 404 with an "Empty intersection" report
                body = e.read()
                if e.code != 404 or b"Empty intersection" not in body:
                    raise
            if body[:5] == b"<?xml":
                if b"Empty intersection" in body:
                    return None
                raise RuntimeError(body[-300:].decode(errors="replace"))
            with rasterio.MemoryFile(io.BytesIO(body)) as mf, mf.open() as d:
                a = d.read(1)
                t = d.transform
            break
        except Exception:
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))
    # the server snaps the window to its 0.25 m pixel grid (offsets up to ~0.4 m), and at the coverage edge it crops
    # the window and stretches what is left to n x n: put every 1 m sample back on its own spot (outside = 0)
    if abs(t.c - x) < SAMPLE / 2 and abs(t.f - y) < SAMPLE / 2 and abs(t.a - SAMPLE) < 1e-6 and abs(t.e + SAMPLE) < 1e-6:
        return a == 1
    centre = (np.arange(n) + 0.5) * SAMPLE
    col = np.floor((x + centre - t.c) / t.a).astype(np.int64)
    row = np.floor((t.f - (y - centre)) / -t.e).astype(np.int64)
    okc, okr = (col >= 0) & (col < a.shape[1]), (row >= 0) & (row < a.shape[0])
    out = np.zeros((n, n), bool)
    out[np.ix_(okr, okc)] = a[np.ix_(row[okr], col[okc])] == 1
    return out


def process(epoch: str, r: int, c: int, index_path: str, meta: dict, cache: str):
    out = Path(cache)
    if out.exists():
        return 0.0
    t0 = time.time()
    x, y = meta["x0"] + c * BLOCK, meta["y0"] - r * BLOCK
    a = fetch(EPOCHS[epoch], x, y)
    idx = np.memmap(index_path, dtype="<u4", mode="r", shape=(meta["rows"], meta["cols"]))
    win = np.zeros((NB, NB), np.uint32)
    part = idx[r:r + NB, c:c + NB]
    win[:part.shape[0], :part.shape[1]] = part
    keep = win > 0
    hexpos = win[keep].astype(np.int64) - 1
    uniq, inv = np.unique(hexpos, return_inverse=True)
    if a is None:
        res = {"hex": uniq, "n": np.zeros(len(uniq)), "crown": np.zeros(len(uniq)), "outside": np.array(True),
               "tile_crown": np.array(0)}
    else:
        blocks = a.reshape(NB, K, NB, K).sum(axis=(1, 3))
        res = {"hex": uniq, "n": np.bincount(inv, minlength=len(uniq)) * K * K,
               "crown": np.bincount(inv, blocks[keep], len(uniq)), "outside": np.array(False),
               "tile_crown": np.array(int(a.sum()))}
    tmp = out.with_suffix(".tmp.npz")
    np.savez(tmp, **res)
    tmp.rename(out)
    return time.time() - t0


def ahn5_flown(work: Path, tiles: list[tuple[int, int]]) -> dict:
    """Per tile: True when AHN5 covers it (see module doc)."""
    from scipy.ndimage import distance_transform_edt
    status = {}
    for r, c in tiles:
        f5, f4 = (work / f"boomkroon/{e}/{r}_{c}.npz" for e in ("ahn5", "ahn4"))
        t5, t4 = np.load(f5), np.load(f4)
        if bool(t5["outside"]):
            status[(r, c)] = 0
        elif int(t5["tile_crown"]) > 0:
            status[(r, c)] = 1
        elif int(t4["tile_crown"]) >= MIN_AHN4 * (TILE / SAMPLE) ** 2:
            status[(r, c)] = 0
        else:
            status[(r, c)] = -1  # undecided
    rs, cs = np.array([t[0] for t in tiles]) // NB, np.array([t[1] for t in tiles]) // NB
    g = np.full((rs.max() + 1, cs.max() + 1), -1, np.int8)
    g[rs, cs] = [status[t] for t in tiles]
    _, (ir, ic) = distance_transform_edt(g < 0, return_indices=True)
    filled = g[ir, ic]
    und = sum(v < 0 for v in status.values())
    flown = {t: bool(filled[t[0] // NB, t[1] // NB] == 1) for t in tiles}
    log(f"AHN5: {sum(v == 1 for v in status.values())} tiles flown, {sum(v == 0 for v in status.values())} not, "
        f"{und} undecided -> nearest; {sum(flown.values())} of {len(tiles)} counted as flown")
    return flown


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--epochs", default="ahn3,ahn4,ahn5")
    ap.add_argument("--limit", type=int, default=0, help="only the first N tiles still to do per epoch (testing)")
    args = ap.parse_args()
    work = Path(args.data) / regions.load("nl")["work"]
    meta = json.loads((work / "s2/hexindex_10m.json").read_text())
    index_path = work / "s2/hexindex_10m.u32"
    index = np.memmap(index_path, dtype="<u4", mode="r", shape=(meta["rows"], meta["cols"]))
    tiles = tile_list(index, meta)
    epochs = args.epochs.split(",")
    log(f"{len(tiles)} tiles of {TILE} m with hexes, epochs {epochs}")

    for epoch in epochs:
        cache = work / "boomkroon" / epoch
        cache.mkdir(parents=True, exist_ok=True)
        todo = [t for t in tiles if not (cache / f"{t[0]}_{t[1]}.npz").exists()]
        if args.limit:
            todo = todo[:args.limit]
        log(f"{epoch}: {len(todo)} tiles to do")
        t0, done = time.time(), 0
        with ProcessPoolExecutor(args.workers) as pool:
            futs = [pool.submit(process, epoch, r, c, str(index_path), meta, str(cache / f"{r}_{c}.npz"))
                    for r, c in todo]
            for f in as_completed(futs):
                f.result()
                done += 1
                if done % 500 == 0 or done == len(todo):
                    rate = done / (time.time() - t0)
                    log(f"  {epoch} {done}/{len(todo)}, {rate * 3600:.0f}/h, ~{(len(todo) - done) / rate / 3600:.1f} h left")
    if args.limit or set(epochs) != set(EPOCHS):
        log("test or partial run: no layer written")
        return

    flown5 = ahn5_flown(work, tiles)
    n_hex = meta["hexes"]
    full = np.zeros(n_hex)  # samples per hex in a complete epoch (AHN4 covers all of nl)
    for r, c in tiles:
        t = np.load(work / f"boomkroon/ahn4/{r}_{c}.npz")
        full[t["hex"]] += t["n"]
    cols = {}
    for epoch in EPOCHS:
        n, crown = np.zeros(n_hex), np.zeros(n_hex)
        for r, c in tiles:
            if epoch == "ahn5" and not flown5[(r, c)]:
                continue
            t = np.load(work / f"boomkroon/{epoch}/{r}_{c}.npz")
            if bool(t["outside"]):
                continue
            n[t["hex"]] += t["n"]
            crown[t["hex"]] += t["crown"]
        # a hex counts only when the epoch covers at least half of it (AHN5 edges)
        has = n >= 0.5 * np.maximum(full, 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            v = np.round(crown / n, 4)
        cols[f"boomkroon_{epoch}_aandeel"] = pa.array(np.where(has, v, np.nan), pa.float64(), mask=~has)
        log(f"{epoch}: {has.sum():,} hexes, national crown share {crown[has].sum() / n[has].sum():.2%}")
    ids = pq.read_table(work / "grid.parquet", columns=["h3_id"])["h3_id"].to_pylist()
    assert len(ids) == n_hex
    table = pa.table({"h3_id": ids, **cols})
    source = {"source": "Klimaateffectatlas 2026 (Rijksmonitor Klimaatadaptatie), boomkroonbedekking RMK_BKB_AHN3/4/5, "
                        "0.25 m sampled at 1 m, CC BY 4.0", "tiles": len(tiles),
              "prepared": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    dest = work / "layers/boomkroon.parquet"
    pq.write_table(table.replace_schema_metadata({"datacube": json.dumps(source)}), dest, compression="zstd")
    log(f"-> {dest}")


if __name__ == "__main__":
    main()
