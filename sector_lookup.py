# sector_lookup.py
"""
Sector Lookup — Ελαφριά, ανεξάρτητη ταξινόμηση κλάδου.
──────────────────────────────────────────────────────
ΔΕΝ είναι fundamentals analysis. Δεν σκοράρει τίποτα, δεν κρίνει ποιότητα
εταιρείας. Είναι απλά ένα static classification lookup (GICS sector μέσω
yfinance) που χρησιμοποιείται ΜΟΝΟ για διαφοροποίηση χαρτοφυλακίου
(max_per_sector) στο scorer_mr.py — τίποτα άλλο.

Κρατιέται ξεχωριστά από το fundamentals.py γιατί το scorer_mr.py (το
κύριο, καθαρά τεχνικό script) δεν πρέπει να έχει ΚΑΜΙΑ εξάρτηση από
fundamentals δεδομένα — ούτε καν για hard filters. Το fundamentals.py
+ ο fundamental-weighted scorer αποτελούν ξεχωριστό, μελλοντικό project.

Χρήση:
    from sector_lookup import get_sectors

    sectors = get_sectors(["AAPL", "MSFT", "XOM"])
    # → {"AAPL": "Technology", "MSFT": "Technology", "XOM": "Energy"}
"""

import json
import time
from pathlib import Path

import yfinance as yf

DEFAULT_MAX_AGE_HOURS = 24 * 30   # 30 μέρες — το sector μιας εταιρείας σχεδόν ποτέ δεν αλλάζει
UNKNOWN_SECTOR        = "Unknown"


def get_sectors(
    tickers:       list[str],
    folder_path:   str = "data",
    universe_name: str = "sp500",
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> dict[str, str]:
    """
    Επιστρέφει {ticker: sector} με local caching.
    Δεν κάνει retry-heavy logic — αν ένα ticker αποτύχει, μπαίνει ως
    "Unknown" και δεν μπλοκάρει τίποτα (η διαφοροποίηση απλά τον
    αγνοεί/τον βάζει σε ξεχωριστό sector bucket).
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

    print(f"🔄 Sector lookup για {len(to_fetch)} tickers...")
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
