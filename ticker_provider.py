"""
ticker_provider.py
────────────────────────────────────────────────────────────────────────────
Provides index constituent lists (current AND point-in-time) plus SEC
CIK <-> ticker mapping.

Two data regimes:

  1. CURRENT list only (legacy behavior, unchanged):
     Scrapes Wikipedia for today's index composition. Works for any index
     in SOURCES. Cached locally, short TTL (default 7 days).

  2. POINT-IN-TIME list (new):
     Only available for "sp500" for now. Built from a free, community
     maintained historical dataset (fja05680/sp500 on GitHub) instead of
     Wikipedia, since Wikipedia only ever shows the current composition.
     This lets a backtest ask "who was in the S&P 500 on 2019-03-03?"
     instead of silently using today's constituents for the whole history
     (survivorship bias).

Backward compatibility:
  get_tickers(index_name) with no `as_of_date` behaves EXACTLY as before.
  Existing notebooks require no changes.
"""

import pandas as pd
import requests
import json
import re
import time
from io import StringIO
from pathlib import Path
from typing import Optional


SOURCES = {
    # ── US ──────────────────────────────────────────────────
    "sp500": {
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "table_index": 0,
        "column": "Symbol",
        "suffix": None,
    },
    "nasdaq100": {
        "url": "https://en.wikipedia.org/wiki/Nasdaq-100",
        "column": "Ticker",
        "suffix": None,
    },
    "dow30": {
        "url": "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
        "column": "Symbol",
        "suffix": None,
    },
    "russell2000": {
        "url": "https://en.wikipedia.org/wiki/Russell_2000_Index",
        "column": "Symbol",
        "suffix": None,
        "note": "Partial list only — Wikipedia does not maintain a full Russell 2000 table",
    },

    # ── Europe ──────────────────────────────────────────────
    "dax40": {
        "url": "https://en.wikipedia.org/wiki/DAX",
        "column": "Ticker",
        "suffix": ".DE",
    },
    "cac40": {
        "url": "https://en.wikipedia.org/wiki/CAC_40",
        "column": "Ticker",
        "suffix": ".PA",
    },
    "eurostoxx50": {
        "url": "https://en.wikipedia.org/wiki/Euro_Stoxx_50",
        "column": "Ticker",
        "suffix": None,   # mixed exchanges → needs a future ticker-mapping layer
    },
    "ftse100": {
        "url": "https://en.wikipedia.org/wiki/FTSE_100_Index",
        "column": "EPIC",
        "suffix": ".L",
    },
    "aex25": {
        "url": "https://en.wikipedia.org/wiki/AEX_index",
        "column": "Symbol",
        "suffix": ".AS",
    },
    "ftse_mib": {
        "url": "https://en.wikipedia.org/wiki/FTSE_MIB",
        "column": "Ticker",
        "suffix": ".MI",
    },
    "ibex35": {
        "url": "https://en.wikipedia.org/wiki/IBEX_35",
        "column": "Ticker",
        "suffix": ".MC",
    },
}

# Indices for which we have a free point-in-time membership source.
# Only "sp500" for now — see build_sp500_membership_intervals().
INDICES_WITH_HISTORY = {"sp500"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

# The SEC REQUIRES a descriptive User-Agent with a real contact email
# (not a browser-spoofed UA). Requests without one may be throttled or
# blocked. Replace this with your own project name + email before use.
SEC_USER_AGENT = "quant-project georgegolfos@yahoo.gr"
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT}

# ── Free, historical S&P 500 membership sources (fja05680/sp500 on GitHub) ──
#   - base file:    periodic snapshots from 1996-01-02 to 2019-01-11
#   - changes file: explicit add/remove events from 2019 to present
SP500_HIST_BASE_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes.csv"
)
SP500_HIST_CHANGES_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "sp500_changes_since_2019.csv"
)
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# ── Cache TTLs ────────────────────────────────────────────────────────────
# The base file is a FROZEN historical snapshot — it will never change again,
# so once downloaded it is cached effectively forever (no point re-fetching
# it on a schedule). The changes file is actively maintained and grows over
# time, so it gets a much shorter TTL.
SP500_BASE_CACHE_MAX_AGE_HOURS = 24 * 365 * 10   # ~10 years == "forever" in practice
SP500_CHANGES_CACHE_MAX_AGE_HOURS = 24 * 7        # 1 week
SEC_TICKER_MAP_CACHE_MAX_AGE_HOURS = 24 * 7        # 1 week


# ─────────────────────────────────────────────────────────────
# Generic download helper (used for Wikipedia / SEC / GitHub raw files)
# ─────────────────────────────────────────────────────────────

def _download_with_retry(
    url: str,
    headers: dict,
    max_retries: int = 3,
    retry_sleep: int = 2,
    timeout: int = 15,
) -> requests.Response:

    response = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response
        except requests.RequestException:
            pass
        if attempt < max_retries - 1:
            time.sleep(retry_sleep)

    raise ConnectionError(f"Failed to download: {url}")


# ─────────────────────────────────────────────────────────────
# Wikipedia scraping (legacy — unchanged behavior)
# ─────────────────────────────────────────────────────────────

def _scrape_tickers(index_name: str) -> list[str]:
    cfg = SOURCES[index_name]

    response = _download_with_retry(cfg["url"], headers=HEADERS)

    # Wrapped in StringIO: passing a raw string directly makes lxml treat it
    # as a filename/URL instead of HTML content, raising OSError.
    tables = pd.read_html(StringIO(response.text))

    if "table_index" in cfg:
        table = tables[cfg["table_index"]]
    else:
        table = next((t for t in tables if cfg["column"] in t.columns), None)
        if table is None:
            raise ValueError(f"Column '{cfg['column']}' not found")

    col = cfg["column"]

    tickers = table[col].dropna().astype(str).str.strip().tolist()

    # Filter out rows that aren't valid tickers (numbers, footnotes like
    # "[1]", empty strings, unreasonably long strings).
    tickers = [
        t for t in tickers
        if t and not t.isdigit() and not re.match(r"^\[?\d+\]?$", t) and len(t) <= 15
    ]

    cleaned = []
    suffix = cfg.get("suffix")

    for t in tickers:
        # Only strip trailing dot-suffixes (e.g. "ASML.AS" -> "ASML").
        # Dashes are kept as-is for US tickers (e.g. "BRK-B").
        base = t.split(".")[0].strip()
        if not base:
            continue

        if index_name == "eurostoxx50":
            cleaned.append(base)
            continue

        if suffix:
            cleaned.append(base + suffix)
        else:
            # US tickers: replace dots with dashes (yfinance convention)
            cleaned.append(base.replace(".", "-"))

    return sorted(set(cleaned))


# ─────────────────────────────────────────────────────────────
# Point-in-time S&P 500 membership
# ─────────────────────────────────────────────────────────────

def _normalize_hist_ticker(raw: str) -> Optional[str]:
    """
    Cleans a single ticker token from the fja05680 dataset:
      - strips the trailing "-YYYYMM" removal-date annotation (we don't need
        it, since removal dates are re-derived ourselves by diffing
        consecutive snapshots)
      - converts dots to dashes (yfinance convention, same as _scrape_tickers)
    """
    raw = raw.strip()
    if not raw:
        return None

    # Strip a suffix like "-200006" (dash + exactly 6 digits at the end)
    raw = re.sub(r"-\d{6}$", "", raw)

    if not raw:
        return None

    return raw.replace(".", "-").upper()


def _parse_snapshot_cell(cell) -> set[str]:
    if pd.isna(cell) or not str(cell).strip():
        return set()

    tickers = set()
    for t in str(cell).split(","):
        norm = _normalize_hist_ticker(t)
        if norm:
            tickers.add(norm)
    return tickers


def _load_sp500_base_csv(folder_path: str) -> pd.DataFrame:
    """
    Loads the frozen 1996-2019 historical snapshot file. Cached essentially
    forever, since this file never changes once published.
    """
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    cache_path = folder / "sp500_historical_base.csv"

    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < SP500_BASE_CACHE_MAX_AGE_HOURS:
            return pd.read_csv(cache_path, parse_dates=["date"])

    print("Downloading S&P 500 historical base file (fja05680/sp500)...")
    response = _download_with_retry(SP500_HIST_BASE_URL, headers=HEADERS)
    cache_path.write_bytes(response.content)

    return pd.read_csv(cache_path, parse_dates=["date"])


def _load_sp500_changes_csv(folder_path: str) -> pd.DataFrame:
    """
    Loads the actively-maintained post-2019 changes file. Cached for
    SP500_CHANGES_CACHE_MAX_AGE_HOURS, since new events get appended
    every few months.
    """
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    cache_path = folder / "sp500_historical_changes.csv"

    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < SP500_CHANGES_CACHE_MAX_AGE_HOURS:
            return pd.read_csv(cache_path, parse_dates=["date"])

    print("Downloading recent S&P 500 changes (fja05680/sp500)...")
    response = _download_with_retry(SP500_HIST_CHANGES_URL, headers=HEADERS)
    cache_path.write_bytes(response.content)

    return pd.read_csv(cache_path, parse_dates=["date"])


def build_sp500_membership_intervals(
    folder_path: str = "cache",
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Builds a point-in-time membership table: one row per (ticker, period of
    S&P 500 membership).

    Returns a DataFrame with columns:
        ticker, date_added (Timestamp), date_removed (Timestamp or NaT if
        still a current member)

    Method:
      1. Base file (1996-2019): diff consecutive snapshots to reconstruct
         add/remove events.
      2. Changes file (2019-present): explicit add/remove events, applied
         sequentially on top of the base file's last snapshot.

    KNOWN LIMITATION: there is no data before 1996-01-02. Tickers already
    present on that first date are recorded with date_added=1996-01-02,
    which is not necessarily their true index-entry date.

    The final parsed result is itself cached (same TTL as the underlying
    changes file), so repeated calls in the same run/day don't re-parse
    ~2600 rows every time.
    """
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    parsed_cache_path = folder / "sp500_membership_intervals.csv"

    if not force_refresh and parsed_cache_path.exists():
        age_hours = (time.time() - parsed_cache_path.stat().st_mtime) / 3600
        if age_hours < SP500_CHANGES_CACHE_MAX_AGE_HOURS:
            return pd.read_csv(
                parsed_cache_path,
                parse_dates=["date_added", "date_removed"],
            )

    base = _load_sp500_base_csv(folder_path).sort_values("date")
    changes = _load_sp500_changes_csv(folder_path).sort_values("date")

    events: list[tuple[str, pd.Timestamp, Optional[pd.Timestamp]]] = []
    open_positions: dict[str, pd.Timestamp] = {}
    prev_set: set[str] = set()

    # ── Phase 1: diff-based reconstruction over the base file ──────────────
    for _, row in base.iterrows():
        date = row["date"]
        cur_set = _parse_snapshot_cell(row["tickers"])

        added = cur_set - prev_set
        removed = prev_set - cur_set

        for t in added:
            open_positions[t] = date

        for t in removed:
            start = open_positions.pop(t, base["date"].min())
            events.append((t, start, date))

        prev_set = cur_set

    # ── Phase 2: explicit events from the changes file (2019 -> present) ───
    for _, row in changes.iterrows():
        date = row["date"]

        add_cell = row.get("add")
        remove_cell = row.get("remove")

        added = _parse_snapshot_cell(add_cell) if pd.notna(add_cell) else set()
        removed = _parse_snapshot_cell(remove_cell) if pd.notna(remove_cell) else set()

        for t in removed:
            start = open_positions.pop(t, date)
            events.append((t, start, date))

        for t in added:
            open_positions[t] = date

    # ── Whatever is still open remains a current member ─────────────────────
    for t, start in open_positions.items():
        events.append((t, start, pd.NaT))

    result = pd.DataFrame(events, columns=["ticker", "date_added", "date_removed"])
    result = result.sort_values(["ticker", "date_added"]).reset_index(drop=True)

    result.to_csv(parsed_cache_path, index=False)

    return result


def get_sp500_as_of(date, folder_path: str = "cache") -> list[str]:
    """Returns the list of S&P 500 tickers as of a specific date."""
    intervals = build_sp500_membership_intervals(folder_path)
    ts = pd.Timestamp(date)

    mask = (intervals["date_added"] <= ts) & (
        intervals["date_removed"].isna() | (intervals["date_removed"] > ts)
    )

    return sorted(intervals.loc[mask, "ticker"].unique().tolist())


def get_sp500_ever_between(start, end, folder_path: str = "cache") -> list[str]:
    """
    Returns the UNION of every ticker that was EVER in the S&P 500 at any
    point within [start, end] — including delisted/removed companies.
    This is the correct universe for a backtest to use, in order to avoid
    survivorship bias.
    """
    intervals = build_sp500_membership_intervals(folder_path)
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)

    mask = (intervals["date_added"] <= end_ts) & (
        intervals["date_removed"].isna() | (intervals["date_removed"] >= start_ts)
    )

    return sorted(intervals.loc[mask, "ticker"].unique().tolist())


# ─────────────────────────────────────────────────────────────
# CIK <-> ticker mapping (SEC), for the upcoming fundamentals pipeline
# ─────────────────────────────────────────────────────────────

def _load_sec_ticker_map(folder_path: str = "cache") -> dict[str, int]:

    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    cache_path = folder / "sec_company_tickers.json"

    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < SEC_TICKER_MAP_CACHE_MAX_AGE_HOURS:
            raw = json.loads(cache_path.read_text())
            return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}

    print("Downloading SEC company_tickers.json...")
    response = _download_with_retry(SEC_TICKERS_URL, headers=SEC_HEADERS)
    cache_path.write_bytes(response.content)

    raw = response.json()
    return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}


def get_cik_for_ticker(ticker: str, folder_path: str = "cache") -> Optional[int]:
    """Returns the SEC CIK for a ticker, or None if not found."""
    mapping = _load_sec_ticker_map(folder_path)
    return mapping.get(ticker.upper())


# ─────────────────────────────────────────────────────────────
# Public API (signature kept as close as possible to the original —
# fully backward compatible)
# ─────────────────────────────────────────────────────────────

def get_tickers(
    index_name: str = "sp500",
    folder_path: str = "cache",
    max_age_hours: int = 168,
    as_of_date=None,
) -> list[str]:
    """
    WITHOUT as_of_date: identical to the previous behavior — returns
    TODAY's list (Wikipedia scraping, cached). No existing notebook needs
    to change.

    WITH as_of_date: returns the list AS IT WAS on that date.
    Currently only available for index_name="sp500".
    """

    if as_of_date is not None:
        if index_name not in INDICES_WITH_HISTORY:
            raise NotImplementedError(
                f"Point-in-time membership is not available yet for "
                f"'{index_name}'. Available indices: {sorted(INDICES_WITH_HISTORY)}."
            )
        return get_sp500_as_of(as_of_date, folder_path=folder_path)

    # ── Legacy behavior, unchanged ──────────────────────────────────────
    if index_name not in SOURCES:
        raise ValueError(
            f"Unknown index: {index_name}. "
            f"Available: {list(SOURCES.keys())}"
        )

    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)

    cache_path = folder / f"tickers_{index_name}.json"

    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            try:
                data = json.loads(cache_path.read_text())
                return data.get("tickers", [])
            except Exception:
                print("Corrupt cache -> re-scraping")

    print(f"Scraping {index_name} tickers...")

    try:
        tickers = _scrape_tickers(index_name)
    except Exception as e:
        print(f"Scraping error: {e}")
        if cache_path.exists():
            print("Falling back to old cache")
            try:
                return json.loads(cache_path.read_text()).get("tickers", [])
            except Exception:
                print("Corrupt cache — no fallback available.")
        raise

    cache = {
        "index": index_name,
        "tickers": tickers,
        "scraped_at": time.time(),
        "scraped_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(tickers),
    }

    cache_path.write_text(json.dumps(cache, indent=2))

    print(f"Saved {len(tickers)} tickers -> {cache_path}")

    return tickers


def get_all_ever_tickers(
    index_name: str = "sp500",
    start: Optional[str] = None,
    end: Optional[str] = None,
    folder_path: str = "cache",
) -> list[str]:
    """
    Returns the UNION of every ticker that was EVER in the index at any
    point within [start, end]. Used by data_engine.py so that the backtest
    universe includes delisted/removed companies, not just today's list.
    """
    if index_name not in INDICES_WITH_HISTORY:
        raise NotImplementedError(
            f"Historical universe is not available yet for '{index_name}'. "
            f"Available indices: {sorted(INDICES_WITH_HISTORY)}."
        )
    if start is None or end is None:
        raise ValueError("Both start and end dates are required (e.g. '2015-01-01').")

    return get_sp500_ever_between(start, end, folder_path=folder_path)


def get_custom_tickers(tickers: list[str]) -> list[str]:
    print(f"Custom ticker list: {len(tickers)} tickers.")
    return tickers
