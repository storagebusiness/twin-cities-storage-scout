"""
Applies MnGeo's statewide compiled parcel dataset (see
lookup_mn_statewide_parcel.py) to MN tax-forfeited listings that still
need an assessed value — i.e. anything with assessed_value.source ==
"mn_pending" (set by scrape_public_surplus.py for listings whose
price_basis is "minimum_bid_only" with no EMV of their own, in a county
outside Met Council's metro coverage).

Reads and rewrites data/public_surplus_taxforfeited.json in place, after
scrape_public_surplus.py has already run — same pattern as
geocode_public_surplus.py / geocode_county_centroid.py.

Only attempts listings whose county is in
lookup_mn_statewide_parcel.COVERED_COUNTIES — per explicit user direction
(2026-08-20), this project is not pursuing further per-county research
beyond this one dataset, so listings in uncovered counties (Sibley,
Goodhue, Cottonwood, Kittson, as of this build) are left at
assessed_value.source == "mn_pending" indefinitely, not retried.

⚠️ VERIFICATION STATUS: see lookup_mn_statewide_parcel.py's module
docstring — the field schema is confirmed real, the parcel_id matching
format is not yet confirmed against live data. Treat the first real
run's DEBUG output (especially any "sample county_pin values" diagnostic
lines) as the actual verification step.
"""
import json
import sys
import time
from pathlib import Path

from lookup_mn_statewide_parcel import is_covered, lookup_parcel_by_pin

DATA_DIR = Path(__file__).parent.parent / "data"
TARGET_PATH = DATA_DIR / "public_surplus_taxforfeited.json"

REQUEST_DELAY_SECONDS = 0.5


def main():
    if not TARGET_PATH.exists():
        print(f"{TARGET_PATH} not found — run scrape_public_surplus.py first", file=sys.stderr, flush=True)
        return

    listings = json.loads(TARGET_PATH.read_text())
    print(f"Loaded {len(listings)} tax-forfeited listings", file=sys.stderr, flush=True)

    candidates = [
        l for l in listings
        if l.get("state") == "MN"
        and (l.get("assessed_value") or {}).get("source") == "mn_pending"
        and l.get("parcel_id")
        and is_covered(l.get("county"))
    ]
    print(f"{len(candidates)} MN listings need an assessed value and are in a covered county",
          file=sys.stderr, flush=True)

    skipped_uncovered = sum(
        1 for l in listings
        if l.get("state") == "MN"
        and (l.get("assessed_value") or {}).get("source") == "mn_pending"
        and not is_covered(l.get("county"))
    )
    if skipped_uncovered:
        print(f"{skipped_uncovered} MN listings need an assessed value but are in an "
              f"uncovered county — left at mn_pending, not retried (expected; see module docstring)",
              file=sys.stderr, flush=True)

    matched = 0
    no_match = 0
    for listing in candidates:
        county = listing["county"]
        parcel_id = listing["parcel_id"]
        print(f"  looking up: {county}/{parcel_id!r}...", file=sys.stderr, flush=True)
        result = lookup_parcel_by_pin(county, parcel_id)
        if result is not None:
            emv_total = result.get("emv_total")
            if emv_total:
                listing["assessed_value"]["emv_total"] = emv_total
                listing["assessed_value"]["source"] = "mn_statewide_parcel_mngeo"
                listing["assessed_value"]["lookup_status"] = "matched"
                # Also populate tier1_emv_price on the price object if it
                # was empty — this is what actually drives the app's
                # discount-from-EMV tier coloring (see taxForfeitedTierFor
                # in the app), not just the assessed_value block.
                if listing.get("price", {}).get("tier1_emv_price") is None:
                    listing["price"]["tier1_emv_price"] = emv_total
                    if listing["price"].get("price_basis") == "minimum_bid_only":
                        listing["price"]["price_basis"] = "self_contained_dual_tier"
                matched += 1
            else:
                listing["assessed_value"]["lookup_status"] = "matched_but_no_emv_value"
                no_match += 1
        else:
            listing["assessed_value"]["lookup_status"] = "no_match"
            no_match += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Matched {matched}/{len(candidates)} to a real assessed value "
          f"({no_match} no match)", file=sys.stderr, flush=True)

    TARGET_PATH.write_text(json.dumps(listings, indent=2))
    print(f"Wrote {len(listings)} records back to {TARGET_PATH}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
