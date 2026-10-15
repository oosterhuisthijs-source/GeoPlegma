//! Minimal GeoParquet reading/writing: just enough for the files produced by
//! `ogr2ogr -f Parquet -lco GEOMETRY_ENCODING=WKB` (polygonal sources) and for
//! writing plain attribute tables keyed by `h3_id`.

use std::collections::HashMap;
use std::fs::File;
use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result, anyhow, bail};
use arrow::array::{Array, ArrayRef, BinaryArray, BooleanArray, Float64Array, Int64Array, StringArray};
use arrow::compute::cast;
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use geo::{Coord, LineString, MultiPolygon, Polygon};
use parquet::arrow::ArrowWriter;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::properties::WriterProperties;

/// A polygonal source table, column-oriented.
pub struct PolygonTable {
    pub geometries: Vec<MultiPolygon<f64>>,
    /// Every numeric attribute column, cast to f64.
    pub numeric: HashMap<String, Vec<Option<f64>>>,
    /// Every string attribute column.
    pub strings: HashMap<String, Vec<Option<String>>>,
}

/// Read a GeoParquet file whose primary geometry column is WKB (multi)polygons.
pub fn read_polygon_table(path: &Path) -> Result<PolygonTable> {
    let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let geo_meta = builder
        .schema()
        .metadata()
        .get("geo")
        .ok_or_else(|| anyhow!("{} has no GeoParquet 'geo' metadata", path.display()))?;
    let geo_meta: serde_json::Value = serde_json::from_str(geo_meta)?;
    let geom_col = geo_meta["primary_column"]
        .as_str()
        .ok_or_else(|| anyhow!("no primary_column in geo metadata"))?
        .to_string();

    let mut table = PolygonTable {
        geometries: Vec::new(),
        numeric: HashMap::new(),
        strings: HashMap::new(),
    };

    for batch in builder.build()? {
        let batch = batch?;
        for (field, column) in batch.schema().fields().iter().zip(batch.columns()) {
            let name = field.name();
            if *name == geom_col {
                let wkb = column
                    .as_any()
                    .downcast_ref::<BinaryArray>()
                    .ok_or_else(|| anyhow!("geometry column is not Binary"))?;
                for i in 0..wkb.len() {
                    table.geometries.push(parse_wkb_polygonal(wkb.value(i))?);
                }
            } else if field.data_type().is_numeric() {
                let col = cast(column, &DataType::Float64)?;
                let col = col.as_any().downcast_ref::<Float64Array>().unwrap();
                table
                    .numeric
                    .entry(name.clone())
                    .or_default()
                    .extend(col.iter());
            } else if matches!(field.data_type(), DataType::Utf8 | DataType::LargeUtf8) {
                let col = cast(column, &DataType::Utf8)?;
                let col = col.as_any().downcast_ref::<StringArray>().unwrap();
                table
                    .strings
                    .entry(name.clone())
                    .or_default()
                    .extend(col.iter().map(|s| s.map(str::to_string)));
            }
            // Anything else (e.g. GDAL's `geom_bbox` covering struct) is ignored.
        }
    }
    Ok(table)
}

/// Parse a WKB Polygon or MultiPolygon (2D, either byte order).
pub fn parse_wkb_polygonal(bytes: &[u8]) -> Result<MultiPolygon<f64>> {
    let mut r = WkbReader { bytes, pos: 0, little: true };
    let geom_type = r.header()?;
    match geom_type {
        3 => Ok(MultiPolygon(vec![r.polygon_body()?])),
        6 => {
            let n = r.u32()?;
            let mut polys = Vec::with_capacity(n as usize);
            for _ in 0..n {
                if r.header()? != 3 {
                    bail!("MultiPolygon member is not a Polygon");
                }
                polys.push(r.polygon_body()?);
            }
            Ok(MultiPolygon(polys))
        }
        // Points/lines have no area, so they can't contribute to an area
        // overlay; return an empty geometry (callers keep row alignment).
        1 | 2 | 4 | 5 => Ok(MultiPolygon(vec![])),
        other => bail!("unsupported WKB geometry type {other} (expected Polygon/MultiPolygon)"),
    }
}

struct WkbReader<'a> {
    bytes: &'a [u8],
    pos: usize,
    little: bool,
}

impl WkbReader<'_> {
    fn take<const N: usize>(&mut self) -> Result<[u8; N]> {
        let end = self.pos + N;
        let slice = self.bytes.get(self.pos..end).ok_or_else(|| anyhow!("truncated WKB"))?;
        self.pos = end;
        Ok(slice.try_into().unwrap())
    }
    fn header(&mut self) -> Result<u32> {
        self.little = self.take::<1>()?[0] == 1;
        let t = self.u32()?;
        if t > 1000 {
            bail!("Z/M WKB geometries are not supported (type {t})");
        }
        Ok(t)
    }
    fn u32(&mut self) -> Result<u32> {
        let b = self.take::<4>()?;
        Ok(if self.little { u32::from_le_bytes(b) } else { u32::from_be_bytes(b) })
    }
    fn f64(&mut self) -> Result<f64> {
        let b = self.take::<8>()?;
        Ok(if self.little { f64::from_le_bytes(b) } else { f64::from_be_bytes(b) })
    }
    fn polygon_body(&mut self) -> Result<Polygon<f64>> {
        let n_rings = self.u32()?;
        let mut rings = Vec::with_capacity(n_rings as usize);
        for _ in 0..n_rings {
            let n_pts = self.u32()?;
            let mut coords = Vec::with_capacity(n_pts as usize);
            for _ in 0..n_pts {
                let x = self.f64()?;
                let y = self.f64()?;
                coords.push(Coord { x, y });
            }
            rings.push(LineString(coords));
        }
        let mut rings = rings.into_iter();
        let exterior = rings.next().unwrap_or_else(|| LineString(vec![]));
        Ok(Polygon::new(exterior, rings.collect()))
    }
}

/// A column to be written to an output table.
pub enum OutColumn {
    F64(Vec<Option<f64>>),
    I64(Vec<Option<i64>>),
    Str(Vec<Option<String>>),
    Bool(Vec<Option<bool>>),
}

/// Write named columns as a (non-geo) Parquet file.
pub fn write_table(path: &Path, columns: Vec<(String, OutColumn)>) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut fields = Vec::with_capacity(columns.len());
    let mut arrays: Vec<ArrayRef> = Vec::with_capacity(columns.len());
    for (name, col) in columns {
        let (dt, arr): (DataType, ArrayRef) = match col {
            OutColumn::F64(v) => (DataType::Float64, Arc::new(Float64Array::from(v))),
            OutColumn::I64(v) => (DataType::Int64, Arc::new(Int64Array::from(v))),
            OutColumn::Str(v) => (DataType::Utf8, Arc::new(StringArray::from(v))),
            OutColumn::Bool(v) => (DataType::Boolean, Arc::new(BooleanArray::from(v))),
        };
        fields.push(Field::new(name, dt, true));
        arrays.push(arr);
    }
    let schema = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::try_new(3)?))
        .build();
    let mut writer = ArrowWriter::try_new(File::create(path)?, schema, Some(props))?;
    writer.write(&batch)?;
    writer.close()?;
    Ok(())
}

/// Read a plain Parquet table into columns (numeric -> f64, strings kept).
pub fn read_table(path: &Path) -> Result<(HashMap<String, Vec<Option<f64>>>, HashMap<String, Vec<Option<String>>>)> {
    let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let mut numeric: HashMap<String, Vec<Option<f64>>> = HashMap::new();
    let mut strings: HashMap<String, Vec<Option<String>>> = HashMap::new();
    for batch in ParquetRecordBatchReaderBuilder::try_new(file)?.build()? {
        let batch = batch?;
        for (field, column) in batch.schema().fields().iter().zip(batch.columns()) {
            if field.data_type().is_numeric() {
                let col = cast(column, &DataType::Float64)?;
                let col = col.as_any().downcast_ref::<Float64Array>().unwrap();
                numeric.entry(field.name().clone()).or_default().extend(col.iter());
            } else if matches!(field.data_type(), DataType::Utf8 | DataType::LargeUtf8) {
                let col = cast(column, &DataType::Utf8)?;
                let col = col.as_any().downcast_ref::<StringArray>().unwrap();
                strings
                    .entry(field.name().clone())
                    .or_default()
                    .extend(col.iter().map(|s| s.map(str::to_string)));
            }
        }
    }
    Ok((numeric, strings))
}
