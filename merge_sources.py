"""
Combines each source's individual output file (startribune_names.json,
finance_commerce_names.json, ...) into the single data/names.json that
the Twin Cities Storage Scout app actually fetches from GitHub.

Adding a new source later just means: write a new scrape_X.py that
outputs to data/X_names.json, then add that filename to SOURCE_FILES
below and to the workflow's "Scrape" steps.
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
SOURCE_FILES = [
    "startribune_names.json",
    "finance_commerce_names.json",
]
OUT_PATH = DATA_DIR / "names.json"


def main():
    combined = []
    for filename in SOURCE_FILES:
        path = DATA_DIR / filename
        if not path.exists():
            print(f"  {filename}: not found, skipping")
            continue
        records = json.loads(path.read_text())
        print(f"  {filename}: {len(records)} records")
        combined.extend(records)

    OUT_PATH.write_text(json.dumps(combined, indent=2))
    print(f"Wrote {len(combined)} total records to {OUT_PATH}")


if __name__ == "__main__":
    main()
