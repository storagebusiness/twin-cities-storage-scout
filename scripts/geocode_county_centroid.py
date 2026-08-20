"""
Fallback geocoding for tax-forfeited listings that have a county name but
NO street address — the majority of current MN listings (Koochiching,
Morrison, Sibley, Goodhue), which geocode_public_surplus.py (address-
based, via the Census address Geocoder) correctly leaves untouched since
there's no street address to send it.

This uses a DIFFERENT, much coarser approach: the Census Bureau's own
Gazetteer file for counties, which gives each county's "internal point"
(INTPTLAT/INTPTLONG) — essentially a representative center point for the
whole county, not a specific parcel location. A pin placed here could be
many miles from the actual property. Every record touched by this script
gets `coordinates.precision: "county_centroid"` so the app can visually
distinguish these from real address-level geocodes (which get
`coordinates.precision: "address"`) rather than presenting both as
equally accurate.

Source: US Census Bureau Gazetteer Files, 2025 vintage, Counties file.
Column layout confirmed directly from Census's own documentation
(census.gov/programs-surveys/geography/technical-documentation/
records-layout/gaz-record-layouts.html, 2026-08-19) — NOT from the data
file itself, since this sandbox's own fetch tool respects robots.txt and
the census.gov data-file host disallows automated fetching for it. That
robots.txt restriction is specific to this conversation's fetch tool; it
is NOT evidence the file blocks ordinary programmatic download the way
Public Surplus/MNBid actively do — Census gazetteer files are public bulk
data explicitly published for this kind of consumption. Still, this
script's actual live download has not been run from this sandbox — same
standing rule as everything else here: run it and report back.

Confirmed real per-state file URL pattern (from the Census's own current
download page):
  https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_gaz_counties_{FIPS}.txt
Confirmed column layout (pipe-delimited):
  USPS | GEOID | GEOIDFQ | ANSICODE | NAME | ALAND | AWATER |
  ALAND_SQMI | AWATER_SQMI | INTPTLAT | INTPTLONG
NAME includes the "County" suffix (e.g. "Koochiching County") — stripped
here for matching against our scraped `county` field, which does not
include the suffix (see parse_county_from_description() in
scrape_public_surplus.py).
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent.parent / "data"
TARGET_PATH = DATA_DIR / "public_surplus_taxforfeited.json"

GAZETTEER_URL_TEMPLATE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_gaz_counties_{fips}.txt"

# Only states currently confirmed to have real listings need a fetch —
# no point downloading all 50 states' files. Extend as new states with
# real inventory come online (see schema-tax-forfeited-land.md).
STATE_FIPS = {
    "MN": "27",
    "GA": "13",
    "SC": "45",
    "TN": "47",
    "AL": "01",
}

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30  # gazetteer files are small but this is a bulk download, not a quick API call
MAX_RETRIES_PER_REQUEST = 2
REQUEST_DELAY_SECONDS = 1


def fetch_county_centroids(state: str) -> dict:
    """Returns {county_name_lowercase: (lat, lon)} for one state. Empty
    dict on failure — callers should treat that as 'no centroids
    available for this state' rather than crash the whole run."""
    fips = STATE_FIPS.get(state)
    if not fips:
        print(f"  no known FIPS code for state {state!r} — skipping", file=sys.stderr, flush=True)
        return {}

    url = GAZETTEER_URL_TEMPLATE.format(fips=fips)
    for attempt in range(1, MAX_RETRIES_PER_REQUEST + 2):
        try:
            print(f"  fetching county centroids for {state} (attempt {attempt})...", file=sys.stderr, flush=True)
            resp = requests.get(
                url,
                headers={"User-Agent": "twin-cities-storage-scout/1.0 (research project)"},
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            return parse_gazetteer_file(resp.text)
        except requests.exceptions.RequestException as e:
            print(f"  attempt {attempt} failed for {state}: {e}", file=sys.stderr, flush=True)
            if attempt <= MAX_RETRIES_PER_REQUEST:
                time.sleep(2)
            else:
                return {}
    return {}


def parse_gazetteer_file(text: str) -> dict:
    """Parses the pipe-delimited Counties Gazetteer format. Tries a tab
    delimiter as a fallback and logs which was used — the Census's
    general notes page states files are pipe-delimited, but older
    vintages of this specific file family have historically used tabs,
    so this guards against a documentation/reality mismatch rather than
    trusting the stated format blindly."""
    lines = [l for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return {}

    delimiter = "|"
    sample_cols = lines[0].split(delimiter)
    if len(sample_cols) < 11:
        delimiter = "\t"
        sample_cols = lines[0].split(delimiter)
        print(f"  pipe delimiter gave {len(lines[0].split('|'))} columns (expected 11) — using tab instead", file=sys.stderr, flush=True)

    centroids = {}
    header_skipped = False
    for line in lines:
        cols = line.split(delimiter)
        if len(cols) < 11:
            continue
        # Skip a header row if present (first column would be "USPS" literally)
        if not header_skipped and cols[0].strip().upper() == "USPS":
            header_skipped = True
            continue
        name = cols[4].strip()
        county_name = re.sub(r"\s+County$", "", name, flags=re.IGNORECASE).strip()
        try:
            lat = float(cols[9].strip())
            lon = float(cols[10].strip())
        except ValueError:
            continue
        centroids[county_name.lower()] = (lat, lon)

    return centroids


def main():
    if not TARGET_PATH.exists():
        print(f"{TARGET_PATH} not found — run scrape_public_surplus.py first", file=sys.stderr, flush=True)
        return

    listings = json.loads(TARGET_PATH.read_text())
    print(f"Loaded {len(listings)} tax-forfeited listings", file=sys.stderr, flush=True)

    # Only listings that STILL have no coordinates after the address-based
    # pass, but DO have a county name — the address-based script
    # (geocode_public_surplus.py) should run first; this is a fallback,
    # not a replacement.
    candidates = [
        l for l in listings
        if l.get("county")
        and (not l.get("coordinates") or l["coordinates"].get("lat") is None)
    ]
    print(f"{len(candidates)} listings have a county but no coordinates — attempting county-centroid fallback", file=sys.stderr, flush=True)

    states_needed = sorted(set(l.get("state", "MN") for l in candidates))
    centroid_cache = {}
    for state in states_needed:
        centroid_cache[state] = fetch_county_centroids(state)
        time.sleep(REQUEST_DELAY_SECONDS)

    placed = 0
    skipped_no_match = 0
    for listing in candidates:
        state = listing.get("state", "MN")
        county = listing["county"].lower()
        centroids = centroid_cache.get(state, {})
        match = centroids.get(county)
        if match is None:
            print(f"    no gazetteer match for county {listing['county']!r} in {state}", file=sys.stderr, flush=True)
            skipped_no_match += 1
            continue
        lat, lon = match
        listing["coordinates"] = {"lat": lat, "lng": lon, "precision": "county_centroid"}
        placed += 1

    print(f"Placed {placed}/{len(candidates)} at county centroids ({skipped_no_match} had no gazetteer match)", file=sys.stderr, flush=True)

    TARGET_PATH.write_text(json.dumps(listings, indent=2))
    print(f"Wrote {len(listings)} records back to {TARGET_PATH}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
