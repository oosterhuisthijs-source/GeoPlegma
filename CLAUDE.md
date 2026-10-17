# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GeoPlegma is a Rust workspace that provides a common abstraction layer over multiple Discrete Global
Grid Reference Systems (DGGRS) — DGGRID, H3, DGGAL, and a native Hex9/H9 backend — largely conformant
to the OGC API for DGGS. The goal is one consistent API (zones from a bbox/point/parent/id, zone
counts, refinement levels) regardless of which underlying grid engine is doing the work.

## Workspace layout

Workspace members (see root `Cargo.toml`): `geoplegma`, `datacube-zh`, `gp-bindings/js`, `gp-encoding`,
`gp-proj`, `viewer-3d/src-tauri`. `tests/`, `diagrams/`, and `docs/` are explicitly excluded from the workspace.

- **`geoplegma`** — the core crate: the `DggrsApi` port trait, the adapter implementations for each
  backend, and the factory that dispatches between them. This is what everything else depends on.
- **`gp-proj`** — standalone geometry/projection library: polyhedron definitions (icosahedron, etc.),
  ellipsoid/authalic-latitude math, and projections (e.g. vertex-great-circle / "slice and dice") that
  map geographic coordinates to polyhedron faces and back. Independent of the DGGRS abstraction; has a
  legacy `src/old/` module kept for reference.
- **`gp-encoding`** — library + CLI (`gp-encoding` binary) for encoding GDAL-readable rasters into a
  Zarr-backed, DGGRS-indexed internal format, plus querying/stats/pyramid-building over that format.
  Depends on `geoplegma` (with the `serde` feature) and `gdal` (built with `bindgen`, needs
  `libgdal-dev`).
- **`gp-bindings/js`** — napi-rs Node.js native bindings (crate `gp-bindings-js`, cdylib) exposing
  `geoplegma` to JS. Depends on `geoplegma` **by path**, not git — see "hex9-sys / links constraint"
  below for why that matters.
- **`viewer-3d/src-tauri`** — Tauri desktop app (Cargo package name `visualization-app`) that reads
  `gp-encoding` output and renders it with deck.gl. Has its own JS/pnpm toolchain; excluded from
  workspace-wide `cargo clippy`/`cargo test` invocations in CI because of that.
- **`datacube-zh`** — personal research crate (binary `datacube-zh`) plus Python prep scripts that rebuild
  the Zuid-Holland / national H3 res-9 datacube from source data, with `geoplegma` doing all the hex work.
  See its `README.md` and the `own_data/` section below.
- **`gp-dggrs`** — an early-stage stub, **not a workspace member**. Its `Cargo.toml` currently depends
  on itself (`gp-dggrs = { path = "../gp-dggrs/" }`) and `api.rs` is unfinished. Treat as WIP/unwired;
  don't assume it builds.
- **`gp-test-fixtures`** — just a home for static JSON/GeoJSON fixtures used in tests.
- **`tests/`** — workspace-wide integration tests kept outside any single crate. Deliberately excluded
  from the `[workspace]` (per its README, the intent is to test across all members without gluing the
  test crate to one of them, but the final home for this — root `tests/` vs. a proper member like
  `gp-tests` — is still an open question).

## Build / test / lint

Build a single crate (works from repo root or the crate dir):
```bash
cargo build -p geoplegma
```

Run the whole workspace's tests (this is what CI does, `viewer-3d`'s package excluded since it needs
the Tauri/node toolchain):
```bash
cargo test --workspace --exclude visualization-app
```

Run one test / filter by name:
```bash
cargo test -p geoplegma zone_count
```

`cargo fmt --check` and `cargo clippy --workspace --exclude visualization-app --all-targets -- -D
warnings` run in CI but are currently **advisory only** (`continue-on-error: true`) — the codebase
predates them. Don't treat clippy/fmt failures as build breakage; do keep new code clean regardless.

`cargo check` or [`bacon`](https://dystroy.org/bacon/) are faster than a full build for iterating.

### External / system dependencies needed to build everything

- **DGGRID**: the `dggrid` executable must be compiled and either on `$PATH` or referenced explicitly
  — the `dggrid` adapter shells out to it. CI does *not* install it, so DGGRID-dependent tests are
  skipped there (see the `--skip` list in `.github/workflows/ci.yml`): parent/child and zone-count
  equivalence tests for `igeo7`/`isea3h`. If you're changing the DGGRID adapter, run those manually
  with a local DGGRID install.
- **`libgdal-dev`**: needed for `gp-encoding`'s `gdal` dependency (bindgen feature).
- **`cmake` + `libclang-dev`**: needed because `geoplegma`'s `hex9-sys` dependency builds the libhex9 C
  core via cmake and generates bindings with bindgen.

### hex9-sys / `links` constraint

`hex9-sys` (git dependency, pinned by tag, e.g. `v2.1.0`) declares `links = "hex9"`, so **only one
version of it can exist in a single dependency graph**. This is why `gp-bindings/js` depends on
`geoplegma` by **path** rather than git: a git dependency on `geoplegma`'s master would pull in a
second, independently-resolved copy of `hex9-sys`, and two versions can't coexist — it breaks the
whole workspace resolve. If you ever touch the `hex9-sys` pin in `geoplegma/Cargo.toml`, check
`gp-bindings/js` still resolves to the same one.

### Examples

```bash
cd geoplegma && cargo run --example basic
cd gp-proj && cargo run --example basic   # and example/polygon.rs
```

### gp-encoding CLI

```bash
cargo run -p gp-encoding -- convert --input input.tif --output output.zarr --dggrs H3 --report
cargo run -p gp-encoding -- add-level --store output.zarr --target-level 5
cargo run -p gp-encoding -- stats --store output.zarr
cargo run -p gp-encoding -- --help
```

Benchmarks read input files from `gp-encoding/benches/files` and write reports to
`gp-encoding/benches/bench_out`:
```bash
cargo bench --bench convert   # configure DGGRS_TYPES / BANDS constants in benches/convert.rs
```

### viewer-3d (Tauri app)

```bash
cd viewer-3d && pnpm install && pnpm tauri dev    # or `pnpm tauri build`
```

## Architecture: the DGGRS port/adapter pattern

`geoplegma` is structured as ports-and-adapters around one trait:

- **Port**: `DggrsApi` (`geoplegma/src/api.rs`) — the interface every backend implements:
  `zones_from_bbox`, `zone_from_point`, `zones_from_parent`, `primary_parent_from_zone`,
  `zone_from_id`, `zone_count`, plus min/max/default refinement level and relative depth accessors.
  Every method except the level/depth accessors takes an `Option<DggrsApiConfig>` controlling which
  optional fields (`region`, `center`, `vertex_count`, `children`, `neighbors`, `area_sqm`, `densify`)
  get populated on the returned `Zones` — all default to `true`.
- **Adapters**: `geoplegma/src/adapters/{dggrid,dggal,h3o,hex9}/` — one module per backend, each
  implementing `DggrsApi` for the DGGRS variants that engine supports. `dggrid` shells out to the
  external DGGRID binary; `dggal` wraps the `dggal-rust` crate; `h3o` wraps the `h3o` crate; `hex9`
  wraps `hex9-sys` FFI bindings to libhex9. There is also an `adapters/rhealpix/` module in the tree
  that is **not** declared in `adapters/mod.rs` — it's unwired/unused; don't assume it's reachable.
- **Factory**: `geoplegma/src/factory.rs::get(DggrsUid) -> Result<Arc<dyn DggrsApi>, FactoryError>`
  matches first on `DggrsImplementation` (which tool backs the grid), then on the specific `DggrsUid`,
  and constructs the right adapter. `factory::registry()` exposes the static `DGGRS_SPECS` table for
  discovery. Adding a new grid to an existing backend usually means: add a `DggrsUid` variant + entry
  in `constants::DGGRS_SPECS` (`types.rs`/`constants.rs`), wire it into `factory::get`, and implement/
  extend the adapter.
- **Registry**: `geoplegma/src/constants.rs::DGGRS_SPECS` — a static array of `DggrsSpec` (one per
  `DggrsUid`) carrying aperture, min/max/default refinement level, and max/default relative depth.
  `DggrsUid::spec()` indexes into it via a hand-maintained `idx()` match — keep `idx()`, the enum
  variant list, and the `DGGRS_SPECS` array in sync when adding/removing a UID.
- **Core types** (`geoplegma/src/types.rs`): `Point`, `Region`/`BoundingBox` (plain lat/lon, convert to
  `geo` primitives via `to_coord`/`to_geo_polygon`), `Zone`/`Zones` (the output shape, all fields
  gated by `DggrsApiConfig`), `ZoneId` (a `StrId | HexId | IntId` enum — `FromStr` sniffs
  decimal-first, then hex-looking, then falls back to string), and `RefinementLevel`/`RelativeDepth`
  (newtypes over `i32` that reject negative values at construction — always go through `::new`/
  `TryFrom`, never construct with a raw `i32` in adapter code except via the `_const` constructors
  used for static specs).
- **Errors** (`geoplegma/src/error/`): one `thiserror` enum per backend (`dggal.rs`, `dggrid.rs`,
  `h3o.rs`, `hex9.rs`, `factory.rs`, `port.rs`) all folded into the top-level `DggrsError` in
  `error/mod.rs` via `#[from]`. Add backend-specific error variants in that backend's file, not in
  `mod.rs`.

Hex9 is the one backend with materially different semantics worth knowing before touching its tests
(`geoplegma/tests/hex9.rs`): refinement levels run 0..=30 with 12 well-defined L0 base hexagons; the
*addressing* API (`zone_from_point`, `zone_from_id`, `primary_parent_from_zone`) works across that
whole range, but the *enumeration* API (`zones_from_bbox`, `zones_from_parent`) goes through
libhex9's `hex9_grid_create`, which the currently-linked build restricts to layers 1..29 — L0 and the
deepest layers can't be enumerated. `to_zones` never populates `children` for Hex9.

## `own_data/` — CBS overlay reproduction & benchmarking (active investigation)

**Status: committed on branch `feature/own-data-cbs-overlay`** (the `geoplegma/example/own_data_*.rs`
examples, the `own_data/` fetch scripts + column schemas + small sample, and the whole `datacube-zh` crate).
The large fetched/derived data under `own_data/` stays local and gitignored (see `.gitignore`). This is a
personal research thread, not an upstream repo feature; pick it back up by reading this section and
`datacube-zh/README.md`, not by re-deriving context.

### Current state: `datacube-zh` (the follow-up to everything below)

The overlay work below grew into `datacube-zh`, which rebuilds the datacube from original sources with
correct sums (`overlap / square_area`, see `datacube-zh/src/overlay.rs`) instead of reverse-engineering
`own_data`'s parquet. Done and audited:

- **zh is a slice of nl** (`slice_of = "nl"`, 2026-10-16): run prepare steps for nl only; `assemble.py --region zh`
  cuts zh from the assembled nl cube + zh grid columns + TESSERA (zh only). Order: nl assemble/coarsen, then zh.
- Regions in `datacube-zh/config/regions.toml`: `zh` (37,818 hexes → `own_data/out/datacube_zh`) and `nl`
  (468,688 hexes → `own_data/out_nl/datacube_nl`), both `year_int`-partitioned Delta tables for 2018-2025
  (plus `.parquet` and `.columns.json` exports), and the same at H3 res 8 and 7 (`coarsen.py` ->
  `datacube_<region>_r8/_r7.parquet`, rule per column in their `.columns.json`).
- Layers: grid, CBS 100m squares, admin (gemeente/wijk/buurt), static rasters/vectors (AHN, LIWO, RIVM,
  REV, Natura 2000, BRO; RIVM wind turbines `wind_*`, static snapshot Jan 2026; noise in two RIVM editions, `geluid_lden` 2020 + `geluid_lden_2022`), LGN 2018-2025 from the raw WUR 5 m rasters (`prepare/lgn.py`, both regions; history LGN3-7/HGN in `own_data/lgn/` not used yet), CBS NBBG land use, BRP crop groups, 3D BAG,
  buurt kerncijfers (`wb_*`), Leefbaarometer 3.0 (`lbm_*`), BAG verblijfsobjecten per gebruiksdoel (`num_*`,
  yearly, `datacube-zh bag`), NDFF soortendiversiteit (`ndff_*` -> `soortenrijkdomsklasse`), and (zh only,
  2018-2025) TESSERA embeddings (`tessera_*`: 16-dim PCA per hex + year-on-year change, split agri/non-agri).
  CBS breakdowns (age, sex, household type, construction period) add up to their totals: suppressed
  parts are filled in per square (`[[parts]]` in `config/cbs.toml`, amount in `bijgeschat_*_sum`).
  zh cube 404 columns, nl 376. The 0-255 drop-in export now produces all 232 original columns.
- Remaining gaps: median household income only 2018-2019 (CBS stopped publishing it);
  `soortenrijkdomsklasse` matches the original only at Spearman 0.75 (older map edition not found).
  Full list of known gaps and accepted differences is in `datacube-zh/README.md`.

**Separation of concerns (user's explicit wish):** GeoPlegma only *prepares* the datacube. All game /
scenario logic lives elsewhere and reads the cube as read-only input. Consumers read **releases**, not
`own_data/out`: `uv run datacube-zh/release.py --region zh --notes "..."` freezes the assembled cube into
`own_data/releases/datacube_zh/<version>/` (+ `latest` symlink, `CHANGELOG.md`, `diff.md`, `GUIDE.md`), checking
consumer contracts listed in `datacube-zh/config/consumers.toml` (see README "Releases"). This session cuts
releases; the game session bumps its pin. Consumers (each with its own contract): `simzh-deckgl` (zh) and
`simcityzh-unity` (zh + nl), plus `cube-sim` (zh + nl, contract in place) and `cube-analyst` (nl, contract file not
written yet). Announce each release in all three handoff files: `GAME_SESSION.md`, `ANALYST_SESSION.md`,
`SIM_SESSION.md` (the sim, `~/cube-sim`). Current releases: `datacube_zh` 2026.10.17.8, `datacube_nl` 2026.10.17.7
(RIVM deposition 2025 = provisional break; gdn series has a fixed spatial emission pattern). Open: NDVI (`s2`) wiring
+ release once Earth Engine delivers 2022-2025 (method validated on 2018-2021 after the LGN water-mask fix);
upwind index parked (sim v4: no signal); Lbv buy-outs per gemeente (sim, low priority); % aardgaswoningen (2024
buurt map only, offered); analyst contract file missing. Before: zh .17.7 / nl .17.6 (national wind per year as
side file `datacube_<region>_jaar.parquet`, `prepare/knmi_wind.py`). Before: zh .17.6 / nl .17.5 (measured NH3 `man_*`,
`prepare/man.py`; suspected 2019 gas break declared). Before: zh .17.5 / nl .17.4 (NH3 emissions `nh3_*` 2019-2024, `prepare/nh3.py`, scripted
Emissieregistratie export). Before: zh .17.4 / nl .17.3 (heating degree days `knmi_graaddagen`, CBS
livestock zeros -> null, gas warning). Before: zh .17.3 / nl .17.2 (KNMI climate
`knmi_*`, `prepare/knmi.py`). Before: zh .17.1 / nl .17.1 (livestock `vee_*`,
`prepare/vee.py`: CBS census per gemeente spread over BRP farmland). Before: zh .16.14 /
nl .16.12 (KDW per Natura 2000 habitat + yearly exceedance, `prepare/kdw.py`; LIWO flood junk fixed via `valid_max`). Before: zh .13 / nl .11 (machine-readable known
breaks, `[[break]]` in `config/guide.toml` -> `availability.parquet` `breaks`/`break_note` + GUIDE.md table). Before
that, zh .12 / nl .10 added provenance per column (
`config/provenance.toml` -> `tier`/`native_resolution`/`reference_period`/`time` + `availability.parquet`; yearly RIVM
deposition + GCN air quality (`prepare/rivm.py`) and TROPOMI NO2 (`prepare/tropomi.py`). Before: static OSM grid/solar/
marinas, NDW charging, OSM motorways/railways with a whole-line `datacube_<region>_lijnen.parquet`; zh .9 has a wrong
`crs: null` in that line file, skip it). Talk to
the game sessions through `own_data/releases/handoff/GAME_SESSION.md` (append a section per release).

Full rebuild order per region: prepare steps (README) -> `assemble.py` -> `coarsen.py` -> `release.py` (refuses
coarse files older than the cube). `release.py` also writes `GUIDE.md` (from `config/guide.toml`: one column per
concept, trend ok/care/no) and lists year-to-year steps in `diff.md`, each documented or **new**; keep "new" at 0
by explaining a step in the guide/column warning or fixing the source. `python3 datacube-zh/sources.py` checks
every source for newer versions (exit 10 on NEW; years after the cube's range are only listed).
Never edit the game repos from here:
- `~/simzh-deckgl` — the user's deck.gl version of the SimCityZH game (own TS sim, lenses, 3D BAG). Not a
  git repo. `scripts/export_cells.py` converts the release pinned in its `cube.lock` (zh) to
  `public/data/cells_<year>.json`; contract `~/simzh-deckgl/cube_contract.json`.
- `~/simzh-unity/` — the original Unity game (data-only modding via `.bin` grids). Data pipeline in
  `datapack/` (`build_cube_pack.py`, `cube.lock` pinning zh + nl, `cube_contract.json` / `_nl.json`); its
  own CLAUDE.md has the layout. `~/Downloads/SimCityZH v0.95/` is gone (only a `MOVED.md` stub).
- `~/ruimtelijke-assistent` — LLM spatial assistant reading `volledige_tijdreeks_delta` (the 0-255 compat export).

### Night 2026-10-16/17: running unattended (pick up here)

- `datacube-ndvi-watch.service` (systemd user unit): waits for the 8 Earth Engine NDVI exports
  (`prepare/s2_ndvi.py`, tasks in `own_data/sources/s2_ndvi/tasks.json`, output gs://som-scenario/datacube/ndvi/),
  downloads them, then runs `prepare/s2_metrics.py` once -> `work_nl/layers/s2.parquet`. Log:
  `own_data/sources/s2_ndvi/watch.log`. It does NOT release. Next: validate (sealed share vs 3D BAG/NBBG, new
  sealing vs new BAG addresses, 2018/2022 anomaly vs KNMI deficit, green share vs groenklasse), wire `s2` into
  assemble (YEARLY_FILES), provenance/guide/coarsen, then release. Status 2026-10-17 22:00: 2018-2021 downloaded,
  2022-2025 queued for 20+ h; the som-scenario project is in Earth Engine's noncommercial "restricted mode".
  `s2_metrics.py` was run on 2018-2021 and validated (ad hoc): water now from LGN (the NDVI-only rule counted the
  Wadden Sea and lakes as never-green land); never-green vs 3D BAG footprint r 0.53, vs groenklasse -0.58;
  greenhouses sit in between (say so in the column text). The watcher still runs `s2_metrics.py` when all 8 are in. The NL assemble already peaks at ~12.4 GB under the 12 GB cap: raise the cap a bit
  or trim assemble memory before adding `s2`.
- KNMI: download complete (2018-2025, all 5 variables), released in zh .17.3 / nl .17.2.
- Earth Engine auth = gcloud Application Default Credentials (quota project som-scenario); the old
  `~/.config/earthengine/credentials` (expired token) was moved to `credentials.old`.
- Hex index raster (geoplegma, `datacube-zh --region nl hexindex`): `work_nl/s2/hexindex_10m.u32` (3.9 GB).

### Session 2026-10-16 (late evening): measurement layers + analyst-agent plan

- Goal (user): use the NL cube to tell "what happened in the Netherlands" with correlations over time, backed by
  more *measured* data, and later LLM agents as analysts. Timeline stays 2018-2025 (set by LGN/CBS/BAG; TROPOMI
  starts May 2018). Picked: KNMI climate, RIVM GCN air quality, TROPOMI NO2, RIVM nitrogen deposition. Parked by
  the user: energy use per postcode, BRO groundwater, KRW water quality, InSAR, the long res-7 cube, TESSERA for
  NL (the download in `own_data/tessera/` is Zuid-Holland only, bbox 3.75-5.15 E / 51.65-52.35 N).
- Analyst agents live in their own repo `~/cube-analyst` with its own Claude session (user's wish; reads releases via
  its `cube.lock` only; talk through `own_data/releases/handoff/ANALYST_SESSION.md`, announce releases there):
  access layer, hash-chained log, planted-truth tests. Finding: iid p-values call a random smooth field
  significant ~90% of the time, cluster-robust ~15%; hence its `placebo_p()`.
- Plan order: Phase 0 provenance (done) -> KNMI (code done, data downloading) + GDN + GCN + TROPOMI (done) ->
  Phase 2 analyst agents in a separate repo (e.g. `~/cube-analyst`, reads releases only): explorer / tester /
  skeptic / writer roles, read-only DuckDB, change-on-change + fixed effects + spatial block bootstrap, checks at
  res 9/8/7, every test logged with Benjamini-Hochberg, provenance gate on `availability.parquet`, validated on
  planted known truths before use.

### Session 2026-10-16 (evening) recap

- Started after VS Code crashed (likely the Unity NL sim being OOM-killed: >25 GB on this 31 GB machine; the
  Unity session caps NL sims with `systemd-run --user --scope -p MemoryMax=16G`). Keep heavy jobs here under
  ~8 GB (`coarsen.py --memory`, NL LGN/assemble peak ~7-8 GB).
- **Always run NL-region steps under a memory cap**: `systemd-run --user --scope -p MemoryMax=12G -p MemorySwapMax=0
  uv run datacube-zh/<script>.py --region nl ...`. On 2026-10-16 19:32 an uncapped NL assemble (~9 GB at 390
  columns, plus Unity + Firefox + VS Code) ran the machine out of RAM, and the OOM killer took VS Code and every
  Claude session in it down. A local PreToolUse hook (`.claude/hooks/guard.py`, registered in
  `settings.local.json`, not in git) blocks uncapped `--region nl` commands.
- Consumers split and registered; Unity moved to `~/simzh-unity/datapack`. First NL releases.
- LGN from the raw WUR rasters (`own_data/lgn/*.zip`; `prepare/lgn.py`, `config/lgn.toml`): replaces the
  Databricks stopgap, r >= 0.99 where the stopgap was complete; the stopgap's "2022 bug" is in LGN itself.
  Read rasters at full resolution only (2018-2021 overviews are averaged = invalid codes).
- Res 8/7 (`coarsen.py`), source checks (`sources.py`), noise map 2022 edition, `GUIDE.md` + step detection.
- Source fixes found by the audit/step detection: Wijk- en Buurtkaart 2021 `percentage_ongehuwd` wrong at CBS
  (recomputed, `[[remainder]]` in `config/wb.toml`); CBS columns with nothing published in a year are null, not 0
  (`cbs.rs`); suppressed CBS breakdown parts filled in (`[[parts]]`).
- Reported by the game session and fixed: from Wijk- en Buurtkaart 2019 on CBS publishes average buurt incomes
  for only ~14% of buurten; `[fallback]` in `config/wb.toml` fills from wijk, else gemeente (`*_eigen_buurt` =
  share with the buurt's own value). Step detection now also flags fill-rate drops (`kind: fill`).
- Audit of the releases (integrity, ranges, zh == nl on shared hexes, res 9/8/7 totals, logic checks): clean.
  The audit scripts were ad hoc (session scratchpad), not in the repo.
- Open, offered but not done: weekly systemd user timer for `sources.py` (needs the user's OK); the history
  table (HGN 1900-1990 + LGN3-7, ~7 groups per hex and epoch, separate file); new datasets for the games
  (RIVM nitrogen deposition + critical deposition values, Plancapaciteit/Monitor Woningbouw, Primos
  projections, grid congestion map, EP-online energy labels, subsidence InSAR).

### Session 2026-10-16 (afternoon) recap

- Drift correction is now default in `tessera_hex.py` (`tessera_change_nonagri_share_dc`; zh cube 385 cols).
  Release setup added (`release.py`, `config/consumers.toml`); first release cut. Draft game contract +
  handoff note for the game session: `own_data/releases/handoff/`.
- Earlier work on this branch (committed in c67b37b): BAG `num_*` layer (`src/bag.rs`, `prepare/bag.py`, `config/bag.toml`),
  NDFF static layers, income midpoints (`cbs.rs` `category_midpoints`), heeft_kepler_data rule in
  `assemble.py`, TESSERA 2017-2021 + `--drift-correct` flag; README updated. Both cubes reassembled.
- TESSERA findings: the non-agri "rise" 3.3 -> 5.0% is per-year input noise (no trend over 2017-2025);
  Noordwijk dunes and the Goeree wetland are transient flooding (2024 / 2023-24), Rotterdam and Lansingerland
  are real earthworks/construction. Grassland pilot: AUC 0.93-0.95 but over-predicts natural ~4x; not wired in.
- `~/datacube`: commit `0fc345e` on branch `bug/cbs-sum-source-fraction` (not pushed, PR needs a kanban item):
  sum weights = intersect/square area; `h3_polyfillash3` (centroid-only: 88% of 100m squares got no hex, the
  likely real cause of the ~1/10 factor in `own_data`) replaced by a broadcast ST_Intersects join to
  `provinciegrens_h3`; averages no longer count null-valued squares. Verified with local Sedona on CBS 2022:
  matches `datacube-zh` exactly (r 1.000). Downstream `_sum` values become ~8-10x larger. LGN broadcast hint: local Sedona stand-in shows
  no nested-loop fallback (auto-broadcast; forced fallback = RangeJoin, ~2.7x slower) -> code left unchanged;
  real proof needs `.explain()` in a Databricks run (local `az` account lacks feed access).

### Session 2026-10-15/16 recap

- Pushed branch `feature/own-data-cbs-overlay` to the user's fork `oosterhuisthijs-source/GeoPlegma`
  (remote `fork`, branch tracks it). `gh` is logged into two accounts: `oosterhuisthijs-source` (personal,
  for the fork) and `thijsradijs-pzh` (work). Auto mode blocks `git push` for Claude; the user pushes with
  `! git push`.
- `simzh-deckgl`: re-exported `cells_2018..2025.json` (`YEARS` in `export_cells.py` extended to 2025;
  backup of the old export in `data-build/export_backup_20261015_2356/`). The game itself still shows
  2018-2023 only: `src/data.ts:6` `YEARS` and the "2018–2023" trend labels in `src/metrics.ts` are
  game-side work, not done. (Later the same day both games switched to pinned releases; see above.)
- TESSERA (see `datacube-zh/README.md` "TESSERA embeddings" for method + findings): tiles 2022-2025
  (~40 GB) + venv in `own_data/tessera/`; `prepare/tessera_hex.py` -> `own_data/work/layers/tessera_<year>.parquet`;
  PCA + thresholds frozen in `own_data/tessera/pca_threshold.npz` (fit 2024+2025). Key findings: PCs =
  land use without labels; all-land change = mostly crop rotation; non-agri change = construction sites;
  not a housing index.
- Considered and parked: grassland agri-vs-natural pilot (`prepare/tessera_grass.py`, written, smoke-tested
  only; matters for the Unity game's nitrogen math), Spheer FM (gated HF dataset, nature-focused), Clay on
  PDOK luchtfoto's (only worthwhile for rooftop solar; needs ROCm on the AMD RX 9060 XT).

### What `own_data/` is

`own_data/volledige_tijdreeks_met_namen.parquet` is a **personally-built** dataset (not part of this repo's
normal build), combining CBS vierkantstatistieken (100m population/housing grid), LGN land-use, and some
NationaalGeoregister layers, pre-aggregated to H3 resolution 9 for the province of Zuid-Holland, for years
2018-2023 (37,614 unique hexes, 225,684 cell-year rows). Every `aantal_*`/`gemiddeld(e)_*` column is
**min-max normalized to 0-255 per column across all years combined** (confirmed: exact min 0.0 / max 255.0).
This is *separate* from the real production pipeline that generates the underlying CBS/LGN consumption
tables — see "The real pipeline" below.

### What was built (all in `geoplegma/example/`, all runnable via `cargo run --release --example <name>`)

- **`own_data_h3_coverage.rs`** — hydrates every dataset hex's polygon via `H3Impl::zone_from_id`, writes
  `own_data/h3_cells_hydrated.geojson`, and diffs against `zones_from_bbox` over the dataset's bounding
  rectangle to find "gap" cells (`own_data/h3_coverage_gap_centers.geojson`). Confirmed: real gaps here are
  mostly the bbox rectangle spilling into the sea/neighbouring provinces, not real data holes — the real
  pipeline clips to the actual province polygon instead of a bbox (see below).
- **`own_data_cbs_overlay.rs`** — the core method-validation tool: hydrates 5 hand-picked hexes, loads a
  small CBS 100m-square sample (`own_data/cbs_vk100_sample.geojson`, fetched from PDOK's OGC API, see the
  file's doc comment for the exact curl), does a real polygon intersection (`geo::BooleanOps`) + area-weight
  overlay, and reports the **scale factor** vs. the stored (normalized) value. Also where a real bug was
  found and fixed: PDOK returns clockwise-wound rings, which makes `geo::GeodesicArea::geodesic_area_unsigned()`
  silently return the area of the sphere's *other side* (~5.1e14 m²) unless you `.orient(Direction::Default)`
  first — do this to any polygon parsed from PDOK GeoJSON before computing area.
- **`own_data_province_overlay.rs`** — full-province, single-year, 2-column (`aantal_inwoners`/`aantal_woningen`)
  benchmark: R-tree (`rstar`) spatial index over CBS squares, hex hydration + overlay for all 37,614 hexes.
  Also has the partition-of-unity correctness check (per-CBS-square total claimed weight, should be ≤1.0)
  and a mass-conservation check (raw vs. apportioned totals). Measured: ~48ms H3 hydration + ~800ms overlay
  for all hexes, one year.
- **`own_data_province_overlay_allcols.rs`** — same thing but all 135 numeric CBS columns at once (weight
  computed once per hex/square pair, reused across every column — this is why going 2→135 columns cost
  ~0ms extra: ~770ms either way). Exports a per-hex ndjson (geometry + a few highlight columns + coverage)
  for visualization.
- **`own_data/fetch_cbs_full_province.py`** / **`fetch_cbs_full_province_allcols.py`** — tiled/parallel PDOK
  fetchers (province bbox split into an 8x8 grid, fetched concurrently, since PDOK's OGC API caps `limit` at
  1000/page with cursor pagination). ~75k squares in ~23s per year. `_allcols` variant keeps all 135 numeric
  columns (writes a sidecar `*.columns.json` with the positional column order) instead of just 2.
- All 6 years (2018-2023) have been fetched both ways: `own_data/cbs_vk100_zuidholland_<year>.ndjson` (2 cols)
  and `..._<year>_allcols.ndjson` + `..._allcols.columns.json` (135 cols).

### The validated finding: what `own_data`'s `_sum` columns actually contain

`own_data`'s `aantal_inwoners_sum`/`aantal_woningen_sum` are **not** a naive area-weighted apportioned total —
replicating the *actual* production formula (see next section) against real fetched CBS data reproduces the
stored values to a clean, consistent **~1.20x** (inwoners) / **~0.58x** (woningen) scale factor across every
hex tested (vs. ~10.6x/~5.1x for a mathematically "correct" apportioned sum). The residual constant factor is
plausibly `own_data`'s own subsequent 0-255 normalization interacting with the pipeline's actual (buggy, see
below) formula — not fully pinned down, and not worth chasing further unless it becomes load-bearing again.

### The real production pipeline: `/home/thijsradijs/datacube` (separate repo, has its own CLAUDE.md)

This is the actual Databricks/PySpark/Sedona pipeline that produces the CBS/LGN consumption tables `own_data`
is presumably derived from. Two concrete, evidenced findings from reading it (not yet reported upstream or
fixed):

1. **`cbs_integration_to_consumption.py`'s `_sum` aggregation is actually a coverage-weighted average, not a
   sum.** `compute_hex_weights` computes one `coverage_fraction = intersect_area / hex_area` per
   (square, hex) pair and `aggregate_by_h3` feeds that same column into both `area_weighted_sum_expr` and
   `area_weighted_average_expr` — correct for average (weights sum to ~1 across a hex), wrong for sum (needs
   `intersect_area / square_area` instead, so contributions from multiple squares actually add up). Proof
   this is a real, known bug pattern: `geodata_to_h3_processing/geodata_integration_to_consumption.py`
   already found and fixed this *exact* mistake (comment: "The previous code divided by `hex_area`... instead
   of `area`... for a polygon spanning multiple hexes, that neither summed to the original value nor
   represented a meaningful per-hex fraction") — the fix was never backported to `cbs`.
2. **`lgn_integration_to_consumption.py`'s spatial join against the province H3 grid is missing the
   `/*+ BROADCAST(h) */` hint** that `geodata_to_h3_processing` proved necessary for the identical join
   pattern (comment there: without it, Sedona can fall back to a non-indexed nested-loop join, "which is why
   some layers ran 20x slower than others"). `datacube_common/data_check.py::check_area_coverage` and
   `geodata_to_h3_processing` both have the hint; `lgn`'s main production join does not.

### Artifacts published this session (private, viewable at claude.ai/code/artifacts)

- 2022-only `aantal_inwoners` choropleth of all 37,614 hexes (hover for exact values):
  `https://claude.ai/code/artifact/59ba16b0-057e-41a1-afe7-f9485a2f30cc`
- 6-year (2018-2023) x 4-metric (`aantal_inwoners`/`aantal_woningen`/`gemiddelde_woz_waarde_woning`/
  `gemiddeld_gasverbruik_woning`) interactive version with year/metric pickers:
  `https://claude.ai/code/artifact/47edc339-1fa0-4fbc-bb10-a8aa27746dc0`
- Both built from `own_data/artifact_build/*.html` (source kept there for re-publishing) using a packed
  binary payload embedded in the page (not raw GeoJSON — keeps an ~8MB, 6-year, 4-metric page instead of
  ~50MB+), decoded client-side into typed arrays.

### Environment gotchas hit while building this (this machine specifically)

- `hex9-sys` needs `cmake` (not installed by default here) — fixed with `pip install --user cmake`
  (`~/.local/bin` is already on `$PATH`).
- `dggal-rust`'s bundled C build needs a **static** `libz.a`, which Fedora's `zlib-devel` doesn't ship
  (Ubuntu's `zlib1g-dev` does, which is why CI never hit this). Worked around by pointing `LIBRARY_PATH` at
  a `libz.a` pulled from an existing container image on this machine — not a real fix, just unblocks local
  builds. A machine with `zlib-static` installed doesn't need this.

### Plausible next steps (not started)

(See also the "open" bullet in the evening recap above, which is more current.)

- TESSERA follow-ups: check the unexplained hotspots on the aerial photo (Noordwijk dunes ~52.30,4.51;
  Rotterdam ~51.949,4.40; Lansingerland ~51.985,4.483; Goeree wetland ~51.823,4.028); decide whether the
  rising non-agri change (3.3% -> 5.0%) is real or input drift; optionally add 2017-2021 (same PCA, no refit);
  optionally run the grassland pilot (`prepare/tessera_grass.py all`).
- Game side (in `~/simzh-deckgl`, not here): extend `YEARS` to 2025,
  and use `tessera_*` columns for e.g. a change lens, "find similar places" or analogue-based scenarios.
- Fill the remaining null columns in `datacube-zh` (see "Current state" above).
- Port the CBS `_sum` fix upstream into `datacube` (add a `source_fraction` weight, wire it into
  `aggregate_by_h3` for `sum`-type columns only). `datacube-zh/src/overlay.rs` already implements the
  correct weighting and can serve as the reference.
- Time `lgn_integration_to_consumption.py` with/without the missing broadcast hint (via `just run-lgn` or a
  real Databricks run) to confirm the 20x-slower risk is real here too, not just plausible.
- If continuing the Databricks-vs-geoplegma comparison: no real Databricks timing numbers exist yet, only
  reasoned expectations (see chat history) — getting an actual `cbs`/`lgn` job duration would make that
  comparison real instead of inferred.

## Git workflow

Two long-lived branches: `develop` (integration) and `master` (always releasable — never commit
directly, only merges via release PR + tags). Branch protection requires PRs, passing `ci` check, and
review approvals (2 for `master`, 1 for `develop`), plus linear history on `master`.

- Feature branches are cut from `develop`: `feature/<name>`, `task/<name>`, `bug/<name>`,
  `refactor/<name>`, `hotfix/<name>`.
- PRs into `develop` are **squash-merged** (never merge-commit — keeps `develop`'s history atomic per
  PR).
- Releases: a "release PR" from `develop` → `master` is merged with a **regular merge commit** (not
  squashed), so `master`'s history shows the batch of already-squashed commits. Then
  `cargo release <patch|minor|major>` (config in `release.toml`) bumps the single shared
  `[workspace.package].version`, commits, tags `vX.Y.Z`, and pushes — which triggers
  `.github/workflows/release.yml` (re-runs tests/clippy, publishes eligible crates, cuts a GitHub
  Release) and `release-js.yml` (builds the JS native bindings).
- `geoplegma` and `gp-encoding` are marked `publish = false` — they depend on unreleased git deps
  (`dggal-rust`, `hex9-sys`) that block crates.io publishing.
- Every PR must reference a kanban board item/issue tagged `Feature`/`Task`/`Bug` (or `Suggestion` if
  none of those fit).
