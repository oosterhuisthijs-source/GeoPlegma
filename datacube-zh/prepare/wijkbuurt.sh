#!/usr/bin/env bash
# Download the CBS Wijk- en Buurtkaart and convert its `buurten` and `wijken`
# layers to GeoParquet clipped to the Zuid-Holland bbox (OGC:CRS84), for
# `datacube-zh admin`.
#
# Source: https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/wijk-en-buurtkaart-2024
# 2024 is the vintage the original ruimtelijke-assistent table used (all its
# buurtcodes match 2024; 2023 lacks Nieuwkoop's re-divided buurten).
#
# Usage: datacube-zh/prepare/wijkbuurt.sh [DATA_DIR] [REGION]   (defaults: own_data zh)
set -euo pipefail

DATA_DIR="${1:-own_data}"
REGION="${2:-zh}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="2024_v2"
SRC="$DATA_DIR/sources/wijkbuurt"
OUT="$DATA_DIR/$("$HERE/region.py" "$REGION" work)/wijkbuurt"
mkdir -p "$SRC" "$OUT"

GPKG="$SRC/WijkBuurtkaart_$VERSION/wijkenbuurten_$VERSION.gpkg"
if [ ! -f "$GPKG" ]; then
  zip="$SRC/WijkBuurtkaart_$VERSION.zip"
  [ -f "$zip" ] || curl -sSfL -o "$zip" "https://geodata.cbs.nl/files/Wijkenbuurtkaart/WijkBuurtkaart_$VERSION.zip"
  unzip -o -q "$zip" -d "$SRC"
fi

BBOX="$("$HERE/region.py" "$REGION" bbox_wgs84)"
ogr2ogr -f Parquet "$OUT/buurten.parquet" "$GPKG" buurten -overwrite \
  -select buurtcode,buurtnaam,wijkcode,gemeentecode,gemeentenaam,water \
  -spat $BBOX -spat_srs EPSG:4326 -t_srs OGC:CRS84 \
  -lco GEOMETRY_ENCODING=WKB -lco COMPRESSION=ZSTD
ogr2ogr -f Parquet "$OUT/wijken.parquet" "$GPKG" wijken -overwrite \
  -select wijkcode,wijknaam \
  -spat $BBOX -spat_srs EPSG:4326 -t_srs OGC:CRS84 \
  -lco GEOMETRY_ENCODING=WKB -lco COMPRESSION=ZSTD
ls -lh "$OUT"
