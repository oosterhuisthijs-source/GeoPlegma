# datacube-zh

Rebuilds the Zuid-Holland H3 (res 9) datacube used by ruimtelijke-assistent
(`src/backend/data/volledige_tijdreeks_delta`) from original source data, with
geoplegma doing all hex work (grid enumeration, cell boundaries).

Compared to the original table:

- **Real values.** No 0-255 normalization in the layers; a final export step
  can produce the normalized drop-in version.
- **Correct sums.** CBS counts are apportioned by `overlap / square_area`, so
  totals are conserved (the Databricks pipeline used `overlap / hex_area`,
  i.e. a coverage-weighted average). See `src/overlay.rs`.
- **Grid** = every H3 cell overlapping the official PDOK province boundary
  (37,818 cells; a strict superset of the original 37,614), with a
  `province_fraction` column to filter slivers if wanted.

## Running

Data lives under `own_data/` (gitignored): `sources/` = downloads,
`work/` = prepared inputs and per-layer outputs.

```bash
# dggal-rust needs a static libz.a; on Fedora: `sudo dnf install zlib-static`
# (or point LIBRARY_PATH at a directory containing libz.a)
cargo build --release -p datacube-zh

# 0. province boundary (PDOK bestuurlijke gebieden)
mkdir -p own_data/sources/bestuurlijk
curl -sSfL "https://api.pdok.nl/kadaster/bestuurlijkegebieden/ogc/v1/collections/provinciegebied/items?f=json&limit=20" \
  -o own_data/sources/bestuurlijk/provincies.json

# 1. base grid
./target/release/datacube-zh grid                  # -> work/grid.parquet

# 2. CBS 100m squares 2018-2023
datacube-zh/prepare/cbs.sh own_data                 # download + clip/reproject to GeoParquet
./target/release/datacube-zh cbs                   # -> work/layers/cbs_<year>.parquet
```

## Layers

| Step | Layer | Source | Method | Status |
|---|---|---|---|---|
| 1 | grid | PDOK provinciegebied | geoplegma `zones_from_bbox` + polygon filter | done |
| 2 | CBS (~135 cols, yearly) | [CBS vierkantstatistieken 100m](https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/kaart-van-100-meter-bij-100-meter-met-statistieken) | polygon overlay, `config/cbs.toml` | done |
| 3 | LGN `_fraction` (yearly) | LGN 25m rasters (to be supplied) | raster cell centres -> `zone_from_point`, class counts | todo |
| 4 | gemeente/wijk/buurt codes + names | CBS wijk- en buurtkaart | hex centre point-in-polygon | todo |
| 5 | static layers: groundheight (AHN), flooddepth_1..5, bodem (BRO), BAG functions, risk areas, geluid, hitte, waterberging, lichtemissie, Natura2000, groen/soortenrijkdom | AHN / Klimaateffectatlas / PDOK / RIVM | raster sampling or polygon lookup | todo, sources to verify |
| 6 | assemble + optional 0-255 export (+ Delta) | — | join on `h3_id`, `year_int` | todo |

## Validation (CBS)

- Mass conservation: province-wide `aantal_inwoners_sum` is within 0.1% of
  the sum of CBS squares whose centroid is inside Zuid-Holland, every year
  (the excess is border hexes that also cover squares just outside).
- Against the original table (2021, 37,614 shared hexes): median Spearman
  rank correlation 0.987 over 133 columns; averages/min/max ~0.99, `_sum`
  columns 0.83-0.92 (expected — the original used the coverage-weighted
  formula).
- New vs. original: distance/count columns exist for 2022-2023 too, and
  `mediaan_inkomen_huishouden` (categorical, 2018-2019 only) is kept as an
  area-weighted mode instead of being lost as 0.
