"""
Builds the area-income choropleth layer: fetches block group boundaries
+ median household incomes for EVERY county in Minnesota, Georgia, and
South Carolina, colors each block group by its income relative to its
STATE's median (not a fixed dollar amount — see lookup_census_geo.py's
module docstring for why), and writes one GeoJSON FeatureCollection the
app can render directly as a Leaflet layer underneath the existing
property dots.

DESIGN DECISIONS (from project discussion, 2026-08-18):
  - Geography: block group — the smallest geography ACS publishes
    median household income for (tract is the next level up; block
    itself has no income data).
  - Reference point: each block group's income is compared to its own
    STATE's overall median household income, not a national or county
    figure, and not a fixed dollar amount. This is the piece that makes
    the color scale meaningful in any state — Minnesota and Mississippi
    have very different income distributions, and a ratio to the LOCAL
    state's median stays interpretable in both.
  - Color bands (ratio to state median): tunable, not fixed by any
    external standard — see AREA_INCOME_COLORS/THRESHOLDS below.
  - Educational attainment: deliberately NOT included — discussed and
    explicitly deferred, since it's strongly correlated with income at
    this geography and would mostly duplicate the same visual signal.
    If revisited, extend fetch_county_block_group_incomes-style logic
    with table B15003 rather than building a parallel pipeline.

UPDATE (2026-08-19, second revision) — FULLY STATEWIDE, INDEPENDENT OF
LISTINGS: earlier versions of this script only fetched block groups for
counties that had at least one property/listing in them (first to keep
mortgage-foreclosure-only metro coverage fast, then briefly extended to
also cover tax-forfeited-land coordinates). Per explicit user direction,
the map coloring should NOT depend on whether anything is listed in a
given county — every county in MN/GA/SC gets colored regardless. This
script no longer reads real_estate_scored.json or
public_surplus_taxforfeited.json at all; it enumerates every county in
each in-scope state directly via the same Census county Gazetteer file
already confirmed working in geocode_county_centroid.py (same URL
pattern, same verified column layout: USPS|GEOID|GEOIDFQ|ANSICODE|NAME|
ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG — GEOID is the
concatenated 5-digit state+county FIPS code).

SCALE NOTE — UNTESTED AT THIS VOLUME: this now fetches ~87 (MN) + 159
(GA) + 46 (SC) ≈ 292 counties' worth of block group boundaries + income
data every run, versus only the handful of counties that happened to
have a listing before. Whether the Census API has rate limits that would
be hit at this volume, and whether a full run completes within a
reasonable time, are both genuinely unknown until the first live run —
same standing rule as everything else in this project: treat that run's
output as the real verification step, not this docstring's reasoning.

⚠️ VERIFICATION STATUS: same as lookup_census_geo.py — built carefully
from documented API shapes, not yet run against live data (network
blocked in this sandbox).
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

from lookup_census_geo import (
    fetch_state_median_income,
    fetch_county_block_group_incomes,
    fetch_county_block_group_boundaries,
)

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_PATH = DATA_DIR / "area_income_blockgroups.json"

# Every state currently in the app's state dropdown gets full coverage —
# extend this list alongside the dropdown itself if more states are added
# (see schema-tax-forfeited-land.md's "confirmed future candidates" note
# for TN/AL, which are NOT yet in scope here).
STATES_IN_SCOPE = ["MN", "GA", "SC"]
STATE_FIPS = {"MN": "27", "GA": "13", "SC": "45"}

GAZETTEER_URL_TEMPLATE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_gaz_counties_{fips}.txt"

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
MAX_RETRIES_PER_REQUEST = 2
REQUEST_DELAY_SECONDS = 1
FETCH_DELAY_SECONDS = 0.5

# Ratio to STATE median household income -> color tier. Starting point
# proposed during project design discussion; easy to retune once seen
# rendered against real data — not derived from any external standard
# (HUD's AMI bands exist for a different purpose — housing-assistance
# eligibility — and use different cutoffs than what makes sense for a
# general-purpose "is this area relatively wealthy" map tint).
AREA_INCOME_THRESHOLDS = {"green": 1.30, "yellow": 1.00, "orange": 0.70}  # below "orange" = red
AREA_INCOME_COLORS = {
    "red": "#f85149", "orange": "#f4a300", "yellow": "#f7d654", "green": "#3fb950",
}


def tier_for_ratio(ratio: float) -> str:
    if ratio >= AREA_INCOME_THRESHOLDS["green"]:
        return "green"
    if ratio >= AREA_INCOME_THRESHOLDS["yellow"]:
        return "yellow"
    if ratio >= AREA_INCOME_THRESHOLDS["orange"]:
        return "orange"
    return "red"


def parse_gazetteer_counties(text: str) -> list[tuple[str, str]]:
    """Parses the Counties Gazetteer format into [(state_fips,
    county_fips), ...] for every county row. Same delimiter-detection
    approach as geocode_county_centroid.py — tries pipe first (per
    Census's stated format), falls back to tab if that doesn't yield the
    expected column count, and logs which was used."""
    lines = [l for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return []

    delimiter = "|"
    if len(lines[0].split(delimiter)) < 11:
        delimiter = "\t"
        print(f"  pipe delimiter gave {len(lines[0].split('|'))} columns (expected 11) — using tab instead", file=sys.stderr, flush=True)

    counties = []
    header_skipped = False
    for line in lines:
        cols = line.split(delimiter)
        if len(cols) < 11:
            continue
        if not header_skipped and cols[0].strip().upper() == "USPS":
            header_skipped = True
            continue
        geoid = cols[1].strip()
        if len(geoid) != 5 or not geoid.isdigit():
            continue
        counties.append((geoid[:2], geoid[2:]))

    return counties


def fetch_all_counties_for_state(state: str) -> list[tuple[str, str]]:
    """Returns [(state_fips, county_fips), ...] for EVERY county in the
    given state, regardless of whether anything is listed there — via
    the Census's own county Gazetteer file, same source already
    confirmed working in geocode_county_centroid.py."""
    fips = STATE_FIPS.get(state)
    if not fips:
        print(f"  no known FIPS code for state {state!r} — skipping", file=sys.stderr, flush=True)
        return []

    url = GAZETTEER_URL_TEMPLATE.format(fips=fips)
    for attempt in range(1, MAX_RETRIES_PER_REQUEST + 2):
        try:
            print(f"  fetching county list for {state} (attempt {attempt})...", file=sys.stderr, flush=True)
            resp = requests.get(
                url,
                headers={"User-Agent": "twin-cities-storage-scout/1.0 (research project)"},
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            return parse_gazetteer_counties(resp.text)
        except requests.exceptions.RequestException as e:
            print(f"  attempt {attempt} failed for {state}: {e}", file=sys.stderr, flush=True)
            if attempt <= MAX_RETRIES_PER_REQUEST:
                time.sleep(2)
            else:
                return []
    return []


def main():
    needed_counties = set()  # {(state_fips, county_fips), ...}
    for state in STATES_IN_SCOPE:
        counties = fetch_all_counties_for_state(state)
        print(f"  {state}: {len(counties)} counties enumerated", file=sys.stderr, flush=True)
        needed_counties.update(counties)
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Total: {len(needed_counties)} counties across {len(STATES_IN_SCOPE)} states", file=sys.stderr, flush=True)

    if not needed_counties:
        OUT_PATH.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        print(f"Wrote empty FeatureCollection to {OUT_PATH} (no counties enumerated)", file=sys.stderr, flush=True)
        return

    # Fetch each needed state's median income once, cached.
    state_median_income = {}
    for state_fips, _ in needed_counties:
        if state_fips in state_median_income:
            continue
        time.sleep(FETCH_DELAY_SECONDS)
        median = fetch_state_median_income(state_fips)
        state_median_income[state_fips] = median
        print(f"  state {state_fips} median household income: {median}", file=sys.stderr, flush=True)

    # For each county, fetch block group incomes + boundaries, join them,
    # compute the color tier, and build GeoJSON features.
    features = []
    tier_counts = {"red": 0, "orange": 0, "yellow": 0, "green": 0}
    for state_fips, county_fips in sorted(needed_counties):
        state_median = state_median_income.get(state_fips)
        if not state_median:
            print(f"  SKIPPING {state_fips}/{county_fips}: no state median income available", file=sys.stderr, flush=True)
            continue

        time.sleep(FETCH_DELAY_SECONDS)
        incomes = fetch_county_block_group_incomes(state_fips, county_fips)
        time.sleep(FETCH_DELAY_SECONDS)
        boundaries = fetch_county_block_group_boundaries(state_fips, county_fips)

        matched = 0
        for feature in boundaries.get("features", []):
            geoid = (feature.get("properties") or {}).get("GEOID")
            income = incomes.get(geoid)
            if income is None:
                continue
            ratio = income / state_median
            tier = tier_for_ratio(ratio)
            tier_counts[tier] += 1
            matched += 1
            feature["properties"]["median_household_income"] = income
            feature["properties"]["state_median_household_income"] = state_median
            feature["properties"]["income_ratio_to_state_median"] = round(ratio, 3)
            feature["properties"]["tier"] = tier
            feature["properties"]["color"] = AREA_INCOME_COLORS[tier]
            features.append(feature)

        print(f"  {state_fips}/{county_fips}: {len(boundaries.get('features', []))} block group "
              f"boundaries, {len(incomes)} income values, {matched} successfully joined", file=sys.stderr, flush=True)

    print(f"Tier breakdown: {tier_counts}", file=sys.stderr, flush=True)

    out = {"type": "FeatureCollection", "features": features}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out))
    print(f"Wrote {len(features)} block group features to {OUT_PATH}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

