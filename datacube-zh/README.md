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

# 4b. CBS Nieuw Bestand Bodemgebruik (NBBG2022 mutatiebestand, 2.2 GB zip)
mkdir -p own_data/sources/bbg own_data/work/bbg && cd own_data/sources/bbg && \
  curl -sSfLO https://geodata.cbs.nl/files/Bodemgebruik/NBBG2022/CBS_Mutatiebestand_NBBG2022_v1_def_GPKG.zip && \
  unzip -q CBS_Mutatiebestand_NBBG2022_v1_def_GPKG.zip && cd -
ogr2ogr -f Parquet own_data/work/bbg/nbbg_undissolved.parquet \
  own_data/sources/bbg/CBS_Mutatiebestand_NBBG2022_v1_def_GPKG/CBS_Mutatiebestand_NBBG2022_v1_def.gpkg \
  CBS_Mutatiebestand_NBBG2022_v1_def_undissolved -spat 3.76 51.63 5.05 52.35 -spat_srs EPSG:4326 \
  -t_srs OGC:CRS84 -dim XY -nlt MULTIPOLYGON -lco GEOMETRY_ENCODING=WKB -lco COMPRESSION=ZSTD
./target/release/datacube-zh bbg                   # -> work/layers/bbg_2020.parquet, bbg_2022.parquet (~5 s)

# 4c. BRP gewaspercelen (national yearly downloads; 2018-19 FileGDB, 2020+ GeoPackage)
datacube-zh/prepare/brp.py --brp-dir <dir with <year>/ downloads>   # -> work/brp/, config/brp_groups.toml
./target/release/datacube-zh brp                   # -> work/layers/brp_<year>.parquet (~17 s)

# 5. assemble -> out/datacube_zh (real values, Delta + parquet)
#             -> out/volledige_tijdreeks_delta (drop-in replacement, 0-255)
uv run datacube-zh/assemble.py
```

## Layers

| Step | Layer | Source | Method | Status |
|---|---|---|---|---|
| 1 | grid | PDOK provinciegebied | geoplegma `zones_from_bbox` + polygon filter | done |
| 2 | CBS (~135 cols, yearly) | [CBS vierkantstatistieken 100m](https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/kaart-van-100-meter-bij-100-meter-met-statistieken) | polygon overlay, `config/cbs.toml` | done |
| 3 | LGN `_fraction` (yearly) | **stopgap**: Databricks `lgn<year>_pzh_vectorized_consumption` export (`own_data/sources/lgn/`), 2018-2022, 2023 = copy of 2022. Raw rasters wanted (lgn.nl has no direct links, 4TU copies embargoed) | imported as-is in `assemble.py`; raw route would be raster engine with class counts | stopgap |
| 4 | gemeente/wijk/buurt codes + names | [CBS Wijk- en Buurtkaart 2024 v2](https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/wijk-en-buurtkaart-2024) | area-weighted mode of buurtcode; wijk/gemeente derived from that buurt | done |
| 5 | static layers (see below) | AHN, LIWO, RIVM, REV, PDOK, BRO | rasters: pixel centre -> `zone_from_point`; vectors: polygon overlay | done, except BAG (`num_*`, deferred) and `soortenrijkdomsklasse` (source unknown) |
| 5b | NBBG land use (41 categories, peiljaren 2020 + 2022) | [CBS NBBG2022 mutatiebestand](https://geodata.cbs.nl/files/Bodemgebruik/NBBG2022/) | `hex_overlaps` once, then overlap per category for both years; `config/bbg.toml` | done |
| 5c | BRP crop groups (15 groups, yearly 2018-2023) | BRP gewaspercelen (RVO via PDOK) | overlay fractions per crop group; `config/brp_groups.toml` | done |
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
  - Not yet produced, left null: BAG `num_*`, `soortenrijkdomsklasse`, `heeft_kepler_data`,
    `mediaan_inkomen_huishouden_area_weighted_average` (was all-0 anyway).
  - Validation: median per-year Spearman vs the original 0.987 over 146
    shared columns. Pooled over all years it looks lower (0.60) only because
    the original has zeros for years it was missing (e.g. distances only in
    2021) where this table has real data.
- Differences in meaning the app's `_llm_metadata.json` should pick up:
  distance/count columns now exist for 2018-2023; `natura2000_id` uses
  official Natura 2000 site numbers; nulls where there's no data (instead of
  0); the hex set is 37,818 (204 more border slivers).

## LGN stopgap (Databricks export)

Assessment of `own_data/lgn_parquet_files.zip` (the Databricks LGN
consumption tables, already on H3 res 9):

- OK: correct weighting (overlap / hex area), plausible province shares,
  stable classes stable year to year, all hexes inside our grid.
- ~3,000 hexes (8%) missing, 90% of them open water (Haringvliet,
  Grevelingen, lakes) -> null, `lgn_coverage` null.
- Fractions don't sum to 1: median 0.96 on land (2021: 0.998), a quarter of
  hexes < 0.86 -> whole polygons were dropped. `lgn_coverage` = row sum, so
  consumers can filter or renormalise knowingly.
- 2022 mapping bug: `natuurlijk_beheerde_agrarische_graslanden` (6.2% ->
  0.2% of the province) and `hoofdinfrastructuur_en_spoorbaanlichamen`
  (1.8% -> 0.06%) collapse vs 2021 (flagged in the manifest).
- 2023 = 2022 (`lgn_source_year` = 2022).
- The original ruimtelijke-assistent table's LGN columns do *not* match this
  export (r ~0.3-0.4 for grass/water/forest) and look less plausible.

`out/datacube_zh.columns.json` is a per-column manifest: source, unit,
aggregation, type, warning and non-null share per year.

## NBBG land use (`datacube-zh bbg`)

- Source: CBS Nieuw Bestand Bodemgebruik, *mutatiebestand* NBBG2022 v1,
  `_undissolved` layer: every polygon carries both its 2020 (`NBBG20`) and
  2022 (`NBBG22`) category, corrected to be comparable (CBS recommends the
  mutatiebestand for comparisons over time).
- Output: `bbg_<category>_fraction` (41 categories, real 0-1 shares),
  `bbg_coverage` (sum, = 1.000 for every hex: NBBG maps water and sea too),
  `bbg_dominant`, `bbg_source_year`. Cube years 2018-2021 use 2020,
  2022-2023 use 2022.
- One geometric pass serves both years (`overlay::hex_overlaps`); ~5 s.
- The overlay engine now cuts polygons with > 512 vertices into pieces first
  (`subdivide`, like PostGIS `ST_Subdivide`). Some NBBG polygons have >1M
  vertices; without this the run took >10 min. Results for other layers are
  unchanged (CBS output verified bit-identical).
- vs. SimCityZH `bodemgebruik.bin` (aggregated to the game's 750 m hexes):
  province means match closely for nearly all classes (woonterrein
  0.112/0.113, bedrijfsterrein 0.052/0.051, glastuinbouw 0.018/0.018),
  per-hex r 0.6-0.97. The game has two classes the final NBBG2022 v1 lacks
  (Natuurlijk grasland 4.2%, Kustduinen 1.8%), so it was built on a
  pre-release NBBG version; that area shows up here in Overig agrarisch
  terrein and Open droog/nat natuurlijk terrein.

## Audit (all layers, `out/datacube_zh`)

Checked: (h3_id, year) keys unique and complete (37,818 x 6), no NaN/inf or
leftover sentinels, ranges per column type, static columns identical across
years, sums/partitions consistent. Found and fixed:

- **Degenerate-sliver areas.** Boolean-op results can contain near-zero-area
  slivers whose orientation is numerically undefined; `geo`'s geodesic area
  then returns the area of the whole Earth (5.1e14 m2). All areas now go
  through `overlay::area_m2`, which orients and takes the complement of
  anything above half the Earth. Effect: 4 NBBG hexes (fractions summed to
  up to 1.97, now 1.000); no other layer changed.
- **Invalid source polygons** (1 in REV basisnet): their own area is
  meaningless, so a polygon's area is now the sum of its (valid) pieces.
- **CBS percentages > 100** in the 2018-2020 source (up to 160): nulled
  (`percentage_max`).
- **koop/huur percentages**: always sum to 100 per square, but one of the pair
  is suppressed in ~28k of ~79k squares, so hex averages ran over different
  squares (koop + huur median 118). The suppressed one is now filled as
  100 - the other (`complements`); hex sums are now 98.7-102 (p1-p99).
- LGN export fractions overshoot 1 by up to 0.007 (rounding): capped at 1.

Known and accepted (documented, not "fixed"):
- CBS suppression (-99997 = small/confidential counts) makes subtotals run
  low: men + women = 98.8% and the age groups 95.1% of `aantal_inwoners_sum`.
  When *every* square touching a hex is suppressed, the `_sum` is null, e.g.
  `aantal_woningen_sum` in ~4.3k populated hexes.
- CBS distances/facility counts are only published for squares with
  residents: ~61% of hexes (mostly non-residential) have no value.
- Static rasters/vectors have nulls over open water or unmapped areas
  (groundheight/hitte ~9%, groenklasse ~11%, waterberging ~13%, bodem 27%;
  natura2000 null = not in a Natura 2000 site).
- 5 NBBG categories are always 0 in Zuid-Holland (IJsselmeer, Randmeer,
  Waddenzee, Ooster-/Westerschelde); kept for a stable schema.

## BRP crop groups (`datacube-zh brp`)

- Source: BRP gewaspercelen definitief, 2018-2023 (national downloads,
  2018-2019 FileGDB with different field names, 2020+ GeoPackage; normalised
  by `prepare/brp.py`). "Landschapselement" parcels (only registered from
  ~2020) are excluded so years compare.
- ~320 gewascodes -> 15 crop groups by keyword rules in `prepare/brp.py`,
  written to `config/brp_groups.toml` with the area per code for review.
- Output per year: `brp_<group>_fraction` (0 where there are no parcels),
  `brp_coverage` (farmed share of the hex), `brp_dominant`.
- 2023 has ~40% more parcels (new GLB, parcels split) but the same area.
- Validation: vs the LGN stopgap 2021 (LGN takes its crops from BRP):
  aardappelen r 0.999, granen 0.996, bieten 0.993, mais 0.990, bloembollen
  0.974, grasland 0.946, fruit vs LGN fruitkwekerijen 0.93. Farmed share vs
  NBBG agricultural categories r 0.86 (BRP 35% vs NBBG 42% of the province;
  NBBG also counts farmyards and ditches).
- NBBG (land use) + BRP (crops, yearly) replace LGN for national use; LGN
  stays in as the ZH-only stopgap.
