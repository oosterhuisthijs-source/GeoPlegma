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

# 4d. 3D BAG (Portolan NL mirror, 1.4 GB, shared), buurt kerncijfers, Leefbaarometer
datacube-zh/prepare/static.py --only bag3d && ./target/release/datacube-zh static
datacube-zh/prepare/wb.py && ./target/release/datacube-zh cbs --layer wb
datacube-zh/prepare/lbm.py && ./target/release/datacube-zh cbs --layer lbm

# 4e. BAG verblijfsobjecten (LV BAG levenscyclus extract, VBO part only, ~1.2 GB) and NDFF soortendiversiteit
datacube-zh/prepare/bag.py && ./target/release/datacube-zh bag     # -> work/layers/bag_<year>.parquet
datacube-zh/prepare/static.py --only ndff_soortendiversiteit,ndff_soortendiversiteit_rl && ./target/release/datacube-zh static

# 4f. LGN2018-LGN2025 (WUR 5 m rasters, zips in own_data/lgn/), see config/lgn.toml and "LGN" below
python3 datacube-zh/prepare/lgn.py --region zh      # -> work/layers/lgn_<year>.parquet (~40 s; nl ~10 min)

# 4g. wind turbines (RIVM WFS snapshot, static), see "Wind turbines" below
python3 datacube-zh/prepare/wind.py --region zh     # -> work/layers/wind.parquet (seconds)

# 4h. OSM motorways + railways (Geofabrik extract, latest only), see "OpenStreetMap lines" below
curl -L -o own_data/sources/osm/netherlands-latest.osm.pbf https://download.geofabrik.de/europe/netherlands-latest.osm.pbf
uv run datacube-zh/prepare/osm.py --region zh       # -> work/layers/osm.parquet + osm_lijnen.parquet (~20 s)
uv run datacube-zh/prepare/osm_extra.py --region zh # -> work/layers/osm_extra.parquet (grid, solar, marinas; ~20 s)
datacube-zh/prepare/laadpalen.py --region zh       # NDW charging points -> work/layers/laadpalen.parquet (downloads once)

# 4i. measurements and model maps per year, see "Climate, deposition, air quality" below
uv run datacube-zh/prepare/rivm.py --region zh --download   # RIVM deposition + GCN air quality -> work/layers/rivm.parquet
uv run datacube-zh/prepare/tropomi.py --region zh           # TROPOMI NO2 (TEMIS monthly) -> work/layers/tropomi.parquet
uv run datacube-zh/prepare/kdw.py --region zh               # KDW per habitat + exceedance -> work/layers/kdw.parquet (after rivm.py)
uv run datacube-zh/prepare/vee.py --region zh               # livestock per gemeente spread over farmland -> work/layers/vee.parquet (needs nl BRP layers)
uv run datacube-zh/prepare/knmi.py download                 # KNMI daily grids, once for all regions (~15 h, rate-limited)
uv run datacube-zh/prepare/knmi.py --region zh              # -> work/layers/knmi.parquet

# Every step takes --region (zh default, nl = Nederland; config/regions.toml):
#   datacube-zh/prepare/cbs.sh own_data nl, ./target/release/datacube-zh --region nl grid, ...
# zh is a SLICE of nl (`slice_of = "nl"`, since 2026-10-16): run the prepare steps for nl only; zh needs just its
# grid (`datacube-zh --region zh grid`) and its own layers (TESSERA, zh only). Then:
#   assemble.py --region nl  ->  coarsen.py --region nl  ->  assemble.py --region zh  ->  coarsen.py --region zh

# 5. assemble -> out/datacube_zh (real values, Delta + parquet)
#             -> out/volledige_tijdreeks_delta (drop-in replacement, 0-255)
uv run datacube-zh/assemble.py

# 5b. coarser H3 resolutions (res 8 and 7) from the res-9 cube, see "Coarser resolutions"
uv run datacube-zh/coarsen.py --region zh            # -> out/datacube_zh_r8.parquet, _r7 (+ .columns.json)

# 6. freeze it as an immutable release for consumers (games, ruimtelijke-assistent), see "Releases"
uv run datacube-zh/release.py --region zh --notes "what changed"
```

## Layers

| Step | Layer | Source | Method | Status |
|---|---|---|---|---|
| 1 | grid | PDOK provinciegebied | geoplegma `zones_from_bbox` + polygon filter | done |
| 2 | CBS (~135 cols, yearly) | [CBS vierkantstatistieken 100m](https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/kaart-van-100-meter-bij-100-meter-met-statistieken) | polygon overlay, `config/cbs.toml` | done |
| 3 | LGN `_fraction` + `lgn_groep_*` (yearly 2018-2025, zh + nl) | LGN2018-LGN2025 5 m rasters (WUR, CC BY 4.0), local zips in `own_data/lgn/` | `prepare/lgn.py`: hexes burned onto the 5 m RD grid (pixel-centre rule), pixel counts per hex and class | done |
| 4 | gemeente/wijk/buurt codes + names | [CBS Wijk- en Buurtkaart 2024 v2](https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/wijk-en-buurtkaart-2024) | area-weighted mode of buurtcode; wijk/gemeente derived from that buurt | done |
| 5 | static layers (see below) | AHN, LIWO, RIVM, REV, PDOK, BRO | rasters: pixel centre -> `zone_from_point`; vectors: polygon overlay | done |
| 5b | NBBG land use (41 categories, peiljaren 2020 + 2022) | [CBS NBBG2022 mutatiebestand](https://geodata.cbs.nl/files/Bodemgebruik/NBBG2022/) | `hex_overlaps` once, then overlap per category for both years; `config/bbg.toml` | done |
| 5c | BRP crop groups (15 groups, yearly 2018-2023) | BRP gewaspercelen (RVO via PDOK) | overlay fractions per crop group; `config/brp_groups.toml` | done |
| 5d | TESSERA embeddings: 16-dim PCA + year-on-year change (2018-2025, zh only) | [TESSERA](https://github.com/ucam-eo/geotessera) v1.1 annual 10 m embeddings (Sentinel-1/2) | `prepare/tessera_hex.py`: pixel centre -> H3 (`h3` lib), hex mean -> fixed PCA; per-pixel cosine distance between years | done (trial) |
| 5e | BAG verblijfsobjecten per gebruiksdoel (`num_*`, yearly 2018-2025) | [LV BAG levenscyclus-extract](https://service.pdok.nl/kadaster/bag/atom/bag.xml) (PDOK, full history) | state on 1 January; point -> geoplegma `zone_from_point`; `config/bag.toml` | done |
| 5f | wind turbines (`wind_*`, static, snapshot January 2026) | [RIVM windturbines](https://data.overheid.nl/dataset/54341-windturbines---vermogen) (WFS `data.rivm.nl/geo/alo/wfs`) | `prepare/wind.py`: turbine point -> H3 cell; count, power, max hub/rotor/tip height; distance hex centre -> nearest turbine | done |
| 5g | OSM motorways + railways: line file + `osm_*_lengte_m` per cell (static, latest extract) | [Geofabrik](https://download.geofabrik.de/europe/netherlands.html) `netherlands-latest.osm.pbf` (© OpenStreetMap contributors, ODbL) | `prepare/osm.py`: ways by tag, exact line x hex intersection in RD | done |
| 5h | OSM high-voltage grid, substations, solar park power, marinas (`osm_hoogspanning_*`, `osm_onderstation_aantal`, `osm_zonnepark_vermogen_kw`, `osm_jachthaven_aantal`; static) | same Geofabrik extract | `prepare/osm_extra.py`: >= 50 kV only; exact lengths/areas in RD | done |
| 5i | public charging points (`laad_punten_aantal`, `laad_punten_snel_aantal`; static snapshot) | [NDW open data](https://opendata.ndw.nu/) `charging_point_locations.geojson.gz` | `prepare/laadpalen.py`: location point -> H3 cell; points and >= 50 kW points | done |
| 6 | assemble + 0-255 drop-in export (Delta) | — | `assemble.py`: join on `h3_id`, `year_int` | done |
| 5j | climate per year (`knmi_*`) | KNMI Data Platform daily 1 km grids Tg1, Tx1, Tn1, Rd1, EV24 (CC BY 4.0) | `prepare/knmi.py`: yearly indicators per grid cell, cell under the hex centre (coast: nearest cell with data) | done |
| 5k | nitrogen + acid deposition (`gdn_*`), air quality (`gcn_*`) per year | RIVM historical deposition series (edition 2026-10-06), GCN concentration maps per round | `prepare/rivm.py`: 1 km cell under the hex centre | done |
| 5n | livestock per cell (`vee_*`), yearly | CBS StatLine 80781ned per gemeente | `prepare/vee.py`: spread over BRP farmland | done |
| 5o | ammonia emissions per cell (`nh3_*`), 2019-2024 | RIVM Emissieregistratie, ammoniak per subsector, 1x1 km | `prepare/nh3.py`: square split over its hexes by area | done |
| 5p | measured ammonia (`man_*`), yearly | RIVM Meetnet Ammoniak Nederland (man.rivm.nl) | `prepare/man.py`: area mean over the area polygon, extra points in their own cell | done |
| 5q | national wind per year (`datacube_<region>_jaar.parquet`, not per cell) | KNMI daily station data FG/FHX/DDVEC | `prepare/knmi_wind.py`: fixed station set, yearly means, direction shares, calm/storm days | done |
| 5r | permit-holder housing per gemeente and year (`datacube_<region>_gemeente.parquet`, not per cell) | JenV/COA "Overzicht huisvesting vergunninghouders 1 januari" PDFs (open.overheid.nl) | `prepare/vergunninghouders.py download`, then build: parse, map to 2024 gemeenten | done |
| 5m | critical deposition values + exceedance (`kdw_*`) | RIVM Koppeltabel relevante habitats (2026) + GDN historical series | `prepare/kdw.py`: AERIUS hexagon centre -> H3 cell | done |
| 5l | NO2 column from space (`tropomi_*`) per year | KNMI TEMIS monthly TROPOMI NO2 v1.0 (0.125 degree) | `prepare/tropomi.py`: mean of monthly means, 2018 empty | done |

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
  area-weighted mode, and as `_area_weighted_average` = mean percentile midpoint
  (see below), instead of being lost as 0.

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
| ndff_soortendiversiteit_klasse | NDFF soortendiversiteit per km-hok (BIJ12, 2017), all species, class 1-8, area-weighted mean | rho 0.57 |
| ndff_soortendiversiteit_rl_klasse (-> soortenrijkdomsklasse) | idem, Red List species | rho 0.75 |

Notes:
- gifwolk: 57% of hexes vs 63% in the original; RIVM changed the gifwolk
  calculation on 2026-01-01, so the current REV areas are smaller.
- AHN, hitte, groenklasse and bodem have no value over open water / unmapped
  (built-up) areas, where the original seems to have filled 0.
- The original's fractional class values (x/7) suggest it sampled the 7 H3
  res-10 child centres per hex; this pipeline uses every pixel instead.
  The year-independent `aantal_inwoners` (~CBS 2020/2021 totals) will be
  filled from the CBS layer at assembly.

## Outputs (`assemble.py`)

- `out/datacube_zh/` (+ `datacube_zh.parquet`): 302,544 rows (37,818 hexes x
  2018-2025), 399 columns, real values. Includes columns the original lacks:
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
  - All 232 original columns are now produced; `heeft_kepler_data` = scaled
    `aantal_inwoners_sum` > 0 (the original's exact rule); `soortenrijkdomsklasse` = NDFF Red List
    km-hok class; `num_*` are yearly (the original was one snapshot).
  - Validation: median per-year Spearman vs the original 0.987 over 146
    shared columns. Pooled over all years it looks lower (0.60) only because
    the original has zeros for years it was missing (e.g. distances only in
    2021) where this table has real data.
- Differences in meaning the app's `_llm_metadata.json` should pick up:
  distance/count columns now exist for 2018-2023; `natura2000_id` uses
  official Natura 2000 site numbers; nulls where there's no data (instead of
  0); the hex set is 37,818 (204 more border slivers).

## LGN (`prepare/lgn.py`, yearly 2018-2025, zh + nl)

- Source: the WUR LGN2018-LGN2025 rasters (5 m, EPSG:28992, one shared grid). Read at full
  resolution only: the 2018-2021 files carry AVERAGE-resampled overviews whose values are not
  class codes (reading them at a coarser resolution gives codes 0-376).
- Method: per 20 km tile the grid's hexes (`h3` boundaries projected to RD) are burned into a
  hex-index raster with GDAL's pixel-centre rule; each year's pixels are counted per (hex, class).
  `<class>_fraction` = pixels / all pixel centres in the hex (~4,200 per hex). Cross-check: 0 of
  16,000 random pixel centres disagree with `h3.latlng_to_cell`.
- Columns: one per class, named like the old stopgap so the switch is drop-in (a class LGN
  renamed keeps its label per year: code 45 is `natuurgraslanden` to 2020, then
  `natuurlijk_beheerde_agrarische_graslanden`; null in years whose legend lacks the class);
  10 `lgn_groep_*` groups (config/lgn.toml) that sum to `lgn_coverage`; `landgebruiksklasse`
  (dominant class), `lgn_source_year`.
- LGN's own method changes (counted on the rasters, ZH window, km2), flagged per column in the
  manifest: class 45 natural grassland 151 (2018-20), 235 (2021), **10 / 14 (2022 / 2023)**, 107
  (2024-25), the rest mapped as agrarisch gras; roads split into 251-253 from 2021, and 2022 has
  ~50% more road area than 2021/2023; "kale grond in bebouwd gebied" only to 2020; sea mapped
  much further out from 2021; more zoet water from 2022. Use the groups for time series.
- Validation (zh, 2021): vs the old Databricks stopgap, r >= 0.99 for nearly every class where
  the stopgap covered the hex; its 2,964 missing hexes (open water) and dropped polygons (main
  roads 149 vs 56 km2, water) are the only differences, so the old "2022 mapping bug" was LGN
  itself, not the export. vs NBBG 2022: water r 0.98, glastuinbouw 0.98, akker 0.96, agrarisch
  grasland 0.89, zonnepark 0.89, bos 0.84.
- Not yet used: LGN3-LGN7 (1995-2012, 25 m) and HGN 1900-1990 (historical land use), also in
  `own_data/lgn/`; planned as a separate history table.

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
- NBBG (land use) + BRP (crops, yearly) complement LGN: NBBG has the functional categories
  (woonterrein, bedrijfsterrein, recreatie), LGN the yearly cover (crops, nature types, urban green).

## Wind turbines (`prepare/wind.py`, static)

RIVM's `rivm_<date>_windturbines_vermogen` layer has one point per turbine with power (kW), hub height
and rotor diameter, for the Netherlands (land, inland water, sea) plus Belgian and German turbines near
the border. The snapshot in use (`rivm_20260101_…`, 5,357 turbines) is pinned in `prepare/wind.py`
and downloaded once to `own_data/sources/wind/`. Per hex: `wind_aantal`, `wind_vermogen_kw`,
`wind_ashoogte_max_m`, `wind_rotordiameter_max_m`, `wind_tiphoogte_max_m` (empty without a turbine)
and `wind_afstand_min_km` (hex centre to the nearest turbine of any country, land or sea).
zh: 268 turbines in 258 hexes, 775 MW; nl: 3,605 turbines (all 3,592 Dutch ones on land and inland
water, plus 13 foreign ones in border cells), 7.0 GW.

**Static:** the source has no construction or demolition year, so every cube year carries the
January 2026 turbines. A real time series would need the turbines' commissioning dates (e.g. from
OpenStreetMap history or older RIVM snapshots; `rivm_20240101_…` exists); not done.

## OpenStreetMap lines (`prepare/osm.py`, static)

For the games' moving cars and trains. Ways with `highway=motorway/motorway_link/trunk/trunk_link` or
`railway=rail/light_rail/subway/tram` from the latest Geofabrik Netherlands extract (timestamp in the
files' metadata and the column manifest). Two outputs:

- `datacube_<region>_lijnen.parquet` (copied into every release): one row per OSM way that crosses the
  region, **whole ways, not clipped**, so vehicles can move along them across cells. GeoParquet (WKB,
  lon/lat) with `osm_id`, `soort` (snelweg, snelweg_afrit, autoweg, autoweg_afrit, spoor, lightrail,
  metro, tram), `naam`, `ref`, `rijstroken`, `maxsnelheid_kmh`, `eenrichting` (one-way geometry runs in
  the direction of travel), `tunnel`, `brug`, `laag`, `geelektrificeerd`, `gebruik`, `dienst` (yard,
  siding, spur), `lengte_m` and `cellen` (the region's res-9 cells it passes through).
- per cell in the cube (sum at res 8/7): `osm_snelweg_lengte_m`, `osm_autoweg_lengte_m`,
  `osm_spoor_lengte_m` (through track), `osm_spoor_overig_lengte_m` (`service=*` tracks) and
  `osm_metro_tram_lengte_m`. Exact length inside the hex (line x hex boundary in RD); the per-cell
  lengths add up exactly to the lines clipped to the region. Each carriageway and each track counts
  separately; tunnels and bridges are included (filter on `tunnel` in the line file to hide them).

zh: 15,633 ways; 981 km snelweg, 230 km autoweg, 833 km spoor, 641 km service track, 757 km
metro/tram. nl: 72,013 ways; 6,964 / 2,377 / 5,695 / 1,956 / 1,196 km. OSM has no history in this
setup, so every cube year carries the same network. Licence: ODbL, credit "© OpenStreetMap contributors".

## OpenStreetMap grid, solar parks, marinas (`prepare/osm_extra.py`) and NDW charging points (`prepare/laadpalen.py`), static

Per cell, the same value in every year (no history in either source):

- `osm_hoogspanning_lengte_m`: overhead line >= 50 kV (`power=line`), each pylon line once; `osm_hoogspanning_kabel_lengte_m`:
  underground/underwater cable >= 50 kV (less complete in OSM); `osm_hoogspanning_afstand_min_km`: cell centre to the
  nearest overhead line >= 50 kV anywhere in NL.
- `osm_onderstation_aantal`: substations >= 50 kV (or `substation=transmission`). The ~100k neighbourhood transformers
  are mostly missing in OSM (~2,400 mapped), so they are left out.
- `osm_zonnepark_vermogen_kw`: solar parks (`power=plant`, `plant:source=solar`), power split over cells by area. Only
  443 of 930 parks have a usable power tag; the others get area x the median of the tagged ones (1,183 kW/ha, stored in
  the layer metadata). For solar park *area* use the existing `zonneparken_fraction` (LGN, 2021-2025) or
  `bbg_zonnepark_fraction` (NBBG); they agree (zh 1.7-1.9 km2).
- `osm_jachthaven_aantal`: marinas (`leisure=marina`). Commercial port land is not tagged as such in Dutch OSM (it is
  `landuse=industrial`; many Rotterdam basins are only `waterway=canal`), so there is no harbour-area column.
- `laad_punten_aantal`, `laad_punten_snel_aantal` (>= 50 kW): NDW's national register of public charging points
  (operators' OCPI feeds). OSM has only ~25% of these (~46k points vs 203k), hence NDW. Private home chargers are not in it.

zh: 360 km overhead, 472 km cable, 168 substations, 258 MW solar (26 MW estimated), 233 marinas, 57,988 charge points
(1,687 fast). nl: 4,136 km / 2,244 km / 759 / 7,588 MW (892 estimated) / 1,189 / 203,062 (7,444).

## Climate, deposition, air quality (`prepare/knmi.py`, `rivm.py`, `tropomi.py`), yearly

The first layers that are measurements or physical models rather than registers or classifications. All are grids
much coarser than a hex (1 km, TROPOMI ~14 x 9 km), so each hex takes the value of the grid cell under its centre;
neighbouring hexes share a value. `native_resolution` in the `.columns.json` says so.

- `knmi_*`: per calendar year from KNMI's daily grids: mean temperature, heating degree days (< 18 °C, unweighted),
  summer (>= 25 °C), tropical (>= 30 °C) and
  frost days, precipitation total and days >= 20 mm, Makkink evaporation, and the largest potential precipitation
  deficit of the growing season (KNMI definition: cumulative evaporation - precipitation from 1 April, maximum up to
  30 September). The daily files (~14,600 for 2018-2025, ~30 GB) come through the KNMI Open Data API, which allows
  ~1000 requests/hour with a registered key (`KNMI_API_KEY`); KNMI's public anonymous key shares one quota with
  everybody and is mostly rate-limited. `knmi.py download` resumes and waits out rate limits.
  A hex whose centre falls on a grid cell without data (along the coast) takes the nearest cell with data within 3 km.
  Precipitation and evaporation are interpolated over land only, so ~6,600 hexes of open water (Wadden Sea, outer
  Westerschelde) have no `knmi_neerslag_mm`/`knmi_verdamping_mm`/`knmi_zware_neerslag_dagen`/
  `knmi_neerslagtekort_max_mm`; temperature covers every hex.
- `gdn_stikstof_mol_ha`, `gdn_verzuring_mol_ha`: RIVM's historical series, the whole 2005-2025 range recalculated with
  one method (edition 6 October 2026), so suitable for trends. Not the yearly GDN rounds (RIVM: those cannot be strung
  together).
- `gcn_no2_ug_m3`, `gcn_pm10_ug_m3`, `gcn_pm25_ug_m3`, `gcn_nh3_ug_m3`: GCN background concentration maps, one
  calculation round per year. There is no historical concentration series; RIVM warns the rounds are not a series
  (calibration changed), hence trend "care". NH3 2019: the calibrated map of April 2021 (the zip also holds the
  uncalibrated v1.0).
- `tropomi_no2_1e15_cm2`: Sentinel-5P tropospheric NO2 column, mean of KNMI TEMIS's monthly means (reprocessed record
  from May 2018). 2018 is empty (no winter months); a year needs >= 10 months (`tropomi_no2_maanden`).

zh 2018 -> 2025: deposition 1,398 -> 1,070 mol N/ha, NO2 18.8 -> 14.0 ug/m3 (2023-2024 13.0), TROPOMI 5.70 (2019) ->
5.17, with the 2020 COVID dip in both. nl: 1,410 -> 1,017 mol N/ha, NO2 13.7 -> 9.1 ug/m3. zh == nl on all shared hexes.

## Critical deposition values (`prepare/kdw.py`)

RIVM's "Koppeltabel relevante habitats" (2026 monitor): 1 ha AERIUS hexagons with, per nitrogen-sensitive habitat
type, its critical deposition value (KDW, 429-2399 mol N/ha/yr) and relevant area (mapped area x coverage). Each
hexagon goes to the H3 cell containing its centre: `kdw_habitat_m2`, `kdw_min_mol_ha` (most sensitive habitat),
`kdw_gewogen_mol_ha`, `kdw_habitat_type` (static), and per year with the RIVM historical deposition series sampled at
each hexagon: `kdw_overschreden_m2`, `kdw_overschreden_aandeel`, `kdw_overschrijding_mol_ha`. Check: relevant area
1,711 km2 (RIVM 1,711), 2024 exceedance 71.1% of the area (RIVM, with its 1 ha deposition: 69.3%). zh: 85 km2 of
relevant habitat, exceeded 26% (2018) -> 8% (2025).

## Livestock (`prepare/vee.py`)

Farm-level animal counts (RVO I&R, WUR GIAB) are not open data. The finest open level is CBS's agricultural census
per gemeente (StatLine 80781ned, animals on 1 April, each year in that year's boundaries). `vee.py` spreads each
gemeente's count over its farmland of that year (BRP): grazing animals (cattle, dairy cows, sheep, goats, horses)
over grass + maize + fodder crops, housed animals (pigs, chickens, other poultry) over all farmland; gemeenten with
animals but no BRP land spread over their whole area. Hexes go to the gemeente of the year containing their centre
(Wijk- en Buurtkaart; its land and water features are merged per code first). Always computed on the national grid,
so zh == nl; gemeente totals are exact. `vee_bedrijven_*` = number of farms, spread the same way. NL cattle
3.92 M (2018) -> 3.64 M (2025), pigs 12.4 M -> 9.8 M, chickens 105 M -> 88 M. A cell value is a plausible density,
not a farm: housed animals especially live in barns, not on the fields they are spread over.
Zeros CBS uses for "not published" become null: a measure that is 0 in every gemeente in a year (horses 2022), and
from 2024 a farm count of 0 in a gemeente that still has those animals (~20-130 gemeenten per farm column).

## Ammonia emissions (`prepare/nh3.py`)

RIVM's Emissieregistratie (dataset "ER Reeks 1990-2024 Definitief") publishes ammonia emissions per 1x1 km square
(5x5 km at sea) per subsector; the export is the portal's own form, scripted in `nh3.py download` (one ~60 MB xlsx
per year, 1-3 min each). Regionalised years are 2019-2024 only (before: 2015, 2010, ...; 2025 national only until
early 2027), so 2018 and 2025 are null. Square codes are the lower-left corner in RD km (`014369_km1`; checked:
cattle emissions per square follow `vee_rundvee` better with this than with the centre, Spearman 0.76 vs 0.65).
Each square's emission is split over the hexes whose centre lies in it, by hex area, so sums are kept.

The animal columns (`nh3_rundvee_kg`, `_varkens_`, `_pluimvee_`, `_overige_dieren_`) hold all emissions of that
animal's manure: stable, storage, grazing and application on the field. Cattle 2024: 52.7 kt here against 30 kt for
stable + storage only in CBS 83982NED. So they also land on fields away from the farm. `nh3_kunstmest_kg`
(fertiliser), `nh3_landbouw_kg` (whole sector) and `nh3_totaal_kg` (all sectors) complete it. Tier modelled; RIVM's
deposition (`gdn_*`) is calculated from these emissions and livestock counts are an input, so not independent.

## Measured ammonia (`prepare/man.py`)

RIVM's Meetnet Ammoniak Nederland (until 2026 "in Natuurgebieden") measures monthly ammonia with passive samplers in
~100 nature areas (1-15 points each) and, since 2021, at 17 points outside them. man.rivm.nl offers one csv with
the yearly mean per point (2005-2025, no coordinates) and the area polygons + extra point coordinates as the site's
own geojson. Per hex: inside an area, the mean of its points that year; the cell of an extra point, that point.
The Veluwe cross-section points have no location and are skipped. Validation: per area and year, `man_nh3_ug_m3`
vs `gcn_nh3_ug_m3` r 0.83-0.94 (2018-2025), mean levels within 0.4 ug/m3 (GCN is calibrated on MAN, so this checks
the placement, not the model).

## National wind per year (`prepare/knmi_wind.py`)

Wind per cell per year says little: the pattern between places is the same every year and what changes between
years is one national signal. So wind is a separate table of 8 rows, `datacube_<region>_jaar.parquet` (`year_int` +
`wind_*`), shipped with every release (same file for zh and nl). From KNMI's daily station data, only the stations
complete (>= 95% of days) in every year 2018-2025 (39 of 48, coast and IJsselmeer included), so station changes don't
make fake jumps: mean speed, share of station-days per 45-degree direction sector (`wind_zw_w_aandeel`,
`wind_o_no_aandeel`), calm days (station mean < 2 m/s) and storm days (hourly mean >= 20.8 m/s, Beaufort 9, at one or
more stations). An upwind-emission index per cell is not built: only if a consumer finds a signal worth refining.

## Permit-holder housing per gemeente (`prepare/vergunninghouders.py`)

For cube-sim's asylum card. The ministry gives every gemeente a target (taakstelling) per half year for housing
permit holders (statushouders), proportional to population; COA tracks what each gemeente actually housed. Source:
the monthly PDF "Overzicht huisvesting vergunninghouders" (JenV/COA, open.overheid.nl, scripted search + download);
the 1 January file of year Y+1 closes year Y in year Y's gemeenten. Three table layouts (2018; 2019-2022; 2023 on,
with a corrections column), each row checked on its own sums. Unlisted corrections (a 1 July stand that isn't
begin + target - housed; five 2021 year-end stands, Rotterdam -41) are kept as published and counted in
`vh_correcties`, so `vh_stand_eind = vh_stand_begin + vh_taakstelling + vh_correcties - vh_realisatie` holds on
every row. Name differences (2018 spellings, typos in the 2019 and 2023 files, province suffixes) are aliased.

Not per cell: a gemeente total spread over hexes would invent detail. So it is a side table
`datacube_<region>_gemeente.parquet` (`year_int`, `gemeentecode`, `gemeentenaam`, `vh_*`), keyed on the cube's own
static gemeenten (Wijk- en Buurtkaart 2024): earlier gemeenten map onto their 2024 gemeente by hex centres, weighted
by that year's inhabitants when one was split (Haaren 2021), so national totals are exact (2018 target 23,880; 2025
28,000 = 15,000 + 13,000 as in the ministry's letter). Stand: + = backlog. Researched but not built: COA reception
locations (no official series; only Wayback snapshots of coa.nl, coordinates + capacity from mid-2022, names only
before), asylum seekers in reception per gemeente (COA occupancy is national only, CBS has no gemeente table).

## zh as a slice of nl (`slice_of` in `config/regions.toml`)

Building zh separately duplicated every prepare step and let the two drift apart. Now `assemble.py --region zh` takes
the zh grid's hexes from the assembled nl cube, replaces the grid columns (`province_fraction`, `code`, `naam`:
share inside Zuid-Holland) with zh's own, adds the zh-only TESSERA columns (same position as before), cuts the
whole-line OSM file to lines crossing zh (`cellen` restricted to zh), and writes the same outputs as before
(Delta, parquet, columns.json, the 0-255 `volledige_tijdreeks_delta`). Compared with the last separate zh build,
only the static rasters changed (12 columns, now from nl's 25 m fetch instead of native resolution; zh mean ground
height -0.37 -> -0.40 m NAP) and NDFF at the zh border (zh had clipped its source); every other column is identical
(CBS, BRP, BAG, wb, lbm, NBBG, LGN, ... to 1e-6), as are TESSERA and the 15,633 OSM lines. The zh-specific
`work/layers/*` (except TESSERA and grid) are no longer read.

## Provenance and availability (`config/provenance.toml`)

Every column carries `tier` (measured, register, mapped, classified, modelled, embedding, meta, key),
`native_resolution`, `reference_period` (what moment a cube year describes, e.g. WOZ = 1 January of Y-1, income ~Y-2)
and `time` (yearly, biennial, editions, static) in its `.columns.json`, from the first matching rule in
`config/provenance.toml`. `release.py` refuses a cube with a column no rule covers, shows the tiers per concept in
`GUIDE.md`, and writes `availability.parquet`: one row per column and year with fill rate, provenance, the guide's
trend verdict and the steps detected in that year. Meant for analyses (and the analyst agents) to say what a claim
rests on and where data is missing.

Known breaks are machine-readable too: `[[break]]` entries in `config/guide.toml` (columns as globs, the years whose
values are not comparable with the year before, a kind: method / classification / definition / edition / coverage /
preliminary / noise, and a note). They end up in `availability.parquet` (`breaks`, `break_note`), in a "Known
breaks" table in `GUIDE.md`, and mark matching detected steps in `diff.md` as `[known break: kind]`. Steps that are
documented only in prose are real changes (BRP 2023: new CAP rules) or not explained yet (births 2022, cafes
within 1 km 2022, distance to a transfer station 2019/2021, western migration background fill 2020, Leefbaarometer
physical environment): don't declare a break without knowing the cause.

## Source checks (`sources.py`)

`datacube-zh/sources.py` compares, per source, the version the cube is built from (read from the
prepare scripts and configs that pin it) with what the publisher offers now (CBS file listings,
PDOK ATOM, lgn.nl, the geotessera registry, WCS capabilities, HTTP Last-Modified). `NEW` = a newer
version for a cube year (or a new CBS vk100 year, which is what extends the cube); `+` = a year
after the cube's last year, shown but not alarming. Exit code 10 on NEW, so it can run from a timer.
Acting on NEW: update the pin (URL/version in the prepare script or config), re-run that step,
assemble, coarsen, release. Report also in `own_data/sources_check.json`.

## Coarser resolutions (`coarsen.py`)

`out/datacube_<region>_r8.parquet` and `_r7` (H3 res 8 ~0.74 km2, res 7 ~5.2 km2), same columns plus
`n_children` (res-9 cells in the grid, max 7 / 49) and `covered_area_m2`; built straight from res 9
(res 7 not via res 8). Each column's rule is in the `_r<res>.columns.json` (`rule`, `weight`):
sums for counts (`*_sum`, `num_*`, 3D BAG totals, pixel counts), area-weighted means for shares
(`*_fraction`, `*_coverage`, `lgn_groep_*`; relative to the covered part of the coarse cell), means
weighted by area x layer coverage for averages (CBS, `wb_*`, `lbm_*`), by footprint for 3D BAG
height/build year, by pixel counts for TESSERA; min/max for `*_min`/`*_max`; area-weighted majority
for categories (buurt/wijk/gemeente and code/name pairs taken from the same value); any-true for flags.
Checks (zh): inhabitants, homes, building volume and class areas identical at res 9/8/7; LGN groups
still sum to `lgn_coverage`. Releases include these files when they are newer than the res-9 cube.

## Regions, years and later additions

- `config/regions.toml`: `zh` (default, `own_data/work`, `out/datacube_zh`)
  and `nl` (`work_nl`, `out_nl/datacube_nl`; rasters fetched at >= 25 m).
  The national cube is 468,688 hexes x 2018-2025 (3.75M rows, 371 columns; zh: 399);
  assembly streams one year at a time into a `year_int`-partitioned Delta
  table (~60 s, ~7 GB peak).
- Years 2018-2025. CBS squares 2024 (v2) and 2025 (v1, preliminary; also
  publishes many squares with suppressed residents). The 2025 v1 squares carry
  no distances (`dichtstbijzijnde_*` all -99995), and the GP-post distance is
  absent from 2024 too; these stay null (no carry-forward), consumers list the
  gap per column in their contract (see `config/consumers.toml`). NBBG 2024-2025 use
  peiljaar 2022; LGN is yearly 2018-2025 for both regions.
- CBS sums are "the sum of published values": a hex touched only by
  suppressed squares gets 0 (not null), consistent with hexes without squares.
  A column with no published value at all in a year (absent, or suppressed
  everywhere, e.g. births in 2025 v1) is null in every hex, not 0.
- Wijk- en Buurtkaart 2021 v3 has a wrong `percentage_ongehuwd` (4-5% where
  2020/2022 have ~70-77%): recomputed as 100 - gehuwd - gescheiden - verweduwd
  (`[[remainder]]` in config/wb.toml, applied in prepare/wb.py).
  CBS 2025 split "bouwjaar 15 en later"; `[derived]` in cbs.toml sums it back.
- **3D BAG** (TU Delft, via Portolan NL GeoParquet on source.coop): per hex
  number of buildings (apportioned by footprint share), footprint, LoD2.2
  volume, greenhouse/warehouse footprint, built-up fraction, mean/max height
  (volume / footprint; > 250 m treated as modelling error), mean construction
  year. b3_bouwlagen left out (only ~half the buildings, capped at 5). Checks:
  1.81M buildings in ZH, footprint sum = coverage, kas vs NBBG glastuinbouw
  r 0.98.
- **BRP** 2018-2025 from the local official downloads; exact duplicate
  parcels dropped, overlapping registrations (nature parcels on top of crop
  parcels) resolved by trimming "natuur" first.
- **Buurt kerncijfers** (`wb_*`, 40 indicators: income, wealth, labour,
  households, housing, energy, cars, urbanity) from each year's Wijk- en
  Buurtkaart; area-weighted average of overlapping buurten. Income figures
  run to 2023 (the 2024 v2 / 2025 v1 kaarten don't carry them yet).
  Percentages > 100 in the source are nulled. Income vs CBS WOZ r 0.62.
- **Leefbaarometer 3.0** (`lbm_*`): total score, deviation and the five
  dimensions on the 100 m grid, measurement years 2018/2020/2022/2024; odd
  cube years take the previous measurement (`lbm_source_year`). Only cells
  with addresses are scored. Leefbaarometer vs income r 0.68.
- Final audit (both regions): no duplicate keys, no NaN/inf/sentinels, all
  fractions/percentages in range, NBBG and BRP fractions sum to <= 1 (NL NBBG
  min 0.76 = border hexes partly abroad). Population vs CBS official: NL 2023
  17.14M (CBS-published squares; suppression makes it a few % low).

## BAG verblijfsobjecten (`prepare/bag.py`, `datacube-zh bag`)

- Source: PDOK LV BAG levenscyclus extract (`lvbag-extract-nl.zip`, national, full history). Only the
  VBO member (~1.2 GB) is fetched, by HTTP range; all 26.2M voorkomens are parsed into
  `sources/bag/vbo_voorkomens.parquet` (shared), then clipped per region.
- Per year the state on 1 January (like CBS): voorkomens valid on yyyy-01-01 with status in gebruik /
  in gebruik (niet ingemeten) / verbouwing. Points -> hex with geoplegma `zone_from_point`.
- Columns: `num_verblijfsobjecten` + `num_<gebruiksdoel>` for the 11 BAG gebruiksdoelen (names as in
  the original). A verblijfsobject with several gebruiksdoelen counts for each.
- `eindRegistratie` is not a filter: an ordinary mutation ends both the validity and the
  registration of the previous voorkomen. 0 inactive voorkomens, 0 overlapping validity periods.
- Validation: the original is one recent snapshot (counts of verblijfsobjecten, not panden, despite its
  metadata). Per-year Spearman rises to the present: woonfunctie 0.967 (2018) -> 0.982 (2025); other
  functions 0.86-0.96 in 2025. Current in-use state vs original: 1.80M vs 1.83M woonfunctie, rho 0.983.
  NL woonfunctie 1 Jan 2023 = 8.13M (CBS housing stock ~8.09M).
- Caveat: history as registered today (later corrections apply backwards).

## NDFF soortendiversiteit (`soortenrijkdomsklasse`)

- The original's column is a km-hok product: its fractional (x/7) hexes lie on the 1 km RD grid and
  the value is constant per km-hok (3,441 of 3,444). Source: BIJ12/NDFF "Soortendiversiteit in
  Nederland" (all species and Red List), edition 2017-11-15, classes 1-8, WFS
  geoservices.ndff.nl. Licence listed as "onbekend".
- Columns: `ndff_soortendiversiteit_klasse`, `ndff_soortendiversiteit_rl_klasse` (area-weighted mean
  class, static, null over sea). The drop-in `soortenrijkdomsklasse` uses the Red List map:
  Spearman 0.75 (all species 0.57). The 0 = no-data pattern matches exactly; the values don't, so
  the original likely used an earlier generation of the same map. Checked and ruled out: RIVM
  ANK/ALO bee, earthworm and habitat rasters (rho <= 0.33), km grid shifts.
- Sources: [all species](https://data.overheid.nl/dataset/31470-soortendiversiteit-in-nederland),
  [Red List](https://data.overheid.nl/dataset/31469-soortendiversiteit-in-nederland-op-basis-van-rode-lijstsoorten),
  [NDFF explanation](https://serviceteam.ndff.nl/hc/nl/articles/33167830715677-Soortendiversiteit-kaarten).

## Median household income (`mediaan_inkomen_huishouden_area_weighted_average`)

- CBS squares carry this only in 2018-2019, as national income-percentile classes. `cbs.toml`
  `category_midpoints` turns each class into its range midpoint (00-20 -> 10, 40-80 -> 60), then
  takes the area-weighted average: a percentile position from 0 to 100, not euros. Spearman vs
  buurt standardised income 0.62-0.67, vs WOZ 0.75. 2020+ stays null (not published; the buurt
  kerncijfers have no median household income; see `wb_*`).

## TESSERA embeddings (`prepare/tessera_hex.py`, zh only, 2018-2025)

Annual 10 m embeddings from the TESSERA foundation model (Cambridge; Sentinel-1 + Sentinel-2 time
series, 128 dims), via `geotessera` NPY tiles (v1.1, `cambridge` variant: the default `dclimate` run is
Zarr-only; never mix variants/versions). Tiles 2017-2025 (98 per year, ~9.7 GB per year, sizes checked
against the manifest) + venv live in `own_data/tessera/`.

```bash
python3 -m venv own_data/tessera/.venv
own_data/tessera/.venv/bin/pip install geotessera scikit-learn lightgbm duckdb geopandas pyarrow h3 rasterio
for y in $(seq 2017 2025); do
  own_data/tessera/.venv/bin/geotessera download --bbox 3.75,51.65,5.15,52.35 --year $y -f npy \
    --dataset-variant cambridge -o own_data/tessera/tiles_$y
done
own_data/tessera/.venv/bin/python datacube-zh/prepare/tessera_hex.py --years 2017 2018 2019 2020 2021 2022 2023 2024 2025
uv run datacube-zh/assemble.py --region zh
```

- Columns (28): `tessera_pc01..pc16` + `tessera_pixels` (2018-2025); the change vs the previous year:
  `tessera_change{,_agri,_nonagri}_{mean,share}`, `tessera_change_nonagri_share_dc`, `tessera_{land,agri,nonagri}_pixels`,
  `tessera_change_from` (2018 is compared with 2017, which is downloaded only for that). The nl cube has none.
- PCA (92% of the hex-mean variance) and the change thresholds (p95 of 2024->2025 pixel distances:
  all land 0.368, non-agricultural 0.176) are fitted once and saved in `own_data/tessera/pca_threshold.npz`;
  2017-2023 are projected onto the same axes. `--refit` refits (and changes every year's values).
- Change = per-pixel cosine distance between consecutive years; water (NBBG 2022 classes 70-83) excluded;
  `_agri` = pixels in a BRP parcel in either year, `_nonagri` = the rest. BRP 2017 does not exist, so
  2017->2018 uses BRP 2018 for both years. A field that drops out of BRP (e.g. becomes a building site)
  moves its pixels into `_nonagri`.
- Hex assignment uses the `h3` library (same res-9 cells as geoplegma's h3o), not `raster.rs`; fine for a
  trial, the production route would be a geoplegma raster path.
- 21 North Sea hexes off Hoek van Holland stay null (TESSERA has no tile there).

What it shows (validated against BRP, NBBG, CBS, PDOK aerial photos):
- PCs are land use without labels: PC1 grassland vs water/dunes, PC2 built-up vs sea, PC3 arable
  (r 0.83), PC4 industry/greenhouses vs forest. PC1-2 are stable in every year 2017-2025 (per hex r 0.97-0.99
  vs 2024), PC1-4 province year offsets <= 0.15 SD; PC5-8 carry year effects (up to 0.75 SD), so compare
  years via the change columns or after subtracting each year's province mean. Use ~PC1-8 for similarity/SOM.
- All-land change is dominated by agriculture (crop rotation; Spearman +0.69 with arable share, +0.66
  with BRP crop-group shifts), hence the agri / non-agri split.
- Non-agricultural change finds construction and earthworks: top hexes are mostly NBBG `Bouwterrein`
  (Maasvlakte 2, Vlaardingen, Maassluis, Ridderkerk, Waddinxveen, Hoeksche Waard), Lansingerland's new
  district (51.98, 4.48; sand put down 2023, houses 2025) and recurring earthworks north of Rotterdam
  Noord-West (51.949, 4.40; bare 2017-19, grass 2020-23, bare again 2024-25).
- It also flags transient nature dynamics that are not land-use change: Noordwijk dune slacks (52.30, 4.51)
  were flooded in 2024 only (2024 is 0.20-0.30 from every other year, 2025 is back to 2022), and a Haringvliet
  wetland island at Stellendam (51.823, 4.028) flooded 2023-24 and regrew in 2025. Of the top-200 non-agri
  hexes of 2023->2024, ~19% are such transients (all dune hexes), the Bouwterrein ones are all persistent.
  Test: a transient year is far from both neighbours while those neighbours stay close.
- It is *not* a housing index: over 2022->2024 hexes with 50+ new homes change ~6x more than stable
  ones but the correlation is weak; over one year there is no housing signal.
- Year noise: each year's embedding has its own noise level (median pair distance ~ u_a + u_b +
  0.007 per extra gap year; u non-agri 2017 .039, 2018 .028, 2019 .027, 2020 .026, 2021 .043, 2022 .025,
  2023 .023, 2024 .032, 2025 .022), visible in stable built-up land and forest too, spatially uniform
  (not tile seams). So the raw province non-agri share swings per pair (17-18 7.0%, 18-19 4.1%,
  19-20 3.0%, 20-21 4.5%, 21-22 6.8%, 22-23 3.3%, 23-24 4.1%, 24-25 5.0%) without a trend; the earlier
  "rise" 3.3 -> 5.0% is this input drift. Pairs with 2017, 2021 or 2024 read high. Agri/all-land shares
  carry the same noise (~11-12% agri 2022-25, 6.8-11.6% before).
- Drift correction (default; `--no-drift-correct` skips it) adds `tessera_change_nonagri_share_dc`: the
  non-agri threshold per pair is scaled by p90(non-agri distances of the pair) / p90(of 2024->2025) (0.1386,
  in `own_data/tessera/drift_ref.npz`). Since ~95% of pixels don't really change, the bulk of a pair's
  distances measures that pair's noise level, so this asks "changed unusually much for this pair" instead
  of "moved more than a fixed amount"; it is close to quantile-matching a stable reference class. Province
  share then 4.7-5.9% for every pair (stable built-up spread 1.8x instead of 6x); construction sites keep
  their signal. Use `_dc` for time series; the raw `_share` columns are fine within one pair. Caveat: real
  province-wide change is normalised away too. Non-agri only (crop rotation is real change). The manifest
  warns on the raw `_share` columns.
- Analysis scripts, aerial montages and a stride-3 pixel sample of all 36 year pairs:
  `own_data/work/tessera_analysis/`.

Grassland pilot (`prepare/tessera_grass.py all --out own_data/work/tessera_grass`, 2022, not wired in):
splits grassland into agricultural vs natural (BRP 265/266 vs 331/332/1926 labels, LightGBM). Spatial CV
(5-fold by tile, 84 tiles): pixel AUC 0.926, parcel AUC 0.946, but F1(natural) 0.66: at p >= 0.5 it calls
519 km2 natural vs 131 km2 BRP-natural (12% of BRP 265 parcels; spot checks show wet/extensive meadows, so
it reads management intensity, not registration). Needs calibration (threshold ~0.8 matches the BRP prior,
precision 0.70 / recall 0.72) before a per-hex "extensive grassland" column is worth adding.

## Releases (`release.py`)

`out/` is a working build that changes whenever a layer is rebuilt. Consumers (the SimCityZH games,
ruimtelijke-assistent) read an immutable **release** instead and pin its version, so cube development
never reaches them by accident.

```bash
uv run datacube-zh/release.py --region zh --notes "TESSERA 2018-2021 + drift correction"   # --dry-run to preview
```

- Every release also carries `availability.parquet` (see "Provenance and availability") and `GUIDE.md` (from `config/guide.toml`): per concept the ONE column to use,
  alternatives, and whether year-to-year differences are real change (ok / care / no). And `diff.md` lists
  year-to-year steps WITHIN the release (province mean spiking and coming back, sudden steps > 20% of the
  column's range, columns going empty), each marked documented (column warning or guide) or **new**; a new
  one means: find out whether it's real, then document it or fix the source.
- Writes `own_data/releases/datacube_<region>/<version>/` (version = date, `YYYY.MM.DD`, or `--version`):
  copies of the Delta table, parquet, `.columns.json` (and for zh the 0-255 `volledige_tijdreeks_delta`),
  plus `manifest.json` (git commit, assembly time, rows/columns/years, sources per column group, sha256 of
  every file, contract results), `stats.json` (per column and year: non-null share, min, max, mean) and
  `diff.md` (vs the previous release: columns added/removed/retyped, years, mean shifts > 5% and non-null
  shifts > 0.02). Files are made read-only; an existing version is never overwritten.
- `releases/datacube_<region>/latest` points at the newest release; `CHANGELOG.md` gets one entry per release.
- Refuses to release with uncommitted changes in `datacube-zh/` (`--allow-dirty` overrides, recorded), so
  every release maps to a commit.
- **Contracts** (`config/consumers.toml`): each consumer keeps a contract in its own repo listing the
  columns it reads (type, optional min/max/max_null_share, years). A release that breaks one is refused
  unless `--allow-breaking`, which is recorded. Removed/retyped columns or years also mark a release
  breaking. A meaning change with the same name and type (like the CBS `_sum` fix) is not detected
  automatically: say so in `--notes`, and prefer a new column name for such changes.
- Consumers: pin a version (e.g. a `cube.lock` with the release path), read only from there, and read
  `diff.md` + `CHANGELOG.md` before bumping the pin.
