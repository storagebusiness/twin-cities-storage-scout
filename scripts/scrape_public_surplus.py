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
    real captured data: 'PID 55-070-01000', 'Parcel 36.036.0300'."""
    match = re.search(r"PID\s+([\d\-]+)", title)
    if match:
        return match.group(1)
    match = re.search(r"Parcel\s+([\d.]+)", title)
    if match:
        return match.group(1)
    return None


def looks_like_address(title: str) -> bool:
    """Heuristic: titles like '14 24th ST, WINDOM' start with a number and
    contain a comma. Titles like 'Koochiching County Tax-Forfeiture...' or
    'Sale 2601: Tax Forfeited Land...' don't."""
    return bool(re.match(r"^\d+\s+\S+.*,", title))


def fetch_page(state: str, page: int) -> str | None:
    url = f"{BASE_URL}/sms/all,{state}/browse/cataucs"
    params = {"catid": CATID, "page": page, "slth": "y", "sortBy": "timeLeft", "sortDesc": "N"}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        print(f"  {state.upper()} page {page}: request failed — {e}", file=sys.stderr)
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


def get_total_count(html: str) -> int | None:
    match = re.search(r'id="totalCategoryAuctionsValue">(\d+)<', html)
    return int(match.group(1)) if match else None


def scrape_state(state: str) -> list[dict]:
    all_listings = []
    seen_ids = set()

    for page in range(MAX_PAGES_PER_STATE):
        html = fetch_page(state, page)
        if html is None:
            break

        if page == 0:
            total = get_total_count(html)
            print(f"  {state.upper()}: {total if total is not None else 'unknown'} total auctions reported", file=sys.stderr)

        page_listings = parse_listings(html, state)
        new_listings = [l for l in page_listings if l["id"] not in seen_ids]

        if not new_listings:
            break  # no new rows — either last page or parsing found nothing

        for listing in new_listings:
            seen_ids.add(listing["id"])
        all_listings.extend(new_listings)

        time.sleep(REQUEST_DELAY_SECONDS)

    return all_listings


def main():
    all_listings = []

    for state in STATES:
        print(f"Scraping Public Surplus tax sale listings: {state.upper()}", file=sys.stderr)
        state_listings = scrape_state(state)
        print(f"  {state.upper()}: {len(state_listings)} listings parsed", file=sys.stderr)
        all_listings.extend(state_listings)
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Total: {len(all_listings)} listings across {len(STATES)} states", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_listings, indent=2))
    print(f"Wrote {len(all_listings)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
