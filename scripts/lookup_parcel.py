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

BUILDING DETAIL FIELDS (added 2026-08-18): confirmed via the layer's own
schema (fetched directly by the user, since this sandbox's network is
blocked from arcgis.metc.state.mn.us) that bedrooms/bathrooms do NOT
exist anywhere in this dataset — not a per-county gap, genuinely absent
from the schema. This is a *tax parcel* dataset, not a residential
listing dataset. What IS present and now pulled: lot size (both
GIS-measured and deed-recorded acreage), finished square footage, year
built, home style/dwelling type, garage, basement, heating/cooling, and
unit count. ⚠️ UNVERIFIED: the schema confirms these FIELDS exist, but
this project has not yet seen a real query response with them
POPULATED — MetroGIS's own documentation warns "not all attributes are
populated for all counties," so expect some of these to come back null
for a given property, same as EMV_TOTAL/SALE_VALUE already sometimes do.
Treat the first live run pulling these as the actual verification step,
same practice as everywhere else in this project.
"""

import sys
from datetime import datetime, timezone

import requests

PARCEL_QUERY_URL = (
    "https://arcgis.metc.state.mn.us/arcgis/rest/services/"
    "BaseLayer/Parcels/MapServer/0/query"
)
# Building-detail fields added 2026-08-18 — see module docstring for the
# "confirmed absent" bed/bath finding and the "unverified populated"
# caveat on everything below ACRES_POLY.
OUT_FIELDS = ("PIN,ST_NAME,ANUMBER,CTU_NAME,ZIP,OWNER_NAME,EMV_LAND,EMV_BLDG,EMV_TOTAL,"
              "SALE_VALUE,SALE_DATE,CO_NAME,"
              "ACRES_POLY,ACRES_DEED,FIN_SQ_FT,YEAR_BUILT,HOME_STYLE,DWELL_TYPE,"
              "GARAGE,GARAGESQFT,BASEMENT,HEATING,COOLING,NUM_UNITS")


# USPS standard suffix abbreviation -> full word. A simple "is the
# abbreviation a prefix of the full word" trick (tried first) only works
# for some suffixes (Ave/Avenue, St/Street, Dr/Drive) and silently
# breaks for others (Blvd/Boulevard, Ln/Lane, Rd/Road, Ct/Court,
# Trl/Trail, Pkwy/Parkway) — confirmed by checking all 12 suffixes this
# scraper recognizes. Matching against both known forms explicitly is
# more robust than any prefix heuristic.
SUFFIX_FULL_FORMS = {
    "AVE": "AVENUE", "ST": "STREET", "DR": "DRIVE", "BLVD": "BOULEVARD",
    "LN": "LANE", "RD": "ROAD", "WAY": "WAY", "CT": "COURT",
    "CIR": "CIRCLE", "PL": "PLACE", "TRAIL": "TRAIL", "TRL": "TRAIL",
    "PKWY": "PARKWAY", "TERRACE": "TERRACE", "TER": "TERRACE",
}


def build_where_clause(house_number: str, street_name: str, city: str | None = None,
                        suffix: str | None = None) -> str:
    """ANUMBER + ST_NAME is the primary match key. Suffix (ST_POS_TYP)
    and city (CTU_NAME) narrow further when available.

    Suffix matches against BOTH the abbreviated form we extract from
    notice titles ("Ave") AND its full-word equivalent ("Avenue"), since
    live testing confirmed the parcel dataset stores at least some
    suffixes spelled out in full — and there's no guarantee it's
    consistent across all suffix types, so matching either form is safer
    than assuming one convention."""
    street_escaped = street_name.upper().replace("'", "''")
    clause = f"ANUMBER={int(house_number)} AND UPPER(ST_NAME)='{street_escaped}'"
    if suffix:
        suffix_upper = suffix.upper().replace("'", "''")
        full_form = SUFFIX_FULL_FORMS.get(suffix_upper, suffix_upper)
        # Match multiple real-world variants: the bare abbreviation
        # ("AVE"), the abbreviation with a trailing period ("AVE." —
        # plausible since this dataset compiles records from 7 different
        # counties' own source systems, which may not format
        # consistently even though the one live-confirmed example used
        # the full spelled-out form), and the full word ("AVENUE"). A
        # trailing period on the full word isn't a realistic convention,
        # so that combination is skipped.
        candidates = {suffix_upper, f"{suffix_upper}."}
        if full_form != suffix_upper:
            candidates.add(full_form)
        or_clause = " OR ".join(f"UPPER(ST_POS_TYP)='{c}'" for c in sorted(candidates))
        clause += f" AND ({or_clause})"
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


def lookup_parcel(house_number: str, street_name: str, city: str | None = None,
                   suffix: str | None = None) -> dict | None:
    """Returns the best-matching parcel's assessed value, sale history,
    building details, and centroid coordinates, or None if no match /
    ambiguous with no way to disambiguate.

    Tries progressively looser filter combinations until one produces a
    single unambiguous match:
      1. house + street + suffix + city   (most precise)
      2. house + street + suffix          (city label may be wrong —
                                            confirmed happens in real data)
      3. house + street + city            (suffix format may not match
                                            this dataset's convention —
                                            unverified against live data
                                            as of this fix)
      4. house + street only              (last resort; only accepted
                                            if it happens to be unique)
    Stops at the first tier that returns exactly one result."""
    attempts = [
        (suffix, city),
        (suffix, None),
        (None, city),
        (None, None),
    ]
    tried = set()
    for suf, cty in attempts:
        key = (suf, cty)
        if key in tried:
            continue
        tried.add(key)
        result = _query_parcel(house_number, street_name, city=cty, suffix=suf)
        if result is not None:
            return result
    return None


def _query_parcel(house_number: str, street_name: str, city: str | None,
                   suffix: str | None = None) -> dict | None:
    where = build_where_clause(house_number, street_name, city, suffix)
    params = {
        "where": where,
        "outFields": OUT_FIELDS,
        "outSR": "4326",
        "f": "json",
        "resultRecordCount": 10,  # bumped from 5 so ambiguity logging reflects real counts, not our own cap
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
        print(f"  ambiguous match for {house_number} {street_name} "
              f"(suffix={suffix!r}, city={city!r}, {len(features)} results)", file=sys.stderr)
        return None

    feature = features[0]
    attrs = feature.get("attributes", {})
    geometry = feature.get("geometry", {})
    centroid = compute_centroid(geometry.get("rings", []))

    # Building-detail sentinel handling (found on a live run, 2026-08-18
    # — see module docstring "BUILDING DETAIL FIELDS"): Hennepin doesn't
    # populate these fields in this dataset at all, but rather than
    # returning null, it fills them with per-type placeholder "no data"
    # values — 0 for the numeric fields, "" for HOME_STYLE/DWELL_TYPE,
    # "Unknown" for GARAGE — that look like real data if taken at face
    # value ("0 finished sq ft" is misleading; the truth is "not
    # reported"). Confirmed empirically: every Hennepin record in a live
    # run showed this exact combination; every Ramsey record had real,
    # varied values instead — including cases where Ramsey legitimately
    # returns "Unknown" for BASEMENT or null for HEATING/COOLING on an
    # otherwise fully-populated record, so this check is deliberately
    # narrow (only the 4 fields that are NEVER legitimately 0/blank for
    # an occupied residential parcel) rather than blanket-nulling
    # anything that looks empty.
    unpopulated = (
        attrs.get("FIN_SQ_FT") == 0
        and (attrs.get("HOME_STYLE") or "") == ""
        and (attrs.get("DWELL_TYPE") or "") == ""
        and attrs.get("NUM_UNITS") == 0
    )

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
        # Building details — see module docstring "BUILDING DETAIL
        # FIELDS" note. acres_poly is GIS-measured from the parcel
        # boundary; acres_deed is the legally recorded deed acreage —
        # they can differ (surveying, easements, rounding); expose both
        # rather than picking one. Neither needs sentinel handling —
        # acres_poly is populated for every county observed so far;
        # acres_deed is a genuine null (not a sentinel) when absent.
        "acres_poly": attrs.get("ACRES_POLY"),
        "acres_deed": attrs.get("ACRES_DEED"),
        # year_built is NOT gated — confirmed independently populated
        # for Hennepin even when everything else is blank, so it needs
        # no sentinel translation.
        "year_built": attrs.get("YEAR_BUILT"),
        # basement/heating/cooling ARE gated by `unpopulated`, same as
        # the fields below — correction from an earlier version of this
        # fix: Hennepin's basement="Unknown"/heating=""/cooling="" ARE
        # part of the same blanket sentinel pattern as HOME_STYLE etc.
        # (confirmed: real Hennepin data shows this exact combination).
        # The wrinkle: "Unknown" for basement can ALSO be a genuine
        # answer on an otherwise fully-populated Ramsey record (real
        # example found: Castillo, fin_sq_ft/home_style real,
        # basement="Unknown") — gating by the record's own `unpopulated`
        # status (not the field's own value) correctly keeps that case
        # as real data while still nulling Hennepin's blanket sentinel.
        "basement": None if unpopulated else attrs.get("BASEMENT"),
        "heating": None if unpopulated else (attrs.get("HEATING") or None),
        "cooling": None if unpopulated else (attrs.get("COOLING") or None),
        "fin_sq_ft": None if unpopulated else attrs.get("FIN_SQ_FT"),
        "home_style": None if unpopulated else (attrs.get("HOME_STYLE") or None),
        "dwell_type": None if unpopulated else (attrs.get("DWELL_TYPE") or None),
        "garage": None if unpopulated else attrs.get("GARAGE"),
        "garage_sqft": None if unpopulated else attrs.get("GARAGESQFT"),
        # Number of units on the parcel — potentially useful signal for
        # the known multi-unit-condo-building match limitation (see
        # HANDOFF.md "801 Washington Lofts" note): a high NUM_UNITS on an
        # otherwise-ambiguous match might explain WHY it's ambiguous,
        # even though this doesn't change the matching logic itself.
        "num_units": None if unpopulated else attrs.get("NUM_UNITS"),
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
