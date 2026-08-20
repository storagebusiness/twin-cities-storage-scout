"""
Reverse-geocodes a lat/lng into a Census block group (via the free Census
Geocoder), then fetches ACS 5-year median household income (table
B19013_001E) at both the block-group level and the state level, plus the
block group's boundary polygon (via TIGERweb's ArcGIS REST service).

WHY THIS EXISTS: to color the real estate map's background by area
income — a block group's income relative to its STATE's median income
(a ratio, not a dollar amount) — see HANDOFF.md "Area-wealth map
coloring" for the full design discussion. A ratio-based, state-relative
approach is deliberate: fixed dollar breakpoints tuned for one state's
income distribution look meaningless (all-red or all-green) in a state
with a very different cost of living, and this project is meant to work
in any state, not just Minnesota.

⚠️ VERIFICATION STATUS: built from documented API patterns (Census
Geocoder docs, Census Data API docs, TIGERweb's own REST service
metadata), but NOT yet run against live data — this sandbox's network is
blocked from these endpoints. Same practice as everywhere else in this
project: treat this as a solid first pass, not a proven-correct one,
until a real run's DEBUG output confirms the actual response shapes
match what's assumed below.

FIX (2026-08-19) — SWITCHED FROM FULL-RESOLUTION TO GENERALIZED
BOUNDARIES: the first live run of the statewide (MN+GA+SC, ~292 county)
version of this pipeline produced a 202MB output file, well over
GitHub's 100MB push limit. Root cause: fetch_county_block_group_boundaries
was querying TIGERweb/Tracts_Blocks/MapServer/1 — full-resolution
TIGER/Line geometry (survey-grade precision, every legal/water boundary
vertex included), never a problem at the old scale (a handful of
counties) but completely impractical at 292 counties statewide.

Census publishes a SEPARATE, purpose-built "Generalized" REST service
family specifically for thematic maps like this one — confirmed via live
search of the service's own metadata (2026-08-19):
  https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS{year}/Tracts_Blocks/MapServer
"500K" = simplified to 1:500,000 scale — dramatically fewer vertices per
polygon than the full-resolution TIGER/Line source, while remaining
visually accurate at any zoom level reasonable for a thematic web map.

IMPORTANT — the Block Groups layer's ID WITHIN that service is NOT
stable across vintages: ACS2015/ACS2021 have it at layer 2, but ACS2024
added an extra "Census Tracts 5M" layer that shifts Block Groups to
layer 3. Rather than hardcode either number and risk breaking again on
the next Census vintage bump, this queries the service's own `/layers`
endpoint at runtime and finds whichever layer's name contains "Block
Groups" — self-correcting across vintages instead of assuming a fixed
index.

REMAINING UNVERIFIED ASSUMPTION: the generalized layer's field names
(STATE, COUNTY, GEOID, etc.) are assumed to match the full-resolution
layer's (both are standard Census TIGER/geography services, which
typically share these identifier field names) — but this has NOT been
directly confirmed for the generalized block-groups layer specifically.
If the WHERE clause below returns zero features against a live layer
that Step 1 successfully resolved, mismatched field names are the first
thing to check — the DEBUG output will show the attempted query and the
raw (likely error or empty) response.

APIs used (all free, no signup required for light use):
  - Census Geocoder (geographies/coordinates endpoint) — reverse geocode
    a point to its block group. https://geocoding.geo.census.gov
  - Census Data API (ACS 5-year Detailed Tables) — median household
    income (B19013_001E), available down to block group.
    https://api.census.gov/data
  - TIGERweb ArcGIS REST, Generalized_ACS{year}/Tracts_Blocks — block
    group boundary polygons, generalized for thematic mapping, GeoJSON
    output. https://tigerweb.geo.census.gov/arcgis/rest/services

RATE LIMITS: the Census Data API allows some unauthenticated use but
recommends a free API key for anything beyond light/occasional use — see
https://api.census.gov/data/key_signup.html. This module reads an
optional CENSUS_API_KEY environment variable and appends it to requests
if present. Now that score_area_income.py fetches statewide (MN+GA+SC,
~292 counties) rather than just a handful, getting a real key is a much
higher priority than it was at the original small scale.
"""
import os
import sys
import time
from functools import lru_cache

import requests

GEOCODER_BASE = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
ACS_BASE = "https://api.census.gov/data/{year}/acs/acs5"
GENERALIZED_TRACTS_BLOCKS_BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS{year}/Tracts_Blocks/MapServer"

# ACS 5-year vintage to use. Bump this as newer vintages become
# available (Census typically releases a new 5-year vintage each
# December) — not verified live against what's currently published, see
# module docstring. The Generalized boundary service uses the SAME
# {year} in its URL, so bumping this one constant keeps both in sync.
ACS_YEAR = 2023

MEDIAN_HOUSEHOLD_INCOME_VAR = "B19013_001E"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}

REQUEST_DELAY_SECONDS = 0.5  # be polite to free government APIs


def _census_api_key_param() -> dict:
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    return {"key": key} if key else {}


def geocode_to_block_group(lat: float, lng: float) -> dict | None:
    """Reverse-geocode a point to its Census block group. Returns
    {"state": "27", "county": "053", "tract": "026723", "block_group": "2",
    "geoid": "270530267232"} or None if the lookup fails.

    NOTE: the Census Geocoder's coordinate-lookup endpoint takes x=lng,
    y=lat (longitude first) — easy to get backwards, double-checked
    against the documented parameter names before writing this.

    NOTE ON THE "layers" PARAMETER (found on a live run, 2026-08-18): the
    Current_Current benchmark/vintage does NOT expose "Block Groups" as
    its own top-level geography category, regardless of the "layers"
    param requested — it's silently ignored, and the response always
    contains whatever fixed set of categories that benchmark returns
    (States, Counties, "2020 Census Blocks", Census Tracts, etc., no
    "Block Groups" key at all). Block-group info is still present,
    though: every "2020 Census Blocks" record carries STATE/COUNTY/
    TRACT/BLKGRP fields — the BLKGRP field IS the block group, just
    attached to the more-granular block record rather than exposed as
    its own category. So instead of looking for a "Block Groups" key,
    this searches every returned category for the first record carrying
    all four of those fields and builds the GEOID from them directly.
    Dropped the "layers" param since it wasn't actually influencing the
    response."""
    params = {
        "x": lng, "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    try:
        resp = requests.get(GEOCODER_BASE, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  WARNING: geocoder request failed for ({lat}, {lng}): {e}", file=sys.stderr)
        return None

    try:
        geographies = data["result"]["geographies"]
    except KeyError as e:
        print(f"  WARNING: unexpected geocoder response shape for ({lat}, {lng}): {e} "
              f"— raw response: {data!r}", file=sys.stderr)
        return None

    for records in geographies.values():
        if not records:
            continue
        first = records[0]
        if all(k in first for k in ("STATE", "COUNTY", "TRACT", "BLKGRP")):
            state, county, tract, bg = first["STATE"], first["COUNTY"], first["TRACT"], first["BLKGRP"]
            return {
                "state": state, "county": county, "tract": tract, "block_group": bg,
                "geoid": f"{state}{county}{tract}{bg}",
            }

    print(f"  WARNING: no geography category with STATE/COUNTY/TRACT/BLKGRP fields "
          f"found for ({lat}, {lng}) — categories returned: {list(geographies.keys())}", file=sys.stderr)
    return None


def fetch_state_median_income(state_fips: str) -> float | None:
    """One value per state — the reference point every block group in
    that state gets compared against. Cached by the caller (see
    score_area_income.py) since this only needs to be fetched once per
    state, not once per property."""
    url = ACS_BASE.format(year=ACS_YEAR)
    params = {"get": MEDIAN_HOUSEHOLD_INCOME_VAR, "for": f"state:{state_fips}",
               **_census_api_key_param()}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as e:
        print(f"  WARNING: state median income fetch failed for state {state_fips}: {e}", file=sys.stderr)
        return None

    # ACS API returns [[header, ...], [value, ...]] — a 2-row table, not
    # a dict. rows[1] is the data row; MEDIAN_HOUSEHOLD_INCOME_VAR is
    # column 0 based on the "get" param order above.
    if len(rows) < 2:
        return None
    try:
        value = float(rows[1][0])
        return value if value > 0 else None  # ACS uses negative sentinel codes for missing data
    except (ValueError, IndexError):
        return None


def fetch_county_block_group_incomes(state_fips: str, county_fips: str) -> dict[str, float]:
    """One API call covers every block group in the county — far more
    efficient than one call per block group. Returns {geoid: income}."""
    url = ACS_BASE.format(year=ACS_YEAR)
    params = {
        "get": MEDIAN_HOUSEHOLD_INCOME_VAR,
        "for": "block group:*",
        "in": f"state:{state_fips} county:{county_fips}",
        **_census_api_key_param(),
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as e:
        print(f"  WARNING: block group income fetch failed for {state_fips}/{county_fips}: {e}", file=sys.stderr)
        return {}

    if len(rows) < 2:
        return {}

    header = rows[0]
    # Column order matches the "get"/"for"/"in" params: [income, state, county, tract, block group]
    try:
        idx_income = header.index(MEDIAN_HOUSEHOLD_INCOME_VAR)
        idx_state = header.index("state")
        idx_county = header.index("county")
        idx_tract = header.index("tract")
        idx_bg = header.index("block group")
    except ValueError as e:
        print(f"  WARNING: unexpected ACS response header {header!r}: {e}", file=sys.stderr)
        return {}

    result = {}
    for row in rows[1:]:
        try:
            income = float(row[idx_income])
        except (ValueError, IndexError):
            continue
        if income <= 0:  # ACS negative sentinel codes for missing/suppressed data
            continue
        geoid = f"{row[idx_state]}{row[idx_county]}{row[idx_tract]}{row[idx_bg]}"
        result[geoid] = income
    return result


@lru_cache(maxsize=1)
def _resolve_generalized_block_groups_layer_id() -> int | None:
    """Finds the Block Groups layer's ID within the Generalized
    Tracts_Blocks service by querying the service's own /layers
    metadata endpoint, rather than hardcoding an index — confirmed via
    live search (2026-08-19) that this index is NOT stable across ACS
    vintages (ACS2015/ACS2021 have it at layer 2; ACS2024 shifted it to
    layer 3 after adding an extra 'Tracts 5M' layer). Cached with
    lru_cache since this only needs to be resolved once per run, not
    once per county — the service's layer structure doesn't change
    mid-run.
    """
    url = f"{GENERALIZED_TRACTS_BLOCKS_BASE.format(year=ACS_YEAR)}/layers"
    try:
        resp = requests.get(url, params={"f": "json"}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  WARNING: failed to resolve Block Groups layer ID: {e}", file=sys.stderr)
        return None
    except ValueError as e:
        print(f"  WARNING: layer metadata response was not JSON: {e}", file=sys.stderr)
        return None

    for layer in data.get("layers", []):
        name = layer.get("name", "")
        if "Block Groups" in name:
            layer_id = layer.get("id")
            print(f"  resolved Generalized Block Groups layer: id={layer_id}, name={name!r}", file=sys.stderr)
            return layer_id

    print(f"  WARNING: no layer with 'Block Groups' in its name found — "
          f"available layers: {[l.get('name') for l in data.get('layers', [])]}", file=sys.stderr)
    return None


def fetch_county_block_group_boundaries(state_fips: str, county_fips: str) -> dict:
    """Returns a GeoJSON FeatureCollection of block group boundary
    polygons for one county, via TIGERweb's GENERALIZED ArcGIS REST
    service (Generalized_ACS{year}/Tracts_Blocks) — deliberately NOT the
    full-resolution TIGERweb/Tracts_Blocks service, which produced a
    202MB output file at statewide (MN+GA+SC) scale. See module
    docstring's 2026-08-19 fix note for the full story."""
    layer_id = _resolve_generalized_block_groups_layer_id()
    if layer_id is None:
        print(f"  SKIPPING {state_fips}/{county_fips}: could not resolve Block Groups layer ID", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    query_url = f"{GENERALIZED_TRACTS_BLOCKS_BASE.format(year=ACS_YEAR)}/{layer_id}/query"
    params = {
        "where": f"STATE='{state_fips}' AND COUNTY='{county_fips}'",
        "outFields": "GEOID,STATE,COUNTY,TRACT,BLKGRP,BASENAME",
        "f": "geojson",
    }
    try:
        resp = requests.get(query_url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except requests.RequestException as e:
        print(f"  WARNING: generalized boundary fetch failed for {state_fips}/{county_fips}: {e}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}
    except ValueError as e:  # JSON decode failure
        print(f"  WARNING: generalized boundary service returned non-JSON for {state_fips}/{county_fips}: {e}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    # If the field names assumed above (STATE/COUNTY/GEOID) don't
    # actually exist on this layer, the WHERE clause will typically
    # error rather than silently return zero rows — surface that clearly
    # rather than let it look like "this county just has no block
    # groups," which would be a confusing false conclusion.
    if "error" in result:
        print(f"  WARNING: generalized boundary query returned an error for {state_fips}/{county_fips}: "
              f"{result['error']!r} — the assumed field names (STATE/COUNTY/GEOID) may not match this "
              f"layer's actual schema; this is the remaining unverified assumption flagged in the module "
              f"docstring", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    return result
