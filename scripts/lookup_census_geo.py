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
metadata — confirmed via web search that layer 1 of TIGERweb/
Tracts_Blocks/MapServer is "Census Block Groups" and supports geoJSON
output), but NOT yet run against live data — this sandbox's network is
blocked from these endpoints, same limitation as every other API this
project talks to. Same practice as everywhere else in this project:
treat this as a solid first pass, not a proven-correct one, until a real
run's DEBUG output confirms the actual response shapes match what's
assumed below.

APIs used (all free, no signup required for light use):
  - Census Geocoder (geographies/coordinates endpoint) — reverse geocode
    a point to its block group. https://geocoding.geo.census.gov
  - Census Data API (ACS 5-year Detailed Tables) — median household
    income (B19013_001E), available down to block group.
    https://api.census.gov/data
  - TIGERweb ArcGIS REST (Tracts_Blocks/MapServer, layer 1 = Census
    Block Groups) — block group boundary polygons, GeoJSON output.
    https://tigerweb.geo.census.gov/arcgis/rest/services

RATE LIMITS: the Census Data API allows some unauthenticated use but
recommends a free API key for anything beyond light/occasional use — see
https://api.census.gov/data/key_signup.html. This module reads an
optional CENSUS_API_KEY environment variable and appends it to requests
if present; works without one at the volume this project currently
needs (one state, ~7 counties), but get a key before expanding to many
states — see module docstring in score_area_income.py.
"""

import os
import sys
import time
from functools import lru_cache

import requests

GEOCODER_BASE = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
ACS_BASE = "https://api.census.gov/data/{year}/acs/acs5"
TIGERWEB_BLOCKGROUPS_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer/1/query"
)

# ACS 5-year vintage to use. Bump this as newer vintages become
# available (Census typically releases a new 5-year vintage each
# December) — not verified live against what's currently published, see
# module docstring.
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
    {"state": "27", "county": "053", "tract": "005400", "block_group": "1",
    "geoid": "270530054001"} or None if the lookup fails.

    NOTE: the Census Geocoder's coordinate-lookup endpoint takes x=lng,
    y=lat (longitude first) — easy to get backwards, double-checked
    against the documented parameter names before writing this."""
    params = {
        "x": lng, "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "layers": "Block Groups",
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
        block_groups = data["result"]["geographies"]["Block Groups"]
        if not block_groups:
            return None
        bg = block_groups[0]
        return {
            "state": bg["STATE"],
            "county": bg["COUNTY"],
            "tract": bg["TRACT"],
            "block_group": bg["BLKGRP"],
            "geoid": bg["GEOID"],
        }
    except (KeyError, IndexError) as e:
        print(f"  WARNING: unexpected geocoder response shape for ({lat}, {lng}): {e} "
              f"— raw response: {data!r}", file=sys.stderr)
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


def fetch_county_block_group_boundaries(state_fips: str, county_fips: str) -> dict:
    """Returns a GeoJSON FeatureCollection of block group boundary
    polygons for one county, via TIGERweb's ArcGIS REST service — same
    query pattern already proven in lookup_parcel.py for Met Council's
    ArcGIS endpoint, just a different service and a different WHERE
    clause. STATE/COUNTY are the field names TIGERweb uses (confirmed
    via the service's own field metadata during research for this
    script — not yet confirmed against a live query response)."""
    params = {
        "where": f"STATE='{state_fips}' AND COUNTY='{county_fips}'",
        "outFields": "GEOID,STATE,COUNTY,TRACT,BLKGRP,BASENAME",
        "f": "geojson",
    }
    try:
        resp = requests.get(TIGERWEB_BLOCKGROUPS_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"  WARNING: TIGERweb boundary fetch failed for {state_fips}/{county_fips}: {e}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}
    except ValueError as e:  # JSON decode failure
        print(f"  WARNING: TIGERweb returned non-JSON for {state_fips}/{county_fips}: {e}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}
