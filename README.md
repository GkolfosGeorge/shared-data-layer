# Shared Data Layer

Common infrastructure shared across the trading scanner repos (mean-reversion,
trend-following, and future strategies): ticker universe lookup, sector
classification, regime detection, technical signal building blocks, the
OHLCV data engine, and raw-data scanners consumed by the daily archivers.

## What's here

```
ticker_provider.py     — current + point-in-time index constituent lists
sector_lookup.py       — ticker -> sector/industry classification
regime_detector.py     — market regime classification (VIX, breadth, etc.)
signals.py             — shared technical indicator building blocks
data_engine.py          — OHLCV download/cache/cleaning pipeline
membership_local.py    — reads a static membership CSV (no DB needed)
db.py                  — Postgres connection (maintenance-only, see below)
export_membership_to_csv.py — one-off export: DB -> data/sp500_membership.csv
schema.sql              — Postgres schema (maintenance-only)
options_scanner.py     — options chain scan (PCR, OI, IV) + raw strike-level capture
options_archive.py     — Parquet persistence for the raw options chain
news_scanner.py         — raw company news fetch (Finnhub, ticker-tagged)
news_archive.py         — Parquet persistence for raw news, with cross-day dedup
```

## Design note: no live database at runtime

`data_engine.py` gets its point-in-time index membership (which tickers were
in the S&P 500 on any given historical date, including delisted ones) from a
static CSV via `membership_local.py` — not from a live Postgres query. This
means every consumer repo (this package's dependents) works with zero
database setup: `pip install`, drop a `data/sp500_membership.csv` in the
working directory, done.

`db.py` and `export_membership_to_csv.py` exist purely to *produce* that CSV
from Postgres occasionally (e.g. after an S&P 500 reconstitution) — nothing
at runtime imports them. Install the `maintenance` extra only if you need to
refresh the CSV yourself:
```bash
pip install "trading-shared-data[maintenance] @ git+https://github.com/USERNAME/shared-data-layer.git"
python export_membership_to_csv.py
```

## Design note: raw scanners produce data, they don't decide anything

`options_scanner.py` and `news_scanner.py` are intentionally "dumb" — they
fetch and structure raw data (full options chain per strike, raw news
headlines/summaries per ticker) and hand it to their matching `*_archive.py`
module for Parquet persistence. Neither module scores, ranks, or judges the
data (no sentiment scoring, no PCR-based buy/sell signal is *required* to
use the archive — `options_scanner.py` happens to also compute a PCR signal
for ad-hoc reports, but the archived data is always the untouched raw chain).

This separation matters because scoring methodology changes over time (a
better sentiment model next year, a refined PCR threshold, etc.) — the raw
archive must stay independent of any single scoring method so it can always
be re-scored later without having lost anything.

## Using this from another repo (mean-reversion, trend, daily-market-data, ...)

Add to that repo's `requirements.txt`:
```
trading-shared-data @ git+https://github.com/USERNAME/shared-data-layer.git@<tag>
```
Then in that repo's own `data/` folder, place a `sp500_membership.csv`
(copy it from wherever you last exported it — it changes only a few times a
year, so this is a rare manual step, not an ongoing sync burden).

Existing code in dependent repos needs no import changes — `ticker_provider`,
`sector_lookup`, `regime_detector`, `signals`, `data_engine`,
`options_scanner`, `options_archive`, `news_scanner`, `news_archive` all
install as top-level modules:
```python
from ticker_provider import get_tickers
from data_engine import download_universe_data
from news_scanner import NewsScanner
```

`news_scanner.py` requires a `FINNHUB_API_KEY` environment variable at
runtime (free tier at finnhub.io) — set it as a repo secret in any consuming
CI workflow, or as a local env var for manual runs.

## Local development (editable install)

While actively developing across repos, install this package in editable
mode so changes here are picked up immediately without re-pushing:
```bash
pip install -e /path/to/local/clone/shared-data-layer
```
Push to GitHub only when you want the *other* repos (that installed via the
git URL, not `-e`) to pick up the change — they'd need to
`pip install --upgrade` to see it.

## Bumping the version

Whenever a change here is *functional* (new module, changed signature,
behavior change), run `./bump_version.sh <version> "<message>"` from inside
this repo — it updates `pyproject.toml`, commits, tags, and pushes. Then
update the pinned tag in every dependent repo's `requirements.txt` and
reinstall. Documentation-only changes (like this README) don't need a
version bump — a plain commit + push is enough.
