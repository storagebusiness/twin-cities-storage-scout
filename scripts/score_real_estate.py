"""
Runs the full Real Estate pipeline: reads the mortgage-foreclosure
records from EACH county source (see SOURCE_FILES below), looks up each
property's county-assessed value via lookup_parcel.py, computes the
undervalued comparison, and writes one combined output file the app
can consume directly (has coordinates, no separate geocoding needed).

Adding a new county source later just means: write a new scrape_X.py
that outputs to data/X_realestate.json in the same record shape as the
existing scrapers (house_number/street_name/street_suffix/city/
mortgage_amount/property_address_raw at minimum), then add that filename
to SOURCE_FILES below and to the workflow's "Scrape" steps — same pattern
as merge_sources.py uses for the storage-auction side.
"""

import json
import sys
from pathlib import Path

from lookup_parcel import lookup_parcel, score_undervalued

DATA_DIR = Path(__file__).parent.parent / "data"
SOURCE_FILES = [
    "finance_commerce_realestate.json",       # Hennepin County, via Finance & Commerce
    "stpaul_legal_ledger_realestate.json",    # Ramsey County, via minnlawyer.com — see HANDOFF.md
]
OUT_PATH = DATA_DIR / "real_estate_scored.json"


def load_mortgage_records() -> list[dict]:
    combined = []
    for filename in SOURCE_FILES:
        path = DATA_DIR / filename
        if not path.exists():
            print(f"  {filename}: not found, skipping (run its scraper first)", file=sys.stderr)
            continue
        records = json.loads(path.read_text())
        print(f"  {filename}: {len(records)} records", file=sys.stderr)
        combined.extend(records)
    return combined


def main():
    mortgage_records = load_mortgage_records()
    if not mortgage_records:
        print("No mortgage foreclosure records found from any source", file=sys.stderr)
        OUT_PATH.write_text("[]")
        return

    print(f"Loaded {len(mortgage_records)} mortgage foreclosure records total", file=sys.stderr)

    scored_records = []
    matched = 0
    unmatched = 0
    for record in mortgage_records:
        house_number = record.get("house_number")
        street_name = record.get("street_name")
        city = record.get("city")
        suffix = record.get("street_suffix")  # was extracted but never passed through — bug fix
        if not house_number or not street_name:
            unmatched += 1
            continue

        parcel = lookup_parcel(house_number, street_name, city, suffix)
        if parcel is None:
            unmatched += 1
            print(f"  no parcel match: {record.get('property_address_raw')}", file=sys.stderr)
            continue

        matched += 1
        score = score_undervalued(record["mortgage_amount"], parcel)
        scored_records.append({
            **record,
            "parcel": parcel,
            "scoring": score,
        })

    print(f"Matched {matched}/{len(mortgage_records)} properties to parcel records "
          f"({unmatched} unmatched)", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(scored_records, indent=2))
    print(f"Wrote {len(scored_records)} scored records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
