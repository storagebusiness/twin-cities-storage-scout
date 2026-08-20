"""
Scrapes Public Surplus's Tax Sale category (catid=1506) for tax-forfeited
land listings. Public Surplus's URL pattern is state-agnostic
(publicsurplus.com/sms/all,{state}/browse/cataucs?catid=1506), so this
script takes a list of state codes and covers all of them in one run —
no per-state scraper needed, per the finding in HANDOFF-2.md.

IMPORTANT — UNTESTED AGAINST THE LIVE SITE: this sandbox's network is
blocked from publicsurplus.com. The parsing logic below is built from real
HTML captured by the user (MN, catid=1506, 2026-08-19), but the pagination
logic and the exact behavior on GA/SC has not been verified live. Run this
via GitHub Actions or locally and report back any parsing errors or empty
results before trusting the output — per HANDOFF-2.md's rule against
guessing at real-site behavior blind.

Known gaps (see schema-tax-forfeited-land.md):
- No detail-page fetching yet. This script only pulls what's on the
  category/list page: id, title, price (single figure, NOT an EMV/min-bid
  pair), state. County and parcel/PID are best-effort regex extraction
  from the title, which is NOT reliably present for all listings (e.g.
  bare street addresses with no county in the title at all).
- No "no land access" disclaimer detection — that requires detail-page
  text, which hasn't been captured yet.
- assessed_value.source is left as "{state}_pending" for GA/SC and
  "met_council_arcgis" is NOT wired up here for MN — that's a separate
  lookup step (parallel to lookup_parcel.py in Piece 2) not yet built.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_PATH = DATA_DIR / "public_surplus_taxforfeited.json"

BASE_URL = "https://www.publicsurplus.com"
CATID = "1506"  # Tax Sale subcategory under Real Estate — confirmed in HANDOFF-2.md

# The three states in scope for now. Public Surplus's URL pattern works
# for any 2-letter USPS code — add more here as new states come into scope.
STATES = ["mn", "ga", "sc"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

REQUEST_DELAY_SECONDS = 1  # be polite; same convention as scrape_stpaul_legal_ledger_personalproperty.py
MAX_PAGES_PER_STATE = 20  # safety cap so a bug can't loop forever
CONNECT_TIMEOUT_SECONDS = 10  # separate connect vs read timeout — a single float can let DNS/connect hangs slip past it
READ_TIMEOUT_SECONDS = 20
MAX_RETRIES_PER_REQUEST = 2  # fail fast rather than hang; total worst case per request ~= (10+20)*(1+2) = 90s
FETCH_DETAIL_PAGES = True  # fetch each listing's detail page for minimum-bid price + buildability text

# Confirmed real phrases, each tagged with why it's an exclusion category.
# Kept as an exact-match list per the rule against guessing at phrasing
# blind — expand only from real examples or explicit user direction, never
# invented ones.
# "no_land_access" confirmed via Koochiching County auc=4045302.
# "water_access_only", "floodplain", and "wetlands" confirmed via a St.
# Louis County listing (Ash River frontage, RES-5 zoning) — note these are
# DISTINCT exclusion reasons even though a single listing can hit more
# than one. "not_buildable" phrases added at the user's explicit direction
# (generic terms, not yet tied to one specific captured listing).
BUILDABILITY_PHRASES = [
    {"phrase": "no developed land access", "category": "no_land_access"},
    {"phrase": "water access only", "category": "water_access_only"},
    {"phrase": "floodplain", "category": "floodplain"},
    {"phrase": "wetlands", "category": "wetlands"},
    {"phrase": "not buildable", "category": "not_buildable"},
    {"phrase": "unbuildable", "category": "not_buildable"},
]


def parse_county(title: str) -> str | None:
    """Best-effort county extraction. Many titles don't contain one at all
    (e.g. bare street addresses) — returns None rather than guessing."""
    match = re.search(r"([A-Za-z .]+?)\s+County", title)
    if match:
        return match.group(1).strip()
    match = re.search(r"County of\s+([A-Za-z .]+)", title, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def parse_parcel_id(title: str) -> str | None:
    """Best-effort PID/Parcel extraction from the title. Formats seen in
    real captured data: 'PID 55-070-01000', 'Parcel 36.036.0300',
    'Parcel 36-0032-000' (Sibley — hyphenated, unlike the dot-separated
    Goodhue format). Must allow both hyphens and periods or hyphenated
    parcel numbers get truncated at the first hyphen."""
    match = re.search(r"PID\s+([\d\-]+)", title)
    if match:
        return match.group(1)
    match = re.search(r"Parcel\s+([\d.\-]+)", title)
    if match:
        return match.group(1)
    return None


def parse_county_from_description(description_text: str) -> str | None:
    """Fallback county extraction from the description text, for listings
    whose title doesn't contain a county name (common — e.g. bare street
    addresses, or 'Sale NNNN: Tax Forfeited Land - Township, Parcel X').
    Confirmed real phrasing: 'MANAGED BY GOODHUE COUNTY', 'MANAGED BY
    COTTONWOOD COUNTY', 'MANAGED BY SIBLEY COUNTY'."""
    match = re.search(r"MANAGED BY\s+([A-Za-z\s]+?)\s+COUNTY", description_text, re.IGNORECASE)
    if match:
        return match.group(1).strip().title()
    return None


def looks_like_address(title: str) -> bool:
    """Heuristic: titles like '14 24th ST, WINDOM' start with a number and
    contain a comma. Titles like 'Koochiching County Tax-Forfeiture...' or
    'Sale 2601: Tax Forfeited Land...' don't."""
    return bool(re.match(r"^\d+\s+\S+.*,", title))


def fetch_page(state: str, page: int) -> str | None:
    url = f"{BASE_URL}/sms/all,{state}/browse/cataucs"
    params = {"catid": CATID, "page": page, "slth": "y", "sortBy": "timeLeft", "sortDesc": "N"}

    for attempt in range(1, MAX_RETRIES_PER_REQUEST + 2):  # +1 for the initial try
        try:
            print(f"  {state.upper()} page {page}: requesting (attempt {attempt})...", file=sys.stderr, flush=True)
            resp = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),  # (connect, read) — bounds hangs at both stages
            )
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            print(f"  {state.upper()} page {page}: attempt {attempt} failed — {e}", file=sys.stderr, flush=True)
            if attempt <= MAX_RETRIES_PER_REQUEST:
                time.sleep(2)
            else:
                print(f"  {state.upper()} page {page}: giving up after {attempt} attempts", file=sys.stderr, flush=True)
                return None

    return None


def parse_listings(html: str, state: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table#auctionTableView tbody tr")
    listings = []

    for row in rows:
        row_id = row.get("id", "")
        auction_id = row_id.replace("catList", "")
        if not auction_id:
            continue

        link = row.select_one("td.text-start a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        listing_url = f"{BASE_URL}{href}" if href.startswith("/") else href

        price_cell = row.select_one(f"#val_{auction_id}catList")
        current_bid = None
        if price_cell:
            price_text = price_cell.get_text(strip=True).replace("$", "").replace(",", "")
            try:
                current_bid = float(price_text)
            except ValueError:
                pass

        county = parse_county(title)
        parcel_id = parse_parcel_id(title)
        address = title if looks_like_address(title) else None

        listings.append({
            "id": f"publicsurplus-{auction_id}",
            "source": "public_surplus",
            "state": state.upper(),
            "county": county,
            "listing_category": "tax_forfeited_land",

            "listing_type": {
                "owner": "state",
                "phase": "unknown",  # not present on the category page; needs detail-page fetch
                "notes": "Phase not extractable from category listing page.",
            },

            "address": address,
            "parcel_id": parcel_id,
            "coordinates": None,

            "listing_url": listing_url,
            "date_listed": None,
            "date_closes": None,
            "status": "open",

            "price": {
                "current_bid": current_bid,
                "minimum_bid": None,
                "tier1_emv_price": None,
                "tier2_minimum_price": None,
                "price_basis": "minimum_bid_only",
            },

            "assessed_value": {
                "emv_total": None,
                "source": f"{state.lower()}_pending",
                "lookup_status": "not_yet_attempted",
            },

            "buildable": True,
            "buildability_notes": [],

            "area_wealth": {
                "census_acs_median_income": None,
                "census_tract": None,
            },

            "raw_source_data": {"title": title},
        })

    return listings


def parse_minimum_bid_price(description_text: str) -> tuple[float | None, float | None]:
    """Extracts price info from the listing description. Returns
    (tier1_emv_or_appraised, tier2_minimum_bid) — either may be None.

    Confirmed real formats across different counties on Public Surplus
    (counties clearly do NOT share one convention — same lesson as
    Finance & Commerce's per-feed format differences in Piece 1):
      - Koochiching/Goodhue: "Minimum bid price: $809.57" — single tier,
        returned as tier2 (this IS the statutory minimum, no separate EMV
        shown).
      - Cottonwood: "1st Auction Min. $17,700.00 (Appraised Value)" /
        "2nd Auction Min. $2,826.60 (Sum of Taxes, Penalties & Costs)" —
        genuine dual-tier, same EMV/minimum-bid structure the handoff
        described for MNBid specifically; Public Surplus has it too for
        at least this county.
      - Sibley: "Estimated Market Value: $6,500.00" — single tier,
        returned as tier1 (this is the EMV, not a minimum bid).
      - Morrison: no price info in the description at all — both return
        None; the real number isn't present on this page for this county.
    """
    tier1 = None
    tier2 = None

    # Cottonwood-style dual tier
    first_match = re.search(r"1st Auction Min\.?\s*\$?([\d,]+\.\d\d)", description_text, re.IGNORECASE)
    second_match = re.search(r"2nd Auction Min\.?\s*\$?([\d,]+\.\d\d)", description_text, re.IGNORECASE)
    if first_match:
        tier1 = float(first_match.group(1).replace(",", ""))
    if second_match:
        tier2 = float(second_match.group(1).replace(",", ""))
    if tier1 is not None or tier2 is not None:
        return tier1, tier2

    # Koochiching/Goodhue-style single "Minimum bid price"
    min_bid_match = re.search(r"Minimum bid price:\s*\$?([\d,]+\.\d\d)", description_text, re.IGNORECASE)
    if min_bid_match:
        return None, float(min_bid_match.group(1).replace(",", ""))

    # Sibley-style "Estimated Market Value"
    emv_match = re.search(r"Estimated Market Value:\s*\$?([\d,]+\.\d\d)", description_text, re.IGNORECASE)
    if emv_match:
        return float(emv_match.group(1).replace(",", "")), None

    return None, None


def parse_auction_dates(html: str) -> tuple[str | None, str | None]:
    """Best-effort extraction of the 'Auction Started' / 'Auction Ends'
    date strings. Kept as raw strings (not parsed to datetime) since the
    format includes a timezone abbreviation (e.g. 'MDT') that varies."""
    started = None
    ends = None
    started_match = re.search(
        r"Auction Started\s*</div>\s*<div[^>]*>\s*([A-Za-z]+ \d{1,2}, \d{4} [\d:]+ [AP]M \w+)",
        html,
    )
    if started_match:
        started = started_match.group(1).strip()
    ends_match = re.search(
        r"Auction Ends\s*</div>\s*<div>\s*([A-Za-z]+ \d{1,2}, \d{4} [\d:]+ [AP]M \w+)",
        html,
    )
    if ends_match:
        ends = ends_match.group(1).strip()
    return started, ends


def check_buildability(description_text: str) -> tuple[bool, list[dict]]:
    """Returns (buildable, notes). Defaults to buildable=True; only flips
    to False on an exact match against BUILDABILITY_PHRASES, per the rule
    against guessing — under-flagging is safer than over-flagging. A
    single listing can match more than one category (e.g. both floodplain
    and water-access-only) — all matches are recorded, not just the first.

    KNOWN LIMITATION: plain substring matching has no negation awareness.
    A listing phrased as "no known floodplain issues" would still match
    "floodplain" and get incorrectly excluded. No real listing with this
    phrasing has been seen yet — if one turns up, that's the concrete
    case to build negation-handling around, rather than guessing at it
    now."""
    notes = []
    text_lower = description_text.lower()
    for entry in BUILDABILITY_PHRASES:
        if entry["phrase"] in text_lower:
            notes.append({
                "phrase": entry["phrase"],
                "category": entry["category"],
                "matched_from": "listing_description",
            })
    return (len(notes) == 0), notes


def parse_detail_page(html: str, auction_id: str) -> dict:
    """Parses a single listing's detail page. Returns a dict of fields to
    merge into the category-page record. Detail-page HTML is much larger
    and messier than the category page, so this sticks to targeted regex
    extraction rather than trying to fully model the DOM."""
    soup = BeautifulSoup(html, "html.parser")

    # The description lives in <section class="description">; pull all
    # its text for both parsing and raw storage.
    description_section = soup.select_one("section.description")
    description_text = description_section.get_text(separator=" ", strip=True) if description_section else ""

    minimum_bid_tier1, minimum_bid_tier2 = parse_minimum_bid_price(description_text)
    date_listed, date_closes = parse_auction_dates(html)
    buildable, buildability_notes = check_buildability(description_text)
    county_from_description = parse_county_from_description(description_text)

    # Current price on the detail page uses id="val_{auction_id}" (no
    # "catList"/"catGrid" suffix — that's the category-page id pattern).
    current_bid = None
    price_el = soup.select_one(f"#val_{auction_id}")
    if price_el:
        price_text = price_el.get_text(strip=True).replace("$", "").replace(",", "")
        try:
            current_bid = float(price_text)
        except ValueError:
            pass

    return {
        "current_bid": current_bid,
        "tier1_price": minimum_bid_tier1,
        "tier2_minimum_bid": minimum_bid_tier2,
        "date_listed": date_listed,
        "date_closes": date_closes,
        "buildable": buildable,
        "buildability_notes": buildability_notes,
        "county": county_from_description,
        "description_text": description_text[:2000],  # cap length for raw storage
    }


def fetch_detail_page(state: str, auction_id: str) -> str | None:
    url = f"{BASE_URL}/sms/all,{state}/auction/view"
    params = {"auc": auction_id}

    for attempt in range(1, MAX_RETRIES_PER_REQUEST + 2):
        try:
            print(f"    detail {auction_id}: requesting (attempt {attempt})...", file=sys.stderr, flush=True)
            resp = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            print(f"    detail {auction_id}: attempt {attempt} failed — {e}", file=sys.stderr, flush=True)
            if attempt <= MAX_RETRIES_PER_REQUEST:
                time.sleep(2)
            else:
                return None
    return None


def get_total_count(html: str) -> int | None:
    match = re.search(r'id="totalCategoryAuctionsValue">(\d+)<', html)
    return int(match.group(1)) if match else None


def enrich_with_detail(listing: dict, state: str) -> dict:
    auction_id = listing["id"].replace("publicsurplus-", "")
    html = fetch_detail_page(state, auction_id)
    if html is None:
        return listing  # keep category-page data; detail fetch failed

    detail = parse_detail_page(html, auction_id)

    listing["price"]["current_bid"] = detail["current_bid"] if detail["current_bid"] is not None else listing["price"]["current_bid"]
    listing["price"]["tier1_emv_price"] = detail["tier1_price"]
    listing["price"]["tier2_minimum_price"] = detail["tier2_minimum_bid"]
    # "minimum_bid" mirrors tier2 when present (that's the actual floor
    # price to compare against assessed value); left null for Sibley-style
    # listings that only give an EMV with no separate minimum.
    listing["price"]["minimum_bid"] = detail["tier2_minimum_bid"]
    listing["price"]["price_basis"] = (
        "self_contained_dual_tier"
        if detail["tier1_price"] is not None and detail["tier2_minimum_bid"] is not None
        else "minimum_bid_only"
    )

    listing["date_listed"] = detail["date_listed"]
    listing["date_closes"] = detail["date_closes"]
    listing["buildable"] = detail["buildable"]
    listing["buildability_notes"] = detail["buildability_notes"]

    # Fall back to description-derived county if the title-based parse
    # (done at category-page scrape time) came up empty.
    if listing["county"] is None and detail["county"] is not None:
        listing["county"] = detail["county"]

    listing["raw_source_data"]["description_text"] = detail["description_text"]

    return listing


def scrape_state(state: str) -> list[dict]:
    all_listings = []
    seen_ids = set()

    for page in range(MAX_PAGES_PER_STATE):
        html = fetch_page(state, page)
        if html is None:
            break

        if page == 0:
            total = get_total_count(html)
            print(f"  {state.upper()}: {total if total is not None else 'unknown'} total auctions reported", file=sys.stderr, flush=True)

        page_listings = parse_listings(html, state)
        new_listings = [l for l in page_listings if l["id"] not in seen_ids]

        if not new_listings:
            break  # no new rows — either last page or parsing found nothing

        for listing in new_listings:
            seen_ids.add(listing["id"])
        all_listings.extend(new_listings)

        time.sleep(REQUEST_DELAY_SECONDS)

    if FETCH_DETAIL_PAGES:
        print(f"  {state.upper()}: fetching {len(all_listings)} detail pages for minimum-bid price + buildability text", file=sys.stderr, flush=True)
        for i, listing in enumerate(all_listings):
            all_listings[i] = enrich_with_detail(listing, state)
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_listings


def main():
    all_listings = []

    for state in STATES:
        print(f"Scraping Public Surplus tax sale listings: {state.upper()}", file=sys.stderr, flush=True)
        state_listings = scrape_state(state)
        print(f"  {state.upper()}: {len(state_listings)} listings parsed", file=sys.stderr, flush=True)
        all_listings.extend(state_listings)
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Total: {len(all_listings)} listings across {len(STATES)} states", file=sys.stderr, flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_listings, indent=2))
    print(f"Wrote {len(all_listings)} records to {OUT_PATH}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
