#!/usr/bin/env python3
"""BAG verblijfsobjecten (LV BAG levenscyclus extract) -> <work>/bag/vbo.parquet.

Downloads only the verblijfsobject part of PDOK's national LV BAG extract
(`lvbag-extract-nl.zip`, 3.6 GB; the VBO member is ~1.2 GB) with an HTTP
range request, parses every *voorkomen* (version) of every verblijfsobject
from its XML files in parallel into a national cache
(`sources/bag/vbo_voorkomens.parquet`), then clips to the region:

    lon, lat      point (RD -> lon/lat; a verblijfsobject mapped as a polygon
                  gets the mean of its exterior ring vertices)
    begin, eind   validity period as yyyymmdd (eind null = still valid)
    status        e.g. "Verblijfsobject in gebruik"
    gebruiksdoel  comma-separated gebruiksdoelen

Voorkomens that were made inactive (`tijdstipInactief` set: withdrawn by a
correction) are dropped. (`eindRegistratie` is *not* a filter: an ordinary
mutation ends both the validity and the registration of the previous
voorkomen.) The script reports how many verblijfsobjecten still have
overlapping validity periods (should be 0). Counting per
year happens in `datacube-zh bag` (config/bag.toml).

Needs lxml, pyarrow, pyproj and duckdb.

Usage: datacube-zh/prepare/bag.py [--data own_data] [--region zh]
"""
import argparse
import io
import os
import struct
import subprocess
import sys
import urllib.request
import zipfile
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import region as regions  # noqa: E402

EXTRACT = "https://service.pdok.nl/kadaster/bag/atom/downloads/lvbag-extract-nl.zip"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class RemoteFile(io.RawIOBase):
    """Seekable read-only view of a URL via HTTP range requests (to list the
    extract's members without downloading it)."""

    def __init__(self, url):
        self.url = url
        req = urllib.request.Request(url, method="HEAD")
        self.size = int(urllib.request.urlopen(req, timeout=120).headers["Content-Length"])
        self.pos = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{self.pos + n - 1}"})
        data = urllib.request.urlopen(req, timeout=120).read()
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


def download_vbo_member(dest_dir: Path) -> Path:
    """Fetch just the `9999VBO<date>.zip` member of the national extract."""
    existing = sorted(dest_dir.glob("9999VBO*.zip"))
    if existing:
        return existing[-1]
    members = zipfile.ZipFile(io.BufferedReader(RemoteFile(EXTRACT), buffer_size=1 << 16)).infolist()
    vbo = next(m for m in members if m.filename.startswith("9999VBO"))
    following = min((m.header_offset for m in members if m.header_offset > vbo.header_offset), default=None)
    end = following - 1 if following else ""
    log(f"bag: downloading {vbo.filename} ({vbo.compress_size / 1e9:.2f} GB) from {EXTRACT}")
    out = dest_dir / vbo.filename
    tmp = out.with_suffix(".part")
    curl = subprocess.Popen(["curl", "-sSfL", "-r", f"{vbo.header_offset}-{end}", EXTRACT], stdout=subprocess.PIPE)
    r = curl.stdout
    header = r.read(30)
    assert header[:4] == b"PK\x03\x04", "not a zip local file header"
    name_len, extra_len = struct.unpack("<HH", header[26:30])
    r.read(name_len + extra_len)
    inflate = zlib.decompressobj(-15)
    with open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(inflate.decompress(chunk))
            if inflate.eof:
                break
        f.write(inflate.flush())
    curl.stdout.close()
    curl.wait()
    tmp.rename(out)
    return out


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def ymd(text):
    """'2018-03-26' or '2018-03-26T10:02:04.000' -> 20180326."""
    return int(text[:10].replace("-", "")) if text else None


def parse_member(args):
    """Parse one XML file of the VBO zip -> list of voorkomen tuples."""
    from lxml import etree

    zip_path, name = args
    rows = []
    with zipfile.ZipFile(zip_path) as z, z.open(name) as f:
        for _, el in etree.iterparse(f, events=("end",), tag="{*}Verblijfsobject"):
            rec = {"doel": []}
            coords = None
            for child in el.iter():
                t = local(child.tag)
                if t == "identificatie" and child.get("domein", "").endswith("Verblijfsobject"):
                    rec["id"] = child.text
                elif t in ("beginGeldigheid", "eindGeldigheid", "tijdstipInactief", "eindRegistratie", "status"):
                    rec[t] = child.text
                elif t == "gebruiksdoel":
                    rec["doel"].append(child.text)
                elif t in ("pos", "posList") and coords is None:
                    v = [float(x) for x in child.text.split()]
                    dim = int(child.get("srsDimension") or child.getparent().get("srsDimension") or 3)
                    pts = list(zip(v[0::dim], v[1::dim]))
                    coords = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
            rows.append((
                rec.get("id"), ymd(rec.get("beginGeldigheid")), ymd(rec.get("eindGeldigheid")),
                rec.get("tijdstipInactief") is not None, rec.get("eindRegistratie") is not None,
                rec.get("status"), ",".join(rec["doel"]),
                coords[0] if coords else None, coords[1] if coords else None,
            ))
    return rows


def parse_national(zip_path: Path, cache: Path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    names = [n for n in zipfile.ZipFile(zip_path).namelist() if n.lower().endswith(".xml")]
    log(f"bag: parsing {len(names)} XML files from {zip_path.name}")
    schema = pa.schema([
        ("id", pa.string()), ("begin", pa.int64()), ("eind", pa.int64()), ("inactief", pa.bool_()),
        ("eind_registratie", pa.bool_()), ("status", pa.string()), ("gebruiksdoel", pa.string()),
        ("x", pa.float64()), ("y", pa.float64()),
    ])
    tmp = cache.with_suffix(".part")
    n = 0
    with pq.ParquetWriter(tmp, schema, compression="zstd") as w, ProcessPoolExecutor(os.cpu_count()) as pool:
        for i, rows in enumerate(pool.map(parse_member, [(zip_path, nm) for nm in names], chunksize=4)):
            if rows:
                w.write_table(pa.Table.from_pylist([dict(zip(schema.names, r)) for r in rows], schema=schema))
                n += len(rows)
            if i % 200 == 0:
                log(f"  {i}/{len(names)} files, {n} voorkomens")
    tmp.rename(cache)
    log(f"bag: {n} voorkomens -> {cache}")


def clip_region(cache: Path, reg: dict, out: Path):
    import duckdb
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from pyproj import Transformer

    xmin, ymin, xmax, ymax = reg["bbox_rd"]
    con = duckdb.connect()
    stats = con.sql(f"""
        SELECT count(*) AS all_rows,
               count(*) FILTER (WHERE inactief) AS inactief,
               count(*) FILTER (WHERE x IS NULL) AS no_geom
        FROM '{cache}'""").fetchone()
    log(f"bag: national {stats[0]} voorkomens; dropped: {stats[1]} inactive, {stats[2]} without geometry")
    t = con.sql(f"""
        SELECT id, begin, eind, status, gebruiksdoel, x, y FROM '{cache}'
        WHERE NOT inactief AND x IS NOT NULL
          AND x BETWEEN {xmin} AND {xmax} AND y BETWEEN {ymin} AND {ymax}
        ORDER BY id, begin, eind NULLS LAST""").arrow()
    if hasattr(t, "read_all"):
        t = t.read_all()
    # Sanity: at most one voorkomen per verblijfsobject valid on any date.
    overlaps = con.sql("""
        SELECT count(*) FROM (
          SELECT begin, lag(coalesce(eind, 99991231)) OVER (PARTITION BY id ORDER BY begin, eind NULLS LAST) AS prev_eind FROM t)
        WHERE prev_eind > begin""").fetchone()[0]
    lon, lat = Transformer.from_crs("EPSG:28992", "OGC:CRS84", always_xy=True).transform(
        t["x"].to_numpy(), t["y"].to_numpy())
    t = t.drop_columns(["x", "y"]).append_column("lon", pa.array(np.asarray(lon))).append_column(
        "lat", pa.array(np.asarray(lat)))
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, out, compression="zstd")
    log(f"bag: {t.num_rows} voorkomens in the region bbox ({overlaps} overlapping validity periods) -> {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    args = ap.parse_args()
    reg = regions.load(args.region)
    src = Path(args.data) / "sources/bag"
    src.mkdir(parents=True, exist_ok=True)
    cache = src / "vbo_voorkomens.parquet"
    if not cache.exists():
        parse_national(download_vbo_member(src), cache)
    clip_region(cache, reg, Path(args.data) / reg["work"] / "bag/vbo.parquet")


if __name__ == "__main__":
    main()
