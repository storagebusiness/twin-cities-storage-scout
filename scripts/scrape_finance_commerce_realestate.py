"""
Fetches Finance & Commerce's Real Estate RSS feed (foreclosure/lien sale
notices) and extracts (property_address, mortgagor_name, mortgage_amount,
county, auction_date) records for the "undervalued property" comparison —
mortgage debt vs. county-assessed value (via the Met Council parcel API,
see lookup_parcel.py).

KEY FINDING (confirmed against real data, unlike earlier Personal Property
work which needed several rounds of guessing): the <title> field of each
RSS item IS the property street address directly — e.g. "6364 Juneau Ln N
Maple Grove" — no need to parse an address out of dense legal text. The
<description> uses yet another different format from other F&C feeds:
plain "Auction Date: X" / "Description: Y" labels, not the
Section:/Category:/Summary:/Posted: format seen elsewhere on this same
platform. Every F&C feed category has turned out to use a genuinely
different internal format — don't assume they're consistent.

NOTE ON COVERAGE: Real Estate notices come in several genuinely different
types, not just standard mortgage foreclosures:
  - Standard mortgage foreclosure (MORTGAGOR(S)/MORTGAGEE/PRINCIPAL AMOUNT)
    — this is the majority pattern and the only one this script extracts
    a mortgagor name + dollar amount from.
  - HOA/condo "assessment lien" foreclosures — no MORTGAGOR field, names
    the property owner as "fee owner(s)" instead, no principal amount in
    the same format. NOT extracted by this version.
  - Sale postponements, civil judgment sales — different structure
    entirely. NOT extracted by this version.
This script only pulls the standard mortgage-foreclosure pattern; other
types are skipped (counted, not silently dropped) so their volume is
visible in the log rather than invisible.
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

RSS_BASE = "https://finance-commerce.com/public-notice/export-rss/"
OUT_PATH = Path(__file__).parent.parent / "data" / "finance_commerce_realestate.json"
MAX_PAGES = 10

STREET_SUFFIXES = r"(?:Ave|St|Dr|Blvd|Ln|Rd|Way|Ct|Cir|Pl|Trail|Trl|Pkwy|Terrace|Ter)"
DIRECTIONS = r"(?:N|S|E|W|NE|NW|SE|SW)"
DIRECTION_WORDS = {"N": "N", "S": "S", "E": "E", "W": "W",
                    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}

MORTGAGOR_RE = re.compile(
    r"MORTGAGOR\(S\):\s*(.+?)\s*MORTGAGEE:", re.IGNORECASE | re.DOTALL,
)
PRINCIPAL_AMOUNT_RE = re.compile(
    r"(?:ORIGINAL|MAXIMUM)\s+PRINCIPAL\s+AMOUNT\s+OF\s+MORTGAGE:\s*\$([\d,]+\.\d{2})",
    re.IGNORECASE,
)
COUNTY_RE = re.compile(r"([A-Z][a-zA-Z]+)\s+County", re.IGNORECASE)
AUCTION_DATE_RE = re.compile(r"Auction Date:\s*(\d{1,2}/\d{1,2}/\d{4})")


def parse_address_from_title(title: str) -> dict:
    """Split an F&C Real Estate title into house number / street name /
    suffix / direction / city / zip. Verified against 29 real titles
    covering single and multi-word street names, embedded zip codes, and
    the "St" (Street vs. Saint) ambiguity — see module docstring.

    Also strips a LEADING direction word (abbreviated OR spelled out —
    "W 101st St" / "South 1st St") before the street name. This was a
    real bug found against live data: 8 of 46 real addresses failed to
    match a parcel on the first live run, and inspection showed several
    of the failures had a leading direction word polluting the parsed
    street name (e.g. "W 101st" instead of "101st"), which the parcel
    database doesn't store that way — it keeps directions in a separate
    field. Confirmed fixed against the actual failing titles."""
    title = title.strip()
    m = re.search(r",?\s*MN,?\s*(\d{5})\s*(.*)$", title, re.IGNORECASE)
    zipcode = None
    if m:
        zipcode = m.group(1)
        title = title[:m.start()].strip()

    num_m = re.match(r"^(\d+)\s+(.*)$", title)
    if not num_m:
        return {"raw": title, "house_number": None, "street_name": None,
                "suffix": None, "direction": "", "city": None, "zip": zipcode}
    house_number, rest = num_m.groups()

    lead_m = re.match(rf"^({'|'.join(DIRECTION_WORDS.keys())})\s+(.*)$", rest, re.IGNORECASE)
    leading_direction = ""
    if lead_m:
        leading_direction = DIRECTION_WORDS[lead_m.group(1).upper()]
        rest = lead_m.group(2)

    matches = list(re.finditer(rf"\b({STREET_SUFFIXES})\b\s*({DIRECTIONS})?\b", rest, re.IGNORECASE))
    if not matches:
        return {"raw": title, "house_number": house_number, "street_name": rest.strip(),
                "suffix": None, "direction": leading_direction, "city": None, "zip": zipcode}
    suf_m = matches[-1]  # last match — see docstring for why

    street_name = rest[:suf_m.start()].strip()
    suffix = suf_m.group(1)
    trailing_direction = suf_m.group(2) or ""
    city_part = rest[suf_m.end():].strip().lstrip(",").strip()

    return {
        "raw": title, "house_number": house_number, "street_name": street_name,
        "suffix": suffix, "direction": leading_direction or trailing_direction,
        "city": city_part if city_part else None, "zip": zipcode,
    }


def parse_item(title: str, description_text: str, source_url: str) -> dict | None:
    """Returns a structured record for a standard mortgage-foreclosure
    notice, or None if this item isn't that pattern (HOA lien,
    postponement, civil judgment sale, etc. — see module docstring)."""
    mortgagor_m = MORTGAGOR_RE.search(description_text)
    amount_m = PRINCIPAL_AMOUNT_RE.search(description_text)
    if not mortgagor_m or not amount_m:
        return None  # not a standard mortgage foreclosure — skip, don't guess

    address = parse_address_from_title(title)
    county_m = COUNTY_RE.search(description_text)
    auction_m = AUCTION_DATE_RE.search(description_text)

    return {
        "mortgagor_name": mortgagor_m.group(1).strip().rstrip(","),
        "mortgage_amount": float(amount_m.group(1).replace(",", "")),
        "property_address_raw": address["raw"],
        "house_number": address["house_number"],
        "street_name": address["street_name"],
        "street_suffix": address["suffix"],
        "street_direction": address["direction"],
        "city": address["city"],
        "zip": address["zip"],
        "county": county_m.group(1) if county_m else None,
        "auction_date": auction_m.group(1) if auction_m else None,
        "source_url": source_url,
    }


def parse_rss(xml_text: str) -> list[dict]:
    records = []
    skipped_other_type = 0

    if len(xml_text.strip()) < 100 or not xml_text.strip().startswith("<?xml"):
        print(f"DEBUG: response doesn't look like real RSS (len={len(xml_text.strip())}), "
              f"treating as end of results", file=sys.stderr)
        return records

    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    print(f"DEBUG: RSS contains {len(items)} items", file=sys.stderr)

    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description_el = item.find("description")
        description_text = "".join(description_el.itertext()) if description_el is not None else ""

        record = parse_item(title, description_text, link)
        if record is None:
            skipped_other_type += 1
            continue
        records.append(record)

    print(f"  {len(records)} standard mortgage foreclosures extracted, "
          f"{skipped_other_type} other-type notices skipped (not yet supported)", file=sys.stderr)
    return records


def fetch_page(page_num: int) -> str:
    resp = requests.get(
        RSS_BASE,
        params={"feeds": "real_estate", "pageindex": page_num},
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def main():
    all_records = []
    for page in range(1, MAX_PAGES + 1):
        xml_text = fetch_page(page)
        print(f"DEBUG: page {page} response length = {len(xml_text)} chars", file=sys.stderr)
        records = parse_rss(xml_text)
        if not records and page > 1:
            break
        all_records.extend(records)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
