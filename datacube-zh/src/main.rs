//! Rebuild the H3 datacube (Zuid-Holland, or any region in
//! config/regions.toml, e.g. the whole Netherlands) from source data, one
//! layer at a time.
//!
//! Each subcommand reads prepared source files (see `prepare/`) and writes one
//! Parquet file keyed by `h3_id` (+ `year_int` for yearly layers) under the
//! region's work directory. Hex geometry always comes from geoplegma's H3
//! adapter.

mod admin;
mod bbg;
mod brp;
mod cbs;
mod geoparquet;
mod grid;
mod overlay;
mod raster;
mod static_layers;

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::{Context, Result, anyhow};
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// Root data directory (holds `sources/` and `work/`).
    #[arg(long, global = true, default_value = "own_data")]
    data: PathBuf,
    /// Region key from config/regions.toml (zh = Zuid-Holland, nl = Nederland).
    #[arg(long, global = true, default_value = "zh")]
    region: String,
    #[arg(long, global = true, default_value = "datacube-zh/config/regions.toml")]
    regions: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Build the base H3 grid: every cell overlapping the region.
    Grid {
        /// PDOK `provinciegebied` GeoJSON.
        #[arg(long)]
        boundary: Option<PathBuf>,
    },
    /// Aggregate CBS 100m squares onto the grid, per year.
    Cbs {
        #[arg(long, value_delimiter = ',', default_values_t = [2018, 2019, 2020, 2021, 2022, 2023])]
        years: Vec<i64>,
        #[arg(long, default_value = "datacube-zh/config/cbs.toml")]
        config: PathBuf,
    },
    /// Assign gemeente/wijk/buurt (CBS Wijk- en Buurtkaart) to each hex.
    Admin,
    /// CBS Nieuw Bestand Bodemgebruik: per-hex land-use fractions (2020, 2022).
    Bbg {
        #[arg(long, default_value = "datacube-zh/config/bbg.toml")]
        config: PathBuf,
    },
    /// BRP gewaspercelen: per-hex crop-group fractions, per year.
    Brp {
        #[arg(long, value_delimiter = ',', default_values_t = [2018, 2019, 2020, 2021, 2022, 2023])]
        years: Vec<i64>,
        #[arg(long, default_value = "datacube-zh/config/brp_groups.toml")]
        config: PathBuf,
    },
    /// Aggregate the static raster/vector layers from config/static.toml.
    Static {
        #[arg(long, default_value = "datacube-zh/config/static.toml")]
        config: PathBuf,
        /// Only these layers (comma-separated names); default all.
        #[arg(long, value_delimiter = ',')]
        only: Vec<String>,
    },
}

/// One entry of config/regions.toml (only what the Rust side needs).
#[derive(serde::Deserialize)]
struct Region {
    name: String,
    province_codes: Vec<String>,
    work: String,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let regions: BTreeMap<String, Region> = toml::from_str(
        &std::fs::read_to_string(&cli.regions).with_context(|| format!("reading {}", cli.regions.display()))?,
    )?;
    let region = regions
        .get(&cli.region)
        .ok_or_else(|| anyhow!("unknown region {} (known: {:?})", cli.region, regions.keys().collect::<Vec<_>>()))?;
    let work = cli.data.join(&region.work);
    let grid_path = work.join("grid.parquet");
    eprintln!("region {} ({}) -> {}", cli.region, region.name, work.display());

    match cli.command {
        Command::Grid { boundary } => {
            let boundary = boundary.unwrap_or_else(|| cli.data.join("sources/bestuurlijk/provincies.json"));
            grid::build(&boundary, &region.province_codes, &grid_path)?;
        }
        Command::Cbs { years, config } => {
            let config = cbs::CbsConfig::load(&config)?;
            let hexes = grid::load_hexes(&grid_path)?;
            for year in years {
                let input = work.join(format!("cbs/cbs_{year}.parquet"));
                let out = work.join(format!("layers/cbs_{year}.parquet"));
                cbs::run(&config, &hexes, &input, year, &out)?;
            }
        }
        Command::Admin => {
            let hexes = grid::load_hexes(&grid_path)?;
            admin::run(
                &hexes,
                &work.join("wijkbuurt/buurten.parquet"),
                &work.join("wijkbuurt/wijken.parquet"),
                &work.join("layers/admin.parquet"),
            )?;
        }
        Command::Bbg { config } => {
            let config = bbg::BbgConfig::load(&config)?;
            let hexes = grid::load_hexes(&grid_path)?;
            bbg::run(&config, &hexes, &work.join("bbg/nbbg_undissolved.parquet"), &work.join("layers"))?;
        }
        Command::Brp { years, config } => {
            let config = brp::BrpConfig::load(&config)?;
            let hexes = grid::load_hexes(&grid_path)?;
            for year in years {
                brp::run(
                    &config,
                    &hexes,
                    &work.join(format!("brp/brp_{year}.parquet")),
                    year,
                    &work.join(format!("layers/brp_{year}.parquet")),
                )?;
            }
        }
        Command::Static { config, only } => {
            let config = static_layers::StaticConfig::load(&config)?;
            let hexes = grid::load_hexes(&grid_path)?;
            let out = if only.is_empty() { "layers/static.parquet".to_string() } else { "layers/static_partial.parquet".to_string() };
            static_layers::run(&config, &hexes, &work.join("static"), &work.join(out), &only)?;
        }
    }
    Ok(())
}
