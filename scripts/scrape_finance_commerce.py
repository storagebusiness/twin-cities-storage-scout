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

BACKPORT (2026-08-18): after finding real, meaningful under-extraction
in BOTH real estate scrapers (Hennepin AND Ramsey) from the same root
causes — a matched-record-count pagination bug, RSS truncation needing a
detail-page-fetch retry, and multi-format mortgagor/amount phrasing —
this script was checked for the same issues. Confirmed and fixed here
too, without needing a live run to discover them (same bugs, same fix,
already proven correct on two other scripts):
  - Pagination bug: `if not records and page > 1: break` used matched-
    record count as the end-of-feed signal, exactly like the real estate
    scraper's original bug. Fixed the same way — key off raw <item>
    count instead.
  - Facility-address regex missing `,?` before the zip code
    (comma-before-zip fix — see the address regex below) — this exact
    bug was already found and fixed in the Ramsey personal-property
    scraper; never backported here until now.
  - Vehicle-heuristic false negative: bare `" storage "` in
    `storage_signals` matches generic lien-law language unrelated to a
    self-storage business (e.g. "...a daily rate of $20.00 for storage
    accruing" in a manufactured-home notice) — found on Ramsey's feed,
    removed here too since Hennepin's feed almost certainly has the same
    kind of manufactured-home/vehicle notices mixed in.

NOT YET CONFIRMED on real Hennepin data (added defensively, same as the
real estate backport, pending a live run to confirm or rule out):
  - Whether this feed's RSS <description> truncates the way every OTHER
    feed checked on this platform does (Ramsey real estate, Ramsey
    personal property, Hennepin real estate — 3 for 3 so far). Added the
    same detail-page-fetch retry mechanism defensively.
  - Whether Hennepin has storage companies using the inline `Unit #N -
    Name: contents.` or `#N Name Address CONTENTS:` formats confirmed on
    Ramsey's feed (only Acorn's newline-separated `Unit #\n Name\n
    contents` format has ever been confirmed here). Added
    `UNIT_INLINE_RE` and `UNIT_HASH_RE` as fallbacks, tried only if
    `UNIT_BLOCK_RE` matches nothing.
Both are backed by a strong pattern (this session found real, different
under-extraction in 3 of 3 scripts checked before this one) rather than
a blind guess, but genuinely unconfirmed until the next live run's log
— check for `RETRY SUCCEEDED`/`FAILED` lines and `matched_unit_format`
usage before trusting either addition.
"""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

RSS_BASE = "https://finance-commerce.com/public-notice/export-rss/"
OUT_PATH = Path(__file__).parent.parent / "data" / "finance_commerce_names.json"
DETAIL_FETCH_DELAY_SECONDS = 1.0  # be polite — one extra request per suspected-truncated notice

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}

# Permanent exclusion — never run name/fame matching against family court
# matters involving minors. See module docstring.
EXCLUDED_SECTIONS = {"family", "individual and family"}

MAX_PAGES = 10  # sanity cap

# Format 1 (only one confirmed on real Hennepin data so far): newline-
# separated blocks — "Unit # 112\nLenora Ware/Curtis Adams\nsports
# equip. tools luggage furniture boxes\nUnit # 156\n...". A unit can
# list MULTIPLE people sharing one unit, separated by "/".
UNIT_BLOCK_RE = re.compile(
    r"Unit\s*#?\s*[\w-]*\d[\w-]*\s*\n"
    r"([^\n]+)\n"   # name line (possibly multiple names joined by "/")
    r"([^\n]+)",    # contents line
)
# Format 2 — confirmed on Ramsey's feed, NOT yet confirmed here. Inline,
# period-separated: "Unit #N - Name: contents."
UNIT_INLINE_RE = re.compile(
    r"Unit\s*#(\d+)\s*-\s*([^:]+):\s*(.+?)(?=\s*Unit\s*#\d+\s*-|\s*All property|\Z)",
    re.DOTALL,
)
# Format 3 — confirmed on Ramsey's feed, NOT yet confirmed here. Inline,
# no "Unit" keyword, includes renter's own address, supports multiple
# unit numbers per renter.
UNIT_HASH_RE = re.compile(
    r"#([\d/]+)\s+([A-Z][a-zA-Z'.-]+(?:\s+[A-Z][a-zA-Z'.-]+)+)\s+"
    r"(.+?)\s+CONTENTS:\s*([^#]+?)(?=\s*#[\d/]+\s|\Z)",
    re.DOTALL,
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
    sales with abandoned-VEHICLE lien sales (tow yards etc.) and
    manufactured-home lien sales — genuinely different notice types we
    don't want in the storage data. This is a heuristic, not exact.

    NOTE: bare " storage " deliberately excluded from storage_signals —
    confirmed on Ramsey's feed to false-positive against generic
    lien-law language unrelated to a self-storage business (e.g. "...a
    daily rate of $20.00 for storage accruing" in a manufactured-home
    notice), which suppressed this heuristic entirely. The remaining
    signals are specific enough to a storage business on their own."""
    vehicle_signals = ["vehicle", "vin", "license plate", "tow", "manufactured home"]
    storage_signals = ["storagetreasures.com", "storage unit", "self storage",
                        "mini storage"]
    text_lower = text.lower()
    has_vehicle = any(s in text_lower for s in vehicle_signals)
    has_storage = any(s in text_lower for s in storage_signals)
    return has_vehicle and not has_storage


def extract_county(title: str) -> str:
    m = re.search(r"\(([^)]+)\)\s*$", title)
    return m.group(1).strip() if m else ""


def parse_personal_property_summary(summary: str, source_url: str, county: str, posted: str) -> list[dict]:
    """Extract (name, contents, address) records from a Personal Property
    notice's summary text. Format 1 (newline-separated 'Unit #\\nName\\n
    contents') confirmed real from live debug output; formats 2/3 are
    fallbacks confirmed on Ramsey's feed, tried only if format 1 matches
    nothing — see module docstring BACKPORT note for why these are
    included without direct Hennepin confirmation yet. A unit can list
    multiple people sharing it, joined by '/' — split into one record
    per person."""
    summary = TRAILING_NOTICE_BOILERPLATE_RE.sub("", summary)

    addr_m = re.search(
        r"\d{2,6}\s+[\w\s]+?(?:Ave|St|Dr|Blvd|Ln|Rd|Road|Street|Avenue|Way|Circle|Cir)"
        r"[\w\s,]*?MN,?\s*\d{5}",  # comma-before-zip fix — see BACKPORT note
        summary, re.IGNORECASE,
    )
    address = addr_m.group(0).strip() if addr_m else f"UNKNOWN ({county} County)"

    records = []

    # Format 1: newline-separated, confirmed real on Hennepin data
    for m in UNIT_BLOCK_RE.finditer(summary):
        name_line, contents_line = m.groups()
        for name in name_line.split("/"):
            name = name.strip()
            if not name or not re.match(r"^[A-Z][a-zA-Z'\.-]+(\s+[A-Z][a-zA-Z'\.-]+)+$", name):
                continue
            records.append({
                "renter_name": name,
                "contents_text": contents_line.strip(),
                "facility_address_raw": address,
                "facility_address_normalized": normalize_address(address),
                "auction_date": posted,
                "source_url": source_url,
                "matched_unit_format": "block_newline",
            })
    if records:
        return records

    # Format 2 (fallback, not yet confirmed here): "Unit #N - Name: contents."
    for m in UNIT_INLINE_RE.finditer(summary):
        _unit_num, name, contents = m.groups()
        name = name.strip()
        if not re.match(r"^[A-Z][a-zA-Z'\.-]+(\s+[A-Z][a-zA-Z'\.-]+)+$", name):
            continue
        records.append({
            "renter_name": name,
            "contents_text": contents.strip().rstrip("."),
            "facility_address_raw": address,
            "facility_address_normalized": normalize_address(address),
            "auction_date": posted,
            "source_url": source_url,
            "matched_unit_format": "inline_dash",
        })
    if records:
        return records

    # Format 3 (fallback, not yet confirmed here): "#N Name Address CONTENTS:"
    for m in UNIT_HASH_RE.finditer(summary):
        unit_nums, name, renter_addr, contents = m.groups()
        name = name.strip()
        if not re.match(r"^[A-Z][a-zA-Z'\.-]+(\s+[A-Z][a-zA-Z'\.-]+)+$", name):
            continue
        for _unit_num in unit_nums.split("/"):
            records.append({
                "renter_name": name,
                "contents_text": contents.strip(),
                "facility_address_raw": address,
                "facility_address_normalized": normalize_address(address),
                "renter_home_address_raw": re.sub(r"\s+", " ", renter_addr).strip(),
                "auction_date": posted,
                "source_url": source_url,
                "matched_unit_format": "hash_inline",
            })
    return records


def extract_full_notice_text(html: str) -> str:
    """Pull the full, untruncated notice body out of a detail page.
    Copied from scrape_stpaul_legal_ledger_personalproperty.py's version
    of this function — proven correct against LIVE minnlawyer.com HTML
    for both a personal-property and a real-estate detail page, and
    against live finance-commerce.com HTML for real estate. NOT yet
    directly confirmed against a finance-commerce.com Personal Property
    detail page specifically — if this returns empty text on a live run,
    that's the first thing to check, though 3/3 confirmations on the same
    underlying platform so far make it a reasonable bet."""
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#x27;|&rsquo;|&rsquo", "'", text)
    text = re.sub(r"&sect;", "§", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    ad_text_m = re.search(r"Ad Text\s*\n(.+?)(?:Ad #\s*\d+|has abstracted)",
                           text, re.DOTALL | re.IGNORECASE)
    if not ad_text_m:
        return ""
    body = ad_text_m.group(1)
    body = re.sub(r"^\s*\w+ \d{1,2}, \d{4}\s*\n", "", body)
    return body.strip()


def fetch_detail_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_page(page_num: int) -> str:
    resp = requests.get(
        RSS_BASE,
        params={"feeds": "personal_property", "pageindex": page_num},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def process_rss_items(xml_text: str) -> tuple[list[dict], int]:
    """Returns (records, raw_item_count). Fetches each item's detail page
    for the full, untruncated text — same rationale as scrape_stpaul_
    legal_ledger_personalproperty.py: RSS <description> truncation cuts
    off unit listings, sometimes losing 100% of a notice's units, so RSS
    here is discovery-only. This is a behavior CHANGE from the original
    version of this script, which parsed the (possibly truncated) RSS
    summary directly — see module docstring BACKPORT note."""
    all_records = []

    if len(xml_text.strip()) < 100 or not xml_text.strip().startswith("<?xml"):
        print(f"DEBUG: response doesn't look like real RSS "
              f"(len={len(xml_text.strip())}), treating as end of results", file=sys.stderr)
        return [], 0

    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    print(f"DEBUG: RSS contains {len(items)} items", file=sys.stderr)

    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link:
            continue

        time.sleep(DETAIL_FETCH_DELAY_SECONDS)
        try:
            html = fetch_detail_page(link)
        except requests.RequestException as e:
            print(f"  WARNING: failed to fetch detail page {link}: {e}", file=sys.stderr)
            continue

        notice_text = extract_full_notice_text(html)
        if not notice_text:
            print(f"  WARNING: extract_full_notice_text() found nothing for {link} — "
                  f"HTML structure may not match what this was built against, see module docstring", file=sys.stderr)
            continue

        # Section:/Category: labels don't appear in Personal Property
        # summaries at all (confirmed — see extract_field's docstring),
        # so this check is a secondary guard, not the primary one — the
        # primary safeguard is that only feeds=personal_property is ever
        # fetched, never feeds=family. Left exactly as originally
        # written; not touched by this backport.
        section = extract_field(notice_text, "Section")
        if section.lower() in EXCLUDED_SECTIONS:
            print(f"  SKIPPED (excluded section '{section}'): {title}", file=sys.stderr)
            continue

        if looks_like_vehicle_notice(notice_text):
            print(f"  {link}: skipped (looks like a vehicle/manufactured-home notice)", file=sys.stderr)
            continue

        posted_m = POSTED_DATE_RE.search(notice_text)
        posted = posted_m.group(1) if posted_m else ""
        county = extract_county(title)  # dead code — title is always the broken placeholder, see extract_county docstring

        records = parse_personal_property_summary(notice_text, link, county, posted)
        if records:
            print(f"  {link}: {len(records)} records (format: {records[0]['matched_unit_format']})", file=sys.stderr)
        else:
            print(f"  {link}: 0 records — none of the 3 known unit formats matched\n"
                  f"    notice_text length={len(notice_text)}\n"
                  f"    FULL TEXT: {notice_text!r}", file=sys.stderr)
        all_records.extend(records)

    return all_records, len(items)


def main():
    all_records = []
    for page in range(1, MAX_PAGES + 1):
        xml_text = fetch_page(page)
        print(f"DEBUG: page {page} response length = {len(xml_text)} chars", file=sys.stderr)
        records, item_count = process_rss_items(xml_text)
        all_records.extend(records)
        # End-of-feed keyed off raw item count, not matched-record count
        # — same pagination-bug fix already made in
        # scrape_finance_commerce_realestate.py, applied here from the
        # start rather than needing to be rediscovered. See BACKPORT note.
        if item_count == 0 and page > 1:
            break

    format_counts = {}
    for r in all_records:
        fmt = r.get("matched_unit_format", "unknown")
        format_counts[fmt] = format_counts.get(fmt, 0) + 1
    print(f"Format usage: {format_counts}", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
