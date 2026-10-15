#!/usr/bin/env bash
# Download CBS "Kaart van 100 meter bij 100 meter met statistieken" (one
# GeoPackage per year, EPSG:28992) and convert each year to a GeoParquet file
# clipped to the Zuid-Holland bounding box and reprojected to lon/lat
# (OGC:CRS84), which is what `datacube-zh cbs` reads.
#
# Source: https://www.cbs.nl/nl-nl/dossier/nederland-regionaal/geografische-data/kaart-van-100-meter-bij-100-meter-met-statistieken
# ("_vol" = final/definitive release of that year; 2018 only exists as v3).
#
# Column names are left as CBS ships them; per-year renames are applied by
# the Rust side (see config/cbs.toml), so this step stays a dumb format
# conversion.
#
# Usage: datacube-zh/prepare/cbs.sh [DATA_DIR] [REGION]   (defaults: own_data zh)
set -euo pipefail

DATA_DIR="${1:-own_data}"
REGION="${2:-zh}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$DATA_DIR/sources/cbs"
OUT="$DATA_DIR/$("$HERE/region.py" "$REGION" work)/cbs"
mkdir -p "$SRC" "$OUT"

# Region bbox (lon/lat, config/regions.toml) with a small margin; the exact
# clip happens later against the H3 grid.
BBOX="$("$HERE/region.py" "$REGION" bbox_wgs84)"

declare -A URLS=(
  [2018]="https://www.cbs.nl/-/media/cbs/dossiers/nederland-regionaal/vierkanten/100/2021-cbs_vk100_2018_v3.zip"
  [2019]="https://www.cbs.nl/-/media/cbs/dossiers/nederland-regionaal/vierkanten/100/2022-cbs_vk100_2019_vol.zip"
  [2020]="https://www.cbs.nl/-/media/cbs/dossiers/nederland-regionaal/vierkanten/100/2023-cbs_vk100_2020_vol.zip"
  [2021]="https://download.cbs.nl/vierkant/100/2024-cbs_vk100_2021_vol.zip"
  [2022]="https://download.cbs.nl/vierkant/100/2025-cbs_vk100_2022_vol.zip"
  [2023]="https://download.cbs.nl/vierkant/100/2026-cbs_vk100_2023_vol.zip"
  # Not yet final: 2024 v2, 2025 v1 (preliminary).
  [2024]="https://geodata.cbs.nl/files/Vierkant_statistiek/2024/2026-cbs_vk100_2024_v2.zip"
  [2025]="https://geodata.cbs.nl/files/Vierkant_statistiek/2025/2026-cbs_vk100_2025_v1.zip"
)

for year in "${!URLS[@]}"; do
  url="${URLS[$year]}"
  zip="$SRC/$(basename "$url")"
  if ! compgen -G "$SRC/cbs_vk100_${year}*.gpkg" >/dev/null; then
    [ -f "$zip" ] || curl -sSfL -o "$zip" "$url"
    unzip -o -q "$zip" -d "$SRC"
    # 2018 ships a shapefile plus a nested geopackage zip.
    for inner in "$SRC"/cbs_vk100_${year}*-geopackage.zip; do
      [ -f "$inner" ] && unzip -o -q "$inner" -d "$SRC"
    done
  fi
done

for gpkg in "$SRC"/cbs_vk100_*.gpkg; do
  year="$(basename "$gpkg" | grep -oE '20[0-9]{2}')"
  out="$OUT/cbs_${year}.parquet"
  [ -f "$out" ] && { echo "skip $out (exists)"; continue; }
  echo "convert $gpkg -> $out"
  ogr2ogr -f Parquet "$out" "$gpkg" \
    -spat $BBOX -spat_srs EPSG:4326 \
    -t_srs OGC:CRS84 \
    -lco GEOMETRY_ENCODING=WKB -lco COMPRESSION=ZSTD &
done
wait
ls -lh "$OUT"
