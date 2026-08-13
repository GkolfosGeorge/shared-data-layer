# sector_lookup.py
"""
Sector & Market Cap Lookup — lightweight, standalone classification.
────────────────────────────────────────────────────────────────
This is NOT fundamentals analysis. It doesn't score anything or judge
company quality. It's a static classification lookup (GICS sector + market
cap, via yfinance) used in scorer_mr.py for:
  - portfolio diversification (max_per_sector)
  - market-cap filtering (cap_tier)
Nothing else — no fundamental quality judgment.

Kept separate from fundamentals.py because scorer_mr.py (the main, purely
technical script) must have NO dependency on fundamentals data — not even
for hard filters. fundamentals.py and the fundamental-weighted scorer are a
separate, future project.

Sector and market cap come together from the SAME yf.Ticker().info call —
adding market cap costs no extra requests.

Usage:
    from sector_lookup import get_sectors, get_market_caps, get_sectors_and_caps

    sectors = get_sectors(["AAPL", "MSFT", "XOM"])
    # -> {"AAPL": "Technology", "MSFT": "Technology", "XOM": "Energy"}

    caps = get_market_caps(["AAPL", "MSFT", "XOM"])
    # -> {"AAPL": 3_400_000_000_000, "MSFT": 3_100_000_000_000, "XOM": 480_000_000_000}

    sectors, caps = get_sectors_and_caps(["AAPL", "MSFT", "XOM"])
"""

import json
import time
from pathlib import Path
from typing import Optional

import yfinance as yf

DEFAULT_MAX_AGE_HOURS    = 24 * 30   # 30 days — a company's sector almost never changes
MARKET_CAP_MAX_AGE_HOURS = 24 * 3    # 3 days — market cap moves with price, needs more frequent refresh
UNKNOWN_SECTOR           = "Unknown"


def _fetch_sectors_and_caps(
    tickers:       list[str],
    folder_path:   str,
    universe_name: str,
    max_age_hours: int,
) -> tuple[dict[str, str], dict[str, Optional[float]]]:
    """
    Internal function: does the actual fetch+cache for sector AND market cap
    together, from the same yf.Ticker().info call. Sector and market cap have
    different "natural" TTLs (sector is nearly static, market cap moves daily
    with price) — but since they come from the same API call, they're
    refreshed together on the stricter (shorter) TTL.
    """
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    cache_path = folder / f"sectors_caps_{universe_name}.json"

    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}

    cached_at   = cache.get("_cached_at", 0)
    sectors     = dict(cache.get("sectors", {}))
    market_caps = dict(cache.get("market_caps", {}))
    is_stale    = (time.time() - cached_at) / 3600 > max_age_hours

    missing  = [t for t in tickers if t not in sectors or t not in market_caps]
    to_fetch = missing if not is_stale else tickers

    if not to_fetch:
        return (
            {t: sectors.get(t, UNKNOWN_SECTOR) for t in tickers},
            {t: market_caps.get(t) for t in tickers},
        )

    print(f"🔄 Sector + Market Cap lookup for {len(to_fetch)} tickers...")
    for i, ticker in enumerate(to_fetch):
        try:
            info = yf.Ticker(ticker).info
            sectors[ticker]     = info.get("sector") or UNKNOWN_SECTOR
            market_caps[ticker] = info.get("marketCap")  # None if missing
        except Exception:
            sectors[ticker]     = sectors.get(ticker, UNKNOWN_SECTOR)
            market_caps[ticker] = market_caps.get(ticker)

        if (i + 1) % 25 == 0:
            print(f"  ✅ {i + 1}/{len(to_fetch)}")
        time.sleep(0.1)

    cache_path.write_text(json.dumps({
        "sectors":     sectors,
        "market_caps": market_caps,
        "_cached_at":  time.time(),
    }, indent=2))

    return (
        {t: sectors.get(t, UNKNOWN_SECTOR) for t in tickers},
        {t: market_caps.get(t) for t in tickers},
    )


def get_sectors_and_caps(
    tickers:       list[str],
    folder_path:   str = "data",
    universe_name: str = "sp500",
    max_age_hours: int = MARKET_CAP_MAX_AGE_HOURS,
) -> tuple[dict[str, str], dict[str, Optional[float]]]:
    """Returns ({ticker: sector}, {ticker: market_cap}) — one fetch, two dicts."""
    return _fetch_sectors_and_caps(tickers, folder_path, universe_name, max_age_hours)


def get_sectors(
    tickers:       list[str],
    folder_path:   str = "data",
    universe_name: str = "sp500",
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> dict[str, str]:
    """
    Returns {ticker: sector} with local caching.
    Backward-compatible — internally uses the same combined fetch as
    market cap, just returns only the sector dict.
    No retry-heavy logic — if a ticker fails, it's marked "Unknown" and
    doesn't block anything (diversification just ignores it / puts it in
    its own sector bucket).
    """
    sectors, _ = _fetch_sectors_and_caps(tickers, folder_path, universe_name, max_age_hours)
    return sectors


def get_market_caps(
    tickers:       list[str],
    folder_path:   str = "data",
    universe_name: str = "sp500",
    max_age_hours: int = MARKET_CAP_MAX_AGE_HOURS,
) -> dict[str, Optional[float]]:
    """
    Returns {ticker: market_cap} in USD (e.g. MSFT -> 3_100_000_000_000).
    None for tickers where yfinance didn't return a marketCap.
    """
    _, market_caps = _fetch_sectors_and_caps(tickers, folder_path, universe_name, max_age_hours)
    return market_caps
