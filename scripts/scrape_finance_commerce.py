"""
Fetches Finance & Commerce's public notice RSS feed, filtered to the
Personal Property section (where storage/self-storage lien sales live),
and parses out (renter_name, contents_text, facility_address, auction_date)
records — same output shape as scrape_startribune.py, so both feed into
the same data/names.json.

Runs server-side via GitHub Actions (see .github/workflows/daily-scrape.yml).

IMPORTANT — permanently excludes the "Family"/"Individual and Family"
section. That section carries CHIPS petitions, termination-of-parental-
rights notices, and other matters involving minors in active protection
proceedings. This tool does not run name-matching or scoring against
that category, full stop — see EXCLUDED_SECTIONS below.

NOTE: this was built and tested against one real captured "all" feed
sample plus one fabricated Personal Property example (we hadn't seen a
real storage notice from this source at build time). If real Personal
Property notices turn out to have a different internal format than
guessed here, the DEBUG lines below should show why — same pattern that
diagnosed the Star Tribune parsing issues.
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

RSS_BASE = "https://finance-commerce.com/public-notice/export-rss/"
OUT_PATH = Path(__file__).parent.parent / "data" / "finance_commerce_names.json"

# Permanent exclusion — never run name/fame matching against family court
# matters involving minors. See module docstring.
EXCLUDED_SECTIONS = {"family", "individual and family"}

MAX_PAGES = 10  # sanity cap

# Real format confirmed from live debug output: units are separated by
# newlines, not colons/commas — "Unit # 112\nLenora Ware/Curtis
# Adams\nsports equip. tools luggage furniture boxes\nUnit # 156\n...".
# A unit can list MULTIPLE people sharing one unit, separated by "/".
UNIT_BLOCK_RE = re.compile(
    r"Unit\s*#?\s*[\w-]*\d[\w-]*\s*\n"
    r"([^\n]+)\n"   # name line (possibly multiple names joined by "/")
    r"([^\n]+)",    # contents line
)
TRAILING_NOTICE_BOILERPLATE_RE = re.compile(
    r"Posted:.*$", re.IGNORECASE | re.DOTALL,
)


def normalize_address(addr: str) -> str:
    addr = addr.lower().strip()
    addr = re.sub(r"[.,]", "", addr)
    replacements = {
        r"\bavenue\b": "ave", r"\bstreet\b": "st", r"\bdrive\b": "dr",
        r"\bboulevard\b": "blvd", r"\blane\b": "ln", r"\broad\b": "rd",
        r"\bnorth\b": "n", r"\bsouth\b": "s", r"\beast\b": "e", r"\bwest\b": "w",
        r"\bminnesota\b": "mn",
    }
    for pattern, repl in replacements.items():
        addr = re.sub(pattern, repl, addr)
    return re.sub(r"\s+", " ", addr).strip()


POSTED_DATE_RE = re.compile(r"Posted:\s*(\d{1,2}/\d{1,2}/\d{4})")


def extract_field(description_text: str, field_name: str) -> str:
    """Pull 'Section: X' / 'Category: X' / 'Posted: X' out of the
    description, IF that labeled format is present. NOTE: real Personal
    Property items were found to NOT use this labeled format at all —
    they're raw notice text starting directly with 'Sale: [date]
    Summary: [text]', unlike Business/Family items on the 'all' feed
    which did have Section:/Category:/Posted: labels. This function is
    kept for sources/categories that do use labels; Personal Property
    items fall through to parse_personal_property_summary() operating on
    the whole raw description text instead."""
    m = re.search(rf"{field_name}:\s*(.+?)(?=\n|$)", description_text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def looks_like_vehicle_notice(text: str) -> bool:
    """Personal Property notices from this source mix storage-unit lien
    sales with abandoned-VEHICLE lien sales (tow yards etc.) — a genuinely
    different notice type we don't want in the storage data. Vehicles
    show VIN-like alphanumeric codes and phrases like 'the vehicles
    listed above'; storage notices reference storagetreasures.com or a
    storage company name. This is a heuristic, not exact."""
    vehicle_signals = ["vehicle", "vin", "license plate", "tow"]
    storage_signals = ["storagetreasures.com", "storage unit", "self storage",
                        " storage ", "mini storage"]
    text_lower = text.lower()
    has_vehicle = any(s in text_lower for s in vehicle_signals)
    has_storage = any(s in text_lower for s in storage_signals)
    return has_vehicle and not has_storage


def extract_county(title: str) -> str:
    m = re.search(r"\(([^)]+)\)\s*$", title)
    return m.group(1).strip() if m else ""


def parse_personal_property_summary(summary: str, source_url: str, county: str, posted: str) -> list[dict]:
    """Extract (name, contents, address) records from a Personal Property
    notice's summary text. Confirmed real format (from live debug output):
    'Unit # 112\\nLenora Ware/Curtis Adams\\nsports equip. tools luggage
    furniture boxes\\nUnit # 156\\n...' — newline-separated blocks, not the
    colon/comma format originally guessed. A unit can list multiple
    people sharing it, joined by '/' — split into one record per person."""
    summary = TRAILING_NOTICE_BOILERPLATE_RE.sub("", summary)

    addr_m = re.search(
        r"\d{2,6}\s+[\w\s]+?(?:Ave|St|Dr|Blvd|Ln|Rd|Road|Street|Avenue|Way|Circle|Cir)"
        r"[\w\s,]*?MN\s*\d{5}",
        summary, re.IGNORECASE,
    )
    address = addr_m.group(0).strip() if addr_m else f"UNKNOWN ({county} County)"

    records = []
    for m in UNIT_BLOCK_RE.finditer(summary):
        name_line, contents_line = m.groups()
        # split multiple co-renters on a shared unit, e.g. "Lenora
        # Ware/Curtis Adams" -> two separate records, same contents
        for name in name_line.split("/"):
            name = name.strip()
            if not name or not re.match(r"^[A-Z][a-zA-Z'\.-]+(\s+[A-Z][a-zA-Z'\.-]+)+$", name):
                continue  # skip anything that doesn't look like a real name
            records.append({
                "renter_name": name,
                "contents_text": contents_line.strip(),
                "facility_address_raw": address,
                "facility_address_normalized": normalize_address(address),
                "auction_date": posted,
                "source_url": source_url,
            })
    return records


def fetch_page(page_num: int) -> str:
    resp = requests.get(
        RSS_BASE,
        params={"feeds": "personal_property", "pageindex": page_num},
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def parse_rss(xml_text: str) -> list[dict]:
    all_records = []

    # Some pages return a short/malformed response instead of valid RSS
    # (observed: 38-char non-XML response on page 2) — treat that as "no
    # more results" rather than crashing the whole script.
    if len(xml_text.strip()) < 100 or not xml_text.strip().startswith("<?xml"):
        print(f"DEBUG: response doesn't look like real RSS "
              f"(len={len(xml_text.strip())}), treating as end of results", file=sys.stderr)
        return []

    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    print(f"DEBUG: RSS contains {len(items)} items", file=sys.stderr)

    for i, item in enumerate(items):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description_el = item.find("description")

        # description contains nested <div> elements; flatten to plain text
        description_text = "".join(description_el.itertext()) if description_el is not None else ""

        # DEBUG: show the raw, unprocessed content for the first couple
        # items so we can see exactly what the server actually sent,
        # rather than only what our extraction logic pulled out of it.
        if i < 2:
            print(f"DEBUG: item {i} raw title: {title!r}", file=sys.stderr)
            print(f"DEBUG: item {i} raw description (first 1500 chars): "
                  f"{description_text[:1500]!r}", file=sys.stderr)

        section = extract_field(description_text, "Section")
        category = extract_field(description_text, "Category")
        posted_m = POSTED_DATE_RE.search(description_text)
        posted = posted_m.group(1) if posted_m else ""
        county = extract_county(title)

        # Real Personal Property items don't use the labeled Section/
        # Category/Summary/Posted format at all — confirmed via live
        # debug output. Using extract_field("Summary") here was actively
        # HARMFUL: the raw text happens to contain the literal word
        # "Summary:" followed by running prose that spans many lines, but
        # extract_field stops at the first newline — silently truncating
        # away all the real Unit#/Name/contents content that comes after.
        # Always use the full raw text instead.
        summary = description_text.strip()

        if looks_like_vehicle_notice(summary):
            print(f"  item {i}: skipped (looks like a vehicle notice, not storage)", file=sys.stderr)
            continue

        if section.lower() in EXCLUDED_SECTIONS:
            print(f"  SKIPPED (excluded section '{section}'): {title}", file=sys.stderr)
            continue

        records = parse_personal_property_summary(summary, link, county, posted)
        if records:
            print(f"  {title}: {len(records)} records", file=sys.stderr)
        else:
            print(f"  {title}: 0 records (section={section!r} category={category!r})", file=sys.stderr)
        all_records.extend(records)

    return all_records


def main():
    all_records = []
    for page in range(1, MAX_PAGES + 1):
        xml_text = fetch_page(page)
        print(f"DEBUG: page {page} response length = {len(xml_text)} chars", file=sys.stderr)
        records = parse_rss(xml_text)
        if not records and page > 1:
            # heuristic stop: no new records on a later page likely means
            # we've paginated past the end
            break
        all_records.extend(records)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
