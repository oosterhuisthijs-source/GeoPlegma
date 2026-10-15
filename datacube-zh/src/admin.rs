//! Administrative units per hex (CBS Wijk- en Buurtkaart): the buurt covering
//! most of the hex, with its wijk and gemeente derived from that buurt so the
//! hierarchy is always consistent within a hex.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use anyhow::{Result, anyhow};

use crate::geoparquet::{OutColumn, PolygonTable, read_polygon_table, write_table};
use crate::grid::Hex;
use crate::overlay::overlay;

fn string_col<'a>(t: &'a PolygonTable, name: &str) -> Result<&'a Vec<Option<String>>> {
    t.strings.get(name).ok_or_else(|| anyhow!("column {name} missing"))
}

pub fn run(hexes: &[Hex], buurten: &Path, wijken: &Path, out: &Path) -> Result<()> {
    let t0 = Instant::now();
    let buurten = read_polygon_table(buurten)?;
    let wijken = read_polygon_table(wijken)?;

    let codes = string_col(&buurten, "buurtcode")?;
    let result = overlay(hexes, &buurten.geometries, &[], &[codes.as_slice()], false);
    let hex_buurt = &result.modes[0];

    // buurtcode -> its attributes; wijkcode -> wijknaam.
    let attrs = ["buurtnaam", "wijkcode", "gemeentecode", "gemeentenaam"];
    let mut by_buurt: HashMap<&str, usize> = HashMap::new();
    for (i, c) in codes.iter().enumerate() {
        if let Some(c) = c {
            by_buurt.insert(c.as_str(), i);
        }
    }
    let wijknamen: HashMap<&str, &str> = string_col(&wijken, "wijkcode")?
        .iter()
        .zip(string_col(&wijken, "wijknaam")?)
        .filter_map(|(c, n)| Some((c.as_deref()?, n.as_deref()?)))
        .collect();

    let lookup = |attr: &str| -> Result<Vec<Option<String>>> {
        let col = string_col(&buurten, attr)?;
        Ok(hex_buurt
            .iter()
            .map(|b| b.as_deref().and_then(|b| col[by_buurt[b]].clone()))
            .collect())
    };

    let mut columns = vec![
        ("h3_id".to_string(), OutColumn::Str(hexes.iter().map(|h| Some(h.id.clone())).collect())),
        ("buurtcode".to_string(), OutColumn::Str(hex_buurt.clone())),
    ];
    let mut wijkcodes = Vec::new();
    for attr in attrs {
        let vals = lookup(attr)?;
        if attr == "wijkcode" {
            wijkcodes = vals.clone();
        }
        columns.push((attr.to_string(), OutColumn::Str(vals)));
    }
    columns.push((
        "wijknaam".to_string(),
        OutColumn::Str(
            wijkcodes
                .iter()
                .map(|w| w.as_deref().and_then(|w| wijknamen.get(w)).map(|n| n.to_string()))
                .collect(),
        ),
    ));
    write_table(out, columns)?;

    let assigned = hex_buurt.iter().filter(|b| b.is_some()).count();
    eprintln!(
        "admin: {} buurten -> {assigned}/{} hexes assigned in {:.2?} -> {}",
        buurten.geometries.len(),
        hexes.len(),
        t0.elapsed(),
        out.display()
    );
    Ok(())
}
