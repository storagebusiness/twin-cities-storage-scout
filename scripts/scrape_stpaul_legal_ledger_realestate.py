"""
Fetches St. Paul Legal Ledger's (minnlawyer.com) Real Estate RSS feed —
the Ramsey County counterpart to scrape_finance_commerce_realestate.py.

WHY THIS EXISTS: Finance & Commerce is the officially-designated legal
newspaper for the City of Minneapolis and Hennepin County ONLY — not the
Twin Cities metro. Every other metro county has its own separately-
designated paper. St. Paul Legal Ledger is Ramsey County's. Same owner
(BridgeTower Media) and same backend platform ("The Dolan Company Public
Notice Web Service API" — visible in both domains' RSS <generator> tag),
different domain and different county coverage. See HANDOFF.md, "Single-
county coverage limitation," for the full story of how this was found.

FORMAT: confirmed identical to F&C's dedicated real_estate feed — title IS
the property address (e.g. "1374 Searle St Saint Paul"), description uses
"Auction Date: X" / "Description: Y" labels. All parsing logic below is
copied UNCHANGED from scrape_finance_commerce_realestate.py and verified
against real Ramsey-area samples (Saint Paul, Roseville, Arden Hills,
White Bear Lake, Shoreview) before being trusted here — see project chat
history, not fabricated.

NOTE ON TRUNCATION: minnlawyer.com's RSS <description> field is truncated
to roughly 400-600 characters, cutting many notices off mid-sentence
before reaching the county name or full mortgagor/amount text. This is
usually fine for real estate specifically because MORTGAGOR(S)/MORTGAGEE/
PRINCIPAL AMOUNT tend to appear early in a standard notice, before the
truncation point — but COUNTY_RE in particular often lands on "Ramsey
County Recorder" or "Ramsey County Registrar" text that can come AFTER the
cutoff, so `county` may be None more often here than on F&C's feed even
when the record is otherwise well-formed. That's fine: the county is
Ramsey by construction (this script only ever queries Ramsey's feed via
this domain), so downstream code should trust the SOURCE (this script),
not rely on the scraped county field, when tagging county for these
records. Unlike Personal Property, full detail-page fetching was NOT
found necessary for real estate — the truncated RSS payload has enough of
each notice's beginning to extract mortgagor/amount reliably in testing.
If that changes (a batch of records with mortgagor_name=None), the fix is
the same pattern as scrape_stpaul_legal_ledger_personalproperty.py: fetch
source_url per item instead of relying on RSS alone.
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

RSS_BASE = "https://minnlawyer.com/public-notice/export-rss/"
OUT_PATH = Path(__file__).parent.parent / "data" / "stpaul_legal_ledger_realestate.json"
MAX_PAGES = 10
SOURCE_COUNTY = "Ramsey"  # trust the source, not the scraped county field — see module docstring

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
    """Unchanged from scrape_finance_commerce_realestate.py — verified
    against real Ramsey titles including the S-leading/W-trailing double-
    direction case ("3030 S Owasso Blvd W Roseville", keeps leading "S",
    drops trailing "W" — pre-existing quirk, not new here) and the
    embedded-zip case ("608 Beaumont St, Saint Paul, MN, 55130 Saint
    Paul")."""
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
    suf_m = matches[-1]

    street_name = rest[:suf_m.start()].strip()
    suffix = suf_m.group(1)
    trailing_direction = suf_m.group(2) or ""
    city_part = rest[suf_m.end():].strip().lstrip(",").strip()

    return {
        "raw": title, "house_number": house_number, "street_name": street_name,
        "suffix": suffix, "direction": leading_direction or trailing_direction,
        "city": city_part if city_part else None, "zip": zipcode,
    }


def classify_skip_reason(description_text: str) -> str:
    """Distinguish "genuinely not a standard mortgage foreclosure notice"
    from "probably IS one, but minnlawyer.com's RSS truncation (~400-600
    chars, see module docstring) cut it off before we could tell." This
    matters because the first is expected/fine (HOA liens, postponements,
    sheriff's sales don't use the MORTGAGOR(S)/MORTGAGEE/PRINCIPAL AMOUNT
    format at all) and the second is real, recoverable data loss — same
    fix as the personal-property scraper (fetch the detail page) would
    apply here too, if this turns out to be the dominant skip reason."""
    upper = description_text.upper()
    has_mortgagor_label = "MORTGAGOR" in upper
    has_mortgagee_label = "MORTGAGEE" in upper
    has_principal_label = "PRINCIPAL AMOUNT OF MORTGAGE" in upper
    length = len(description_text)
    # RSS descriptions get cut off mid-sentence when truncated — a
    # description near the observed ~400-600 char truncation length that
    # doesn't end on a sentence boundary is a strong truncation signal.
    looks_truncated = length >= 380 and not description_text.rstrip().endswith((".", ")", '"'))

    if not has_mortgagor_label:
        return "not_a_mortgage_notice"  # HOA lien / postponement / sheriff's sale / etc — expected, not a bug
    if not has_mortgagee_label:
        return "truncated_before_mortgagee" if looks_truncated else "malformed_mortgagor_block"
    if not has_principal_label:
        return "truncated_before_principal_amount" if looks_truncated else "missing_principal_amount_label"
    return "has_all_labels_but_regex_still_failed"  # worth investigating directly if this ever shows up


def parse_item(title: str, description_text: str, source_url: str) -> dict | None:
    mortgagor_m = MORTGAGOR_RE.search(description_text)
    amount_m = PRINCIPAL_AMOUNT_RE.search(description_text)
    if not mortgagor_m or not amount_m:
        return None  # not a standard mortgage foreclosure, or truncated before this point — skip, don't guess

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
        # trust the source over the (often-truncated-away) scraped field:
        "county": county_m.group(1) if county_m else SOURCE_COUNTY,
        "auction_date": auction_m.group(1) if auction_m else None,
        "source_url": source_url,
        "source": "stpaul_legal_ledger",
    }


def parse_rss(xml_text: str) -> tuple[list[dict], int]:
    """Returns (matched_records, raw_item_count) — see pagination-fix
    note in scrape_finance_commerce_realestate.py for why raw_item_count
    is what pagination should key off of, not len(matched_records)."""
    records = []
    skipped = 0

    if len(xml_text.strip()) < 100 or not xml_text.strip().startswith("<?xml"):
        print(f"DEBUG: response doesn't look like real RSS (len={len(xml_text.strip())}), "
              f"treating as end of results", file=sys.stderr)
        return records, 0

    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    print(f"DEBUG: RSS contains {len(items)} items", file=sys.stderr)

    skip_reasons = {}  # reason -> count, for the summary line
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description_el = item.find("description")
        description_text = "".join(description_el.itertext()) if description_el is not None else ""

        record = parse_item(title, description_text, link)
        if record is None:
            skipped += 1
            reason = classify_skip_reason(description_text)
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            print(f"  SKIPPED ({reason}, len={len(description_text)}): {title!r} "
                  f"| ends with: ...{description_text[-60:]!r}", file=sys.stderr)
            continue
        records.append(record)

    print(f"  {len(records)} standard mortgage foreclosures extracted, "
          f"{skipped} skipped — breakdown: {skip_reasons}", file=sys.stderr)
    return records, len(items)


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
        records, item_count = parse_rss(xml_text)
        all_records.extend(records)
        if item_count == 0 and page > 1:
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
