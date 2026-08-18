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
before reaching the county name or full mortgagor/amount text. Confirmed
against a real run (2026-08-17): 8 of 38 items (~21%) were genuine
mortgage foreclosures lost purely to truncation, not different notice
types. Original assumption that RSS alone was sufficient for real estate
(unlike Personal Property) was WRONG — fixed by adding the same
detail-page-fetch retry pattern personal property already used, but
applied selectively: only to items classified truncated_before_mortgagee
/ truncated_before_principal_amount by classify_skip_reason, not to every
skip, since items classified not_a_mortgage_notice genuinely aren't
standard notices and fetching their detail page would be wasted requests.

NOTE ON COUNTY: do NOT assume every item in this feed is Ramsey County.
Confirmed against a real run: a Cottage Grove property (Washington
County) appeared in this feed's Real Estate results — this domain/feed
is not purely Ramsey-scoped the way we originally assumed. It happened to
be a non-standard notice type that got skipped anyway, but a future
standard-format notice from another county could slip through. `county`
is left as None when COUNTY_RE can't find it in the (possibly truncated)
text — do NOT fall back to a hardcoded "Ramsey", since that would
mislabel out-of-county records as Ramsey rather than honestly showing
"unknown."
"""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

RSS_BASE = "https://minnlawyer.com/public-notice/export-rss/"
OUT_PATH = Path(__file__).parent.parent / "data" / "stpaul_legal_ledger_realestate.json"
MAX_PAGES = 10
DETAIL_FETCH_DELAY_SECONDS = 1.0  # be polite — one extra request per truncated item

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}
# No hardcoded fallback county — this feed is NOT purely Ramsey-scoped
# (a Cottage Grove/Washington County item was observed in a real run).
# `county` is left None when COUNTY_RE can't find it, rather than guessing
# — see module docstring "NOTE ON COUNTY".

# RSS boilerplate appended to every item's description ("Read More...",
# sometimes wrapped in an <a> tag whose text itertext() picks up) — strip
# this BEFORE truncation-detection in classify_skip_reason, or every
# description looks like it "ends in punctuation" (the "..." in "Read
# More...") regardless of whether the real notice text was cut off.
# Found as a bug in the diagnostic logging itself on 2026-08-17 — see chat.
READ_MORE_TAIL_RE = re.compile(r"\s*Read More\.\.\.\s*$", re.IGNORECASE)

STREET_SUFFIXES = r"(?:Ave|St|Dr|Blvd|Ln|Rd|Way|Ct|Cir|Pl|Trail|Trl|Pkwy|Terrace|Ter)"
DIRECTIONS = r"(?:N|S|E|W|NE|NW|SE|SW)"
DIRECTION_WORDS = {"N": "N", "S": "S", "E": "E", "W": "W",
                    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}

# "(S)" made optional — confirmed against a real run: some notices use
# the singular label "MORTGAGOR:" instead of "MORTGAGOR(S):" (e.g. a
# White Bear Lake notice: "MORTGAGOR: Jason J. Vavra and Jan Arford,
# husband and wife..."). The strict "(S)" version silently dropped these
# as non-matches even though they're standard mortgage foreclosures.
MORTGAGOR_RE = re.compile(
    r"MORTGAGOR(?:\(S\))?:\s*(.+?)\s*MORTGAGEE:", re.IGNORECASE | re.DOTALL,
)
# Fallback for a FOURTH confirmed format: numbered fields instead of
# labeled ones — "1. Date of Mortgage: X  2. Mortgagors: Y  3. Mortgagees:
# Z  4. Rec[orded...]" — confirmed against real text for "1374 Searle St
# Saint Paul" (Dayton's Bluff Neighborhood Housing Services). Tried only
# if the primary regex doesn't match. NOTE: whether PRINCIPAL_AMOUNT_RE
# also needs a numbered-format fallback is NOT yet confirmed — we don't
# have real text showing that field's label in this format. If a record
# matches this MORTGAGOR fallback but still fails on amount, that'll
# surface via classify_skip_reason/the retry-failure snippet log rather
# than being silently swallowed.
MORTGAGOR_NUMBERED_RE = re.compile(
    r"\d+\.\s*Mortgagors?:\s*(.+?)\s*\d+\.\s*Mortgagees?:", re.IGNORECASE | re.DOTALL,
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
    # Strip the RSS "Read More..." boilerplate BEFORE checking how the
    # text ends — every raw description ends with that tail, and its
    # "..." was previously satisfying the "ends in punctuation" check
    # unconditionally, making every truncated record look complete.
    # Found as a real bug on a live run — see module docstring.
    stripped = READ_MORE_TAIL_RE.sub("", description_text).rstrip()

    upper = stripped.upper()
    has_mortgagor_label = "MORTGAGOR" in upper
    has_mortgagee_label = "MORTGAGEE" in upper
    has_principal_label = "PRINCIPAL AMOUNT OF MORTGAGE" in upper
    length = len(stripped)
    # RSS descriptions get cut off mid-sentence when truncated — a
    # description near the observed ~400-600 char truncation length that
    # doesn't end on a sentence boundary is a strong truncation signal.
    looks_truncated = length >= 380 and not stripped.endswith((".", ")", '"'))

    if not has_mortgagor_label:
        return "not_a_mortgage_notice"  # HOA lien / postponement / sheriff's sale / etc — expected, not a bug
    if not has_mortgagee_label:
        return "truncated_before_mortgagee" if looks_truncated else "malformed_mortgagor_block"
    if not has_principal_label:
        return "truncated_before_principal_amount" if looks_truncated else "missing_principal_amount_label"
    return "has_all_labels_but_regex_still_failed"  # worth investigating directly if this ever shows up


def parse_item(title: str, description_text: str, source_url: str) -> dict | None:
    mortgagor_m = MORTGAGOR_RE.search(description_text)
    used_numbered_format = False
    if not mortgagor_m:
        mortgagor_m = MORTGAGOR_NUMBERED_RE.search(description_text)
        used_numbered_format = mortgagor_m is not None
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
        # None (not a hardcoded fallback) when not found — see "NOTE ON
        # COUNTY" in module docstring: this feed isn't purely Ramsey.
        "county": county_m.group(1) if county_m else None,
        "auction_date": auction_m.group(1) if auction_m else None,
        "source_url": source_url,
        "source": "stpaul_legal_ledger",
        # useful for auditing how much of the data depends on the less-
        # tested numbered-field fallback vs. the primary labeled format
        "matched_numbered_format": used_numbered_format,
    }


def extract_full_notice_text(html: str) -> str:
    """Pull the full, untruncated notice body out of a detail page.

    ⚠️ SAME VERIFICATION CAVEAT AS scrape_stpaul_legal_ledger_
    personalproperty.py's copy of this function: built from the VISIBLE
    layout of a browser-exported page (an "Ad Text" section starting
    after a dated line, ending at "Ad #<digits>" or the BridgeTower
    disclaimer), not confirmed against the real HTML/DOM for a REAL
    ESTATE detail page specifically — only for a Personal Property one.
    Both are served by the same platform/template, so this is a
    reasonable bet, not a confirmed one. If DEBUG output shows this
    returning empty text on a live run, that's the first thing to check.
    """
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#x27;|&rsquo;|&rsquo", "'", text)
    text = re.sub(r"&sect;", "§", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    ad_text_m = re.search(r"Ad Text\s*\n(.+?)(?:Ad #\s*\d+|St\. Paul Legal Ledger has abstracted)",
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


def retry_truncated_items(retry_candidates: list[tuple[str, str, str]]) -> list[dict]:
    """Re-attempt parsing for items that looked like real mortgage
    foreclosures but got cut off by RSS truncation — see
    classify_skip_reason's truncated_before_mortgagee /
    truncated_before_principal_amount. Only these, not every skip: items
    classified not_a_mortgage_notice genuinely aren't standard notices,
    so fetching their detail page would be wasted requests."""
    recovered = []
    for title, link, reason in retry_candidates:
        if not link:
            continue
        time.sleep(DETAIL_FETCH_DELAY_SECONDS)
        try:
            html = fetch_detail_page(link)
        except requests.RequestException as e:
            print(f"  RETRY FAILED (fetch error) for {title!r} ({reason}): {e}", file=sys.stderr)
            continue

        full_text = extract_full_notice_text(html)
        if not full_text:
            print(f"  RETRY FAILED (extract_full_notice_text found nothing) for {title!r} "
                  f"— see function's verification caveat", file=sys.stderr)
            continue

        record = parse_item(title, full_text, link)
        if record is None:
            # still doesn't match even with the full text — genuinely
            # not recoverable, or the notice uses yet another label
            # variant we haven't seen. Print a real snippet (not just
            # length/classification) so the actual structure is visible
            # in the log without needing another round-trip to diagnose
            # — same "always use real captured data" rule as everywhere
            # else in this project.
            still_missing = classify_skip_reason(full_text)
            print(f"  RETRY FAILED (still doesn't parse, now classified {still_missing}) "
                  f"for {title!r} — full text len={len(full_text)}\n"
                  f"    first 500 chars: {full_text[:500]!r}", file=sys.stderr)
            continue

        print(f"  RETRY SUCCEEDED for {title!r} (was {reason})", file=sys.stderr)
        recovered.append(record)
    return recovered


def parse_rss(xml_text: str) -> tuple[list[dict], int, list[tuple[str, str, str]]]:
    """Returns (matched_records, raw_item_count, retry_candidates).
    retry_candidates is (title, link, reason) for items that look
    truncated rather than genuinely non-standard — see
    retry_truncated_items."""
    records = []
    skipped = 0
    retry_candidates = []

    if len(xml_text.strip()) < 100 or not xml_text.strip().startswith("<?xml"):
        print(f"DEBUG: response doesn't look like real RSS (len={len(xml_text.strip())}), "
              f"treating as end of results", file=sys.stderr)
        return records, 0, retry_candidates

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
            if reason in ("truncated_before_mortgagee", "truncated_before_principal_amount"):
                retry_candidates.append((title, link, reason))
            continue
        records.append(record)

    print(f"  {len(records)} standard mortgage foreclosures extracted, "
          f"{skipped} skipped — breakdown: {skip_reasons}", file=sys.stderr)
    return records, len(items), retry_candidates


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
    all_retry_candidates = []
    for page in range(1, MAX_PAGES + 1):
        xml_text = fetch_page(page)
        print(f"DEBUG: page {page} response length = {len(xml_text)} chars", file=sys.stderr)
        records, item_count, retry_candidates = parse_rss(xml_text)
        all_records.extend(records)
        all_retry_candidates.extend(retry_candidates)
        if item_count == 0 and page > 1:
            break

    if all_retry_candidates:
        print(f"Retrying {len(all_retry_candidates)} truncated-but-likely-standard "
              f"notices via detail-page fetch...", file=sys.stderr)
        recovered = retry_truncated_items(all_retry_candidates)
        print(f"Recovered {len(recovered)}/{len(all_retry_candidates)} via detail-page fetch", file=sys.stderr)
        all_records.extend(recovered)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
