#!/usr/bin/env python3
"""Shared helper: look up a region in datacube-zh/config/regions.toml.

As a module: `region.load("nl")` -> dict (bbox_wgs84, bbox_rd, work, ...).
From shell:  `datacube-zh/prepare/region.py nl bbox_wgs84` -> "3.2 50.7 7.3 53.7"
"""
import sys
import tomllib
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config" / "regions.toml"


def load(key: str) -> dict:
    regions = tomllib.loads(CONFIG.read_text())
    if key not in regions:
        raise SystemExit(f"unknown region {key!r} (known: {', '.join(regions)})")
    return regions[key]


if __name__ == "__main__":
    value = load(sys.argv[1])[sys.argv[2]]
    print(" ".join(map(str, value)) if isinstance(value, list) else value)
