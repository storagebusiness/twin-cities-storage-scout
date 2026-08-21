"""
Looks up a parcel's assessed value (EMV — Estimated Market Value) from
MnGeo's "Parcels, Compiled from Opt-In Open Data Counties, Minnesota"
dataset — the non-metro equivalent of Piece 2's Met Council lookup
(lookup_parcel.py), for Piece 3 tax-forfeited listings.

WHY THIS EXISTS: Met Council's parcel data was confirmed structurally
unusable for Piece 3 — every county that appears on Public Surplus/MNBid
is, by definition, outside Met Council's 7-county metro coverage (see
schema-tax-forfeited-land.md's "assessed_value.source" notes). This is
the researched replacement: a real, live, statewide-compiled parcel
FeatureServer maintained by MnGeo, confirmed via direct inspection of its
REST service field list (2026-08-20) to include emv_total — the exact
same field name Piece 2's Met Council lookup already uses, strong
evidence both descend from the same GAC Parcel Data Standard lineage
(confirmed: the standard is directly derived from the original 2002
MetroGIS Parcel Standard used by the 7 metro counties).

COVERAGE IS PARTIAL, CONFIRMED (2026-08-20) — 58 of Minnesota's 87
counties are compiled into this dataset ("opt-in", though MnGeo does the
field-mapping work itself, not gated purely on county technical
capability). Checked against every county seen in real Piece 3 data so
far: Koochiching (yes), Morrison (yes), Carlton (yes, MNBid), Lyon (yes,
MNBid) are covered; Sibley, Goodhue, Cottonwood, Kittson are NOT. Per
explicit user direction (2026-08-20): this project is not pursuing
further per-county research beyond this dataset — counties outside
COVERED_COUNTIES stay at assessed_value.source == "mn_pending"
indefinitely, not a bug to fix later.

⚠️ VERIFICATION STATUS: the field SCHEMA is confirmed directly (real
REST service field list inspected, not guessed) — that part is solid.
NOT yet confirmed: whether our scraped parcel_id values (e.g.
Koochiching's "55-070-01000" PID format) actually match this dataset's
county_pin field's real stored format exactly. This sandbox's network is
blocked from the live endpoint, so the matching logic below is built
defensively (see lookup_parcel_by_pin's sample-value fallback) rather
than assumed correct — treat the first live run's DEBUG output as the
real verification step, same as every other scraper in this project.

Source: MnGeo (Minnesota Geospatial Information Office), "Parcels,
Compiled from Opt-In Open Data Counties, Minnesota", updated quarterly.
https://gis.data.mn.gov/maps/69148d3959194a05a23964cc60f6517b
REST service (confirmed live, field list inspected directly):
https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_mngeo/plan_parcels_open/FeatureServer/1
"""
import sys

import requests

PARCELS_QUERY_URL = "https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_mngeo/plan_parcels_open/FeatureServer/1/query"

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 20
MAX_RETRIES_PER_REQUEST = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
}

# Confirmed real coverage list (2026-08-20) — the 58 counties actually
# compiled into this dataset, transcribed directly from the dataset's own
# published coverage list. Counties NOT in this set should not be queried
# at all — per explicit user direction, no further per-county research is
# planned for the remainder, so skipping them outright (rather than
# querying and getting an empty result every time) is both faster and
# more honest about what this script can and can't do.
COVERED_COUNTIES = {
    "aitkin", "anoka", "becker", "benton", "big stone", "carlton", "carver",
    "cass", "chippewa", "chisago", "clay", "clearwater", "cook", "crow wing",
    "dakota", "douglas", "fillmore", "grant", "hennepin", "houston", "isanti",
    "itasca", "jackson", "koochiching", "lac qui parle", "lake",
    "lake of the woods", "lyon", "marshall", "mcleod", "mille lacs",
    "morrison", "mower", "murray", "norman", "olmsted", "otter tail",
    "pennington", "pipestone", "polk", "pope", "ramsey", "red lake",
    "renville", "rice", "scott", "sherburne", "st. louis", "stearns",
    "steele", "stevens", "traverse", "wabasha", "waseca", "washington",
    "wilkin", "winona", "wright", "yellow medicine",
}


def is_covered(county: str) -> bool:
    """Normalizes and checks a county name against COVERED_COUNTIES.
    Our scraped `county` field is typically just the name ("Koochiching")
    without the word "County" — this normalizes both directions so a
    stray "County" suffix doesn't cause a false non-match."""
    if not county:
        return False
    normalized = county.strip().lower().replace(" county", "")
    return normalized in COVERED_COUNTIES


def lookup_parcel_by_pin(county: str, parcel_id: str) -> dict | None:
    """Looks up a parcel by county name + parcel/PIN string. Returns
    {"emv_land": ..., "emv_bldg": ..., "emv_total": ..., "tax_year": ...,
    "mkt_year": ..., "co_name": ...} or None if no exact match.

    On a failed match, fetches a small sample of real county_pin values
    from the SAME county and logs them — this is the concrete diagnostic
    for the exact-format uncertainty flagged in the module docstring. If
    our parcel_id format doesn't match this dataset's stored format
    (e.g. missing/extra hyphens, different padding), the sample values
    will show that immediately on the first live run instead of just
    silently returning "no match" with no way to tell why."""
    if not is_covered(county):
        return None  # don't bother querying counties we know aren't in this dataset

    for attempt in range(1, MAX_RETRIES_PER_REQUEST + 2):
        try:
            resp = requests.get(
                PARCELS_QUERY_URL,
                params={
                    "where": f"UPPER(co_name) LIKE UPPER('%{county}%') AND county_pin = '{parcel_id}'",
                    "outFields": "emv_land,emv_bldg,emv_total,tax_year,mkt_year,co_name,county_pin",
                    "f": "json",
                    "resultRecordCount": 1,
                },
                headers=HEADERS,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.RequestException as e:
            print(f"    MnGeo parcel lookup attempt {attempt} failed for "
                  f"{county}/{parcel_id!r}: {e}", file=sys.stderr, flush=True)
            if attempt > MAX_RETRIES_PER_REQUEST:
                return None
        except ValueError as e:
            print(f"    MnGeo parcel lookup returned non-JSON for "
                  f"{county}/{parcel_id!r}: {e}", file=sys.stderr, flush=True)
            return None
    else:
        return None

    if "error" in data:
        print(f"    MnGeo parcel query error for {county}/{parcel_id!r}: {data['error']!r}",
              file=sys.stderr, flush=True)
        return None

    features = data.get("features", [])
    if features:
        attrs = features[0]["attributes"]
        return attrs

    # No match — pull a small sample of real county_pin values from the
    # same county so a format mismatch is immediately visible in the log,
    # rather than a bare "no match" that gives no clue why.
    print(f"    no MnGeo match for {county}/{parcel_id!r} — fetching sample "
          f"county_pin values from {county} for comparison...", file=sys.stderr, flush=True)
    try:
        sample_resp = requests.get(
            PARCELS_QUERY_URL,
            params={
                "where": f"UPPER(co_name) LIKE UPPER('%{county}%')",
                "outFields": "county_pin,co_name",
                "f": "json",
                "resultRecordCount": 3,
            },
            headers=HEADERS,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        sample_resp.raise_for_status()
        sample_data = sample_resp.json()
        sample_pins = [f["attributes"].get("county_pin") for f in sample_data.get("features", [])]
        print(f"    sample county_pin values actually in {county}: {sample_pins} "
              f"— compare against our parsed parcel_id {parcel_id!r}", file=sys.stderr, flush=True)
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"    (sample fetch also failed: {e})", file=sys.stderr, flush=True)

    return None
