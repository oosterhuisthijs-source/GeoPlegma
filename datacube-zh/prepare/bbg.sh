#!/usr/bin/env bash
# Download the CBS Nieuw Bestand Bodemgebruik mutatiebestand (NBBG2022 v1,
# both peiljaren 2020 + 2022, 2.2 GB zip / 5.9 GB GeoPackage) and clip its
# `_undissolved` layer to the region as GeoParquet for `datacube-zh bbg`.
#
# Usage: datacube-zh/prepare/bbg.sh [DATA_DIR] [REGION]   (defaults: own_data zh)
set -euo pipefail

DATA_DIR="${1:-own_data}"
REGION="${2:-zh}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$DATA_DIR/sources/bbg"
OUT="$DATA_DIR/$("$HERE/region.py" "$REGION" work)/bbg"
mkdir -p "$SRC" "$OUT"

NAME="CBS_Mutatiebestand_NBBG2022_v1_def"
GPKG="$SRC/${NAME}_GPKG/$NAME.gpkg"
if [ ! -f "$GPKG" ]; then
  [ -f "$SRC/${NAME}_GPKG.zip" ] || curl -sSfL -o "$SRC/${NAME}_GPKG.zip" "https://geodata.cbs.nl/files/Bodemgebruik/NBBG2022/${NAME}_GPKG.zip"
  unzip -o -q "$SRC/${NAME}_GPKG.zip" -d "$SRC"
fi

ogr2ogr -f Parquet "$OUT/nbbg_undissolved.parquet" "$GPKG" "${NAME}_undissolved" -overwrite \
  -spat $("$HERE/region.py" "$REGION" bbox_wgs84) -spat_srs EPSG:4326 -t_srs OGC:CRS84 \
  -dim XY -nlt MULTIPOLYGON -lco GEOMETRY_ENCODING=WKB -lco COMPRESSION=ZSTD
ls -lh "$OUT"
