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

SECOND FIX (2026-08-19, same day) — FIELD NAMES ALSO NOT WHAT WAS
ASSUMED: the first version of this fix assumed the generalized layer
shared the full-resolution layer's field names (STATE, COUNTY, GEOID).
The first live run confirmed this assumption was wrong: EVERY single
county (292/292) returned a 400 "Failed to execute query" error — the
classic ArcGIS REST signature for a WHERE clause referencing a
nonexistent field. Rather than guess at a second replacement field name
and risk a THIRD failed run, this now queries the resolved layer's own
field-list metadata at runtime (same self-verifying pattern as the
layer-ID resolution above) and builds the WHERE clause from whatever
fields actually exist: STATE+COUNTY equality if both are present
(matching the full-resolution convention), otherwise a GEOID prefix
match (GEOID is a near-universal identifier field across Census
geography services). If a live run's DEBUG output shows the actual
resolved field list, that's the real confirmation of which path was
taken — treat it as authoritative over this docstring's reasoning.

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
def _resolve_generalized_block_groups_layer() -> dict | None:
    """Finds the Block Groups layer within the Generalized Tracts_Blocks
    service AND its actual field list, by querying the service's own
    metadata endpoints — rather than hardcoding either the layer index
    OR the field names, since BOTH have been confirmed unreliable to
    assume:
      - Layer index: confirmed NOT stable across ACS vintages
        (ACS2015/ACS2021 have Block Groups at layer 2; ACS2024 shifted it
        to layer 3 after adding an extra 'Tracts 5M' layer).
      - Field names: the first live fix assumed STATE/COUNTY/GEOID
        (matching the full-resolution layer) and failed with a 400 error
        on EVERY county (292/292).
      - NAME MATCHING ALONE IS ALSO INSUFFICIENT: the second live fix
        switched to name-matching ("Block Groups" in layer.name) plus
        dynamic field discovery, and it DID resolve a layer — but that
        layer's fields turned out to be just ['OBJECTID', 'BASENAME'],
        which matches a Labels/annotation sub-layer's field set, not a
        real polygon feature layer's. TIGERweb services commonly carry a
        separate label layer alongside the real data layer, and both can
        have "Block Groups" somewhere in their name — a plain substring
        match on name isn't enough to tell them apart.

    THIS VERSION: checks EVERY layer whose name contains "Block Groups",
    fetches each one's actual field list, and picks the first candidate
    that has genuine identifying fields (GEOID, or STATE+COUNTY) — a
    Labels/annotation layer with only OBJECTID/BASENAME gets skipped
    rather than blindly accepted as "the" match. If a live run's DEBUG
    output shows which candidate was accepted (and what was skipped),
    that's the real confirmation — treat it as authoritative over this
    docstring's reasoning.

    Returns {"layer_id": int, "fields": [field_name, ...]} or None on
    failure. Cached since this only needs to run once per pipeline run.
    """
    layers_url = f"{GENERALIZED_TRACTS_BLOCKS_BASE.format(year=ACS_YEAR)}/layers"
    try:
        resp = requests.get(layers_url, params={"f": "json"}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  WARNING: failed to resolve Block Groups layer: {e}", file=sys.stderr)
        return None
    except ValueError as e:
        print(f"  WARNING: layer metadata response was not JSON: {e}", file=sys.stderr)
        return None

    candidates = [
        layer for layer in data.get("layers", [])
        if "Block Groups" in layer.get("name", "")
    ]
    if not candidates:
        print(f"  WARNING: no layer with 'Block Groups' in its name found — "
              f"available layers: {[l.get('name') for l in data.get('layers', [])]}", file=sys.stderr)
        return None

    for candidate in candidates:
        candidate_id = candidate.get("id")
        candidate_name = candidate.get("name")
        layer_detail_url = f"{GENERALIZED_TRACTS_BLOCKS_BASE.format(year=ACS_YEAR)}/{candidate_id}"
        try:
            resp = requests.get(layer_detail_url, params={"f": "json"}, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            layer_data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  candidate layer id={candidate_id} name={candidate_name!r}: "
                  f"failed to fetch field list ({e}), trying next candidate", file=sys.stderr)
            continue

        fields = [f.get("name") for f in layer_data.get("fields", [])]
        has_geoid = "GEOID" in fields
        has_state_county = "STATE" in fields and "COUNTY" in fields

        if has_geoid or has_state_county:
            print(f"  resolved Generalized Block Groups layer: id={candidate_id}, "
                  f"name={candidate_name!r}, fields={fields}", file=sys.stderr)
            return {"layer_id": candidate_id, "fields": fields}
        else:
            print(f"  candidate layer id={candidate_id} name={candidate_name!r} "
                  f"has fields={fields} — no GEOID or STATE+COUNTY, looks like a "
                  f"Labels/annotation layer, not the real data layer; trying next candidate",
                  file=sys.stderr)

    print(f"  WARNING: none of the {len(candidates)} 'Block Groups'-named candidate "
          f"layers had usable identifier fields — cannot proceed", file=sys.stderr)
    return None


def fetch_county_block_group_boundaries(state_fips: str, county_fips: str) -> dict:
    """Returns a GeoJSON FeatureCollection of block group boundary
    polygons for one county, via TIGERweb's GENERALIZED ArcGIS REST
    service (Generalized_ACS{year}/Tracts_Blocks) — deliberately NOT the
    full-resolution TIGERweb/Tracts_Blocks service, which produced a
    202MB output file at statewide (MN+GA+SC) scale.

    WHERE-clause field choice is resolved dynamically from the layer's
    own field list rather than assumed: prefers STATE+COUNTY equality if
    both fields exist (matches the full-resolution layer's convention),
    falls back to a GEOID prefix match if not (GEOID is a near-universal
    identifier field on Census geography services, confirmed present on
    at least one other Generalized-family layer's metadata during
    research for this fix) — this self-corrects instead of repeating the
    same kind of hardcoded-assumption failure that broke the first
    version of this fix."""
    resolved = _resolve_generalized_block_groups_layer()
    if resolved is None:
        print(f"  SKIPPING {state_fips}/{county_fips}: could not resolve Block Groups layer", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    layer_id = resolved["layer_id"]
    fields = resolved["fields"]
    query_url = f"{GENERALIZED_TRACTS_BLOCKS_BASE.format(year=ACS_YEAR)}/{layer_id}/query"

    if "STATE" in fields and "COUNTY" in fields:
        where = f"STATE='{state_fips}' AND COUNTY='{county_fips}'"
    elif "GEOID" in fields:
        where = f"GEOID LIKE '{state_fips}{county_fips}%'"
    else:
        print(f"  WARNING: layer has neither STATE/COUNTY nor GEOID fields — "
              f"cannot build a county filter. Available fields: {fields}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    out_fields = [f for f in ("GEOID", "STATE", "COUNTY", "TRACT", "BLKGRP", "BASENAME") if f in fields]
    if not out_fields:
        out_fields = ["*"]

    params = {
        "where": where,
        "outFields": ",".join(out_fields),
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

    if "error" in result:
        print(f"  WARNING: generalized boundary query returned an error for {state_fips}/{county_fips}: "
              f"{result['error']!r} — WHERE clause used: {where!r}, fields resolved from layer metadata: {fields}",
              file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    return result
