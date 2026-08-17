"""
Fetches Star Tribune's Storage legal-notices category and parses the
free-text postings into structured records: (renter_name, contents_text,
facility_address, auction_date). Runs server-side via GitHub Actions
(see .github/workflows/daily-scrape.yml) — CORS restrictions only apply
to browser JavaScript, not to a script running on GitHub's servers, which
is the whole reason this piece moved here instead of running in-browser.

Writes data/names.json, committed back to the repo by the workflow. The
Twin Cities Storage Scout HTML app fetches that file directly from
raw.githubusercontent.com (which serves with open CORS headers) every
time you click Refresh.
"""

import json
import re
import sys
from pathlib import Path

import requests

CATEGORY_URL = "https://classifieds.startribune.com/mn/storage/search"
OUT_PATH = Path(__file__).parent.parent / "data" / "names.json"

FACILITY_BLOCK_RE = re.compile(
    r"Facility\s+(\d+):\s*#?[\w-]*\s*[-–]?\s*"
    r"(?P<address_block>.+?(?:MN|Minnesota)\s*\d{0,5}(?:-\d{4})?)\s*"
    r"(?:on\s+)?(?P<date>[A-Z][a-z]+\.?\s+\d{1,2},?\s*20\d{2})",
    re.IGNORECASE,
)
NAME_CONTENTS_RE = re.compile(
    r"([A-Z][a-zA-Z'\.-]+(?:\s+[A-Z][a-zA-Z'\.-]+){1,3}),\s*([^;]+?)(?=;|$)",
)
LEADING_TIME_RE = re.compile(r"^\s*,?\s*at\s+\d{1,2}:\d{2}\s*[AP]M\.?\s*", re.IGNORECASE)
TRAILING_BOILERPLATE_RE = re.compile(
    r"(The auction will be (listed|held)|Purchases must be made|"
    r"Sale to be held|Public sale terms|By PS Retail|NOTICE OF PUBLIC SALE|"
    r"Extra Space Storage may refuse).*",
    re.IGNORECASE | re.DOTALL,
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


def parse_notice_text(text: str, source_url: str = "") -> list[dict]:
    records = []
    blocks = list(FACILITY_BLOCK_RE.finditer(text))

    if blocks:
        for i, m in enumerate(blocks):
            address_block = m.group("address_block").strip()
            date = m.group("date").strip()
            start = m.end()
            end = blocks[i + 1].start() if i + 1 < len(blocks) else len(text)
            names_segment = text[start:end]
            names_segment = LEADING_TIME_RE.sub("", names_segment)
            names_segment = TRAILING_BOILERPLATE_RE.sub("", names_segment)
            for name_m in NAME_CONTENTS_RE.finditer(names_segment):
                name, contents = name_m.groups()
                records.append({
                    "renter_name": name.strip(),
                    "contents_text": contents.strip().rstrip("."),
                    "facility_address_raw": address_block,
                    "facility_address_normalized": normalize_address(address_block),
                    "auction_date": date,
                    "source_url": source_url,
                })
    else:
        addr_m = re.search(
            r"\d{2,6}\s+[\w\s]+?(?:Ave|St|Dr|Blvd|Ln|Rd|Road|Street|Avenue)"
            r"[\w\s,]*?MN\s*\d{5}",
            text, re.IGNORECASE,
        )
        address_block = addr_m.group(0).strip() if addr_m else "UNKNOWN"
        date_m = re.search(r"[A-Z][a-z]+\.?\s+\d{1,2},?\s*20\d{2}", text)
        date = date_m.group(0) if date_m else ""
        for name_m in NAME_CONTENTS_RE.finditer(text):
            name, contents = name_m.groups()
            records.append({
                "renter_name": name.strip(),
                "contents_text": contents.strip().rstrip("."),
                "facility_address_raw": address_block,
                "facility_address_normalized": normalize_address(address_block),
                "auction_date": date,
                "source_url": source_url,
            })
    return records


def main():
    session = requests.Session()
    session.headers.update({
        # A realistic browser UA — some sites serve a leaner page to
        # non-browser clients even without a hard bot-detection block.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    resp = session.get(CATEGORY_URL, timeout=20)
    resp.raise_for_status()

    # DEBUG: print what we actually got back, so a zero-notices run tells
    # us why instead of just "0 found". Remove once this is reliably working.
    print(f"DEBUG: response length = {len(resp.text)} chars", file=sys.stderr)
    print(f"DEBUG: first 500 chars:\n{resp.text[:500]}", file=sys.stderr)
    print(f"DEBUG: contains 'classifieds.startribune.com/mn/storage/'? "
          f"{'classifieds.startribune.com/mn/storage/' in resp.text}", file=sys.stderr)

    urls = sorted(set(re.findall(
        r'href="(https://classifieds\.startribune\.com/mn/storage/[^"]+)"', resp.text,
    )))
    print(f"Found {len(urls)} storage notices", file=sys.stderr)

    all_records = []
    for url in urls:
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            text = re.sub(r"<[^>]+>", "\n", r.text)
            text = re.sub(r"\n{2,}", "\n", text)
            records = parse_notice_text(text, source_url=url)
            all_records.extend(records)
            print(f"  {url}: {len(records)} records", file=sys.stderr)
        except Exception as e:
            print(f"  {url}: FAILED ({e})", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_records, indent=2))
    print(f"Wrote {len(all_records)} records to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
