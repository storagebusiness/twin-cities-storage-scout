"""
Builds the area-income choropleth layer: for every county that has at
least one property in data/real_estate_scored.json, fetches that
county's block group boundaries + median household incomes, colors each
block group by its income relative to its STATE's median (not a fixed
dollar amount — see lookup_census_geo.py's module docstring for why),
and writes one GeoJSON FeatureCollection the app can render directly as
a Leaflet layer underneath the existing property dots.

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

WHY THIS SCRIPT ONLY COVERS COUNTIES ALREADY IN real_estate_scored.json:
rather than pre-fetching all block groups for an entire state (a LOT of
data for e.g. 87 Minnesota counties, most of which will never have a
property in this dataset), this only fetches what's actually needed —
the counties where properties exist. This keeps the daily run fast and
avoids fetching/storing boundary data for areas the app will never show.
Trade-off: a newly-added property in a brand-new county needs one full
run to pick up that county's block groups; acceptable since this runs
daily anyway.

⚠️ VERIFICATION STATUS: same as lookup_census_geo.py — built carefully
from documented API shapes, not yet run against live data (network
blocked in this sandbox). Treat the first real run's DEBUG output as the
actual verification step, same as every other scraper in this project.
"""

import json
import sys
import time
from pathlib import Path

from lookup_census_geo import (
    geocode_to_block_group,
    fetch_state_median_income,
    fetch_county_block_group_incomes,
    fetch_county_block_group_boundaries,
)

DATA_DIR = Path(__file__).parent.parent / "data"
IN_PATH = DATA_DIR / "real_estate_scored.json"
OUT_PATH = DATA_DIR / "area_income_blockgroups.json"

GEOCODE_DELAY_SECONDS = 0.3
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


def load_property_coordinates() -> list[tuple[float, float]]:
    """Pulls every property's lat/lng out of the existing scored real
    estate data — same file the app already reads for the dots. Dedupes
    identical coordinates first (the same address can appear multiple
    times across different auction dates in the source data — no need
    to geocode it twice)."""
    if not IN_PATH.exists():
        print(f"{IN_PATH} not found — run score_real_estate.py first", file=sys.stderr)
        return []

    records = json.loads(IN_PATH.read_text())
    seen = set()
    coords = []
    for r in records:
        parcel = r.get("parcel") or {}
        lat, lng = parcel.get("lat"), parcel.get("lng")
        if lat is None or lng is None:
            continue
        key = (round(lat, 5), round(lng, 5))
        if key in seen:
            continue
        seen.add(key)
        coords.append((lat, lng))
    return coords


def main():
    coords = load_property_coordinates()
    print(f"Loaded {len(coords)} unique property coordinates to geocode", file=sys.stderr)
    if not coords:
        OUT_PATH.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        print(f"Wrote empty FeatureCollection to {OUT_PATH} (no coordinates to process)", file=sys.stderr)
        return

    # Step 1: reverse-geocode every property to find which (state, county)
    # pairs actually need block group data.
    needed_counties = set()  # {(state_fips, county_fips), ...}
    geocode_failures = 0
    for lat, lng in coords:
        time.sleep(GEOCODE_DELAY_SECONDS)
        geo = geocode_to_block_group(lat, lng)
        if geo is None:
            geocode_failures += 1
            continue
        needed_counties.add((geo["state"], geo["county"]))

    print(f"Geocoded to {len(needed_counties)} distinct counties across "
          f"{len({s for s, _ in needed_counties})} state(s) "
          f"({geocode_failures} geocode failures)", file=sys.stderr)

    # Step 2: fetch each needed state's median income once, cached.
    state_median_income = {}
    for state_fips, _ in needed_counties:
        if state_fips in state_median_income:
            continue
        time.sleep(FETCH_DELAY_SECONDS)
        median = fetch_state_median_income(state_fips)
        state_median_income[state_fips] = median
        print(f"  state {state_fips} median household income: {median}", file=sys.stderr)

    # Step 3: for each needed county, fetch block group incomes + boundaries,
    # join them, compute the color tier, and build GeoJSON features.
    features = []
    tier_counts = {"red": 0, "orange": 0, "yellow": 0, "green": 0}
    for state_fips, county_fips in sorted(needed_counties):
        state_median = state_median_income.get(state_fips)
        if not state_median:
            print(f"  SKIPPING {state_fips}/{county_fips}: no state median income available", file=sys.stderr)
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
              f"boundaries, {len(incomes)} income values, {matched} successfully joined", file=sys.stderr)

    print(f"Tier breakdown: {tier_counts}", file=sys.stderr)

    out = {"type": "FeatureCollection", "features": features}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out))
    print(f"Wrote {len(features)} block group features to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
