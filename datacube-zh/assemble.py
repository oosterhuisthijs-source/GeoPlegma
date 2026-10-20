# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=17", "deltalake>=0.22"]
# ///
"""Step 6: join the per-layer outputs into the final datacube.

Writes, under the region's out directory (config/regions.toml):

- datacube_<region>/           Delta table partitioned by year_int: real
                               values, one row per (h3_id, year_int), every
                               rebuilt column (the actual product). Built one
                               year at a time, so the national table never
                               has to fit in memory.
- datacube_<region>.columns.json   per-column manifest
- datacube_<region>_lijnen.parquet  OSM main roads/railways as whole lines (GeoParquet, prepare/osm.py),
                               when prepared; the cube carries their lengths per cell (osm_*_lengte_m)
- datacube_<region>_s2_dekking.parquet   NDVI per cell, year and LGN group of the pixel (prepare/s2_cover.py),
                               when prepared; coarsen.py adds _r8/_r7
- datacube_<region>_knmi_voor.parquet    KNMI per cell for years before the cube (2017), when prepared
- datacube_<region>_jaar.parquet   national values per year, not per cell (prepare/knmi_wind.py: wind),
                               when prepared; same file for every region
- datacube_<region>_gemeente.parquet   values per gemeente (2024 borders, join on gemeentecode) and year, not per
                               cell (prepare/vergunninghouders.py), when prepared; zh: its gemeenten only
- datacube_<region>_coa.parquet   COA reception capacity per gemeente (2024 borders) and snapshot date
                               (prepare/coa.py), when prepared; zh: its gemeenten only
- datacube_<region>_datacentra.parquet, _netcongestie.parquet   per gemeente: RVO data centres per year
                               (prepare/datacentra.py), grid congestion per snapshot (prepare/netcongestie.py)
- datacube_<region>_netgebieden.parquet   grid supply areas with MW figures (prepare/netcongestie.py)
- datacube_<region>_waterkeringen.parquet   primary flood defences and HWBP stretches as whole lines
                               (prepare/waterkeringen.py); zh: lines crossing zh
- volledige_tijdreeks_delta/   (zh only) drop-in replacement for ruimtelijke-assistent:
                               the original's exact columns, order and types,
                               CBS columns min-max scaled to 0-255 over all years
                               combined (as the original was), columns this
                               pipeline doesn't produce yet left null

Usage: uv run datacube-zh/assemble.py [--data own_data] [--region zh] [--original PATH]
       uv run datacube-zh/assemble.py --region nl --incremental [--from PATH]
         only the per-cell static layers (static, osm, wind, ...) changed: every year is read from the previous cube
         (default: the latest release), the static columns are refreshed from work/ and new ones inserted where a
         full build puts them; the yearly layers are not recomputed. Writes <name>.incremental.json (added /
         changed columns) for `coarsen.py --incremental`. Parent regions only (zh is a slice: run it as usual).
"""
import argparse
import fnmatch
import glob
import json
import shutil
import sys
import tomllib
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from deltalake import write_deltalake

YEARS = range(2018, 2026)
# per-gemeente side tables (work/layers/<layer>.parquet -> out/datacube_<region>_<suffix>.parquet), keyed on gemeentecode
GEMEENTE_TABLES = {"vergunninghouders_gemeente": "gemeente", "coa_gemeente": "coa", "datacentra_gemeente": "datacentra",
                   "netcongestie_gemeente": "netcongestie"}
DEFAULT_ORIGINAL = "/home/thijsradijs/ruimtelijke-assistent/src/backend/data/volledige_tijdreeks_delta"
CBS_SUFFIXES = ("_sum", "_area_weighted_average", "_min", "_max")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def left_join(left: pa.Table, right: pa.Table, keys) -> pa.Table:
    return left.join(right, keys=keys, join_type="left outer", coalesce_keys=True)


def lgn_columns(work: Path) -> list[str] | None:
    """All LGN columns over the years (prepare/lgn.py), so every year gets the same schema: a class
    LGN added or renamed later (zonneparken, the 2021 road split, class 45's new name) is null in
    the years whose legend lacks it. Class columns in LGN code order, then groups and the rest."""
    files = sorted(work.glob("layers/lgn_*.parquet"))
    if not files:
        return None
    seen = {c for f in files for c in pq.read_schema(f).names} - {"h3_id", "year_int"}
    cfg = tomllib.loads((Path(__file__).parent / "config/lgn.toml").read_text())
    order = []
    for code, name in cfg["classes"].items():
        old = cfg.get("renamed", {}).get(code)
        order += ([f"{old['name']}_fraction"] if old else []) + [f"{name}_fraction"]
    order += [f"lgn_groep_{g}" for g in cfg["groups"]] + ["lgn_coverage", "landgebruiksklasse", "lgn_source_year"]
    return [c for c in order if c in seen]


LGN_SRC = "LGN Landelijk Grondgebruik Nederland (WUR), jaarlijkse 5 m rasters LGN2018-LGN2025"
# LGN method changes that make single classes jump between years (checked on the rasters themselves).
_GRAS = ("LGN2022 en LGN2023 karteren het grootste deel van klasse 45 als agrarisch gras (ZH: 235 -> 10 km2); "
         "gebruik lgn_groep_agrarisch_grasland voor tijdreeksen")
_WEG = ("wegen zijn vanaf LGN2021 gesplitst in 251/252/253 (daarvoor klasse 25) en LGN2022 kent ~50% meer "
        "wegoppervlak dan de jaren eromheen; vergelijk alleen 2021 en 2023-2025 met elkaar")
LGN_WARNINGS = {
    "agrarisch_gras_fraction": _GRAS,
    "natuurlijk_beheerde_agrarische_graslanden_fraction": _GRAS + " (tot en met 2020 heet de klasse natuurgraslanden)",
    "natuurgraslanden_fraction": "LGN-klasse 45 tot en met 2020; vanaf 2021 natuurlijk_beheerde_agrarische_graslanden_fraction",
    "wegen_en_spoorwegen_fraction": "alleen 2018-2020; vanaf 2021 gesplitst, zie lgn_groep_wegen",
    "hoofdinfrastructuur_en_spoorbaanlichamen_fraction": _WEG,
    "halfverharde_wegen_infrastructuur_langzaam_verkeer_en_overige_infrastructuur_fraction": _WEG,
    "smalle_wegen_fraction": _WEG,
    "lgn_groep_wegen": _WEG,
    "kale_grond_in_bebouwd_gebied_fraction": "alleen 2018-2020 gekarteerd (2022 in de legenda maar 0)",
    "zout_water_fraction": "LGN2018-2020 karteren de zee minder ver uit de kust: kustcellen alleen vergelijken vanaf 2021",
    "zoet_water_fraction": "LGN2022 en later karteren meer zoet water (ZH: 443 -> 509 -> 566 km2 in 2021-2023), deels methode",
    # Not LGN, but the same kind of warning: two editions of the RIVM noise map.
    "geluid_lden_2022": ("niet zomaar als verandering t.o.v. geluid_lden (2020) lezen: ZH gemiddeld 1,8 dB stiller, 26% van de "
                         "cellen > 3 dB stiller en 6% > 3 dB luider, terwijl 2020 een coronajaar was; het verschil is "
                         "grotendeels rekenmethode/invoer van RIVM, niet gemeten verandering"),
}


def load_lgn(work: Path, year: int, columns: list[str]) -> pa.Table | None:
    """LGN class fractions for one year, padded to `columns` (see lgn_columns)."""
    f = work / f"layers/lgn_{year}.parquet"
    if not f.exists():
        return None
    t = pq.read_table(f)
    cols = {"h3_id": t["h3_id"], "year_int": t["year_int"]}
    for c in columns:
        cols[c] = t[c] if c in t.column_names else pa.nulls(t.num_rows, pa.float64())
    return pa.table(cols)


# NBBG reference year used for each cube year (NBBG has peiljaren 2020 and 2022).
BBG_YEAR_FOR = {2018: 2020, 2019: 2020, 2020: 2020, 2021: 2020, 2022: 2022, 2023: 2022, 2024: 2022, 2025: 2022}


def load_brp(work: Path, year: int) -> pa.Table | None:
    """BRP crop-group fractions per hex for one year (datacube-zh brp)."""
    f = work / f"layers/brp_{year}.parquet"
    return pq.read_table(f) if f.exists() else None


def load_bbg(work: Path, year: int) -> pa.Table | None:
    """CBS Nieuw Bestand Bodemgebruik fractions per hex (datacube-zh bbg),
    from the NBBG peiljaar used for `year`."""
    f = work / f"layers/bbg_{BBG_YEAR_FOR[year]}.parquet"
    if not f.exists():
        return None
    t = pq.read_table(f)
    return t.append_column("year_int", pa.array([year] * t.num_rows, pa.int64()))


def load_wb(work: Path, year: int) -> pa.Table | None:
    """CBS kerncijfers wijken en buurten (datacube-zh cbs --layer wb)."""
    f = work / f"layers/wb_{year}.parquet"
    return pq.read_table(f) if f.exists() else None


def load_bag(work: Path, year: int) -> pa.Table | None:
    """BAG verblijfsobjecten per gebruiksdoel on 1 January (datacube-zh bag)."""
    f = work / f"layers/bag_{year}.parquet"
    return pq.read_table(f) if f.exists() else None


BAG_SOURCE = ("BAG verblijfsobjecten (LV BAG levenscyclus-extract via PDOK, volledige historie); "
              "stand op 1 januari van het jaar; config/bag.toml")
BAG_AGG = ("aantal verblijfsobjecten met gebruiksdoel {func} waarvan het punt in de cel ligt (status in gebruik / "
           "in gebruik niet ingemeten / verbouwing); een object met meerdere gebruiksdoelen telt bij elk mee")


WIND_SOURCE = ("RIVM Atlas Leefomgeving, windturbines (vermogen, ashoogte, rotordiameter), WFS laag "
               "rivm_20260101_windturbines_vermogen (stand januari 2026, incl. Belgische en Duitse turbines bij de grens); "
               "prepare/wind.py")
WIND_INFO = {
    "wind_aantal": ("aantal", "aantal windturbines waarvan het punt in de cel ligt"),
    "wind_vermogen_kw": ("kW", "som van het nominaal vermogen van die turbines"),
    "wind_ashoogte_max_m": ("m", "hoogste ashoogte van die turbines (leeg zonder turbine)"),
    "wind_rotordiameter_max_m": ("m", "grootste rotordiameter van die turbines (leeg zonder turbine)"),
    "wind_tiphoogte_max_m": ("m", "hoogste tiphoogte (ashoogte + halve rotordiameter) van die turbines (leeg zonder turbine)"),
    "wind_afstand_min_km": ("km", "afstand van het celcentrum tot de dichtstbijzijnde turbine (alle landen, land en zee)"),
}


OSM_SOURCE = ("OpenStreetMap (Geofabrik netherlands-latest.osm.pbf, stand {stamp}), © OpenStreetMap contributors, "
              "ODbL; prepare/osm.py; de lijnen zelf staan in datacube_<regio>_lijnen.parquet")
OSM_INFO = {
    "osm_snelweg_lengte_m": "lengte snelweg (highway=motorway + motorway_link); elke rijbaan apart",
    "osm_autoweg_lengte_m": "lengte autoweg (highway=trunk + trunk_link); elke rijbaan apart",
    "osm_provinciaal_lengte_m": "lengte hoofdweg buiten het snelwegnet (highway=primary + primary_link, veelal N-wegen); "
                                "elke rijbaan apart",
    "osm_regionaal_lengte_m": "lengte regionale weg (highway=secondary + secondary_link); elke rijbaan apart",
    "osm_lokaal_lengte_m": "lengte lokale verbindingsweg (highway=tertiary + tertiary_link); elke rijbaan apart",
    "osm_spoor_lengte_m": "lengte spoor (railway=rail zonder service-tag: doorgaand spoor), elk spoor apart",
    "osm_spoor_overig_lengte_m": "lengte emplacement-, opstel- en raccordementsporen (railway=rail met service-tag)",
    "osm_metro_tram_lengte_m": "lengte metro-, lightrail- en tramspoor (railway=subway/light_rail/tram)",
}
OSM_EXTRA_SOURCE = ("OpenStreetMap (Geofabrik netherlands-latest.osm.pbf, stand {stamp}), © OpenStreetMap contributors, "
                    "ODbL; prepare/osm_extra.py")
OSM_EXTRA_INFO = {
    "osm_hoogspanning_lengte_m": ("m", "lengte bovengrondse hoogspanningslijn >= 50 kV (power=line) binnen de cel, elke "
                                  "lijn (mastenrij) een keer; exacte lengte (lijn x zeshoek in RD)"),
    "osm_hoogspanning_kabel_lengte_m": ("m", "lengte ondergrondse of onderwater hoogspanningskabel >= 50 kV (power=cable) "
                                        "binnen de cel; kabels zijn in OSM minder volledig dan lijnen"),
    "osm_hoogspanning_afstand_min_km": ("km", "afstand van het celcentrum tot de dichtstbijzijnde bovengrondse "
                                        "hoogspanningslijn >= 50 kV (heel NL)"),
    "osm_onderstation_aantal": ("aantal", "hoogspanningsstations >= 50 kV (power=substation) waarvan het midden in de cel "
                                "ligt; wijk- en buurttrafo's ontbreken grotendeels in OSM en tellen niet mee"),
    "osm_zonnepark_vermogen_kw": ("kW", "nominaal vermogen van zonneparken (power=plant, plant:source=solar), verdeeld naar "
                                  "oppervlakteaandeel; parken zonder vermogen in OSM (ongeveer de helft) krijgen hun "
                                  "oppervlak x het mediane kW/ha van de parken met vermogen"),
    "osm_jachthaven_aantal": ("aantal", "jachthavens (leisure=marina) waarvan het midden in de cel ligt"),
    "osm_datacentrum_aantal": ("aantal", "datacentra (telecom=data_center of building=data_center) waarvan het midden in de "
                               "cel ligt; overlappende of rakende vlakken (campus + gebouwen) tellen als een"),
    "osm_datacentrum_m2": ("m2", "grondoppervlak van datacentra binnen de cel (samengevoegde vlakken; als punt gekarteerde "
                           "datacentra tellen niet mee); vermogen en openingsjaar per gemeente in datacube_<regio>_datacentra.parquet"),
}
LAAD_SOURCE = "NDW open data, charging_point_locations.geojson.gz (stand {stamp}); prepare/laadpalen.py"
LAAD_INFO = {
    "laad_punten_aantal": ("aantal", "publieke laadpunten op laadlocaties waarvan het punt in de cel ligt"),
    "laad_punten_snel_aantal": ("aantal", "waarvan snelladers (maximaal vermogen >= 50 kW)"),
}

# Measurements and model maps per year: name -> (source, unit, aggregation)
CELL_CENTRE = "waarde van de rastercel onder het celcentrum"
KNMI_CELL = ("waarde van de rastercel onder het celcentrum (aan de kust: dichtstbijzijnde cel met data, "
             "binnen 3 km)")
KNMI_SRC = ("KNMI Data Platform, dagelijkse 1 km-grids (Tg1 v5, Tx1 v2, Tn1 v2, Rd1 v5, EV24 v2; interpolatie van "
            "stationsmetingen), CC BY 4.0; prepare/knmi.py")
GDN_SRC = ("RIVM historische reeks stikstofdepositie en verzurende depositie, editie 2026-10-06 (hele reeks 2005-2025 "
           "met een methode doorgerekend), 1 km; prepare/rivm.py")
GCN_SRC = "RIVM Grootschalige Concentratiekaarten Nederland (GCN), rekenronde van elk jaar, 1 km; prepare/rivm.py"
TROPOMI_SRC = ("KNMI TEMIS, maandgemiddelde troposferische NO2-kolom van TROPOMI (Sentinel-5P) v1.0, 0,125 graad; "
               "prepare/tropomi.py")
KDW_SRC = ("RIVM Koppeltabel relevante habitats (editie 2026-10-06; AERIUS-hexagonen 1 ha, KDW per habitattype) + "
           "RIVM historische reeks stikstofdepositie; prepare/kdw.py")
VEE_SRC = ("CBS StatLine 80781ned (landbouwtelling per gemeente, dieren op 1 april, gemeente-indeling van dat jaar), "
           "verdeeld over de landbouwgrond (BRP) van de gemeente; prepare/vee.py")
NH3_SRC = ("RIVM Emissieregistratie, ER Reeks 1990-2024 Definitief: ammoniak naar lucht per subsector, 1x1 km "
           "(5x5 km op zee); alleen 2019-2024 regionaal verdeeld; prepare/nh3.py")
NH3_SPLIT = "uitstoot van het 1 km-vierkant verdeeld over de hexagonen met hun centrum erin, naar oppervlak"
NH3_ANIMAL = ("alle uitstoot uit de mest van deze diersoort: stal, opslag, beweiding en uitrijden op het land (dus "
              "ook op velden ver van het bedrijf)")
MAN_SRC = ("RIVM Meetnet Ammoniak Nederland (man.rivm.nl): jaargemiddelde per meetpunt (passieve samplers), "
           "~100 natuurgebieden + 17 punten daarbuiten sinds 2021; prepare/man.py")
S2_SRC = ("Copernicus Sentinel-2 L2A (COPERNICUS/S2_SR_HARMONIZED, wolken weg met Cloud Score+ cs_cdf >= 0,6) via "
          "Earth Engine, NDVI per 10 m-pixel; prepare/s2_ndvi.py + s2_metrics.py")
S2_LAND = "aandeel van de landpixels (10 m, met data) in de cel; water (LGN, of NDVI < 0 in >= 75% van de jaren) telt niet mee"
VEE_GRAZE = "verdeeld naar grasland + mais + voedergewassen (BRP) in de gemeente"
VEE_HOUSED = "verdeeld naar alle landbouwgrond (BRP) in de gemeente: een dichtheid, niet de plek van de stal"
WK_SRC = ("Rijkswaterstaat Waterveiligheidsportaal WFS (nbpw_dijktrajecten_formeel, lbo1_veiligheidsoordeel_dijktrajecten, "
          "hwbp_actuele/afgesloten_projecten_dijkvakken), CC0; huidige stand; prepare/waterkeringen.py")
NET_SRC = ("Netbeheer Nederland capaciteitskaart (congestie_pc6.csv, stand oktober 2026), postcodes via CBS PC6 2024 v2; "
           "geen licentie vermeld; prepare/netcongestie.py")
MEASURE_INFO = {
    "wk_primair_lengte_m": (WK_SRC, "m", "lengte primaire waterkering (dijktrajecten) binnen de cel; exacte lengte (lijn x zeshoek in RD)"),
    "wk_norm_signalering_jaar": (WK_SRC, "jaar (terugkeertijd)", "strengste signaleringswaarde van de dijktrajecten in de cel, als N in 1/N per jaar (Omgevingswet)"),
    "wk_norm_ondergrens_jaar": (WK_SRC, "jaar (terugkeertijd)", "strengste ondergrens van de dijktrajecten in de cel, als N in 1/N per jaar"),
    "wk_lbo1_oordeel": (WK_SRC, "categorie A+..D", "slechtste veiligheidsoordeel (eerste landelijke beoordeling LBO-1, tot 2023) van de dijktrajecten in de cel; A+ = veel veiliger dan de norm, D = veel minder veilig dan de ondergrens"),
    "wk_hwbp_lengte_m": (WK_SRC, "m", "lengte dijk in een lopend HWBP-project (verkenning, planuitwerking of realisatie) binnen de cel"),
    "wk_hwbp_realisatie_start": (WK_SRC, "jaar", "vroegste geplande start van de uitvoering van de HWBP-projecten in de cel (planning, vaak leeg)"),
    "wk_hwbp_realisatie_eind": (WK_SRC, "jaar", "laatste geplande einde van de uitvoering van de HWBP-projecten in de cel (planning, vaak leeg)"),
    "wk_hwbp_afgerond_lengte_m": (WK_SRC, "m", "lengte dijk van afgeronde HWBP-projecten (status dijk veilig) binnen de cel; geen jaartal bekend"),
    "net_afname_kleur_max": (NET_SRC, "kleurcode 0-3", "slechtste congestiekleur voor afname (0 geen, 1 geel, 2 oranje, 3 rood = geen capaciteit, wachtrij) op 7 meetpunten in de cel (middens van de res-10-kinderen), via de postcode (PC6) van elk punt"),
    "net_afname_rood_aandeel": (NET_SRC, "0-1", "aandeel van de 7 meetpunten in de cel met rode congestie voor afname"),
    "net_opwek_kleur_max": (NET_SRC, "kleurcode 0-3", "slechtste congestiekleur voor invoeding/opwek op 7 meetpunten in de cel, via de postcode (PC6)"),
    "net_opwek_rood_aandeel": (NET_SRC, "0-1", "aandeel van de 7 meetpunten in de cel met rode congestie voor invoeding/opwek"),
    "net_voedingsgebied_id": (NET_SRC, None, "voedingsgebied van de regionale netbeheerder (meeste meetpunten); MW-cijfers per gebied in datacube_<regio>_netgebieden.parquet"),
    "knmi_temp_gem_c": (KNMI_SRC, "graden C", f"jaargemiddelde van de daggemiddelde temperatuur; {KNMI_CELL}"),
    "knmi_graaddagen": (KNMI_SRC, "graaddagen", "graaddagen onder 18 graden C: som over de dagen van max(0, 18 - "
                        f"daggemiddelde temperatuur), ongewogen; {KNMI_CELL}"),
    "knmi_zomerse_dagen": (KNMI_SRC, "dagen", f"dagen met maximumtemperatuur >= 25 graden C; {KNMI_CELL}"),
    "knmi_tropische_dagen": (KNMI_SRC, "dagen", f"dagen met maximumtemperatuur >= 30 graden C; {KNMI_CELL}"),
    "knmi_vorstdagen": (KNMI_SRC, "dagen", f"dagen met minimumtemperatuur < 0 graden C; {KNMI_CELL}"),
    "knmi_neerslag_mm": (KNMI_SRC, "mm", f"jaarsom neerslag (dagen van 08 tot 08 UT); {KNMI_CELL}"),
    "knmi_zware_neerslag_dagen": (KNMI_SRC, "dagen", f"dagen met >= 20 mm neerslag; {KNMI_CELL}"),
    "knmi_verdamping_mm": (KNMI_SRC, "mm", f"jaarsom Makkink-referentieverdamping; {KNMI_CELL}"),
    "knmi_neerslagtekort_max_mm": (KNMI_SRC, "mm", "grootste potentiele neerslagtekort van het groeiseizoen: "
                                   "cumulatief verdamping - neerslag vanaf 1 april, maximum tot 30 september (KNMI-"
                                   f"definitie, niet afgekapt op 0); {CELL_CENTRE}"),
    "knmi_neerslagtekort_31aug_mm": (KNMI_SRC, "mm", "potentieel neerslagtekort op 31 augustus: cumulatief verdamping "
                                     f"- neerslag van 1 april tot en met 31 augustus (einde van het NDVI-zomervenster); {CELL_CENTRE}"),
    "nh3_rundvee_kg": (NH3_SRC, "kg NH3/jaar", f"rundvee: {NH3_ANIMAL}; {NH3_SPLIT}"),
    "nh3_varkens_kg": (NH3_SRC, "kg NH3/jaar", f"varkens: {NH3_ANIMAL}; {NH3_SPLIT}"),
    "nh3_pluimvee_kg": (NH3_SRC, "kg NH3/jaar", f"pluimvee: {NH3_ANIMAL}; {NH3_SPLIT}"),
    "nh3_overige_dieren_kg": (NH3_SRC, "kg NH3/jaar", "overige landbouwhuisdieren (schapen, geiten, paarden, "
                              f"konijnen, pelsdieren): {NH3_ANIMAL}; {NH3_SPLIT}"),
    "nh3_kunstmest_kg": (NH3_SRC, "kg NH3/jaar", f"kunstmest; {NH3_SPLIT}"),
    "nh3_landbouw_kg": (NH3_SRC, "kg NH3/jaar", "hele sector landbouw (dieren, kunstmest, bodem en gewas, "
                        f"particuliere landbouw, energie); {NH3_SPLIT}"),
    "nh3_totaal_kg": (NH3_SRC, "kg NH3/jaar", f"alle sectoren (ook verkeer, consumenten, industrie); {NH3_SPLIT}"),
    "man_nh3_ug_m3": (MAN_SRC, "ug/m3", "gemeten jaargemiddelde ammoniakconcentratie: in een MAN-gebied het "
                      "gemiddelde van de meetpunten van dat gebied (over het hele gebied), in de cel van een los "
                      "meetpunt dat punt; leeg buiten de gebieden"),
    "man_meetpunten": (MAN_SRC, "aantal", "aantal meetpunten achter de waarde in dat jaar"),
    "man_gebied": (MAN_SRC, "naam", "MAN-gebied (Natura 2000-gebied of los meetpunt) waarin het celcentrum ligt"),
    "s2_niet_groen_aandeel": (S2_SRC, "0-1", f"nooit groen in april-oktober (hoogste NDVI < 0,30: gebouwen, "
                              f"verharding, kassen, kaal zand); {S2_LAND}"),
    "s2_groen_aandeel": (S2_SRC, "0-1", f"dicht begroeid op enig moment in het seizoen (hoogste NDVI >= 0,60); {S2_LAND}"),
    "s2_verstening_aandeel": (S2_SRC, "0-1", "nieuw en blijvend verlies van groen: het jaar ervoor groen (NDVI >= 0,50), "
                              "dit jaar en het volgende nooit groen; leeg in 2018 en 2025 (vraagt Y-1 en Y+1); "
                              f"{S2_LAND}"),
    "s2_vergroening_aandeel": (S2_SRC, "0-1", "het omgekeerde: het jaar ervoor nooit groen, dit jaar en het volgende "
                               f"groen; leeg in 2018 en 2025; {S2_LAND}"),
    "s2_piek_gem": (S2_SRC, "NDVI -1..1", "gemiddelde over de landpixels van de hoogste NDVI van april-oktober"),
    "s2_zomer_gem": (S2_SRC, "NDVI -1..1", "gemiddelde NDVI in juli-augustus over de landpixels"),
    "s2_zomer_anomalie": (S2_SRC, "NDVI", "NDVI juli-augustus min de eigen mediaan van elke pixel over 2018-2025 "
                          "(vraagt >= 4 jaar), gemiddeld over de landpixels: droogte (negatief) of groenere zomer, los "
                          "van wat er groeit"),
    "s2_helder_piek": (S2_SRC, "aantal", "gemiddeld aantal wolkenvrije waarnemingen per pixel in april-oktober"),
    "s2_helder_zomer": (S2_SRC, "aantal", "gemiddeld aantal wolkenvrije waarnemingen per pixel in juli-augustus"),
    "s2_water_aandeel": (S2_SRC, "0-1", "aandeel van de pixels van de cel dat water is (vast masker, elk jaar gelijk)"),
    "s2_land_pixels": (S2_SRC, "aantal", "landpixels (10 m) met data in dat jaar: gewicht voor de aandelen"),
    "s2_wissel_pixels": (S2_SRC, "aantal", "landpixels met data in Y-1, Y en Y+1: gewicht voor verstening/vergroening"),
    "s2_zomer_pixels": (S2_SRC, "aantal", "landpixels met een juli-augustuswaarde: gewicht voor s2_zomer_gem"),
    "s2_anomalie_pixels": (S2_SRC, "aantal", "landpixels met een anomalie (>= 4 jaar data): gewicht voor s2_zomer_anomalie"),
    "s2_data_pixels": (S2_SRC, "aantal", "pixels met data (land en water): gewicht voor s2_helder_*"),
    "gdn_stikstof_mol_ha": (GDN_SRC, "mol N/ha/jaar", f"totale stikstofdepositie; {CELL_CENTRE}"),
    "gdn_verzuring_mol_ha": (GDN_SRC, "mol H+/ha/jaar", f"potentieel verzurende depositie; {CELL_CENTRE}"),
    "gcn_no2_ug_m3": (GCN_SRC, "ug/m3", f"jaargemiddelde achtergrondconcentratie NO2; {CELL_CENTRE}"),
    "gcn_pm10_ug_m3": (GCN_SRC, "ug/m3", f"jaargemiddelde achtergrondconcentratie PM10; {CELL_CENTRE}"),
    "gcn_pm25_ug_m3": (GCN_SRC, "ug/m3", f"jaargemiddelde achtergrondconcentratie PM2,5; {CELL_CENTRE}"),
    "gcn_nh3_ug_m3": (GCN_SRC, "ug/m3", f"jaargemiddelde achtergrondconcentratie NH3; {CELL_CENTRE}"),
    "tropomi_no2_1e15_cm2": (TROPOMI_SRC, "1e15 moleculen/cm2", "gemiddelde van de maandgemiddelden (alleen "
                             "wolkenvrije overkomsten rond 13:30); leeg in 2018 (pas vanaf mei) en bij < 10 maanden; "
                             f"{CELL_CENTRE}"),
    "tropomi_no2_maanden": (TROPOMI_SRC, "maanden", "aantal maanden met een waarde in dat jaar"),
    **{f"vee_{k}": (VEE_SRC, "aantal", f"{what}; {VEE_GRAZE if graze else VEE_HOUSED}") for k, what, graze in [
        ("rundvee", "rundvee", True), ("melkkoeien", "melk- en kalfkoeien (>= 2 jaar)", True),
        ("schapen", "schapen", True), ("geiten", "geiten", True), ("paarden", "paarden en pony's", True),
        ("varkens", "varkens", False), ("kippen", "kippen", False),
        ("overig_pluimvee", "kalkoenen, slachteenden en overig pluimvee", False),
        ("bedrijven_graasdieren", "bedrijven met graasdieren", True), ("bedrijven_rundvee", "bedrijven met rundvee", True),
        ("bedrijven_hokdieren", "bedrijven met hokdieren", False), ("bedrijven_varkens", "bedrijven met varkens", False),
        ("bedrijven_kippen", "bedrijven met kippen", False)]},
    "kdw_habitat_m2": (KDW_SRC, "m2", "relevant stikstofgevoelig habitatoppervlak (gekarteerd oppervlak x dekking) van de "
                       "AERIUS-hexagonen (1 ha) met hun middelpunt in de cel; leeg = geen relevant habitat"),
    "kdw_min_mol_ha": (KDW_SRC, "mol N/ha/jaar", "laagste kritische depositiewaarde van de habitattypen in de cel"),
    "kdw_gewogen_mol_ha": (KDW_SRC, "mol N/ha/jaar", "kritische depositiewaarde gewogen naar relevant habitatoppervlak"),
    "kdw_habitat_type": (KDW_SRC, "code", "habitattype met het grootste relevante oppervlak (bijv. H4030 droge heiden)"),
    "kdw_overschreden_m2": (KDW_SRC, "m2", "relevant habitatoppervlak waar de depositie (gdn_stikstof, 1 km, op elk "
                            "AERIUS-hexagon) boven de KDW ligt"),
    "kdw_overschreden_aandeel": (KDW_SRC, "0-1", "aandeel van kdw_habitat_m2 boven de KDW"),
    "kdw_overschrijding_mol_ha": (KDW_SRC, "mol N/ha/jaar", "gemiddelde overschrijding over het relevante habitat: "
                                  "som(oppervlak x max(0, depositie - KDW)) / oppervlak"),
}
MEASURE_WARNINGS = {
    "gcn_": ("elk jaar is een eigen rekenronde: RIVM zegt dat de rondes niet als reeks achter elkaar te zetten zijn "
             "(de kalibratie op metingen is in de loop van de jaren veranderd); de landelijke daling is echt, lokale "
             "trends kunnen methode zijn"),
    "net_": ("momentopname van de netbeheerders (oktober 2026), geen reeks: de kleur per postcode volgt het voedingsgebied, "
             "dus een hele wijk kleurt mee; gebruik als beginstand, niet als meting van de straat"),
    "wk_hwbp_realisatie": ("HWBP-planning: jaartallen schuiven bij elke programmering; leeg = (nog) niet gepland"),
    "vee_": ("model: gemeentetotalen (CBS telt dieren in de gemeente van de vestiging) verdeeld over de landbouwgrond; "
             "een cel toont een aannemelijke dichtheid, niet een echt bedrijf. Gemeentetotalen kloppen exact"),
    "kdw_overschr": ("depositie van het 1 km-grid op elk AERIUS-hexagon; RIVM rekent met depositie per hectare, dus "
                     "lokaal anders; landelijk 2024: 71,1% boven de KDW tegen 69,3% bij RIVM"),
    "nh3_": ("model: landelijke emissies (dieraantallen x emissiefactoren, kunstmestverkoop) verdeeld over het land, "
             "stallen op de bedrijfslocatie, uitrijden en beweiding over de landbouwgrond; RIVM berekent de "
             "depositie (gdn_*) hieruit, dus niet onafhankelijk van gdn_* of vee_*. 2018 en 2025 leeg (niet regionaal "
             "verdeeld)"),
    "man_nh3": ("meting, maar weinig punten: een gebiedswaarde is het gemiddelde van 1-15 punten, toegekend aan het "
                "hele gebied; de punten staan waar RIVM meet (vaak aan de rand naar landbouwgrond). Vergelijk met "
                "gcn_nh3_ug_m3 (model, gekalibreerd op deze metingen)"),
    "wb_perc_aardgas": ("alleen 2024 (Wijk- en Buurtkaart 2024): CBS bepaalt elektrische verwarming sinds 2024 uit het "
                        "maandelijkse stroomverbruik, dus geen reeks; andere jaren leeg. Aardgaswoning = hoofdzakelijk "
                        "verwarmd met cv of blokverwarming op gas; aardgasvrij = geen gaslevering (elektrisch of "
                        "stadswarmte). Door afronding en onderdrukking tellen ze niet op tot 100 (gemiddeld ~92). Buurten "
                        "met < 100 woningen onderdrukt: dan de waarde van de wijk, anders de gemeente (zie _eigen_buurt)"),
    "s2_": ("NDVI uit Sentinel-2: kassen, zonneparken en kale akkers (vroeg geoogst, braak) tellen als niet groen; "
            "een wolkenrijk jaar (zie s2_helder_*) geeft een lagere piek. Geen landgebruikskaart: vergelijk met lgn_* "
            "of bag3d_*"),
    "tropomi_": ("satellietmeting van de hele troposferische kolom, niet de concentratie op leefhoogte; ~14 x 9 km, "
                 "dus alleen een regionaal signaal"),
}


def load_lbm(work: Path, year: int) -> pa.Table | None:
    """Leefbaarometer: biennial measurements; odd years take the previous one."""
    src = year if year % 2 == 0 else year - 1
    f = work / f"layers/lbm_{src}.parquet"
    if not f.exists():
        return None
    t = pq.read_table(f)
    t = t.set_column(t.schema.get_field_index("year_int"), "year_int", pa.array([year] * t.num_rows, pa.int64()))
    return t.append_column("lbm_source_year", pa.array([src] * t.num_rows, pa.int64()))


def tessera_schema(work: Path) -> pa.Schema | None:
    """Union schema of all TESSERA layers (the first year has no change columns), or None if there are none."""
    files = sorted(glob.glob(str(work / "layers/tessera_*.parquet")))
    if not files:
        return None
    fields = {}
    for f in files:
        for field in pq.read_schema(f):
            fields.setdefault(field.name, field)
    fields["h3_id"] = pa.field("h3_id", pa.string())  # pandas writes large_string; the other layers use string
    return pa.schema(list(fields.values()))


def load_tessera(work: Path, year: int, schema: pa.Schema | None, h3_ids: pa.ChunkedArray) -> pa.Table | None:
    """TESSERA PCA embedding + year-on-year change (prepare/tessera_hex.py). Years and columns that
    don't exist (before 2022; change columns in the first year) are null, so the cube schema - which
    is taken from the first year, 2018 - still carries them."""
    if schema is None:
        return None
    f = work / f"layers/tessera_{year}.parquet"
    t = pq.read_table(f) if f.exists() else pa.table({"h3_id": h3_ids})
    for field in schema:
        if field.name not in t.column_names:
            t = t.append_column(field, pa.nulls(t.num_rows, field.type))
    t = t.select(schema.names).cast(schema)
    return t.append_column("year_int", pa.array([year] * t.num_rows, pa.int64()))


# Yearly layers kept as one file for all years (h3_id, year_int, ...): measurements and model maps
# (prepare/knmi.py, rivm.py, tropomi.py, kdw.py, vee.py, nh3.py, man.py, s2_metrics.py), read per year.
YEARLY_FILES = ("knmi", "rivm", "tropomi", "kdw", "vee", "nh3", "man", "s2")


def load_yearly(work: Path, layer: str, year: int) -> pa.Table | None:
    # read one year only (row-group filter): caching all years of every layer cost GBs in the nl assemble
    f = work / f"layers/{layer}.parquet"
    if not f.exists():
        return None
    t = pq.read_table(f, filters=[("year_int", "=", year)])
    return t.set_column(t.schema.get_field_index("year_int"), "year_int", pc.cast(t["year_int"], pa.int64()))


def build_year(work: Path, year: int, base: pa.Table, lgn: list[str] | None) -> pa.Table:
    """One year of the cube: CBS (+ grid/admin/static base) + BRP + NBBG + BAG + wb + lbm + TESSERA (+ LGN)."""
    cube = left_join(pq.read_table(work / f"layers/cbs_{year}.parquet"), base, "h3_id")
    tess = load_tessera(work, year, tessera_schema(work), base["h3_id"])
    for layer in (load_brp(work, year), load_bbg(work, year), load_bag(work, year), load_wb(work, year),
                  load_lbm(work, year), tess, load_lgn(work, year, lgn) if lgn else None,
                  *(load_yearly(work, f, year) for f in YEARLY_FILES)):
        if layer is not None:
            cube = left_join(cube, layer, ["h3_id", "year_int"])
    lead = ["h3_id", "year_int"]
    cube = cube.select(lead + [c for c in cube.column_names if c not in lead])
    return cube.sort_by([("h3_id", "ascending")])


def incremental_year(prev: Path, year: int, base: pa.Table, report: dict) -> pa.Table:
    """One year of the cube from the previous cube, with the per-cell static columns (`base`) refreshed.

    A column of `base` that the previous cube lacks goes right after the nearest preceding `base` column it does
    have (the order of a full build); existing ones are replaced in place and listed in report["changed"] when any
    value differs."""
    f = pq.ParquetFile(prev)  # one row group per year in our cubes: read it whole (a row filter costs ~2.5x the memory)
    yi = f.schema_arrow.get_field_index("year_int")
    groups = [i for i in range(f.metadata.num_row_groups)
              if (st := f.metadata.row_group(i).column(yi).statistics) and st.min <= year <= st.max]
    t = f.read_row_groups(groups)
    if len(groups) != 1 or t.num_rows != len(base):
        t = t.filter(pc.equal(t["year_int"], year))
    b = base.sort_by([("h3_id", "ascending")])
    if t["h3_id"] != b["h3_id"]:
        t = t.sort_by([("h3_id", "ascending")])
    if t["h3_id"] != b["h3_id"]:
        sys.exit(f"{prev.name} {year}: other cells than work/grid.parquet; run a full assemble")
    order = list(t.column_names)
    for i, c in enumerate(b.column_names):
        if c == "h3_id":
            continue
        col = b[c]
        if c in order:
            col = pc.cast(col, t.schema.field(c).type)
            if not col.equals(t[c]):
                report["changed"].add(c)
            t = t.set_column(t.schema.get_field_index(c), c, col)
        else:
            report["added"].add(c)
            before = next((x for x in reversed(b.column_names[:i]) if x in order), "year_int")
            order.insert(order.index(before) + 1, c)
            t = t.append_column(c, col)
    return t.select(order)


def scale_0_255(col: pa.ChunkedArray) -> pa.ChunkedArray:
    mm = pc.min_max(col)
    lo, hi = mm["min"].as_py(), mm["max"].as_py()
    if lo is None or hi is None or hi == lo:
        return pc.cast(pc.multiply(pc.is_valid(col), 0), pa.int16())
    scaled = pc.round(pc.multiply(pc.divide(pc.subtract(col, lo), hi - lo), 255.0))
    return pc.cast(scaled, pa.int16())


def build_compat(real: pa.Table, original_schema: pa.Schema) -> pa.Table:
    n = real.num_rows
    year = real["year_int"]
    derived = {
        "year": pc.cast(year, pa.float64()),
        "year_label": pc.cast(year, pa.float64()),
        "year_date": pa.chunked_array([pa.array([f"{y}-01-01" for y in year.to_pylist()])]),
        "year_ms": pa.chunked_array(
            [pa.array([float(pa.scalar(f"{y}-01-01").cast(pa.timestamp("ms")).value) for y in year.to_pylist()])]
        ),
        # The original's static `aantal_inwoners` was a real (unscaled) count
        # matching CBS ~2020/2021; here it is the real CBS sum of that row's year.
        "aantal_inwoners": real["aantal_inwoners_sum"],
        # Stood for "has Atlas Natuurlijk Kapitaal data"; approximated by
        # whether the ANK heat-island layer has a value for the hex.
        "heeft_atlas_data": pc.is_valid(real["hitte"]),
        # In the original this is exactly "the 0-255 scaled aantal_inwoners_sum
        # is > 0" in every year (i.e. the hex was in the populated set that
        # was exported to Kepler); reproduced with the same rule.
        "heeft_kepler_data": pc.fill_null(pc.greater(scale_0_255(pc.cast(real["aantal_inwoners_sum"], pa.float64())), 0), False),
        # NDFF soortendiversiteit per km-hok, Red List map: the closest match
        # to the original's column (Spearman 0.75 vs 0.57 for all species).
        "soortenrijkdomsklasse": real["ndff_soortendiversiteit_rl_klasse"],
    }
    cols, missing = [], []
    for field in original_schema:
        name = field.name
        if name in derived:
            arr = derived[name]
        elif name in real.column_names and name.endswith(CBS_SUFFIXES + ("_fraction",)):
            arr = scale_0_255(pc.cast(real[name], pa.float64()))
        elif name == "natura2000_id":
            arr = pc.cast(real["natura2000_id"], pa.int16())
        elif name in real.column_names:
            arr = real[name]
        else:
            arr = pa.nulls(n, field.type)
            missing.append(name)
        cols.append(pc.cast(arr, field.type))
    log(f"compat: {len(missing)} original columns not produced (null): {', '.join(missing)}")
    return pa.Table.from_arrays(cols, schema=pa.schema([f.remove_metadata() for f in original_schema]))


def valid_shares(table: pa.Table) -> dict:
    """Column -> share of non-null values in `table`."""
    n = max(table.num_rows, 1)
    return {c: round(1 - table[c].null_count / n, 4) for c in table.column_names}


REGION_LABEL = "de regio"  # region name for the manifest, set in main()
OSM_STAMP = None  # OSM extract timestamp, set in main() from work/layers/osm.parquet
LAAD_STAMP = None  # NDW snapshot, set in main() from work/layers/laadpalen.parquet


def build_manifest(schema: pa.Schema, shares_per_year: dict, config_dir: Path) -> dict:
    """Per-column source/aggregation/coverage, for downstream docs (e.g. the
    SimCityZH explanation page)."""
    cbs = tomllib.loads((config_dir / "cbs.toml").read_text())
    static = tomllib.loads((config_dir / "static.toml").read_text())
    rules = tomllib.loads((config_dir / "provenance.toml").read_text())["rule"]
    osm_stamp = OSM_STAMP or "onbekend"
    cbs_src = "CBS Kaart van 100m x 100m met statistieken (vierkantstatistieken), per jaar"
    cbs_aggs = {
        "sum": "som, verdeeld naar oppervlakteaandeel van het CBS-vierkant (overlap / vierkantoppervlak)",
        "area_weighted_average": "oppervlaktegewogen gemiddelde over de vierkanten met waarde",
        "min": "minimum over overlappende vierkanten",
        "max": "maximum over overlappende vierkanten",
    }
    info = {
        "h3_id": ("H3 res 9 cel (geoplegma)", "sleutel", None),
        "year_int": ("jaar", "sleutel", None),
        "lat": ("H3 celcentrum", "graden", None),
        "lon": ("H3 celcentrum", "graden", None),
        "hex_area_m2": ("H3 celoppervlak (geodetisch)", "m2", None),
        "province_fraction": (f"aandeel van de cel binnen {REGION_LABEL} (PDOK provinciegrenzen)", "0-1", None),
        "code": ("provinciecode (PDOK bestuurlijke gebieden)", None, None),
        "naam": ("provincienaam (PDOK bestuurlijke gebieden)", None, None),
        "cbs_coverage": (cbs_src, "0-1", "aandeel van de cel bedekt door CBS-vierkanten"),
        "mediaan_inkomen_huishouden_mode": (cbs_src, "klasse", "oppervlaktegewogen meest voorkomende inkomensklasse"),
        "mediaan_inkomen_huishouden_area_weighted_average": (
            cbs_src, "percentiel 0-100",
            "inkomensklasse van het vierkant (percentielgroep van de landelijke huishoudinkomensverdeling, "
            "bijv. '40-80 midden tot boven midden') als middelpunt (60), oppervlaktegewogen gemiddeld; "
            "alleen 2018-2019 (CBS publiceert de klasse daarna niet meer per vierkant)"),
        "lgn_coverage": (LGN_SRC, "0-1", "aandeel van de cel met een LGN-code (< 1 alleen aan de landsgrens/zee)"),
        "landgebruiksklasse": (LGN_SRC, "klasse", "LGN-klasse met het grootste oppervlak in de cel"),
        "lgn_source_year": (LGN_SRC, "jaar", "LGN-jaar van de waarden (= het kubusjaar)"),
    }
    for col in ["buurtcode", "buurtnaam", "wijkcode", "wijknaam", "gemeentecode", "gemeentenaam"]:
        info[col] = ("CBS Wijk- en Buurtkaart 2024 v2", None, "buurt met de grootste overlap; wijk/gemeente afgeleid van die buurt")
    for agg, cols in cbs["aggregation"].items():
        for c in cols:
            info.setdefault(f"{c}_{agg}", (cbs_src, None, cbs_aggs.get(agg, agg)))
            if c.startswith("bijgeschat_"):
                what = {"leeftijd": "inwoners in leeftijdsgroepen", "geslacht": "inwoners als man/vrouw",
                        "huishoudtype": "huishoudens in huishoudtypen", "bouwjaar": "woningen in bouwjaarklassen"}
                info[f"{c}_{agg}"] = (cbs_src, "aantal", f"aantal {what[c.removeprefix('bijgeschat_')]} dat is bijgeschat: "
                                      "CBS onderdrukt delen < 5 per vierkant; het verschil tussen totaal en gepubliceerde "
                                      "delen is gelijk over de onderdrukte delen verdeeld (max 4 elk), zodat de delen "
                                      "optellen tot het totaal (config/cbs.toml [[parts]])")
            if c.startswith("dichtstbijzijnde_"):
                # CBS publishes -99995 ("not available") for every square in these years
                gap = "2024-2025" if "huisartsenpost" in c else "2025"
                s, u, how = info[f"{c}_{agg}"]
                info[f"{c}_{agg}"] = (s, u, f"{how}; leeg in {gap}: CBS publiceert deze afstand dan (nog) niet")
    for layer in static.get("raster", []):
        src = layer.get("description", "")
        info[layer["name"]] = (f"{src} [{layer['wcs']} {layer['coverage']}]", layer.get("unit"),
                               f"{layer['agg']} over alle pixels in de cel (pixelcentrum -> H3 via geoplegma zone_from_point)")
    numeric_how = {
        "sum": "som, verdeeld naar het aandeel van elk object dat in de cel ligt",
        "area_weighted_average": "oppervlaktegewogen gemiddelde over de objecten in de cel",
        "max": "maximum over de objecten die de cel raken",
        "min": "minimum over de objecten die de cel raken",
    }
    for layer in static.get("vector", []):
        src = layer.get("description", "")
        if layer["agg"] == "numeric":
            src = f"{src} [{layer.get('parquet', '')}]"
            for o in layer.get("outputs", []):
                info[o["name"]] = (src, o.get("unit"), f"{numeric_how[o['agg']]} (bronkolom {o['column']})")
            if layer.get("coverage"):
                info[layer["coverage"]] = (src, "0-1", "aandeel van de cel bedekt door de objecten")
            continue
        how = "true als een polygoon de cel raakt" if layer["agg"] == "any" else "waarde van de polygoon met de grootste overlap"
        info[layer["name"]] = (src, None, how)
        if layer.get("label"):
            info[layer["label"]] = (src, None, how)

    manifest = {}
    for field in schema:
        name = field.name
        src, unit, agg = info.get(name, (None, None, None))
        if name.startswith("wb_"):
            src = "CBS Kerncijfers wijken en buurten (Wijk- en Buurtkaart van dat jaar, laatste versie); config/wb.toml"
            unit = "0-1" if name == "wb_coverage" else None
            agg = ("aandeel van de cel bedekt door buurten" if name == "wb_coverage"
                   else "oppervlaktegewogen gemiddelde van de buurten die de cel overlappen (buurtwaarde gelijk verdeeld over de buurt)")
        elif name.startswith("lbm_"):
            src = "Leefbaarometer 3.0 (BZK), Meting 2024 open data, 100m grid"
            unit = "0-1" if name == "lbm_coverage" else None
            agg = {
                "lbm_coverage": "aandeel van de cel bedekt door Leefbaarometer-gridcellen",
                "lbm_source_year": "meetjaar van de waarden (tweejaarlijks; oneven jaren = vorige meting)",
                "lbm_score": "totaalscore leefbaarheid (absoluut), oppervlaktegewogen gemiddelde van de 100m-cellen",
            }.get(name, "afwijking t.o.v. het landelijk gemiddelde (score/dimensie), oppervlaktegewogen gemiddelde van de 100m-cellen")
        elif name.startswith("tessera_"):
            src = ("TESSERA v1.1 (cambridge variant) annual 10 m embeddings, Sentinel-1/2 (Cambridge, geotessera); "
                   "prepare/tessera_hex.py, PCA + thresholds fitted on 2024+2025 (own_data/tessera/pca_threshold.npz)")
            unit = "0-1" if name.endswith("_share") else None
            if name.startswith("tessera_pc"):
                agg = ("component van het celgemiddelde embedding (alle 10m-pixels in de cel) op een vaste PCA; "
                       "PC1-3 vergelijkbaar tussen jaren, PC5/6/8 hebben jaareffecten")
            else:
                agg = {
                    "tessera_pixels": "aantal 10m-pixels in de cel (pixelcentrum)",
                    "tessera_land_pixels": "landpixels gebruikt voor de verandering (water = NBBG 2022 klassen 70-83)",
                    "tessera_agri_pixels": "landpixels in een BRP-perceel (in een van beide jaren)",
                    "tessera_nonagri_pixels": "landpixels buiten BRP-percelen",
                    "tessera_change_from": "basisjaar van de vergelijking",
                }.get(name)
                if agg is None:
                    part = ("landbouw (BRP-percelen; vooral gewasrotatie)" if "_agri_" in name
                            else "niet-landbouw (bebouwing, natuur, infrastructuur)" if "_nonagri_" in name else "alle land")
                    agg = (f"gemiddelde cosinusafstand per pixel t.o.v. het vorige jaar, {part}" if name.endswith("_mean")
                           else f"aandeel pixels boven een vaste drempel (p95 van 2024->2025), {part}" if name.endswith("_share")
                           else f"aandeel pixels boven een drempel die per jaarpaar is geschaald naar het ruisniveau van dat "
                                f"paar (p90 niet-landbouwafstanden t.o.v. 2024->2025), {part}; gebruik deze om jaren te vergelijken")
                    if name.endswith("_share_dc"):
                        unit = "0-1"
        elif name.startswith("wind_"):
            src = WIND_SOURCE
            unit, agg = WIND_INFO[name]
        elif name in OSM_EXTRA_INFO:
            src = OSM_EXTRA_SOURCE.format(stamp=osm_stamp)
            unit, agg = OSM_EXTRA_INFO[name]
        elif name in MEASURE_INFO:
            src, unit, agg = MEASURE_INFO[name]
        elif name.startswith("regen_"):
            scen = name.split("_")[1] + " in " + name.split("_")[2].replace("h", " uur")
            src = (f"Klimaateffectatlas 2026, waterdiepte bij hevige neerslag ({scen}), 0,5 m, CC BY 4.0; "
                   "prepare/regenwater.py")
            unit, agg = {"diepte_gem_m": ("m", "gemiddelde waterdiepte over de cel (droog = 0), volle resolutie"),
                         "diepte_max_m": ("m", "diepste pixel (0,5 m) in de cel"),
                         "aandeel_10cm": ("0-1", "aandeel van de cel met meer dan 10 cm water"),
                         "aandeel_30cm": ("0-1", "aandeel van de cel met meer dan 30 cm water")}[name.split("_", 3)[3]]
        elif name in LAAD_INFO:
            src = LAAD_SOURCE.format(stamp=LAAD_STAMP or "onbekend")
            unit, agg = LAAD_INFO[name]
        elif name.startswith("osm_"):
            src, unit = OSM_SOURCE.format(stamp=osm_stamp), "m"
            agg = OSM_INFO[name] + "; exacte lengte binnen de cel (lijn x zeshoek in RD), tunnels en bruggen inbegrepen"
        elif name.startswith("ndff_"):
            src = ("NDFF soortendiversiteit per kilometerhok (BIJ12, editie 2017-11-15; NDFF-waarnemingen "
                   "van de 10 jaar ervoor, 8 soortgroepen, gecorrigeerd voor waarneemintensiteit), "
                   + ("Rode-Lijstsoorten" if "_rl_" in name else "alle soorten"))
            unit = "klasse 1-8"
            agg = "oppervlaktegewogen gemiddelde klasse van de km-hokken die de cel overlappen (statisch)"
        elif name.startswith("num_"):
            src, unit = BAG_SOURCE, "aantal"
            agg = (BAG_AGG.format(func=name.removeprefix("num_").replace("_", " ")) if name != "num_verblijfsobjecten"
                   else "aantal verblijfsobjecten (alle gebruiksdoelen) waarvan het punt in de cel ligt")
        elif name.startswith("brp_"):
            src = "BRP gewaspercelen (RVO via PDOK), definitief, per jaar; gewascode -> gewasgroep in config/brp_groups.toml"
            unit = "0-1" if name.endswith(("_fraction", "_coverage")) else None
            agg = {
                "brp_coverage": "aandeel van de cel met geregistreerde landbouwpercelen (excl. landschapselementen)",
                "brp_dominant": "gewasgroep met het grootste oppervlak in de cel",
            }.get(name, "aandeel van het celoppervlak met deze gewasgroep (0 = geen percelen)")
        elif name.startswith("bbg_"):
            src = "CBS Nieuw Bestand Bodemgebruik, mutatiebestand NBBG2022 v1 (peiljaren 2020 en 2022)"
            unit = "0-1" if name.endswith(("_fraction", "_coverage")) else None
            agg = {
                "bbg_source_year": "NBBG-peiljaar van de waarden (2018-2021 -> 2020, 2022-2025 -> 2022)",
                "bbg_coverage": "som van alle bbg_*_fraction (1 = cel volledig gekarteerd)",
                "bbg_dominant": "categorie met het grootste oppervlak in de cel",
            }.get(name, "aandeel van het celoppervlak met deze bodemgebruikscategorie")
        elif name.startswith("lgn_groep_"):
            src, unit, agg = (LGN_SRC, "0-1", "som van de LGN-klassen in deze groep (config/lgn.toml); vergelijkbaar "
                              "over de jaren, anders dan losse klassen")
        elif name.endswith("_fraction") and name != "province_fraction" and name not in info:
            src, unit, agg = (LGN_SRC, "0-1", "aandeel van de cel (pixelcentra) met deze LGN-klasse; leeg in jaren "
                              "waarin de klasse niet in de LGN-legenda staat")
        per_year = {y: shares.get(name) for y, shares in shares_per_year.items()}
        entry = {"source": src, "unit": unit, "aggregation": agg, "type": str(field.type), "non_null_share_per_year": per_year}
        if name in LGN_WARNINGS:
            entry["warning"] = LGN_WARNINGS[name]
        if name.startswith("tessera_change") and name.endswith("_share"):
            entry["warning"] = ("niet vergelijken tussen jaren: elk jaar heeft een eigen ruisniveau in de invoer (2017, 2021 "
                                "en 2024 zijn ruiziger), dus het aandeel springt per jaarpaar zonder echte trend; gebruik "
                                "tessera_change_nonagri_share_dc voor tijdreeksen")
        elif name.startswith("wind_"):
            entry["warning"] = ("statisch: de turbines van januari 2026 in elk jaar (geen bouwjaar in de bron), dus turbines "
                                "die na 2018 zijn gebouwd staan ook in de vroege jaren en gesloopte turbines ontbreken")
        elif name in OSM_EXTRA_INFO or name in LAAD_INFO:
            entry["warning"] = ("statisch: de stand van nu in elk jaar (geen historie in deze bron), dus geen trend; "
                                "laadpunten en zonneparken zijn sinds 2018 sterk gegroeid")
        elif name.startswith(tuple(MEASURE_WARNINGS)):
            entry["warning"] = next(w for k, w in MEASURE_WARNINGS.items() if name.startswith(k))
        elif "gasverbruik" in name or "elektriciteitsverbruik" in name:
            entry["warning"] = ("gemiddelde over de woningen met een aansluiting, uit het standaardjaarverbruik (SJV) van de "
                                "netbeheerders, door CBS omgerekend naar het weer van het jaar: besparing kan maanden "
                                "achterlopen. In cellen met nieuwbouw tellen woningen die halverwege het jaar zijn "
                                "opgeleverd met een deel van een jaar mee, dus verandering per periode is daar vertekend; "
                                "gebruik voor trends cellen met een stabiel aantal woningen. 2025 niet gepubliceerd")
        elif name.endswith("_share_dc"):
            entry["warning"] = "corrigeert voor ruis per jaarpaar, maar haalt daarmee ook echte provinciebrede verandering weg"
        rule = provenance_rule(rules, name)
        if rule is not None:
            entry.update({k: rule[k] for k in PROVENANCE_KEYS})
        manifest[name] = entry
    return manifest


PROVENANCE_KEYS = ("tier", "native_resolution", "reference_period", "time")


def provenance_rule(rules: list[dict], name: str) -> dict | None:
    """First rule of config/provenance.toml whose `columns` glob matches (release.py refuses columns without)."""
    return next((r for r in rules if any(fnmatch.fnmatchcase(name, p) for p in r["columns"])), None)


def write_delta(table: pa.Table, path: Path):
    if path.exists():
        shutil.rmtree(path)
    write_deltalake(str(path), table, mode="overwrite")
    log(f"wrote {path} ({table.num_rows} rows x {table.num_columns} cols)")


SLICE_GRID_COLUMNS = ["province_fraction", "code", "naam"]  # mean something per region: taken from the slice's own grid


def slice_year(parent_parquet: Path, year: int, grid: pa.Table, work: Path) -> pa.Table:
    """One year of a slice region: the parent cube's rows for the region's hexes, the region's own grid columns,
    and its own layers (TESSERA)."""
    import pyarrow.dataset as ds
    ids = grid["h3_id"]
    t = ds.dataset(parent_parquet).to_table(filter=(ds.field("year_int") == year) & ds.field("h3_id").isin(ids))
    if t.num_rows != len(ids):
        sys.exit(f"{parent_parquet.name} {year}: {t.num_rows} rows for {len(ids)} region hexes; rebuild the parent first")
    order = t.column_names
    own = grid.select(["h3_id", *SLICE_GRID_COLUMNS])
    t = left_join(t.drop_columns(SLICE_GRID_COLUMNS), own, "h3_id")
    tess = load_tessera(work, year, tessera_schema(work), ids)
    if tess is not None:
        t = left_join(t, tess, ["h3_id", "year_int"])
        # same position as in a full build: after the Leefbaarometer columns
        extra = [c for c in tess.column_names if c not in ("h3_id", "year_int")]
        at = order.index("lbm_source_year") + 1 if "lbm_source_year" in order else len(order)
        order = order[:at] + extra + order[at:]
    return t.select(order).sort_by([("h3_id", "ascending")])


def slice_lines(parent_lines: Path, ids: set[str], dest: Path):
    """The parent's whole-line file, restricted to lines crossing the region (cellen cut to the region's cells)."""
    t = pq.read_table(parent_lines)
    cells = t["cellen"].to_pylist()
    keep = [i for i, cs in enumerate(cells) if any(c in ids for c in cs)]
    t = t.take(keep)
    t = t.set_column(t.schema.get_field_index("cellen"), "cellen",
                     pa.array([[c for c in cells[i] if c in ids] for i in keep], pa.list_(pa.string())))
    pq.write_table(t.replace_schema_metadata(pq.read_schema(parent_lines).metadata), dest, compression="zstd")
    log(f"wrote {dest} ({t.num_rows} of the parent's lines cross the region)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="own_data")
    ap.add_argument("--region", default="zh")
    ap.add_argument("--original", default=DEFAULT_ORIGINAL, help="original Delta table (for the zh compat schema)")
    ap.add_argument("--year", type=int, choices=YEARS, help="build only this year into datacube_<region>_<year>.parquet "
                    "(+ .columns.json); the full cube is left alone")
    ap.add_argument("--incremental", action="store_true", help="refresh only the static layers (see the doc string)")
    ap.add_argument("--from", dest="prev", default=None, help="previous cube for --incremental "
                    "(default <data>/releases/datacube_<region>/latest/datacube_<region>.parquet)")
    args = ap.parse_args()
    regs = tomllib.loads((Path(__file__).parent / "config/regions.toml").read_text())
    reg = regs[args.region]
    data = Path(args.data)
    work, out = data / reg["work"], data / reg["out"]
    out.mkdir(parents=True, exist_ok=True)
    name = f"datacube_{args.region}"
    global REGION_LABEL
    REGION_LABEL = reg["name"]
    if reg.get("slice_of"):
        if args.incremental:
            sys.exit(f"--incremental is for parent regions; {args.region} is a slice of {reg['slice_of']}: run it as usual")
        return main_slice(args, reg, regs[reg["slice_of"]], data, work, out, name)
    prev = Path(args.prev) if args.prev else data / f"releases/{name}/latest/{name}.parquet"
    report = {"added": set(), "changed": set()}

    grid = pq.read_table(work / "grid.parquet")
    base = left_join(left_join(grid, pq.read_table(work / "layers/admin.parquet"), "h3_id"),
                     pq.read_table(work / "layers/static.parquet"), "h3_id")
    rain = [p.stem for p in sorted((work / "layers").glob("regen_*.parquet"))]  # prepare/regenwater.py, per scenario
    for layer in ("wind", "osm", "osm_extra", "laadpalen", "waterkeringen", "netcongestie", *rain):
        if (work / f"layers/{layer}.parquet").exists():
            base = left_join(base, pq.read_table(work / f"layers/{layer}.parquet"), "h3_id")
    lgn = lgn_columns(work)
    global OSM_STAMP
    if (work / "layers/osm.parquet").exists():
        meta = json.loads(pq.read_schema(work / "layers/osm.parquet").metadata[b"datacube"])
        OSM_STAMP = meta["osm_timestamp"]
    global LAAD_STAMP
    if (work / "layers/laadpalen.parquet").exists():
        LAAD_STAMP = json.loads(pq.read_schema(work / "layers/laadpalen.parquet").metadata[b"datacube"])["snapshot"][:10]

    if args.year:
        t = build_year(work, args.year, base, lgn)
        single = out / f"{name}_{args.year}"
        pq.write_table(t, single.with_suffix(".parquet"), compression="zstd")
        manifest = build_manifest(t.schema, {args.year: valid_shares(t)}, Path(__file__).parent / "config")
        single.with_suffix(".columns.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
        log(f"wrote {single}.parquet ({t.num_rows} rows x {t.num_columns} cols) and .columns.json")
        return

    delta_path = out / name
    if delta_path.exists():
        shutil.rmtree(delta_path)
    schema, shares, writer = None, {}, None
    if args.incremental:
        if prev.resolve().parent == out.resolve():
            sys.exit(f"--from {prev} is the cube this run overwrites; use a release")
        log(f"incremental: years from {prev.resolve()}, static layers from {work}")
    for year in YEARS:
        t = incremental_year(prev, year, base, report) if args.incremental else build_year(work, year, base, lgn)
        if schema is None:
            schema = t.schema
            writer = pq.ParquetWriter(out / f"{name}.parquet", schema, compression="zstd")
        t = t.select(schema.names).cast(schema)
        write_deltalake(str(delta_path), t, mode="append", partition_by=["year_int"])
        writer.write_table(t)
        shares[year] = valid_shares(t)
        log(f"{year}: {t.num_rows} rows x {t.num_columns} cols")
        del t
    writer.close()
    log(f"wrote {delta_path} (partitioned by year_int) and {out / f'{name}.parquet'}")

    if (work / "layers/osm_lijnen.parquet").exists():
        shutil.copy2(work / "layers/osm_lijnen.parquet", out / f"{name}_lijnen.parquet")
        log(f"wrote {out / f'{name}_lijnen.parquet'} (OSM main roads and railways, whole lines)")
    if (work / "layers/waterkeringen_lijnen.parquet").exists():
        shutil.copy2(work / "layers/waterkeringen_lijnen.parquet", out / f"{name}_waterkeringen.parquet")
        log(f"wrote {out / f'{name}_waterkeringen.parquet'} (primary flood defences, whole lines)")
    if (work / "layers/netgebieden.parquet").exists():
        shutil.copy2(work / "layers/netgebieden.parquet", out / f"{name}_netgebieden.parquet")
        log(f"wrote {out / f'{name}_netgebieden.parquet'} (grid supply areas)")
    if (work / "layers/s2_dekking.parquet").exists():
        shutil.copy2(work / "layers/s2_dekking.parquet", out / f"{name}_s2_dekking.parquet")
        log(f"wrote {out / f'{name}_s2_dekking.parquet'} (NDVI per cell, year and LGN group of the pixel)")
    if (work / "layers/knmi_voor.parquet").exists():
        shutil.copy2(work / "layers/knmi_voor.parquet", out / f"{name}_knmi_voor.parquet")
        log(f"wrote {out / f'{name}_knmi_voor.parquet'} (KNMI per cell for years before the cube)")
    if (work / "layers/knmi_wind_jaar.parquet").exists():
        shutil.copy2(work / "layers/knmi_wind_jaar.parquet", out / f"{name}_jaar.parquet")
        log(f"wrote {out / f'{name}_jaar.parquet'} (national values per year)")
    for layer, suffix in GEMEENTE_TABLES.items():
        if (work / f"layers/{layer}.parquet").exists():
            shutil.copy2(work / f"layers/{layer}.parquet", out / f"{name}_{suffix}.parquet")
            log(f"wrote {out / f'{name}_{suffix}.parquet'} (values per gemeente, not per cell)")

    manifest = build_manifest(schema, shares, Path(__file__).parent / "config")
    (out / f"{name}.columns.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    log(f"wrote {out / f'{name}.columns.json'} ({len(manifest)} columns)")
    inc = out / f"{name}.incremental.json"
    if args.incremental:
        info = {"from": str(prev.resolve()), "added": sorted(report["added"]), "changed": sorted(report["changed"])}
        inc.write_text(json.dumps(info, indent=1))
        log(f"incremental: {len(info['added'])} columns added, {len(info['changed'])} changed: {info['changed']}")
    elif inc.exists():
        inc.unlink()  # a full build: coarsen.py --incremental must not trust an old report

    if args.region == "zh":
        write_compat(out, name, args.original)


def write_compat(out: Path, name: str, original: str):
    real = pq.read_table(out / f"{name}.parquet")
    original_schema = pq.read_schema(glob.glob(f"{original}/*.parquet")[0])
    write_delta(build_compat(real, original_schema), out / "volledige_tijdreeks_delta")


def main_slice(args, reg: dict, parent: dict, data: Path, work: Path, out: Path, name: str):
    """A region cut from its parent's assembled cube (`slice_of` in regions.toml): every value comes from the
    parent, so the two never drift apart; only the grid columns (share inside THIS region) and the region's own
    layers (TESSERA) are added. Build the parent first."""
    pname = f"datacube_{reg['slice_of']}"
    parent_out, parent_work = data / parent["out"], data / parent["work"]
    parent_parquet = parent_out / f"{pname}.parquet"
    if not parent_parquet.exists():
        sys.exit(f"missing {parent_parquet}: run assemble.py --region {reg['slice_of']} first")
    global OSM_STAMP, LAAD_STAMP
    if (parent_work / "layers/osm.parquet").exists():
        OSM_STAMP = json.loads(pq.read_schema(parent_work / "layers/osm.parquet").metadata[b"datacube"])["osm_timestamp"]
    if (parent_work / "layers/laadpalen.parquet").exists():
        LAAD_STAMP = json.loads(pq.read_schema(parent_work / "layers/laadpalen.parquet").metadata[b"datacube"])["snapshot"][:10]
    grid = pq.read_table(work / "grid.parquet")
    log(f"{args.region}: slice of {pname} ({len(grid)} hexes) + own layers")

    if args.year:
        t = slice_year(parent_parquet, args.year, grid, work)
        single = out / f"{name}_{args.year}"
        pq.write_table(t, single.with_suffix(".parquet"), compression="zstd")
        manifest = build_manifest(t.schema, {args.year: valid_shares(t)}, Path(__file__).parent / "config")
        single.with_suffix(".columns.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
        log(f"wrote {single}.parquet ({t.num_rows} rows x {t.num_columns} cols) and .columns.json")
        return

    delta_path = out / name
    if delta_path.exists():
        shutil.rmtree(delta_path)
    schema, shares, writer = None, {}, None
    for year in YEARS:
        t = slice_year(parent_parquet, year, grid, work)
        if schema is None:
            schema = t.schema
            writer = pq.ParquetWriter(out / f"{name}.parquet", schema, compression="zstd")
        t = t.select(schema.names).cast(schema)
        write_deltalake(str(delta_path), t, mode="append", partition_by=["year_int"])
        writer.write_table(t)
        shares[year] = valid_shares(t)
        log(f"{year}: {t.num_rows} rows x {t.num_columns} cols")
        del t
    writer.close()
    log(f"wrote {delta_path} (partitioned by year_int) and {out / f'{name}.parquet'}")

    if (parent_out / f"{pname}_lijnen.parquet").exists():
        slice_lines(parent_out / f"{pname}_lijnen.parquet", set(grid["h3_id"].to_pylist()), out / f"{name}_lijnen.parquet")
    if (parent_out / f"{pname}_jaar.parquet").exists():
        shutil.copy2(parent_out / f"{pname}_jaar.parquet", out / f"{name}_jaar.parquet")  # national: same file
    for suffix in ("knmi_voor", "s2_dekking"):  # per-cell side files: keep this region's cells
        if (parent_out / f"{pname}_{suffix}.parquet").exists():
            k = pq.read_table(parent_out / f"{pname}_{suffix}.parquet")
            k = k.filter(pc.is_in(k["h3_id"], value_set=grid["h3_id"].combine_chunks()))
            pq.write_table(k, out / f"{name}_{suffix}.parquet", compression="zstd")
            log(f"wrote {out / f'{name}_{suffix}.parquet'} ({k.num_rows} rows)")
    if (parent_out / f"{pname}_waterkeringen.parquet").exists():
        slice_lines(parent_out / f"{pname}_waterkeringen.parquet", set(grid["h3_id"].to_pylist()),
                    out / f"{name}_waterkeringen.parquet")
    if (parent_out / f"{pname}_netgebieden.parquet").exists():
        areas = set(pq.read_table(out / f"{name}.parquet", columns=["net_voedingsgebied_id"])["net_voedingsgebied_id"]
                    .to_pylist())
        g = pq.read_table(parent_out / f"{pname}_netgebieden.parquet")
        g = g.filter(pa.array([a in areas for a in g["voedingsgebied_id"].to_pylist()]))
        pq.write_table(g.replace_schema_metadata(pq.read_schema(parent_out / f"{pname}_netgebieden.parquet").metadata),
                       out / f"{name}_netgebieden.parquet")
        log(f"wrote {out / f'{name}_netgebieden.parquet'} ({g.num_rows} supply areas)")
    mine = set(pq.read_table(out / f"{name}.parquet", columns=["gemeentecode"])["gemeentecode"].to_pylist())
    for suffix in GEMEENTE_TABLES.values():
        if (parent_out / f"{pname}_{suffix}.parquet").exists():
            g = pq.read_table(parent_out / f"{pname}_{suffix}.parquet")
            g = g.filter(pa.array([c in mine for c in g["gemeentecode"].to_pylist()]))
            pq.write_table(g, out / f"{name}_{suffix}.parquet")
            log(f"wrote {out / f'{name}_{suffix}.parquet'} ({len(mine - {None})} gemeenten)")

    manifest = build_manifest(schema, shares, Path(__file__).parent / "config")
    (out / f"{name}.columns.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    log(f"wrote {out / f'{name}.columns.json'} ({len(manifest)} columns)")
    if args.region == "zh":
        write_compat(out, name, args.original)


if __name__ == "__main__":
    main()
