//! Rebuild the Zuid-Holland H3 datacube from source data, one layer at a time.
//!
//! Each subcommand reads prepared source files (see `prepare/`) and writes one
//! Parquet file keyed by `h3_id` (+ `year_int` for yearly layers) under the
//! work directory. Hex geometry always comes from geoplegma's H3 adapter.

mod cbs;
mod geoparquet;
mod grid;
mod overlay;

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// Root data directory (holds `sources/` and `work/`).
    #[arg(long, global = true, default_value = "own_data")]
    data: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Build the base H3 grid: every cell overlapping the province.
    Grid {
        /// PDOK `provinciegebied` GeoJSON.
        #[arg(long)]
        boundary: Option<PathBuf>,
        /// Province code (28 = Zuid-Holland).
        #[arg(long, default_value = "28")]
        code: String,
    },
    /// Aggregate CBS 100m squares onto the grid, per year.
    Cbs {
        #[arg(long, value_delimiter = ',', default_values_t = [2018, 2019, 2020, 2021, 2022, 2023])]
        years: Vec<i64>,
        #[arg(long, default_value = "datacube-zh/config/cbs.toml")]
        config: PathBuf,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let work = cli.data.join("work");
    let grid_path = work.join("grid.parquet");

    match cli.command {
        Command::Grid { boundary, code } => {
            let boundary = boundary.unwrap_or_else(|| cli.data.join("sources/bestuurlijk/provincies.json"));
            grid::build(&boundary, &code, &grid_path)?;
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
    }
    Ok(())
}
