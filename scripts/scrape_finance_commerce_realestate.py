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

PAGINATION BUG FIX (found while investigating why only Hennepin County
properties were showing up on the map, 2026-08-17): the original loop
stopped paging as soon as a page produced zero *matched* records, using
that as a proxy for "end of feed." But a page can contain real RSS items
that are all non-standard notice types (HOA liens, postponements, etc. —
see above), which legitimately parse to zero matched records without
being the end of the feed. Because F&C's feed is not evenly mixed across
counties per page, this let a single all-skipped page truncate the whole
scrape early and silently drop every county that happened to appear only
on later pages (in the case that surfaced this, everything past Hennepin).
Fixed by keying "end of feed" off the raw <item> count instead of the
filtered record count — an empty page (zero <item> elements) is the only
thing that means we've run off the end of the feed.

MULTI-FORMAT BACKPORT (2026-08-18): while building the Ramsey County
counterpart to this scraper (scrape_stpaul_legal_ledger_realestate.py,
sourced from minnlawyer.com), Ramsey's feed turned out to use FIVE
different mortgagor/amount phrasings, not the single MORTGAGOR(S):/
MORTGAGEE:/PRINCIPAL AMOUNT OF MORTGAGE: format this script originally
assumed was universal. Backported the same fallback regexes here on the
theory that F&C is very likely the same underlying platform (identical
URL structure — public-notice/export-rss/?feeds=X&pageindex=N — and
identical Auction Date:/Description: label format strongly suggest the
same BridgeTower/Dolan backend, just a different newspaper/domain), so
Hennepin notices may include the same format variety.

IMPORTANT CAVEAT: this has NOT been confirmed against real F&C data the
way the Ramsey fixes were — this repo has only ever seen F&C's already-
successfully-parsed JSON output, never its raw RSS XML, so it's unknown
whether F&C's feed (a) actually contains these alternate formats at all,
or (b) truncates descriptions the way minnlawyer.com's does. Added
classify_skip_reason() + a skip_reasons breakdown in the log (same
diagnostic pattern used to find and fix Ramsey's issues) specifically so
the NEXT REAL RUN answers this empirically instead of guessing. If the
breakdown shows meaningful `truncated_before_*` counts, port over the
detail-page-fetch retry mechanism from scrape_stpaul_legal_ledger_
realestate.py too — deliberately NOT added here yet, since there's no
evidence yet it's needed for this feed specifically.
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

# RSS boilerplate that may follow "Read More..." links — stripped before
# truncation-detection in classify_skip_reason, same fix as the Ramsey
# scraper needed (its trailing "..." was defeating the "does this look
# complete" check unconditionally). NOT YET CONFIRMED this exact tail
# appears in F&C's feed specifically — added defensively since it's
# harmless if absent (the regex just won't match anything).
READ_MORE_TAIL_RE = re.compile(r"\s*Read More\.\.\.\s*$", re.IGNORECASE)

# "(S)" made optional — see MULTI-FORMAT BACKPORT note in module docstring.
MORTGAGOR_RE = re.compile(
    r"MORTGAGOR(?:\(S\))?:\s*(.+?)\s*MORTGAGEE:", re.IGNORECASE | re.DOTALL,
)
# Fallback formats confirmed on Ramsey's feed, backported here on the
# theory F&C is the same underlying platform — NOT yet confirmed against
# real Hennepin data. See module docstring.
MORTGAGOR_NUMBERED_RE = re.compile(
    r"\d+\.\s*Mortgagors?:\s*(.+?)\s*\d+\.\s*Mortgagees?:", re.IGNORECASE | re.DOTALL,
)
MORTGAGOR_EXECUTED_BY_RE = re.compile(
    r"executed by\s+(.+?),(?:\s*a\s+.+?,)?\s*as Mortgagor(?:\(s\))?", re.IGNORECASE | re.DOTALL,
)
PRINCIPAL_AMOUNT_RE = re.compile(
    r"(?:ORIGINAL|MAXIMUM)\s+PRINCIPAL\s+AMOUNT\s+OF\s+MORTGAGE:\s*\$([\d,]+\.\d{2})",
    re.IGNORECASE,
)
# Fallback amount phrasing confirmed on Ramsey's feed — same caveat as above.
PRINCIPAL_AMOUNT_SECURED_RE = re.compile(
    r"principal\s+amount\s+secured\s+by\s+the\s+mortgage\s+was:?\s*(?:[^$]*?)\$([\d,]+\.\d{2})",
    re.IGNORECASE,
)
COUNTY_RE = re.compile(r"([A-Z][a-zA-Z]+)\s+County", re.IGNORECASE)
AUCTION_DATE_RE = re.compile(r"Auction Date:\s*(\d{1,2}/\d{1,2}/\d{4})")
# Postponement notices reference an original notice rather than restating
# mortgagor/mortgagee/amount — see classify_skip_reason. Confirmed on
# Ramsey's feed; not yet confirmed F&C's has this notice type at all.
POSTPONEMENT_RE = re.compile(r"NOTICE OF POSTPONEMENT", re.IGNORECASE)


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


def classify_skip_reason(description_text: str) -> str:
    """Diagnostic classification — same purpose and logic as the version
    built for scrape_stpaul_legal_ledger_realestate.py: distinguish
    "genuinely not a standard mortgage foreclosure" from "probably is
    one, but something (truncation, an unhandled format) is preventing
    extraction." See that script + HANDOFF.md for the full story of how
    this was arrived at and the bugs found in earlier versions of this
    same logic (the Read More tail and postponement false-positive
    issues) — both fixes are included here from the start rather than
    needing to be rediscovered."""
    stripped = READ_MORE_TAIL_RE.sub("", description_text).rstrip()

    if POSTPONEMENT_RE.search(stripped):
        return "postponement_notice"

    upper = stripped.upper()
    has_mortgagor_label = "MORTGAGOR" in upper
    has_mortgagee_label = "MORTGAGEE" in upper
    has_principal_label = ("PRINCIPAL AMOUNT OF MORTGAGE" in upper
                            or "PRINCIPAL AMOUNT SECURED BY THE MORTGAGE" in upper)
    length = len(stripped)
    looks_truncated = length >= 380 and not stripped.endswith((".", ")", '"'))

    if not has_mortgagor_label:
        return "not_a_mortgage_notice"
    if not has_mortgagee_label:
        return "truncated_before_mortgagee" if looks_truncated else "malformed_mortgagor_block"
    if not has_principal_label:
        return "truncated_before_principal_amount" if looks_truncated else "missing_principal_amount_label"
    return "has_all_labels_but_regex_still_failed"


def parse_item(title: str, description_text: str, source_url: str) -> dict | None:
    """Returns a structured record for a standard mortgage-foreclosure
    notice, or None if this item isn't that pattern (HOA lien,
    postponement, civil judgment sale, etc. — see module docstring).

    Tries the primary MORTGAGOR(S):/MORTGAGEE: format first, then two
    fallback formats confirmed on Ramsey's feed (numbered fields;
    inverted "executed by X ... as Mortgagor(s)") — see MULTI-FORMAT
    BACKPORT note in module docstring for why these were added here
    without direct confirmation they occur in Hennepin data."""
    mortgagor_m = MORTGAGOR_RE.search(description_text)
    used_numbered_format = False
    used_executed_by_format = False
    if not mortgagor_m:
        mortgagor_m = MORTGAGOR_NUMBERED_RE.search(description_text)
        used_numbered_format = mortgagor_m is not None
    if not mortgagor_m:
        mortgagor_m = MORTGAGOR_EXECUTED_BY_RE.search(description_text)
        used_executed_by_format = mortgagor_m is not None

    amount_m = PRINCIPAL_AMOUNT_RE.search(description_text)
    if not amount_m:
        amount_m = PRINCIPAL_AMOUNT_SECURED_RE.search(description_text)

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
        # useful for auditing how much data rides on the less-common
        # fallback formats, and for spotting whether they're firing at
        # all on this feed (if never True, F&C may just not have these
        # format variants, which would answer the open question above)
        "matched_numbered_format": used_numbered_format,
        "matched_executed_by_format": used_executed_by_format,
    }


def parse_rss(xml_text: str) -> tuple[list[dict], int]:
    """Returns (matched_records, raw_item_count). raw_item_count is what
    pagination should key off of — it's the only reliable signal for
    "this page was actually empty / we've run off the end of the feed."
    matched_records can legitimately be empty on a non-empty page (e.g. a
    page that's all HOA-lien notices) without that meaning end-of-feed."""
    records = []
    skipped = 0
    skip_reasons = {}  # reason -> count, for the summary line — see
                        # classify_skip_reason and MULTI-FORMAT BACKPORT
                        # note in module docstring for why this matters
                        # here specifically: this is what will tell us
                        # whether F&C needs the same detail-page-fetch
                        # retry mechanism Ramsey needed, or not.

    if len(xml_text.strip()) < 100 or not xml_text.strip().startswith("<?xml"):
        print(f"DEBUG: response doesn't look like real RSS (len={len(xml_text.strip())}), "
              f"treating as end of results", file=sys.stderr)
        return records, 0

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
            skipped += 1
            reason = classify_skip_reason(description_text)
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
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
        # End-of-feed is "this page had no RSS items at all," NOT "this
        # page had no items matching the standard-mortgage pattern." A
        # page can be legitimately all HOA-lien/postponement notices
        # (item_count > 0, records == []) without being the end of the
        # feed — see module docstring, this was the Hennepin-only bug.
        if item_count == 0 and page > 1:
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
