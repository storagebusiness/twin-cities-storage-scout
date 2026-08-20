"""
Scrapes MNBid.mn.gov's Real Estate category (category id 353) for
tax-forfeited land listings. MNBid is a Minnesota state-run platform —
unlike Public Surplus, it is MN-only, so this script does not take a
state parameter.

IMPORTANT — UNTESTED AGAINST THE LIVE SITE: this sandbox's network is
blocked from minnbidapi-prod.ecommerce.auction. The request payload and
response-parsing logic below are built from a real captured request and
response (2026-08-19, category=353, 9 real records: Carlton, Kittson, and
Lyon county listings). Run this via GitHub Actions or locally and report
back any errors or empty results before trusting the output — per the
project's standing rule against guessing at live-site behavior blind.

Key finding confirmed from real data: the top-level `sprice` field
reliably equals the Phase-1 EMV/appraised price stated inside `desc_proc`
(checked against 7 listings, exact match every time) — this is a more
reliable source for tier1 than parsing HTML, so tier1_emv_price always
comes from `sprice`, not from desc_proc parsing.

The Phase-2 minimum-bid price is NOT available as a top-level field —
it only appears inside `desc_proc`, and `desc_proc` has been confirmed to
use at least THREE different formats across counties (matching the same
"don't assume consistency" lesson already learned for Finance & Commerce
and Public Surplus):
  - Carlton County: plain <p> tags, legal description only, NO price info
    at all in the description.
  - Kittson County: HTML <table> with rows "Minimum Bid (EMV Sale)" /
    "Minimum Bid (Minimum Bid Sale)".
  - Lyon County: a different <table> structure — "1st Sale Estimated
    market value sale" / "2nd Sale if not sold Minimum Bid".

Partial `region` code decode confirmed from real data:
  361 = Carlton, 357 = Kittson, 362 = Lyon
Not all region codes are known — this list should only be extended from
real captured responses, never guessed.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_PATH = DATA_DIR / "mnbid_taxforfeited.json"

BASE_URL = "https://minnbidapi-prod.ecommerce.auction/api/auction/search"
REAL_ESTATE_CATEGORY_ID = "353"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
}

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 20
MAX_RETRIES_PER_REQUEST = 2
REQUEST_DELAY_SECONDS = 1
PAGE_LIMIT = 30  # matches the real captured payload's "limit": 30
MAX_PAGES = 20  # safety cap

# Confirmed real region-code -> county mappings. Extend ONLY from real
# captured responses (a listing's title/desc_proc naming the county),
# never guessed — the handoff explicitly flagged this field as unresolved.
REGION_CODE_TO_COUNTY = {
    361: "Carlton",
    357: "Kittson",
    362: "Lyon",
}

# The exact real captured request payload shape, with `page` and
# `filters.category.value` as the only parts we vary.
BASE_PAYLOAD = {
    "limit": PAGE_LIMIT,
    "page": 1,
    "prev_page": 1,
    "orderby": "p.date_closed, asc",
    "order": "",
    "auctionView": "Grid",
    "filters": {
        "category": {"value": [REAL_ESTATE_CATEGORY_ID], "type": "array", "field": "it.category"},
        "searchbar": {"value": "", "type": "like", "field": "p.title,p.desc_proc,p.id,it.manufacturer,it.model_no"},
        "condition": {"value": [], "type": "array", "field": "it.conditionTypeId"},
        "location_id": {"value": [], "type": "array", "field": "p.location_id"},
        "region": {"value": [], "type": "array", "field": "it.region"},
        "price": {"value": 0, "type": "greaterequal", "field": "p.wprice"},
        "auctionid": {"value": [], "type": "array", "field": "p.auction"},
        "buynowid": {"value": [], "type": "array", "field": "p.buynow"},
        "min_price": {"value": 0, "type": "greaterequal", "field": "p.wprice"},
        "agency_address": {"value": [], "type": "array", "field": "p.agencyaddress_id"},
        "lot_id": {"value": "", "type": "like", "field": "p.id"},
        "max_price": {"value": 0, "type": "smallerequal", "field": "p.wprice"},
        "live_auction": {"value": 1, "type": "notin", "field": "it.live_auction"},
        "closing_date": {"value": "", "type": "dateequal", "field": "p.date_closed"},
    },
    "having": {
        "future_active": {"value": 0, "type": "in", "field": "future_active"},
    },
}


def parse_county_from_title(title: str) -> str | None:
    """Confirmed real title formats: 'CARLTON COUNTY PARCEL ID# ...',
    'LYON COUNTY PARCEL ID:\\t...'. Titles without a county name (plain
    street addresses, e.g. '603 MINNESOTA STREET DONALDSON, MN 56720')
    return None — county comes from desc_proc or region code instead for
    those."""
    match = re.match(r"^([A-Za-z\s]+?)\s+COUNTY\s+PARCEL", title, re.IGNORECASE)
    if match:
        return match.group(1).strip().title()
    return None


def parse_parcel_id_from_title(title: str) -> str | None:
    """Confirmed real formats: 'PARCEL ID# 48-230-1000, 48-230-1020'
    (only first parcel captured when multiple are listed together) and
    'PARCEL ID:\\t29-108017-1'."""
    match = re.search(r"PARCEL ID[#:]\s*([\d\-]+)", title, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_table_fields(desc_proc: str) -> dict:
    """Parses desc_proc's HTML table (when present) into a label->value
    dict, keys lowercased and whitespace-normalized. Handles both
    confirmed real table structures (Kittson-style single <td> labels,
    Lyon-style <br>-joined two-line labels) since both come out the same
    way once BeautifulSoup flattens text with a separator."""
    soup = BeautifulSoup(desc_proc, "html.parser")
    fields = {}
    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(separator=" ", strip=True).lower().rstrip(":")
        value = cells[1].get_text(separator=" ", strip=True)
        fields[label] = value
    return fields


def parse_tier2_minimum_bid(table_fields: dict) -> float | None:
    """Finds the Phase-2 minimum-bid value among table row labels,
    regardless of which confirmed real phrasing was used. Deliberately
    excludes any label containing 'emv' so the Phase-1 EMV row (matched
    via a different key) is never mistaken for the minimum bid."""
    for label, value in table_fields.items():
        if "minimum bid" in label and "emv" not in label:
            match = re.search(r"\$?([\d,]+\.\d\d)", value)
            if match:
                return float(match.group(1).replace(",", ""))
    return None


def parse_desc_proc(desc_proc: str) -> dict:
    """Extracts whatever structured fields are available from desc_proc.
    Carlton-style listings have no table at all — every field below stays
    None for those, which is correct: the source genuinely doesn't
    provide this data, not a parsing failure."""
    table_fields = extract_table_fields(desc_proc)

    tier2_minimum_bid = parse_tier2_minimum_bid(table_fields)

    address = table_fields.get("property address") or table_fields.get("address")
    city = table_fields.get("city")
    county = table_fields.get("county")
    property_type = table_fields.get("property type") or table_fields.get("category")
    lot_size = table_fields.get("lot size")
    parcel_id = (
        table_fields.get("county parcel id")
        or table_fields.get("lyon county parcel id")
        or table_fields.get("parcel id")
    )
    legal_description = table_fields.get("legal description")

    return {
        "tier2_minimum_bid": tier2_minimum_bid,
        "address": address,
        "city": city,
        "county": county,
        "property_type": property_type,
        "lot_size": lot_size,
        "parcel_id": parcel_id,
        "legal_description": legal_description,
        "has_structured_table": len(table_fields) > 0,
    }


def build_listing(record: dict) -> dict:
    record_id = record.get("id")
    title = record.get("title", "")
    desc_proc = record.get("desc_proc", "")

    desc_fields = parse_desc_proc(desc_proc)

    title_county = parse_county_from_title(title)
    title_parcel_id = parse_parcel_id_from_title(title)

    region_code = record.get("region")
    region_county = REGION_CODE_TO_COUNTY.get(region_code)

    # Prefer desc_proc's own County row (most explicit), then title-based
    # parsing, then the region-code lookup (least direct — depends on a
    # possibly-incomplete manual mapping).
    county = desc_fields["county"] or title_county or region_county

    parcel_id = desc_fields["parcel_id"] or title_parcel_id

    tier1_emv_price = record.get("sprice")  # confirmed reliable top-level field
    tier2_minimum_price = desc_fields["tier2_minimum_bid"]

    price_basis = (
        "self_contained_dual_tier"
        if tier1_emv_price is not None and tier2_minimum_price is not None
        else "minimum_bid_only"
    )

    return {
        "id": f"mnbid-{record_id}",
        "source": "mnbid",
        "state": "MN",
        "county": county,
        "listing_category": "tax_forfeited_land",

        "listing_type": {
            "owner": "state",
            "phase": "unknown",  # MNBid doesn't label current phase explicitly in this response shape
            "notes": "MNBid listing; phase not directly labeled in the search API response.",
        },

        "address": desc_fields["address"],
        "parcel_id": parcel_id,
        "coordinates": None,

        "listing_url": f"https://mnbid.mn.gov/auction/view?auc={record_id}",  # best-effort; unverified URL pattern
        "date_listed": record.get("date_added"),
        "date_closes": record.get("date_closed"),
        "status": record.get("market_status", "open"),

        "price": {
            "current_bid": record.get("current_bid"),
            "minimum_bid": tier2_minimum_price,
            "tier1_emv_price": tier1_emv_price,
            "tier2_minimum_price": tier2_minimum_price,
            "price_basis": price_basis,
        },

        "assessed_value": {
            "emv_total": None,
            "source": "mnbid_self_contained",  # no separate parcel lookup needed when tier1 present
            "lookup_status": "not_needed" if tier1_emv_price is not None else "not_yet_attempted",
        },

        "buildable": True,  # no buildability text source identified for MNBid yet — see gaps note
        "buildability_notes": [],

        "area_wealth": {
            "census_acs_median_income": None,
            "census_tract": None,
        },

        "raw_source_data": {
            "title": title,
            "region_code": region_code,
            "property_type": desc_fields["property_type"],
            "lot_size": desc_fields["lot_size"],
            "legal_description": desc_fields["legal_description"],
            "has_structured_table": desc_fields["has_structured_table"],
            "desc_proc_raw": desc_proc[:2000],
        },
    }


def fetch_page(page: int) -> dict | None:
    payload = json.loads(json.dumps(BASE_PAYLOAD))  # cheap deep copy
    payload["page"] = page
    payload["prev_page"] = page

    for attempt in range(1, MAX_RETRIES_PER_REQUEST + 2):
        try:
            print(f"  page {page}: requesting (attempt {attempt})...", file=sys.stderr, flush=True)
            resp = requests.post(
                BASE_URL,
                json=payload,
                headers=HEADERS,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  page {page}: attempt {attempt} failed — {e}", file=sys.stderr, flush=True)
            if attempt <= MAX_RETRIES_PER_REQUEST:
                time.sleep(2)
            else:
                return None
    return None


def main():
    all_listings = []
    total_records = None

    for page in range(1, MAX_PAGES + 1):
        response = fetch_page(page)
        if response is None:
            print(f"  page {page}: giving up after retries, stopping pagination", file=sys.stderr, flush=True)
            break

        try:
            response_data = response["data"]["responseData"]
            records = response_data["records"]
            total_records = response_data.get("totalRecords")
        except (KeyError, TypeError) as e:
            print(f"  page {page}: unexpected response shape ({e}), stopping", file=sys.stderr, flush=True)
            break

        if page == 1:
            print(f"  total records reported: {total_records}", file=sys.stderr, flush=True)

        if not records:
            break  # no more pages

        for record in records:
            all_listings.append(build_listing(record))

        if total_records is not None and len(all_listings) >= total_records:
            break

        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"Total: {len(all_listings)} MNBid Real Estate listings parsed", file=sys.stderr, flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_listings, indent=2))
    print(f"Wrote {len(all_listings)} records to {OUT_PATH}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
