# Twin Cities Storage Scout — name data

This repo's only job is running `scripts/scrape_startribune.py` once a day
(via `.github/workflows/daily-scrape.yml`) and committing the result to
`data/names.json`.

That file gets fetched directly by the **Twin Cities Storage Scout** HTML
app (a separate, single-file tool you run locally) every time you click
its Refresh button — `raw.githubusercontent.com` serves files with open
CORS headers, so a browser can read it directly, unlike Star Tribune's own
site.

## First-time setup

After uploading these files, go to the **Actions** tab → click into
"Daily Star Tribune scrape" → **Run workflow** to generate the first real
`data/names.json`. After that one manual kick-off, it runs itself daily —
nothing more to do here.

## Checking it's working

Actions tab → most recent run → green checkmark = success. Click into it
to see the log (how many notices found, how many names parsed). If it's
failing, the log will show why — most likely Star Tribune changed their
page structure, which would need `scripts/scrape_startribune.py` updated
to match.
