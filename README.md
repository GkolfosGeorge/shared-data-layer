# Shared Data Layer

Common infrastructure shared across the trading scanner repos (mean-reversion,
trend-following, and future strategies): ticker universe lookup, sector
classification, regime detection, technical signal building blocks, and the
OHLCV data engine.

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

## Using this from another repo (mean-reversion, trend, hype-script, ...)

Add to that repo's `requirements.txt`:
```
trading-shared-data @ git+https://github.com/USERNAME/shared-data-layer.git
```
Then in that repo's own `data/` folder, place a `sp500_membership.csv`
(copy it from wherever you last exported it — it changes only a few times a
year, so this is a rare manual step, not an ongoing sync burden).

Existing code in dependent repos needs no import changes — `ticker_provider`,
`sector_lookup`, `regime_detector`, `signals`, `data_engine` all install as
top-level modules, exactly like today:
```python
from ticker_provider import get_tickers
from data_engine import download_universe_data
```

## Local development (editable install)

While actively developing across repos, install this package in editable
mode so changes here are picked up immediately without re-pushing:
```bash
pip install -e /path/to/local/clone/shared-data-layer
```
Push to GitHub only when you want the *other* repos (that installed via the
git URL, not `-e`) to pick up the change — they'd need to
`pip install --upgrade` to see it.
