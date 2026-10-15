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

# 3. gemeente/wijk/buurt (CBS Wijk- en Buurtkaart 2024)
datacube-zh/prepare/wijkbuurt.sh own_data
./target/release/datacube-zh admin                 # -> work/layers/admin.parquet

# 4. static layers (AHN, LIWO, RIVM, REV, Natura 2000, BRO), see config/static.toml
datacube-zh/prepare/static.py --data own_data       # WCS/WFS/GeoPackage fetch, ~10 min
./target/release/datacube-zh static                # -> work/layers/static.parquet (~2 min)

# 5. assemble -> out/datacube_zh (real values, Delta + parquet)
#             -> out/volledige_tijdreeks_delta (drop-in replacement, 0-255)
uv run datacube-zh/assemble.py
```

## Layers

| Step | Layer | Source | Method | Status |
|---|---|---|---|---|
| 1 | grid | PDOK provinciegebied | geoplegma `zones_from_bbox` + polygon filter | done |
| 2 | CBS (~135 cols, yearly) | [CBS vierkantstatistieken 100m](https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/kaart-van-100-meter-bij-100-meter-met-statistieken) | polygon overlay, `config/cbs.toml` | done |
| 3 | LGN `_fraction` (yearly) | LGN rasters (to be supplied: lgn.nl has no direct download links, 4TU copies are embargoed) | raster cell centres -> `zone_from_point`, class counts | todo |
| 4 | gemeente/wijk/buurt codes + names | [CBS Wijk- en Buurtkaart 2024 v2](https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/wijk-en-buurtkaart-2024) | area-weighted mode of buurtcode; wijk/gemeente derived from that buurt | done |
| 5 | static layers (see below) | AHN, LIWO, RIVM, REV, PDOK, BRO | rasters: pixel centre -> `zone_from_point`; vectors: polygon overlay | done, except BAG (`num_*`, deferred) and `soortenrijkdomsklasse` (source unknown) |
| 6 | assemble + 0-255 drop-in export (Delta) | — | `assemble.py`: join on `h3_id`, `year_int` | done |

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

## Validation (admin)

- Vintage: all 2,290 buurtcodes in the original table exist in the 2024 map
  (2023 lacks 34 re-divided Nieuwkoop buurten), so 2024 v2 is used.
- Agreement with the original (37,614 shared hexes): buurtcode 87%,
  gemeentecode 91%. The disagreements are errors in the original: ~2,900
  hexes fully inside the province (Goeree-Overflakkee, Hoeksche Waard, ...)
  were assigned Rotterdam (GM0599), median ~12 km from that buurt. Our
  assignment matches the buurt containing the hex centre in 96% of those
  cases (the original's in 1%).
- Border hexes get whatever buurt covers most of them, which can be in a
  neighbouring province (~350 hexes); filter on `province_fraction` or
  gemeentecode if needed. The original also had null buurt/wijk names
  (~1k hexes); ours has none.

## Static layers (`config/static.toml`)

Rasters are fetched over WCS (EPSG:28992), warped to lon/lat, and every
pixel centre is mapped to its hex with geoplegma's `zone_from_point`; rasters
coarser than a hex fall back to the pixel under the hex centre. Vectors use the
polygon overlay engine (`any` = overlaps the hex at all, `mode` = covers most
of it).

| Column | Source | r / agreement vs original |
|---|---|---|
| groundheight | AHN4 DTM (PDOK WCS `dtm_05m`, at 5m) | r 0.998 |
| flooddepth_1..5 | LIWO `MaximaleWaterdiepteNederland_Kaart1..5` (no data = 0) | r 0.82 / 0.96 / 0.98 / 0.98 / 0.98 |
| geluid_lden | RIVM Lden alle bronnen 2020 | r 0.977 |
| hitte | RIVM ANK stedelijk hitte-eilandeffect 2022 | r 1.000 |
| groenklasse | RIVM groenkaart 2022 (mean class) | r 0.972 |
| waterberging | RIVM ANK d15 waterberging grondwater (250m) | r 0.959 |
| lichtemissie | RIVM lichtemissie 2023 (375m) | r 0.937 |
| explosie- / brand- / gifwolk- / civielexplosie- / vuurwerkaandachtsgebied | REV WFS (basisnet + buisleidingen + stationaire bronnen) | 99% / 99% / 93% / 100% / 100% |
| natura2000_id (+ natura2000_naam) | PDOK Natura 2000 WFS, official site `nr` | same areas; the original used its own numbering |
| bodemklasse, bodemnaam | BRO Bodemkaart 1:50.000 (primary soil unit) | 93% (where mapped) |

Notes:
- gifwolk: 57% of hexes vs 63% in the original; RIVM changed the gifwolk
  calculation on 2026-01-01, so the current REV areas are smaller.
- AHN, hitte, groenklasse and bodem have no value over open water / unmapped
  (built-up) areas, where the original seems to have filled 0.
- The original's fractional class values (x/7) suggest it sampled the 7 H3
  res-10 child centres per hex; this pipeline uses every pixel instead.
- Not rebuilt: `num_*` (BAG, deferred), `soortenrijkdomsklasse` (no public
  source found), `landgebruiksklasse` (comes with LGN), `heeft_kepler_data`.
  The year-independent `aantal_inwoners` (~CBS 2020/2021 totals) will be
  filled from the CBS layer at assembly.

## Outputs (`assemble.py`)

- `out/datacube_zh/` (+ `datacube_zh.parquet`): 226,908 rows (37,818 hexes x
  2018-2023), 169 columns, real values. Includes columns the original lacks:
  `province_fraction`, `lat`/`lon`, `hex_area_m2`, `cbs_coverage`,
  `mediaan_inkomen_huishouden_mode`, `natura2000_naam`.
- `out/volledige_tijdreeks_delta/`: same 232 columns, order and types as the
  ruimtelijke-assistent table (verified via DuckDB `delta_scan`), so it can
  replace `src/backend/data/volledige_tijdreeks_delta` as-is.
  - CBS `_sum/_area_weighted_average/_min/_max` columns are min-max scaled to
    0-255 int16 over all years combined. (That is what the original did; its
    `_llm_metadata.json` says "per jaar", which is wrong: each column hits 255
    in only one year.)
  - `year`, `year_label`, `year_date`, `year_ms` are filled correctly (were
    mostly 0 in the original).
  - `aantal_inwoners` = real CBS population of that row's year (original:
    one static snapshot); `heeft_atlas_data` = hex has ANK heat-island data.
  - Not yet produced, left null: LGN `_fraction` + `landgebruiksklasse`,
    BAG `num_*`, `soortenrijkdomsklasse`, `heeft_kepler_data`,
    `mediaan_inkomen_huishouden_area_weighted_average` (was all-0 anyway).
  - Validation: median per-year Spearman vs the original 0.987 over 146
    shared columns. Pooled over all years it looks lower (0.60) only because
    the original has zeros for years it was missing (e.g. distances only in
    2021) where this table has real data.
- Differences in meaning the app's `_llm_metadata.json` should pick up:
  distance/count columns now exist for 2018-2023; `natura2000_id` uses
  official Natura 2000 site numbers; nulls where there's no data (instead of
  0); the hex set is 37,818 (204 more border slivers).
