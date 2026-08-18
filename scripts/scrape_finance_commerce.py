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

NAME_CONTENTS_RE = re.compile(
    # Requires a "Unit <id>:" prefix — the actual structural marker before
    # a real name/contents pair. A loose "capitalized words + comma"
    # pattern (no Unit-prefix requirement) was tested first and produced a
    # false positive matching intro boilerplate text as a fake "name" —
    # fixed by anchoring to this more specific, reliable marker instead.
    r"Unit\s+[\w-]*\d[\w-]*\s*:\s*"
    r"([A-Z][a-zA-Z'\.-]+(?:\s+[A-Z][a-zA-Z'\.-]+){1,3}),\s*([^.]+?)(?:\.|$)",
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


def extract_field(description_text: str, field_name: str) -> str:
    """Pull 'Section: X' / 'Category: X' / 'Summary: X' / 'Posted: X' out of
    the description block's div contents."""
    m = re.search(rf"{field_name}:\s*(.+?)(?=\n|$)", description_text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def extract_county(title: str) -> str:
    m = re.search(r"\(([^)]+)\)\s*$", title)
    return m.group(1).strip() if m else ""


def parse_personal_property_summary(summary: str, source_url: str, county: str, posted: str) -> list[dict]:
    """Extract (name, contents, address) records from a Personal Property
    notice's summary text. Storage lien notices from major operators
    (Extra Space, Public Storage etc.) tend to follow a recognizable
    pattern: an address, then a list of 'Unit N: Name, contents' entries —
    similar enough to Star Tribune's format that the same core regex
    approach applies, adjusted for this source's cleaner pre-extracted text."""
    addr_m = re.search(
        r"\d{2,6}\s+[\w\s]+?(?:Ave|St|Dr|Blvd|Ln|Rd|Road|Street|Avenue|Way|Circle|Cir)"
        r"[\w\s,]*?MN\s*\d{5}",
        summary, re.IGNORECASE,
    )
    address = addr_m.group(0).strip() if addr_m else f"UNKNOWN ({county} County)"

    records = []
    for m in NAME_CONTENTS_RE.finditer(summary):
        name, contents = m.groups()
        records.append({
            "renter_name": name.strip(),
            "contents_text": contents.strip(),
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
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    print(f"DEBUG: RSS contains {len(items)} items", file=sys.stderr)

    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description_el = item.find("description")

        # description contains nested <div> elements; flatten to plain text
        description_text = "".join(description_el.itertext()) if description_el is not None else ""

        section = extract_field(description_text, "Section")
        category = extract_field(description_text, "Category")
        summary = extract_field(description_text, "Summary")
        posted = extract_field(description_text, "Posted")
        county = extract_county(title)

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
