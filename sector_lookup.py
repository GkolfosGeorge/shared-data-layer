# sector_lookup.py
"""
Sector Lookup — lightweight, standalone sector classification.
────────────────────────────────────────────────────────────────
This is NOT fundamentals analysis. It doesn't score anything or judge
company quality. It's just a static classification lookup (GICS sector via
yfinance) used ONLY for portfolio diversification (max_per_sector) in
scorer_mr.py — nothing else.

Kept separate from fundamentals.py because scorer_mr.py (the main, purely
technical script) must have NO dependency on fundamentals data — not even
for hard filters. fundamentals.py and the fundamental-weighted scorer are a
separate, future project.

Usage:
    from sector_lookup import get_sectors

    sectors = get_sectors(["AAPL", "MSFT", "XOM"])
    # -> {"AAPL": "Technology", "MSFT": "Technology", "XOM": "Energy"}
"""

import json
import time
from pathlib import Path

import yfinance as yf

DEFAULT_MAX_AGE_HOURS = 24 * 30   # 30 days — a company's sector almost never changes
UNKNOWN_SECTOR        = "Unknown"


def get_sectors(
    tickers:       list[str],
    folder_path:   str = "data",
    universe_name: str = "sp500",
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> dict[str, str]:
    """
    Returns {ticker: sector} with local caching.
    No retry-heavy logic — if a ticker fails, it's marked "Unknown" and
    doesn't block anything (diversification just ignores it / puts it in
    its own sector bucket).
    """
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    cache_path = folder / f"sectors_{universe_name}.json"

    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}

    cached_at = cache.get("_cached_at", 0)
    sectors   = dict(cache.get("sectors", {}))
    is_stale  = (time.time() - cached_at) / 3600 > max_age_hours

    missing = [t for t in tickers if t not in sectors]
    to_fetch = missing if not is_stale else tickers

    if not to_fetch:
        return {t: sectors.get(t, UNKNOWN_SECTOR) for t in tickers}

    print(f"🔄 Sector lookup for {len(to_fetch)} tickers...")
    for i, ticker in enumerate(to_fetch):
        try:
            info = yf.Ticker(ticker).info
            sectors[ticker] = info.get("sector") or UNKNOWN_SECTOR
        except Exception:
            sectors[ticker] = sectors.get(ticker, UNKNOWN_SECTOR)

        if (i + 1) % 25 == 0:
            print(f"  ✅ {i + 1}/{len(to_fetch)}")
        time.sleep(0.1)

    cache_path.write_text(json.dumps({
        "sectors":    sectors,
        "_cached_at": time.time(),
    }, indent=2))

    return {t: sectors.get(t, UNKNOWN_SECTOR) for t in tickers}
