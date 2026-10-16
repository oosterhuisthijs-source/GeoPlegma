# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1"]
# ///
"""Step 7: freeze the assembled cube as an immutable, versioned release for consumers (the games,
ruimtelijke-assistent). Working builds in out/ keep changing; consumers only ever read a release.

Writes <data>/releases/datacube_<region>/<version>/:

- datacube_<region>/, .parquet, .columns.json   copies of the assembled cube (read-only files)
- volledige_tijdreeks_delta/                    (zh) the 0-255 drop-in export
- manifest.json   version, git commit, assembly time, rows/columns/years, sources per column group,
                  sha256 + size of every file, contract results, notes
- stats.json      per column and year: non-null share, min, max, mean (numeric) / distinct (string)
- diff.md         what changed vs the previous release (columns added/removed/retyped, years, null-share
                  and mean shifts), for the consumer to read before bumping its pin

and updates <data>/releases/datacube_<region>/latest (symlink) and CHANGELOG.md.

Consumer contracts (config/consumers.toml) list the columns a consumer reads, with expected type and
optional min/max/max_null_share/years. A release that breaks a contract is refused unless
--allow-breaking (then the break is recorded in manifest, diff and changelog).

Usage: uv run datacube-zh/release.py [--region zh] [--version YYYY.MM.DD] [--notes "..."]
                                     [--allow-dirty] [--allow-breaking] [--dry-run]
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
MEAN_SHIFT = 0.05  # relative mean change per column/year reported in diff.md
NULL_SHIFT = 0.02  # absolute non-null share change reported in diff.md


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True, check=True).stdout.strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else [root]


def column_stats(parquet: Path, columns: dict) -> dict:
    """{column: {year: {non_null, min, max, mean|distinct}}} in one pass over the parquet."""
    numeric = ("int", "uint", "float", "double", "decimal", "bool")
    exprs = []
    for name, meta in columns.items():
        q = f'"{name}"'
        exprs.append(f"avg(({q} IS NOT NULL)::int)")
        if meta["type"].startswith(numeric):
            exprs += [f"min({q})::double", f"max({q})::double", f"avg({q}::double)"]
        else:
            exprs += [f"min({q}::varchar)", f"max({q}::varchar)", f"count(DISTINCT {q})::double"]
    con = duckdb.connect()
    rows = con.sql(f"SELECT year_int, {', '.join(exprs)} FROM '{parquet}' GROUP BY 1 ORDER BY 1").fetchall()
    stats = {}
    for row in rows:
        year, vals = str(row[0]), row[1:]
        for i, (name, meta) in enumerate(columns.items()):
            nn, lo, hi, m = vals[4 * i:4 * i + 4]
            key = "mean" if meta["type"].startswith(numeric) else "distinct"
            stats.setdefault(name, {})[year] = {"non_null": round(nn, 6), "min": lo, "max": hi,
                                                key: None if m is None else round(m, 6)}
    return stats


def check_contract(name: str, contract: dict, columns: dict, stats: dict) -> list[str]:
    """Problems with this release for one consumer contract (empty = OK)."""
    problems = []
    years = set(next(iter(stats.values())).keys())
    missing_years = [y for y in contract.get("years", []) if str(y) not in years]
    if missing_years:
        problems.append(f"years missing: {missing_years}")
    for col, want in contract.get("columns", {}).items():
        if col not in columns:
            problems.append(f"{col}: column missing")
            continue
        if "type" in want and want["type"] != columns[col]["type"]:
            problems.append(f"{col}: type {columns[col]['type']} (contract {want['type']})")
        per_year = stats[col]
        for y in contract.get("years", per_year.keys()):
            s = per_year.get(str(y))
            if s is None:
                continue
            if "max_null_share" in want and 1 - s["non_null"] > want["max_null_share"] + 1e-9:
                problems.append(f"{col} {y}: null share {1 - s['non_null']:.3f} > {want['max_null_share']}")
            if "min" in want and s["min"] is not None and s["min"] < want["min"]:
                problems.append(f"{col} {y}: min {s['min']} < {want['min']}")
            if "max" in want and s["max"] is not None and s["max"] > want["max"]:
                problems.append(f"{col} {y}: max {s['max']} > {want['max']}")
    return problems


def diff(prev_columns: dict, prev_stats: dict, columns: dict, stats: dict) -> dict:
    added = sorted(set(columns) - set(prev_columns))
    removed = sorted(set(prev_columns) - set(columns))
    retyped = sorted(c for c in set(columns) & set(prev_columns) if columns[c]["type"] != prev_columns[c]["type"])
    prev_years = set(next(iter(prev_stats.values())).keys()) if prev_stats else set()
    years = set(next(iter(stats.values())).keys())
    shifts = []
    for c in sorted(set(columns) & set(prev_columns)):
        for y in sorted(years & prev_years):
            a, b = prev_stats.get(c, {}).get(y), stats[c].get(y)
            if a is None or b is None:
                continue
            notes = []
            if abs(b["non_null"] - a["non_null"]) > NULL_SHIFT:
                notes.append(f"non-null {a['non_null']:.3f} -> {b['non_null']:.3f}")
            ma, mb = a.get("mean"), b.get("mean")
            if ma is not None and mb is not None and abs(mb - ma) > MEAN_SHIFT * max(abs(ma), 1e-9):
                notes.append(f"mean {ma:.4g} -> {mb:.4g}")
            if ma is None and mb is not None and a["non_null"] == 0:
                notes.append("newly filled")
            if notes:
                shifts.append((c, y, "; ".join(notes)))
    return {"added": added, "removed": removed, "retyped": retyped,
            "years_added": sorted(years - prev_years) if prev_stats else [],
            "years_removed": sorted(prev_years - years), "shifts": shifts}


def diff_markdown(version: str, prev: str | None, d: dict | None, contracts: dict, breaking: bool) -> str:
    lines = [f"# datacube release {version}", ""]
    if d is None:
        lines += ["First release (nothing to compare with).", ""]
    else:
        lines += [f"Compared with {prev}.", ""]
        for key, title in (("added", "Columns added"), ("removed", "Columns removed (BREAKING)"),
                           ("retyped", "Columns with a new type (BREAKING)"), ("years_added", "Years added"),
                           ("years_removed", "Years removed (BREAKING)")):
            if d[key]:
                lines += [f"## {title} ({len(d[key])})", "", ", ".join(f"`{x}`" for x in d[key]), ""]
        if d["shifts"]:
            by_col = {}
            for c, y, note in d["shifts"]:
                by_col.setdefault(c, []).append(f"{y}: {note}")
            lines += [f"## Value shifts ({len(by_col)} columns; mean > {MEAN_SHIFT:.0%} or non-null share > "
                      f"{NULL_SHIFT} absolute)", ""]
            lines += [f"- `{c}`: " + " | ".join(v) for c, v in by_col.items()]
            lines.append("")
        if not any(d[k] for k in ("added", "removed", "retyped", "years_added", "years_removed", "shifts")):
            lines += ["No schema or value changes above the thresholds.", ""]
    lines += ["## Consumer contracts", ""]
    for name, res in contracts.items():
        if res is None:
            lines.append(f"- {name}: contract file not found, not checked")
        elif res:
            lines.append(f"- {name}: **BROKEN** ({len(res)} problems)")
            lines += [f"  - {p}" for p in res[:50]]
        else:
            lines.append(f"- {name}: OK")
    if breaking:
        lines += ["", "**This release was published with --allow-breaking.**"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--version", default=dt.date.today().strftime("%Y.%m.%d"))
    ap.add_argument("--notes", default="", help="what changed and why, for the changelog (markdown)")
    ap.add_argument("--allow-dirty", action="store_true", help="release even if datacube-zh/ has uncommitted changes")
    ap.add_argument("--allow-breaking", action="store_true", help="release even if a consumer contract breaks")
    ap.add_argument("--dry-run", action="store_true", help="check and print the diff, write nothing")
    args = ap.parse_args()

    reg = tomllib.loads((HERE / "config/regions.toml").read_text())[args.region]
    data = Path(args.data)
    out = data / reg["out"]
    name = f"datacube_{args.region}"
    sources = [out / name, out / f"{name}.parquet", out / f"{name}.columns.json"]
    if args.region == "zh":
        sources.append(out / "volledige_tijdreeks_delta")
    for s in sources:
        if not s.exists():
            sys.exit(f"missing {s}: run assemble.py --region {args.region} first")

    root = data / "releases" / name
    dest = root / args.version
    if dest.exists():
        sys.exit(f"{dest} already exists; releases are immutable, pass another --version (e.g. {args.version}.2)")

    dirty = git("status", "--porcelain", "--", ".")
    if dirty and not args.allow_dirty:
        sys.exit(f"datacube-zh/ has uncommitted changes, commit first (or --allow-dirty):\n{dirty}")
    commit = git("rev-parse", "HEAD")

    columns = json.loads((out / f"{name}.columns.json").read_text())
    log(f"computing stats over {len(columns)} columns ...")
    stats = column_stats(out / f"{name}.parquet", columns)

    consumers = tomllib.loads((HERE / "config/consumers.toml").read_text()).get(args.region, {})
    contracts = {}
    for cname, cfg in consumers.items():
        path = Path(os.path.expanduser(cfg["contract"]))
        contracts[cname] = check_contract(cname, json.loads(path.read_text()), columns, stats) if path.exists() else None
    broken = {c: p for c, p in contracts.items() if p}

    prev = os.readlink(root / "latest") if (root / "latest").is_symlink() else None
    d = None
    if prev:
        p = root / prev
        d = diff(json.loads((p / f"{name}.columns.json").read_text()), json.loads((p / "stats.json").read_text()),
                 columns, stats)
    breaking = bool(broken) or bool(d and (d["removed"] or d["retyped"] or d["years_removed"]))
    md = diff_markdown(args.version, prev, d, contracts, breaking and args.allow_breaking)
    print(md)
    if broken and not args.allow_breaking:
        sys.exit("consumer contract broken (see above); fix the cube or the contract, or pass --allow-breaking")
    if args.dry_run:
        log("dry run: nothing written")
        return

    tmp = root / f".{args.version}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    for s in sources:
        (shutil.copytree if s.is_dir() else shutil.copy2)(s, tmp / s.name)
    files = {}
    for f in files_under(tmp):
        files[str(f.relative_to(tmp))] = {"sha256": sha256(f), "bytes": f.stat().st_size}
    groups = {}
    for col, meta in columns.items():
        groups.setdefault(meta.get("source") or "-", []).append(col)
    years = sorted(next(iter(stats.values())).keys())
    manifest = {
        "name": name, "version": args.version, "region": reg["name"],
        "released": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "assembled": dt.datetime.fromtimestamp((out / f"{name}.parquet").stat().st_mtime).astimezone()
                     .isoformat(timespec="seconds"),
        "git_commit": commit, "git_dirty": bool(dirty),
        "previous": prev, "breaking": breaking, "notes": args.notes,
        "rows": duckdb.sql(f"SELECT count(*) FROM '{tmp / f'{name}.parquet'}'").fetchone()[0],
        "columns": len(columns), "years": [int(y) for y in years],
        "sources": [{"source": s, "columns": len(c), "example": c[:3]} for s, c in groups.items()],
        "contracts": {c: ("not found" if p is None else "broken" if p else "ok") for c, p in contracts.items()},
        "files": files,
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    (tmp / "stats.json").write_text(json.dumps(stats, ensure_ascii=False))
    (tmp / "diff.md").write_text(md)
    for f in files_under(tmp):
        f.chmod(0o444)
    tmp.rename(dest)

    link = root / "latest"
    new_link = root / ".latest.tmp"
    if new_link.is_symlink():
        new_link.unlink()
    new_link.symlink_to(args.version)
    new_link.replace(link)

    summary = "first release" if d is None else (
        f"+{len(d['added'])} / -{len(d['removed'])} columns, {len(d['retyped'])} retyped, "
        f"years +{d['years_added']} -{d['years_removed']}, {len({c for c, _, _ in d['shifts']})} columns shifted")
    entry = (f"## {args.version}{' (BREAKING)' if breaking else ''}\n\n"
             f"- commit `{commit[:10]}`{' (dirty)' if dirty else ''}, {manifest['rows']} rows x {len(columns)} "
             f"columns, years {years[0]}-{years[-1]}\n- vs {prev or '-'}: {summary} (see `{args.version}/diff.md`)\n"
             + (f"- {args.notes}\n" if args.notes else "") + "\n")
    changelog = root / "CHANGELOG.md"
    old = changelog.read_text().removeprefix(f"# {name} releases\n\n") if changelog.exists() else ""
    changelog.write_text(f"# {name} releases\n\n{entry}{old}")
    log(f"released {dest} ({len(files)} files); latest -> {args.version}")


if __name__ == "__main__":
    main()
