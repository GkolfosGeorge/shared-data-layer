# news_scanner.py
"""
News Scanner — raw company news capture (Finnhub)
────────────────────────────────────────────────────────────────────────────
Fetches raw, ticker-tagged news articles from Finnhub's free company-news
endpoint. Captures RAW headline/summary/source/url — no sentiment scoring
here (that's a separate, later processing step). Sentiment methodology
will change over time; the raw text must not.

WHY THIS CAN'T WAIT (same reasoning as options_archive.py):
  There is no source — free or paid — that reliably reconstructs "what
  the news said, and how the market would have read it" for a past date.
  Today's headlines, once the day passes without being archived, cannot
  be recovered with the same fidelity (aggregators rewrite, redirect, or
  drop old articles).

Requires the FINNHUB_API_KEY environment variable (free signup at
finnhub.io). This file does not read .env — set it directly, or load it
with python-dotenv before importing this module (same convention as
db.py's DATABASE_URL).

Usage:
    scanner = NewsScanner()
    scanner.scan(tickers)
    df = scanner.to_dataframe()
"""

import os
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"

# How many days back to re-request on every run. > 1 on purpose: a safety
# net against a missed/failed cron run — the SAME article will then appear
# in 2-3 consecutive daily files. That's intentional redundancy, not a bug;
# dedup happens at load time (news_archive.load_news_archive), never here.
DEFAULT_LOOKBACK_DAYS = 3

# Finnhub free tier: 60 calls/minute. ~1.1s between tickers keeps a safety
# margin instead of running right at the limit.
SLEEP_BETWEEN_TICKERS = 1.1

REQUEST_TIMEOUT = 15


def _get_api_key(api_key: Optional[str] = None) -> str:
    key = api_key or os.environ.get("FINNHUB_API_KEY")
    if not key:
        raise RuntimeError(
            "FINNHUB_API_KEY environment variable is not set. "
            "Get a free key at https://finnhub.io and set it, e.g.:\n"
            "  export FINNHUB_API_KEY=your_key_here"
        )
    return key


class NewsScanner:
    """
    Scans a ticker universe for raw news articles.

    Usage:
        scanner = NewsScanner()
        scanner.scan(tickers)
        df = scanner.to_dataframe()
    """

    def __init__(
        self,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        sleep: float = SLEEP_BETWEEN_TICKERS,
        api_key: Optional[str] = None,
    ):
        self.lookback_days = lookback_days
        self.sleep = sleep
        self.api_key = _get_api_key(api_key)
        self.raw_rows: list[dict] = []

    def scan(self, tickers: list[str], verbose: bool = True) -> "NewsScanner":
        """
        Scans the ticker list. Shows progress every 25 tickers.
        A per-ticker failure is logged and skipped — never aborts the run
        (one bad ticker shouldn't cost you the whole day's archive).
        """
        self.raw_rows = []
        total = len(tickers)

        print(f"\n📰 News Scanner — {total} tickers")
        print(f"   Lookback window: {self.lookback_days} days")
        print(f"{'─'*55}\n")

        errors = 0
        found_articles = 0

        for i, ticker in enumerate(tickers, 1):
            articles = self._fetch_ticker(ticker)
            if articles is None:
                errors += 1
            else:
                self.raw_rows.extend(articles)
                found_articles += len(articles)

            if verbose and (i % 25 == 0 or i == total):
                print(f"  [{i:>4}/{total}]  articles={found_articles}  errors={errors}")

            time.sleep(self.sleep)

        print(f"\n✅ Scan complete")
        print(f"   Articles : {found_articles}")
        print(f"   Errors   : {errors}")

        return self

    def _fetch_ticker(self, ticker: str) -> Optional[list[dict]]:
        """
        Fetches raw news for one ticker over [today - lookback_days, today].
        Returns a list of raw row dicts, or None on failure (caller counts
        it as an error but keeps going).
        """
        today = date.today()
        from_date = today - timedelta(days=self.lookback_days)

        params = {
            "symbol": ticker,
            "from": from_date.isoformat(),
            "to": today.isoformat(),
            "token": self.api_key,
        }

        try:
            r = requests.get(FINNHUB_NEWS_URL, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            print(f"  ⚠️ {ticker} failed: {e}")
            return None

        if not isinstance(items, list):
            return []

        rows = []
        for item in items:
            # Finnhub returns a unix timestamp (seconds) in "datetime".
            published_at = None
            if item.get("datetime"):
                try:
                    published_at = pd.to_datetime(item["datetime"], unit="s")
                except (ValueError, TypeError):
                    published_at = None

            rows.append({
                "ticker":       ticker,
                # Finnhub's own article id — the key used for cross-day
                # dedup in news_archive.load_news_archive(). Without this,
                # the same article re-appearing across overlapping
                # lookback windows can't be safely deduplicated.
                "finnhub_id":   item.get("id"),
                "headline":     item.get("headline"),
                "summary":      item.get("summary"),
                "source":       item.get("source"),
                "url":          item.get("url"),
                "image_url":    item.get("image"),
                "category":     item.get("category"),
                # Comma-separated related tickers as returned by Finnhub —
                # kept raw (string), not parsed here.
                "related":      item.get("related"),
                "published_at": published_at,
            })

        return rows

    def to_dataframe(self) -> pd.DataFrame:
        """Returns the raw scanned articles as a DataFrame, for archiving."""
        return pd.DataFrame(self.raw_rows)

    def save_daily_archive(
        self,
        snapshot_date=None,
        folder_path: str = "news_archive",
    ):
        """
        Convenience method: saves today's scan via
        news_archive.save_news_snapshot(). Same loose-coupling pattern as
        OptionsScanner.save_full_chain_archive() — prints a warning
        instead of raising if news_archive.py isn't found alongside this
        file, so the scanner still works standalone.
        """
        try:
            from news_archive import save_news_snapshot
        except ImportError:
            print("⚠️ news_archive.py not found in the same folder — skipping archiving.")
            return None

        df = self.to_dataframe()
        return save_news_snapshot(df, snapshot_date=snapshot_date, folder_path=folder_path)


# ─────────────────────────────────────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]

    scanner = NewsScanner()
    scanner.scan(test_tickers)

    df = scanner.to_dataframe()
    print(f"\n📋 {len(df)} articles fetched.")
    if not df.empty:
        print(df[["ticker", "headline", "source", "published_at"]].head(10).to_string(index=False))
