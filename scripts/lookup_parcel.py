"""
Looks up a property's county-assessed value, last sale info, and
coordinates from the Metropolitan Council's regional parcel dataset —
one ArcGIS REST API covering all 7 metro counties (Hennepin, Ramsey,
Dakota, Anoka, Washington, Scott, Carver), confirmed live and populated
with real data (EMV_TOTAL, SALE_VALUE, OWNER_NAME, etc.) via a manual
test query before this was built.

Takes the address fields scrape_finance_commerce_realestate.py already
extracts (house_number, street_name, city, zip) and queries by
ANUMBER + ST_NAME, since the parcel dataset's ST_NAME field stores just
the base street name with no suffix ("Juneau", not "Juneau Ln") —
confirmed from the real sample response.

The layer does NOT support server-side centroids (confirmed in its own
schema: "Supports Returning Geometry Centroid: false"), so this computes
a simple vertex-average centroid client-side — a reasonable approximation
for placing a single map pin per parcel, not a survey-grade centroid.
"""

import sys
from datetime import datetime, timezone

import requests

PARCEL_QUERY_URL = (
    "https://arcgis.metc.state.mn.us/arcgis/rest/services/"
    "BaseLayer/Parcels/MapServer/0/query"
)
OUT_FIELDS = "PIN,ST_NAME,ANUMBER,CTU_NAME,ZIP,OWNER_NAME,EMV_LAND,EMV_BLDG,EMV_TOTAL,SALE_VALUE,SALE_DATE,CO_NAME"


def build_where_clause(house_number: str, street_name: str, city: str | None = None) -> str:
    """ANUMBER + ST_NAME is the primary match key. City (CTU_NAME) is
    added when available to disambiguate — the same street name can
    exist in multiple metro cities."""
    street_escaped = street_name.upper().replace("'", "''")  # basic SQL-injection guard
    clause = f"ANUMBER={int(house_number)} AND UPPER(ST_NAME)='{street_escaped}'"
    if city:
        city_escaped = city.upper().replace("'", "''")
        clause += f" AND UPPER(CTU_NAME)='{city_escaped}'"
    return clause


def compute_centroid(rings: list) -> tuple[float, float] | None:
    """Simple vertex-average centroid — adequate for placing a map pin,
    not a true area-weighted centroid. rings is the ArcGIS polygon
    'rings' structure: a list of rings, each a list of [x, y] points."""
    if not rings:
        return None
    all_points = [pt for ring in rings for pt in ring]
    if not all_points:
        return None
    avg_x = sum(p[0] for p in all_points) / len(all_points)
    avg_y = sum(p[1] for p in all_points) / len(all_points)
    return (avg_x, avg_y)  # (lng, lat) once outSR=4326 is used in the query


def parse_sale_date(epoch_ms: int | None) -> str | None:
    """SALE_DATE comes back as Unix epoch milliseconds (confirmed from
    the real sample: 1556668800000). Converts to YYYY-MM-DD, or None if
    no recorded sale (some parcels — e.g. never-sold institutional
    property — have SALE_DATE: null in real data)."""
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def lookup_parcel(house_number: str, street_name: str, city: str | None = None) -> dict | None:
    """Returns the best-matching parcel's assessed value, sale history,
    and centroid coordinates, or None if no match / ambiguous match with
    no way to disambiguate.

    Two-tier lookup: tries the city-filtered query first (precise), and
    if that comes back empty, retries WITHOUT the city filter — but only
    accepts the result if it's a single unambiguous match. This exists
    because live testing found the city label in F&C's own notice titles
    is sometimes wrong: the exact same house number + street successfully
    matched under one city in one posting and failed under a different
    (incorrect) city in another posting for what was clearly the same
    underlying property. Requiring an exact city match was rejecting
    real matches, not just preventing wrong ones — so city becomes a
    disambiguator of last resort, not a hard requirement."""
    result = _query_parcel(house_number, street_name, city)
    if result is not None:
        return result
    if city:
        # city filter may have been wrong — retry without it, but only
        # accept if unambiguous
        return _query_parcel(house_number, street_name, city=None)
    return None


def _query_parcel(house_number: str, street_name: str, city: str | None) -> dict | None:
    where = build_where_clause(house_number, street_name, city)
    params = {
        "where": where,
        "outFields": OUT_FIELDS,
        "outSR": "4326",
        "f": "json",
        "resultRecordCount": 5,
    }
    try:
        resp = requests.get(PARCEL_QUERY_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  parcel lookup failed for {house_number} {street_name}: {e}", file=sys.stderr)
        return None

    features = data.get("features", [])
    if not features:
        return None
    if len(features) > 1:
        # ambiguous — don't guess which one, regardless of whether city
        # was applied this call
        print(f"  ambiguous match for {house_number} {street_name} "
              f"(city={city!r}, {len(features)} results)", file=sys.stderr)
        return None

    feature = features[0]
    attrs = feature.get("attributes", {})
    geometry = feature.get("geometry", {})
    centroid = compute_centroid(geometry.get("rings", []))

    return {
        "pin": attrs.get("PIN"),
        "owner_name": attrs.get("OWNER_NAME"),
        "emv_land": attrs.get("EMV_LAND"),
        "emv_bldg": attrs.get("EMV_BLDG"),
        "emv_total": attrs.get("EMV_TOTAL"),
        "sale_value": attrs.get("SALE_VALUE"),
        "sale_date": parse_sale_date(attrs.get("SALE_DATE")),
        "county": attrs.get("CO_NAME"),
        "lng": centroid[0] if centroid else None,
        "lat": centroid[1] if centroid else None,
    }


def score_undervalued(mortgage_amount: float, parcel: dict) -> dict:
    """Compares mortgage debt to assessed value — the actual 'undervalued'
    signal the user asked for. Positive equity_estimate means the
    property is assessed as worth more than what's owed on it (more
    margin for a buyer); a low equity_ratio flags something closer to
    underwater. This is a straightforward arithmetic comparison, not a
    prediction — MN assessed values are the trusted number here, not an
    estimate this tool is making up."""
    emv_total = parcel.get("emv_total")
    if not emv_total or not mortgage_amount:
        return {"equity_estimate": None, "equity_ratio": None}
    equity_estimate = emv_total - mortgage_amount
    equity_ratio = round(emv_total / mortgage_amount, 2) if mortgage_amount else None
    return {
        "equity_estimate": round(equity_estimate, 2),
        "equity_ratio": equity_ratio,
    }
