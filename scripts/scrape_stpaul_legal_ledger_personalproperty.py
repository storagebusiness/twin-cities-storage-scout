"""
Fetches St. Paul Legal Ledger's (minnlawyer.com) Personal Property RSS
feed — the Ramsey County counterpart to scrape_finance_commerce.py — and
extracts (renter_name, contents_text, facility_address, auction_date)
records, same output shape as the Hennepin scrapers, for data/names.json.

WHY THIS EXISTS: see scrape_stpaul_legal_ledger_realestate.py's docstring
and HANDOFF.md's "Single-county coverage limitation" section — F&C only
covers Hennepin County; St. Paul Legal Ledger is Ramsey's designated
paper, same backend platform, different domain.

⚠️ VERIFICATION STATUS — READ BEFORE TRUSTING THIS IN THE DAILY WORKFLOW:
Everything in RSS-parsing / unit-format-parsing below (parse_rss,
UNIT_BLOCK_RE, UNIT_INLINE_RE, UNIT_HASH_RE, the vehicle-notice heuristic,
the facility-address regex) was tested against REAL captured data — either
real RSS XML or real detail-page text pasted from the live site. See
project chat history, not fabricated.

The ONE piece that is NOT yet verified against real data:
`extract_full_notice_text()`'s HTML parsing. We only ever saw the detail
page as a browser-exported PDF (i.e. rendered/visible text), never the
actual HTML source — so the exact tag/class structure this function
assumes ("Ad Text" heading, then the notice body, then "Ad #<digits>" as
an end marker) is inferred from the VISIBLE layout, not confirmed against
the DOM. This matches the project's own rule (HANDOFF.md gotcha #4):
guessed-at parsing needs a real-data round before being trusted. Before
this runs unattended in the daily workflow: fetch one real detail page's
HTML (not PDF) and confirm extract_full_notice_text() pulls out the same
text block seen in the "1902 3743 4193881" 7th Street Storage / North Star
Mini Storage examples used to build the unit-format regexes. If it's
wrong, the DEBUG output below will make it obvious (0 units extracted from
a page known to have several).

NOTE ON WHY THIS NEEDS A DETAIL-PAGE FETCH AT ALL (unlike the real estate
scraper): minnlawyer.com's RSS <description> is truncated to ~400-600
chars. For real estate that's usually survivable (MORTGAGOR/MORTGAGEE
tends to appear early). For personal property, truncation lands BEFORE
any unit data in some notices — one real sample lost 100% of its units to
RSS truncation alone. So RSS here is used only to discover which notices
exist (via source_url); the full text is fetched per-item.

THREE CONFIRMED UNIT-LISTING FORMATS (see HANDOFF.md for the real samples
each was built against):
  1. Acorn Mini Storage style — newline-separated "Unit #\\nName\\ncontents"
  2. "7th Street Storage" style — inline "Unit #N - Name: contents."
  3. North Star Mini Storage style — inline "#N[/N/N] Name Address CONTENTS: contents",
     renter's OWN address included, multiple unit numbers possible per renter
This is a "try each format, use whichever matches" dispatcher, same spirit
as parse_item()'s try-then-skip handling of non-standard real estate
notices in the Hennepin scraper. There will very likely be a FOURTH format
from some other storage company eventually — that's the established
pattern for this data source, not a one-off surprise.
"""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

RSS_BASE = "https://minnlawyer.com/public-notice/export-rss/"
OUT_PATH = Path(__file__).parent.parent / "data" / "stpaul_legal_ledger_names.json"
MAX_PAGES = 10
SOURCE_COUNTY = "Ramsey"
DETAIL_FETCH_DELAY_SECONDS = 1.0  # be polite — one extra request per notice now

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}

# ---- Permanent exclusion, same as scrape_finance_commerce.py — never run
# name/fame matching against family court matters involving minors.
EXCLUDED_SECTIONS = {"family", "individual and family"}

# ---- Format 1: Acorn Mini Storage style (newline-separated).
# Unchanged from scrape_finance_commerce.py.
UNIT_BLOCK_RE = re.compile(
    r"Unit\s*#?\s*[\w-]*\d[\w-]*\s*\n"
    r"([^\n]+)\n"
    r"([^\n]+)",
)

# ---- Format 2: "7th Street Storage" style (inline, period-separated).
# Tested against real data — see HANDOFF.md.
UNIT_INLINE_RE = re.compile(
    r"Unit\s*#(\d+)\s*-\s*([^:]+):\s*(.+?)(?=\s*Unit\s*#\d+\s*-|\s*All property|\Z)",
    re.DOTALL,
)

# ---- Format 3: North Star Mini Storage style (inline, "#N Name Address
# CONTENTS:", supports multiple unit numbers per renter separated by "/").
# Tested against real data — see HANDOFF.md.
UNIT_HASH_RE = re.compile(
    r"#([\d/]+)\s+([A-Z][a-zA-Z'.-]+(?:\s+[A-Z][a-zA-Z'.-]+)+)\s+"
    r"(.+?)\s+CONTENTS:\s*([^#]+?)(?=\s*#[\d/]+\s|\Z)",
)

# Facility (or, for format 3, first-matching — usually facility, see
# module docstring caveat about ordering) address. Comma-before-zip fix
# applied here — found while testing against Ramsey data, applies equally
# to scrape_finance_commerce.py's copy of this regex (not yet backported
# there — see HANDOFF.md).
ADDRESS_RE = re.compile(
    r"\d{2,6}\s+[\w\s]+?(?:Ave|St|Dr|Blvd|Ln|Rd|Road|Street|Avenue|Way|Circle|Cir)"
    r"[\w\s,]*?MN,?\s*\d{5}",
    re.IGNORECASE,
)

# End-of-useful-content markers seen in real detail pages, e.g.
# "(July 13-20) ======== ST. PAUL LEGAL LEDGER ======= 4178587"
FOOTER_RE = re.compile(r"\(?\w+\s+\d{1,2}-\d{1,2}\)?\s*={4,}.*$", re.DOTALL)


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


def looks_like_vehicle_notice(text: str) -> bool:
    """Same heuristic as scrape_finance_commerce.py — Personal Property
    notices mix storage-unit lien sales with abandoned-vehicle/manufactured-
    home sales (a genuinely different notice type). Confirmed against a
    real Ramsey sample: an Arden Hills manufactured-home sheriff's sale
    mentioning a VIN, correctly flagged for skipping by this heuristic."""
    vehicle_signals = ["vehicle", "vin", "license plate", "tow", "manufactured home"]
    storage_signals = ["storagetreasures.com", "storage unit", "self storage",
                        " storage ", "mini storage"]
    text_lower = text.lower()
    has_vehicle = any(s in text_lower for s in vehicle_signals)
    has_storage = any(s in text_lower for s in storage_signals)
    return has_vehicle and not has_storage


def looks_like_a_name(s: str) -> bool:
    return bool(re.match(r"^[A-Z][a-zA-Z'.-]+(\s+[A-Z][a-zA-Z'.-]+)+$", s.strip()))


def extract_full_notice_text(html: str) -> str:
    """Pull the full, untruncated notice body out of a detail page.

    ⚠️ UNVERIFIED AGAINST REAL HTML — see module docstring. This assumes
    the page has a clearly-delimited "Ad Text" section (as seen in the
    browser-rendered/PDF view) that starts after a dated line and ends at
    an "Ad #<digits>" marker or the BridgeTower disclaimer paragraph,
    whichever comes first. If the real HTML structure differs, this will
    return empty or garbage text — check the DEBUG output on first real
    run before trusting it unattended.
    """
    # Strip tags to get flattened visible text. Deliberately not using a
    # heavier HTML parser dependency for a first pass — revisit if this
    # proves too fragile against the real DOM.
    #
    # HTML comments stripped FIRST and separately, with DOTALL — the
    # generic <[^>]+> tag-stripper below can't safely remove a comment
    # that contains a literal ">" character inside it (common in real
    # comments, e.g. conditional/template markers), which left a stray
    # "-->" leaking into extracted text on a real run — see chat history,
    # 2026-08-18.
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#x27;|&rsquo;|&rsquo", "'", text)
    text = re.sub(r"&sect;", "§", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    # Find the SECOND "Ad Text" occurrence's content — the first "Summary"
    # block on the page is the same truncated preview RSS already gives
    # us; "Ad Text" introduces the full body in the rendered layout.
    ad_text_m = re.search(r"Ad Text\s*\n(.+?)(?:Ad #\s*\d+|St\. Paul Legal Ledger has abstracted)",
                           text, re.DOTALL | re.IGNORECASE)
    if not ad_text_m:
        return ""
    body = ad_text_m.group(1)
    # drop the leading date line (e.g. "July 13, 2026") if present
    body = re.sub(r"^\s*\w+ \d{1,2}, \d{4}\s*\n", "", body)
    return body.strip()


def parse_unit_listings(notice_text: str, source_url: str, facility_address: str,
                         auction_date: str) -> list[dict]:
    """Try each known unit-listing format in turn; use whichever matches.
    See module docstring for the three formats."""
    text = FOOTER_RE.sub("", notice_text).strip()
    records = []

    # Format 1: Acorn-style, newline separated
    for m in UNIT_BLOCK_RE.finditer(text):
        name_line, contents_line = m.groups()
        for name in name_line.split("/"):
            name = name.strip()
            if not looks_like_a_name(name):
                continue
            records.append({
                "renter_name": name,
                "contents_text": contents_line.strip(),
                "facility_address_raw": facility_address,
                "facility_address_normalized": normalize_address(facility_address),
                "auction_date": auction_date,
                "source_url": source_url,
                "source": "stpaul_legal_ledger",
            })
    if records:
        return records

    # Format 2: "Unit #N - Name: contents." inline
    for m in UNIT_INLINE_RE.finditer(text):
        _unit_num, name, contents = m.groups()
        name = name.strip()
        if not looks_like_a_name(name):
            continue
        records.append({
            "renter_name": name,
            "contents_text": contents.strip().rstrip("."),
            "facility_address_raw": facility_address,
            "facility_address_normalized": normalize_address(facility_address),
            "auction_date": auction_date,
            "source_url": source_url,
            "source": "stpaul_legal_ledger",
        })
    if records:
        return records

    # Format 3: "#N[/N/N] Name Address CONTENTS: contents" inline —
    # includes the renter's OWN address, which we keep separately from
    # the facility address rather than discarding it.
    for m in UNIT_HASH_RE.finditer(text):
        unit_nums, name, renter_addr, contents = m.groups()
        name = name.strip()
        if not looks_like_a_name(name):
            continue
        for _unit_num in unit_nums.split("/"):
            records.append({
                "renter_name": name,
                "contents_text": contents.strip(),
                "facility_address_raw": facility_address,
                "facility_address_normalized": normalize_address(facility_address),
                "renter_home_address_raw": renter_addr.strip(),
                "auction_date": auction_date,
                "source_url": source_url,
                "source": "stpaul_legal_ledger",
            })
    return records


def fetch_rss_page(page_num: int) -> str:
    resp = requests.get(
        RSS_BASE, params={"feeds": "personal_property", "pageindex": page_num},
        headers=HEADERS, timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def fetch_detail_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def process_rss_items(xml_text: str) -> tuple[list[dict], int]:
    """Returns (records, raw_item_count). Fetches each item's detail page
    — this is the expensive step, one request per notice, hence the
    politeness delay in main()."""
    all_records = []

    if len(xml_text.strip()) < 100 or not xml_text.strip().startswith("<?xml"):
        print(f"DEBUG: response doesn't look like real RSS (len={len(xml_text.strip())}), "
              f"treating as end of results", file=sys.stderr)
        return [], 0

    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    print(f"DEBUG: RSS contains {len(items)} items", file=sys.stderr)

    for item in items:
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

        if looks_like_vehicle_notice(notice_text):
            print(f"  {link}: skipped (looks like a vehicle/manufactured-home notice)", file=sys.stderr)
            continue

        addr_m = ADDRESS_RE.search(notice_text)
        facility_address = addr_m.group(0) if addr_m else f"UNKNOWN ({SOURCE_COUNTY} County)"

        auction_m = re.search(r"Auction Date:\s*(\d{1,2}/\d{1,2}/\d{4})", notice_text) \
            or re.search(r"Sale:\s*(\d{1,2}/\d{1,2}/\d{4})", notice_text)
        auction_date = auction_m.group(1) if auction_m else ""

        records = parse_unit_listings(notice_text, link, facility_address, auction_date)
        if records:
            print(f"  {link}: {len(records)} records", file=sys.stderr)
        else:
            # Print the FULL text (not just a 200-char preview) so a
            # genuine format-4/format-5 discovery — or an extraction bug
            # — is diagnosable from this log alone, same "always use real
            # captured data" rule as the real estate scraper's retry
            # logging. Also log the vehicle-heuristic's inputs even
            # though it didn't trigger here, so a false negative (heuristic
            # SHOULD have caught this but didn't) is visible directly
            # rather than requiring a guess.
            text_lower = notice_text.lower()
            vehicle_hits = [s for s in ("vehicle", "vin", "license plate", "tow", "manufactured home") if s in text_lower]
            storage_hits = [s for s in ("storagetreasures.com", "storage unit", "self storage", " storage ", "mini storage") if s in text_lower]
            print(f"  {link}: 0 records — none of the 3 known unit formats matched\n"
                  f"    notice_text length={len(notice_text)}\n"
                  f"    vehicle-heuristic signals found: {vehicle_hits} | storage signals found: {storage_hits}\n"
                  f"    FULL TEXT: {notice_text!r}", file=sys.stderr)
        all_records.extend(records)

    return all_records, len(items)


def main():
    all_records = []
    for page in range(1, MAX_PAGES + 1):
        xml_text = fetch_rss_page(page)
        print(f"DEBUG: page {page} response length = {len(xml_text)} chars", file=sys.stderr)
        records, item_count = process_rss_items(xml_text)
        all_records.extend(records)
        # end-of-feed keyed off raw item count, not matched-record count —
        # same fix as scrape_finance_commerce_realestate.py, applied here
        # from the start rather than found as a bug later.
        if item_count == 0 and page > 1:
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
