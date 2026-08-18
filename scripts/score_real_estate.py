"""
Runs the full Real Estate pipeline: reads the mortgage-foreclosure
records scrape_finance_commerce_realestate.py already extracted, looks
up each property's county-assessed value via lookup_parcel.py, computes
the undervalued comparison, and writes one combined output file the app
can consume directly (has coordinates, no separate geocoding needed).
"""

import json
import sys
from pathlib import Path

from lookup_parcel import lookup_parcel, score_undervalued

DATA_DIR = Path(__file__).parent.parent / "data"
IN_PATH = DATA_DIR / "finance_commerce_realestate.json"
OUT_PATH = DATA_DIR / "real_estate_scored.json"


def main():
    if not IN_PATH.exists():
        print(f"{IN_PATH} not found — run scrape_finance_commerce_realestate.py first", file=sys.stderr)
        OUT_PATH.write_text("[]")
        return

    mortgage_records = json.loads(IN_PATH.read_text())
    print(f"Loaded {len(mortgage_records)} mortgage foreclosure records", file=sys.stderr)

    scored_records = []
    matched = 0
    unmatched = 0

    for record in mortgage_records:
        house_number = record.get("house_number")
        street_name = record.get("street_name")
        city = record.get("city")

        if not house_number or not street_name:
            unmatched += 1
            continue

        parcel = lookup_parcel(house_number, street_name, city)
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
