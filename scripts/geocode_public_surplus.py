"""
Geocodes tax-forfeited land listings that have a real street address
(e.g. "858 10TH ST, WESTBROOK") using the US Census Bureau's free,
no-API-key-required Geocoder service.

Reads and rewrites data/public_surplus_taxforfeited.json in place, after
scrape_public_surplus.py has already run. Only attempts records where
`address` is not null AND `coordinates.lat`/`lng` are still null — most
current MN listings (Koochiching, Morrison, Sibley) only have a legal
description with no street address at all and are correctly left
ungeocoded by this script; that's a real data gap, not something this
step can fix (see the user's plan: address-based geocoding first,
county-centroid or another approach later for the rest).

API details confirmed via web search against current documentation
(2026-08-19) — NOT run against the live endpoint from this sandbox,
since network access is blocked here. The request/response shape below
matches multiple independent real usage examples (a Scholarly API
Cookbook Python example, the Census's own PDF documentation, and a
GitHub NICAR20 workshop repo), but has not been tested live by this
script itself. Run it and report back any errors or unexpected empty
results before trusting the output — same standing rule as every other
scraper in this project.

Endpoint: https://geocoding.geo.census.gov/geocoder/locations/onelineaddress
Params: address (single combined string), benchmark, format=json
Response: response["result"]["addressMatches"] is a list (possibly
empty if no match); each match has ["coordinates"]["x"] (longitude) and
["coordinates"]["y"] (latitude).

Using the NAMED benchmark "Public_AR_Current" rather than a numeric ID
(several real examples used 4 or 9) — numeric benchmark IDs change as
the Census Bureau updates their "current" dataset, so the named
benchmark is the more future-proof choice, not an arbitrary pick.

No documented rate limit was found for this free service in the sources
checked — a conservative default delay is used here rather than assume
none is needed.
"""
import json
import sys
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent.parent / "data"
TARGET_PATH = DATA_DIR / "public_surplus_taxforfeited.json"

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
BENCHMARK = "Public_AR_Current"

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 20
MAX_RETRIES_PER_REQUEST = 2
REQUEST_DELAY_SECONDS = 1  # conservative default — no documented rate limit found


def geocode_address(address: str, state: str) -> tuple[float, float] | None:
    """Returns (lat, lng) or None if no match / request failed after
    retries. Appends state to the address string for disambiguation —
    confirmed real usage examples include state in the one-line address
    (e.g. '425 Stadium Dr, Tuscaloosa, AL 35401'), and our address field
    (e.g. '858 10TH ST, WESTBROOK') doesn't already include it."""
    full_address = f"{address}, {state}"
    params = {"address": full_address, "benchmark": BENCHMARK, "format": "json"}

    for attempt in range(1, MAX_RETRIES_PER_REQUEST + 2):
        try:
            resp = requests.get(
                GEOCODER_URL,
                params=params,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            data = resp.json()
            matches = data.get("result", {}).get("addressMatches", [])
            if not matches:
                print(f"    no geocode match: {full_address!r}", file=sys.stderr, flush=True)
                return None
            if len(matches) > 1:
                print(f"    {len(matches)} matches for {full_address!r} — using first", file=sys.stderr, flush=True)
            coords = matches[0]["coordinates"]
            return (coords["y"], coords["x"])  # (lat, lng)
        except requests.exceptions.RequestException as e:
            print(f"    geocode attempt {attempt} failed for {full_address!r}: {e}", file=sys.stderr, flush=True)
            if attempt <= MAX_RETRIES_PER_REQUEST:
                time.sleep(2)
            else:
                return None
        except (KeyError, ValueError, TypeError) as e:
            print(f"    unexpected geocoder response shape for {full_address!r}: {e}", file=sys.stderr, flush=True)
            return None
    return None


def main():
    if not TARGET_PATH.exists():
        print(f"{TARGET_PATH} not found — run scrape_public_surplus.py first", file=sys.stderr, flush=True)
        return

    listings = json.loads(TARGET_PATH.read_text())
    print(f"Loaded {len(listings)} tax-forfeited listings", file=sys.stderr, flush=True)

    candidates = [
        l for l in listings
        if l.get("address")
        and (not l.get("coordinates") or l["coordinates"].get("lat") is None)
    ]
    print(f"{len(candidates)} listings have a street address and no coordinates yet — attempting to geocode", file=sys.stderr, flush=True)
    skipped_no_address = len(listings) - len(candidates)
    if skipped_no_address:
        print(f"{skipped_no_address} listings have no street address on record — left ungeocoded (expected; see script docstring)", file=sys.stderr, flush=True)

    geocoded = 0
    failed = 0
    for listing in candidates:
        address = listing["address"]
        state = listing.get("state", "MN")
        print(f"  geocoding: {address!r} ({state})...", file=sys.stderr, flush=True)
        result = geocode_address(address, state)
        if result is not None:
            lat, lng = result
            listing["coordinates"] = {"lat": lat, "lng": lng}
            geocoded += 1
        else:
            failed += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Geocoded {geocoded}/{len(candidates)} attempted ({failed} failed/no match)", file=sys.stderr, flush=True)

    TARGET_PATH.write_text(json.dumps(listings, indent=2))
    print(f"Wrote {len(listings)} records back to {TARGET_PATH}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
