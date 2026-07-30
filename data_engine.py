# 01_data_loader.py

import yfinance as yf
import pandas as pd
import numpy as np
import json
import time
import io
import requests
from pathlib import Path

from membership_local import get_universe_as_of_local


# ── Default config — override from notebook ──────────────────────────────────

DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_MIN_HISTORY_DAYS = 252

# Fetches only NEW days since last cache instead of re-downloading the full
# `period` history — critical once `period` spans decades (e.g. "max").
DEFAULT_INCREMENTAL_UPDATE = True

DEFAULT_AUTO_ADJUST = True
DEFAULT_REPAIR = True
DEFAULT_THREADS = True

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_SLEEP = 2

# Loaded from ticker_renames_prices.csv — edit the CSV to add/remove
# entries, no code changes needed.
def _load_ticker_renames(csv_path=None) -> dict:
    csv_path = Path(csv_path) if csv_path else Path(__file__).resolve().parent / "ticker_renames_prices.csv"
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path)
    df = df[df["status"] == "active"]
    return dict(zip(df["old_ticker"], df["new_ticker"]))


TICKER_RENAMES = _load_ticker_renames()
DEFAULT_USE_TICKER_RENAME_FALLBACK = True

# ── Same-ticker individual retry (last-resort safety net, e.g. CTRA flukes) ───
# Catches isolated bulk-request flukes (e.g. CTRA fails in bulk despite
# being fully alive/untouched) — not renames, not delistings.
DEFAULT_USE_SAME_TICKER_RETRY = True
DEFAULT_SAME_TICKER_RETRY_SLEEP = 1.0

# ── Stooq fallback (second free price source) ────────────────────────────────
# DISABLED BY DEFAULT: Stooq's /q/d/l/ now requires an API key (since March
# 2026). Left in place, opt-in, for if you get a key later.
DEFAULT_USE_STOOQ_FALLBACK = False
DEFAULT_STOOQ_REQUEST_DELAY = 0.3

DEFAULT_DROP_ALL_NAN_ROWS = True
DEFAULT_REMOVE_INVALID_CLOSE = True
DEFAULT_REMOVE_ZERO_VOLUME = False

DEFAULT_TIMEZONE_NORMALIZE = True


# ── Cache helpers ─────────────────────────────────────────────────────────────

def is_file_stale(
    file_path: Path,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> bool:

    if not file_path.exists():
        return True

    age_hours = (time.time() - file_path.stat().st_mtime) / 3600
    return age_hours > max_age_hours


def is_cache_valid(
    file_path: Path,
    meta_path: Path,
    tickers: list[str],
    period: str,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> bool:

    if not file_path.exists() or not meta_path.exists():
        return False

    if is_file_stale(file_path, max_age_hours):
        return False

    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False

    cached_success_set = set(meta.get("tickers", []))
    previously_requested_set = set(meta.get("requested_tickers", meta.get("tickers", [])))
    requested_set = set(tickers)

    # Compare against what was requested last time, not what succeeded —
    # a structural partial-coverage gap would otherwise invalidate the
    # cache on every single run.
    overlap    = len(requested_set & previously_requested_set)
    total      = len(requested_set | previously_requested_set)
    tickers_ok = (overlap / total) >= 0.90 if total else True

    period_ok = meta.get("period") == period

    return tickers_ok and period_ok


# ── Cleaning ──────────────────────────────────────────────────────────────────

def align_and_clean(
    data: pd.DataFrame,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    drop_all_nan_rows: bool = DEFAULT_DROP_ALL_NAN_ROWS,
    remove_invalid_close: bool = DEFAULT_REMOVE_INVALID_CLOSE,
    remove_zero_volume: bool = DEFAULT_REMOVE_ZERO_VOLUME,
    timezone_normalize: bool = DEFAULT_TIMEZONE_NORMALIZE,
) -> pd.DataFrame:

    """Cleans OHLCV data. No forward-fill is applied."""

    if data.empty:
        return data

    if timezone_normalize:
        try:
            if data.index.tz is not None:
                # tz_localize(None) on an already tz-aware index raises —
                # must tz_convert('UTC') first.
                data.index = data.index.tz_convert("UTC").tz_localize(None)
            else:
                data.index = pd.to_datetime(data.index)
        except Exception:
            pass

    data = data.sort_index()

    if drop_all_nan_rows:
        data = data.dropna(axis=0, how="all")

    tickers = data.columns.get_level_values(0).unique()

    cleaned = {}

    for ticker in tickers:

        try:
            tdf = data[ticker].copy()

            if "Close" not in tdf.columns:
                continue

            tdf = tdf[tdf["Close"].notna()]

            if remove_invalid_close:
                tdf = tdf[tdf["Close"] > 0]

            if remove_zero_volume and "Volume" in tdf.columns:
                tdf = tdf[tdf["Volume"] > 0]

            if {"Open", "High", "Low", "Close"}.issubset(tdf.columns):

                tdf = tdf[
                    (tdf["High"] >= tdf["Low"]) &
                    (tdf["High"] >= tdf["Close"]) &
                    (tdf["High"] >= tdf["Open"]) &
                    (tdf["Low"]  <= tdf["Close"]) &
                    (tdf["Low"]  <= tdf["Open"])
                ]

            if len(tdf) < min_history_days:
                continue

            cleaned[ticker] = tdf

        except Exception as e:
            print(f"⚠️ Cleaning failed for {ticker}: {e}")

    if not cleaned:
        return pd.DataFrame()

    result = pd.concat(cleaned, axis=1)

    return result.sort_index()


# ── Single-ticker column-flattening helper ────────────────────────────────────

def _dedupe_renamed_tickers(data: pd.DataFrame) -> pd.DataFrame:
    """
    Drops the OLD ticker's column whenever BOTH the old and new symbol
    from TICKER_RENAMES are present in `data` (e.g. cache files saved
    before this check existed, or a rename added after the new ticker's
    data already exists). The new symbol's own download already covers
    the full history, so nothing is lost.
    """
    present = set(data.columns.get_level_values(0))
    to_drop = [old for old, new in TICKER_RENAMES.items() if old in present and new in present]
    if to_drop:
        print(f"🧹 Dropping {len(to_drop)} duplicate renamed tickers (new symbol already present): {to_drop}")
        data = data.drop(columns=to_drop, level=0)
    return data


def _flatten_single_ticker_columns(tdf: pd.DataFrame) -> pd.DataFrame:
    """
    A single-ticker yf.download() can still return MultiIndex columns
    (e.g. ('Close', 'BNY')), which makes tdf["Close"] a DataFrame instead
    of a Series and breaks `.mean() > 0.5` comparisons. Flattens to the
    field level (Open/High/Low/Close/Volume) so tdf["Close"] is a Series.
    """
    if isinstance(tdf.columns, pd.MultiIndex):
        price_fields = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        lvl_first = set(tdf.columns.get_level_values(0))
        lvl_last = set(tdf.columns.get_level_values(-1))

        tdf = tdf.copy()
        if price_fields & lvl_first:
            tdf.columns = tdf.columns.get_level_values(0)
        elif price_fields & lvl_last:
            tdf.columns = tdf.columns.get_level_values(-1)

    return tdf


# ── Same-ticker individual retry helper (last-resort safety net) ─────────────

def _retry_same_ticker(
    tickers: list[str],
    period: str,
    auto_adjust: bool,
    repair: bool,
    retry_sleep: float = DEFAULT_SAME_TICKER_RETRY_SLEEP,
) -> dict[str, pd.DataFrame]:
    """
    Retries whatever's still failing after the rename fallback, one ticker
    at a time under its own symbol — catches isolated bulk-request flukes
    (e.g. CTRA fails in bulk despite being fully alive/untouched). A
    genuinely delisted/merged ticker will correctly keep failing here too.
    """
    recovered = {}
    total = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        try:
            tdf = yf.download(
                tickers=ticker,
                period=period,
                auto_adjust=auto_adjust,
                repair=repair,
                threads=False,
                progress=False,
            )
            if tdf is not None and not tdf.empty:
                tdf = _flatten_single_ticker_columns(tdf)

            if (
                tdf is not None
                and not tdf.empty
                and "Close" in tdf.columns
                and tdf["Close"].notna().mean() > 0.5
            ):
                recovered[ticker] = tdf
        except Exception:
            pass

        if i % 20 == 0 or i == total:
            status = "✅" if ticker in recovered else "❌"
            print(f"  🔁 same-ticker retry {i}/{total} (last: {ticker} {status})")

        time.sleep(retry_sleep)

    return recovered


# ── Ticker-rename retry helper ─────────────────────────────────────────────────

def _retry_via_ticker_rename(
    tickers: list[str],
    period: str,
    auto_adjust: bool,
    repair: bool,
    rename_map: dict[str, str] = TICKER_RENAMES,
) -> dict[str, pd.DataFrame]:
    """
    For tickers with a known rename (TICKER_RENAMES), retries under the
    NEW symbol — the company kept trading, Yahoo just drops history under
    the retired symbol. Returns {old_ticker: DataFrame}, keyed by the OLD
    symbol so it lines up with index_membership.
    """
    recovered = {}
    candidates = {t: rename_map[t] for t in tickers if t in rename_map}

    if not candidates:
        return recovered

    print(f"🔤 Trying {len(candidates)} tickers via known rename: {candidates}")

    for old_ticker, new_ticker in candidates.items():
        try:
            tdf = yf.download(
                tickers=new_ticker,
                period=period,
                auto_adjust=auto_adjust,
                repair=repair,
                threads=False,
                progress=False,
            )
            if tdf is not None and not tdf.empty:
                tdf = _flatten_single_ticker_columns(tdf)

            if (
                tdf is not None
                and not tdf.empty
                and "Close" in tdf.columns
                and tdf["Close"].notna().mean() > 0.5
            ):
                recovered[old_ticker] = tdf
                print(f"  ✅ {old_ticker} recovered via {new_ticker}")
            else:
                print(f"  ❌ {old_ticker} ({new_ticker}) returned no usable data")
        except Exception as e:
            print(f"  ❌ {old_ticker} ({new_ticker}) failed: {e}")

    return recovered


# ── Stooq fallback helper ──────────────────────────────────────────────────────

STOOQ_URL = "https://stooq.com/q/d/l/"

def _fetch_stooq_ticker(ticker: str, timeout: int = 15) -> pd.DataFrame | None:
    """
    Fetches daily OHLCV history for one ticker from Stooq — a free, no-key
    second source for tickers Yahoo no longer serves. Returns a DataFrame
    or None if not found.
    """
    try:
        resp = requests.get(
            STOOQ_URL,
            params={"s": f"{ticker.lower()}.us", "i": "d"},
            timeout=timeout,
        )
        if resp.status_code != 200 or not resp.text:
            return None

        # Stooq returns a plain-text "No data" style body for unknown tickers
        # instead of an HTTP error — detect that before trying to parse CSV.
        first_line = resp.text.splitlines()[0] if resp.text.splitlines() else ""
        if "Date" not in first_line:
            return None

        df = pd.read_csv(io.StringIO(resp.text))
        if df.empty or "Close" not in df.columns or "Date" not in df.columns:
            return None

        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()

        return df

    except Exception:
        return None


def _retry_via_stooq(
    tickers: list[str],
    reference_index: pd.DatetimeIndex,
    request_delay: float = DEFAULT_STOOQ_REQUEST_DELAY,
) -> dict[str, pd.DataFrame]:
    """
    Tries Stooq for tickers that failed the Yahoo bulk download. Stooq
    returns full history (often much further back than requested), so
    results are trimmed to `reference_index`'s range for consistency.
    """
    recovered = {}
    total = len(tickers)
    ref_start, ref_end = reference_index.min(), reference_index.max()

    for i, ticker in enumerate(tickers, 1):
        tdf = _fetch_stooq_ticker(ticker)

        if tdf is not None and not tdf.empty:
            tdf = tdf[(tdf.index >= ref_start) & (tdf.index <= ref_end)]

            if not tdf.empty and tdf["Close"].notna().mean() > 0.5:
                recovered[ticker] = tdf

        if i % 20 == 0 or i == total:
            status = "✅" if ticker in recovered else "❌"
            print(f"  🌐 stooq {i}/{total} (last: {ticker} {status})")

        time.sleep(request_delay)

    return recovered


# ── Incremental update helper ───────────────────────────────────────────────────

def _incremental_update(
    file_path: Path,
    meta_path: Path,
    tickers: list[str],
    period: str,
    auto_adjust: bool,
    repair: bool,
    threads: bool,
) -> pd.DataFrame | None:
    """
    Fetches only the days since the last cached date instead of
    re-downloading the full `period` history, plus a one-time full
    download for tickers not yet in the cache. Tickers that fail the
    incremental fetch keep their existing cached history unchanged.

    Returns the merged DataFrame, or None if there's no usable cache to
    build on (caller should fall back to a full download).
    """
    try:
        existing_data = pd.read_parquet(file_path)
        existing_meta = json.loads(meta_path.read_text())
    except Exception:
        return None

    if existing_data is None or existing_data.empty:
        return None

    if existing_meta.get("period") != period:
        # Different period requested (e.g. switched "10y" -> "max") —
        # incremental doesn't make sense here, needs a genuine full download.
        return None

    existing_tickers = set(existing_data.columns.get_level_values(0))
    requested_tickers = set(tickers)

    last_cached_date = existing_data.index.max()
    today = pd.Timestamp.today().normalize()
    incremental_start = (last_cached_date + pd.Timedelta(days=1)).normalize()

    merged = existing_data.copy()

    # ── New days for tickers we already have history for ────────────────────
    tickers_to_refresh = sorted(existing_tickers & requested_tickers)

    if incremental_start <= today and tickers_to_refresh:
        print(f"🔁 Updating cache ({len(tickers_to_refresh)} tickers): {incremental_start.date()} -> {today.date()}")
        try:
            new_rows = yf.download(
                tickers=tickers_to_refresh,
                start=incremental_start.strftime("%Y-%m-%d"),
                group_by="ticker",
                auto_adjust=auto_adjust,
                repair=repair,
                threads=threads,
                progress=False,
            )
            if new_rows is not None and not new_rows.empty:
                merged = pd.concat([merged, new_rows], axis=0)
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                print(f"  ✅ Added up to {len(new_rows)} new rows.")
            else:
                print("  ℹ️ No new rows returned (likely already up-to-date).")
        except Exception as e:
            print(f"  ⚠️ Incremental fetch failed ({e}) — keeping existing cache unchanged.")
    else:
        print("📂 No new days to fetch (already up-to-date, or weekend/holiday).")

    # ── Full history for genuinely NEW tickers (not already in the cache) ──
    # Skips tickers already confirmed unavailable last time (metadata
    # "known_unavailable") — otherwise the same ~200 permanently-delisted
    # tickers get a fresh full-download attempt (and the same wall of
    # "possibly delisted" errors) every single day. Exception: tickers
    # that now have a TICKER_RENAMES entry are always rechecked (cheap),
    # so a newly-added rename gets picked up without a full cache rebuild.
    known_unavailable = set(existing_meta.get("known_unavailable", []))
    candidates = requested_tickers - existing_tickers
    skip_unavailable = known_unavailable - set(TICKER_RENAMES.keys())
    new_tickers = sorted(candidates - skip_unavailable)
    skipped_known_unavailable = sorted(candidates & skip_unavailable)

    if skipped_known_unavailable:
        print(f"⏭️ Skipping {len(skipped_known_unavailable)} tickers already confirmed unavailable last time.")

    if new_tickers:
        print(f"🆕 {len(new_tickers)} new tickers with no history — full download for these.")
        try:
            new_hist = yf.download(
                tickers=new_tickers,
                period=period,
                group_by="ticker",
                auto_adjust=auto_adjust,
                repair=repair,
                threads=threads,
                progress=False,
            )
            if new_hist is not None and not new_hist.empty:
                if not isinstance(new_hist.columns, pd.MultiIndex) and len(new_tickers) == 1:
                    new_hist.columns = pd.MultiIndex.from_product([[new_tickers[0]], new_hist.columns])
                merged = pd.concat([merged, new_hist], axis=1)
        except Exception as e:
            print(f"  ⚠️ New-ticker download failed: {e}")

    return merged


# ── Download ──────────────────────────────────────────────────────────────────


def download_sp500_data(
    tickers: list[str],
    period: str = "5y",
    folder_path: str = "data",
    universe_name: str = "sp500",

    # Cache
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    incremental_update: bool = DEFAULT_INCREMENTAL_UPDATE,

    # Cleaning
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    drop_all_nan_rows: bool = DEFAULT_DROP_ALL_NAN_ROWS,
    remove_invalid_close: bool = DEFAULT_REMOVE_INVALID_CLOSE,
    remove_zero_volume: bool = DEFAULT_REMOVE_ZERO_VOLUME,
    timezone_normalize: bool = DEFAULT_TIMEZONE_NORMALIZE,

    # yfinance
    auto_adjust: bool = DEFAULT_AUTO_ADJUST,
    repair: bool = DEFAULT_REPAIR,
    threads: bool = DEFAULT_THREADS,

    # Retry
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_sleep: int = DEFAULT_RETRY_SLEEP,

    # Ticker-rename fallback (recovers tickers Yahoo dropped after a symbol change)
    use_ticker_rename_fallback: bool = DEFAULT_USE_TICKER_RENAME_FALLBACK,

    # Same-ticker individual retry (last-resort safety net, e.g. CTRA-style flukes)
    use_same_ticker_retry: bool = DEFAULT_USE_SAME_TICKER_RETRY,
    same_ticker_retry_sleep: float = DEFAULT_SAME_TICKER_RETRY_SLEEP,

    # Stooq fallback (second free price source for tickers Yahoo has dropped)
    use_stooq_fallback: bool = DEFAULT_USE_STOOQ_FALLBACK,
    stooq_request_delay: float = DEFAULT_STOOQ_REQUEST_DELAY,

) -> pd.DataFrame | None:

    """
    Downloads OHLCV data and saves it to Parquet, with cache validation,
    ticker-level cleaning, and fallback recovery (renames, same-ticker
    retry, Stooq) for tickers that fail the bulk download.
    """

    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)

    safe_period = period.replace(" ", "_")

    file_path = folder / f"{universe_name}_{safe_period}.parquet"
    meta_path = folder / f"{universe_name}_{safe_period}_meta.json"

    if is_cache_valid(
        file_path=file_path,
        meta_path=meta_path,
        tickers=tickers,
        period=period,
        max_age_hours=max_age_hours,
    ):
        print(f"📂 Fresh cache found ({universe_name}) — loading from disk.")

        try:
            return _dedupe_renamed_tickers(pd.read_parquet(file_path))

        except Exception as e:
            print(f"⚠️ Failed reading cache: {e}")

    # If a previous cache exists, fetch only what's new since last time
    # instead of re-downloading the full `period` history.
    data = None

    if incremental_update and file_path.exists() and meta_path.exists():
        data = _incremental_update(
            file_path=file_path,
            meta_path=meta_path,
            tickers=tickers,
            period=period,
            auto_adjust=auto_adjust,
            repair=repair,
            threads=threads,
        )

    if data is None or data.empty:
        print(f"🔄 Downloading data ({universe_name}) from yfinance...")

        data = None

        for attempt in range(1, max_retries + 1):

            try:
                data = yf.download(
                    tickers=tickers,
                    period=period,
                    group_by="ticker",
                    auto_adjust=auto_adjust,
                    repair=repair,
                    threads=threads,
                    progress=False,
                )

                if data is not None and not data.empty:
                    break

            except Exception as e:
                print(f"⚠️ Attempt {attempt}/{max_retries}: {e}")

            # Sleep after an exception too, not only after a failed
            # download that returned without raising.
            if attempt < max_retries:
                time.sleep(retry_sleep)

    if data is None or data.empty:

        print("❌ Download failed — attempting to load local cache.")

        return load_local_data(
            folder_path=folder_path,
            universe_name=universe_name,
            period=period,
        )

    if not isinstance(data.columns, pd.MultiIndex):
        if len(tickers) == 1:
            data.columns = pd.MultiIndex.from_product([[tickers[0]], data.columns])
        else:
            # Multiple tickers with flat columns is an unexpected state —
            # abort rather than silently mis-parse.
            print("❌ Unexpected flat columns for multiple tickers — re-download.")
            return load_local_data(folder_path=folder_path, universe_name=universe_name, period=period)

    downloaded_tickers = set(
        data.columns.get_level_values(0).unique()
    )

    missing = set(tickers) - downloaded_tickers

    if missing:
        print(f"⚠️ {len(missing)} tickers did not download:")
        print(sorted(missing))

    # Uses absolute valid-day count, not NaN ratio over the full period —
    # a ratio wrongly drops recent IPOs/spinoffs (short-but-real history).
    valid_tickers = []

    for ticker in downloaded_tickers:

        try:

            if "Close" not in data[ticker]:
                continue

            valid_close_count = data[ticker]["Close"].notna().sum()

            if valid_close_count >= min_history_days:
                valid_tickers.append(ticker)

        except Exception:
            continue

    removed = downloaded_tickers - set(valid_tickers)

    if removed:
        print(f"⚠️ {len(removed)} tickers failed the bulk download (Yahoo).")

        if use_ticker_rename_fallback:
            # Skip old_ticker if new_ticker already succeeded independently
            # (e.g. CPRI is itself a current member) — avoids duplicate
            # columns for the same company under two symbols.
            rename_candidates = sorted(
                t for t in removed
                if TICKER_RENAMES.get(t) not in set(valid_tickers)
            )
            skipped_redundant = sorted(set(removed) - set(rename_candidates))
            if skipped_redundant:
                print(f"ℹ️ Skipping {len(skipped_redundant)} renames — new ticker already present independently: {skipped_redundant}")

            renamed_recovered = _retry_via_ticker_rename(
                tickers=rename_candidates,
                period=period,
                auto_adjust=auto_adjust,
                repair=repair,
            )

            if renamed_recovered:
                print(f"✅ Recovered {len(renamed_recovered)} tickers via known rename: {sorted(renamed_recovered.keys())}")

                for ticker, tdf in renamed_recovered.items():
                    tdf = tdf.copy()
                    if isinstance(tdf.columns, pd.MultiIndex):
                        tdf.columns = tdf.columns.get_level_values(-1)
                    tdf.columns = pd.MultiIndex.from_product([[ticker], tdf.columns])

                    if ticker in data.columns.get_level_values(0):
                        data = data.drop(columns=ticker, level=0)

                    data = pd.concat([data, tdf], axis=1)
                    valid_tickers.append(ticker)

                removed = removed - set(renamed_recovered.keys())

        if use_same_ticker_retry and removed:
            print(f"🔁 Trying same-ticker retry (individually, same symbol) for {len(removed)} tickers...")

            same_ticker_recovered = _retry_same_ticker(
                tickers=sorted(removed),
                period=period,
                auto_adjust=auto_adjust,
                repair=repair,
                retry_sleep=same_ticker_retry_sleep,
            )

            if same_ticker_recovered:
                print(f"✅ Recovered {len(same_ticker_recovered)} tickers via same-ticker retry: {sorted(same_ticker_recovered.keys())}")

                for ticker, tdf in same_ticker_recovered.items():
                    tdf = tdf.copy()
                    if isinstance(tdf.columns, pd.MultiIndex):
                        tdf.columns = tdf.columns.get_level_values(-1)
                    tdf.columns = pd.MultiIndex.from_product([[ticker], tdf.columns])

                    if ticker in data.columns.get_level_values(0):
                        data = data.drop(columns=ticker, level=0)

                    data = pd.concat([data, tdf], axis=1)
                    valid_tickers.append(ticker)

                removed = removed - set(same_ticker_recovered.keys())

        if use_stooq_fallback:
            print(f"🌐 Trying Stooq (second, free source) for {len(removed)} tickers...")

            recovered = _retry_via_stooq(
                tickers=sorted(removed),
                reference_index=data.index,
                request_delay=stooq_request_delay,
            )

            if recovered:
                print(f"✅ Recovered {len(recovered)} tickers via Stooq: {sorted(recovered.keys())}")

                for ticker, tdf in recovered.items():
                    tdf = tdf.copy()
                    if isinstance(tdf.columns, pd.MultiIndex):
                        tdf.columns = tdf.columns.get_level_values(-1)
                    tdf.columns = pd.MultiIndex.from_product([[ticker], tdf.columns])

                    # Drop existing (broken) columns first to avoid duplicate labels.
                    if ticker in data.columns.get_level_values(0):
                        data = data.drop(columns=ticker, level=0)

                    data = pd.concat([data, tdf], axis=1)
                    valid_tickers.append(ticker)

                removed = removed - set(recovered.keys())

        if removed:
            print(f"❌ {len(removed)} tickers remain unavailable from any source (genuine delisting/merger):")
            print(sorted(removed))

    if not valid_tickers:
        print("❌ No valid tickers remain.")
        return None

    data = data[sorted(valid_tickers)]
    data = _dedupe_renamed_tickers(data)

    print("🧹 Cleaning dataset...")

    data = align_and_clean(
        data=data,
        min_history_days=min_history_days,
        drop_all_nan_rows=drop_all_nan_rows,
        remove_invalid_close=remove_invalid_close,
        remove_zero_volume=remove_zero_volume,
        timezone_normalize=timezone_normalize,
    )

    if data.empty:
        print("❌ Dataset empty after cleaning.")
        return None

    final_tickers = data.columns.get_level_values(0).unique()

    print(
        f"✅ Clean dataset: "
        f"{len(data)} trading days, "
        f"{len(final_tickers)} tickers."
    )

    data.to_parquet(file_path)

    print(f"✅ Saved:")
    print(file_path)

    meta = {
        "universe_name": universe_name,
        "tickers": sorted(final_tickers.tolist()),
        # Original requested list, kept separate from "tickers" (the
        # successful subset) — used by is_cache_valid.
        "requested_tickers": sorted(set(tickers)),
        # Tickers that failed every source (rename, same-ticker, Stooq) on
        # THIS run — read back by _incremental_update() next time so it
        # doesn't retry a full download for the same permanently-delisted
        # tickers every single day.
        "known_unavailable": sorted(removed),
        "period": period,
        "rows": int(len(data)),
        "downloaded_at": time.time(),
        "downloaded_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),

        # Cleaning settings
        "min_history_days": min_history_days,
        "auto_adjust": auto_adjust,
        "timezone_normalize": timezone_normalize,
    }

    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"✅ Metadata:")
    print(meta_path)

    return data


# ── Universe-aware download (point-in-time, survivorship-bias-free) ──────────

def download_universe_data(
    index_name: str,
    start,
    end,
    period: str = "5y",
    folder_path: str = "data",

    # Cache
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    incremental_update: bool = DEFAULT_INCREMENTAL_UPDATE,

    # Cleaning
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    drop_all_nan_rows: bool = DEFAULT_DROP_ALL_NAN_ROWS,
    remove_invalid_close: bool = DEFAULT_REMOVE_INVALID_CLOSE,
    remove_zero_volume: bool = DEFAULT_REMOVE_ZERO_VOLUME,
    timezone_normalize: bool = DEFAULT_TIMEZONE_NORMALIZE,

    # yfinance
    auto_adjust: bool = DEFAULT_AUTO_ADJUST,
    repair: bool = DEFAULT_REPAIR,
    threads: bool = DEFAULT_THREADS,

    # Retry
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_sleep: int = DEFAULT_RETRY_SLEEP,

    # Ticker-rename fallback (recovers tickers Yahoo dropped after a symbol change)
    use_ticker_rename_fallback: bool = DEFAULT_USE_TICKER_RENAME_FALLBACK,

    # Same-ticker individual retry (last-resort safety net, e.g. CTRA-style flukes)
    use_same_ticker_retry: bool = DEFAULT_USE_SAME_TICKER_RETRY,
    same_ticker_retry_sleep: float = DEFAULT_SAME_TICKER_RETRY_SLEEP,

    # Stooq fallback (second free price source for tickers Yahoo has dropped)
    use_stooq_fallback: bool = DEFAULT_USE_STOOQ_FALLBACK,
    stooq_request_delay: float = DEFAULT_STOOQ_REQUEST_DELAY,

) -> pd.DataFrame | None:
    """
    Pulls the point-in-time universe for `index_name` between [start, end]
    from the local membership CSV (includes delisted tickers, so no
    survivorship bias), then hands that list to download_sp500_data() for
    the OHLCV download.

    `start`/`end` define membership window; `period` is independent and
    controls how much yfinance history to actually download.

    Note: `index_name` is accepted for interface compatibility but the CSV
    is already scoped to one index at export time (see
    export_membership_to_csv.py) — it isn't used to filter here.
    """
    tickers = get_universe_as_of_local(start=start, end=end)

    if not tickers:
        # Don't fall through to download_sp500_data() with an empty list —
        # its cache check trivially passes (0 >= 0) and would silently
        # load a stale/unrelated parquet instead of erroring.
        raise ValueError(
            f"No tickers found between {start} and {end}. Check that "
            f"data/sp500_membership.csv exists and covers this date range "
            f"(run export_membership_to_csv.py to refresh it)."
        )

    print(f"📋 Universe {index_name} [{start} -> {end}]: {len(tickers)} tickers (incl. delisted).")

    return download_sp500_data(
        tickers=tickers,
        period=period,
        folder_path=folder_path,
        universe_name=index_name.lower(),
        max_age_hours=max_age_hours,
        incremental_update=incremental_update,
        min_history_days=min_history_days,
        drop_all_nan_rows=drop_all_nan_rows,
        remove_invalid_close=remove_invalid_close,
        remove_zero_volume=remove_zero_volume,
        timezone_normalize=timezone_normalize,
        auto_adjust=auto_adjust,
        repair=repair,
        threads=threads,
        max_retries=max_retries,
        retry_sleep=retry_sleep,
        use_ticker_rename_fallback=use_ticker_rename_fallback,
        use_same_ticker_retry=use_same_ticker_retry,
        same_ticker_retry_sleep=same_ticker_retry_sleep,
        use_stooq_fallback=use_stooq_fallback,
        stooq_request_delay=stooq_request_delay,
    )


# ── Local loading ─────────────────────────────────────────────────────────────

def load_local_data(
    folder_path: str = "data",
    universe_name: str = "sp500",
    period: str = "5y",
) -> pd.DataFrame | None:

    safe_period = period.replace(" ", "_")

    file_path = Path(folder_path) / f"{universe_name}_{safe_period}.parquet"

    if file_path.exists():

        print("📂 Loading local data.")

        try:
            return pd.read_parquet(file_path)

        except Exception as e:
            print(f"⚠️ Failed loading local parquet: {e}")

    print("⚠️ No local data found.")

    return None


# ── Single ticker helper ─────────────────────────────────────────────────────

def get_ticker_data(
    data: pd.DataFrame,
    ticker: str,
) -> pd.DataFrame | None:

    if data is None or data.empty:
        return None

    available = data.columns.get_level_values(0).unique()

    if ticker not in available:

        print(f"⚠️ {ticker} not found in data.")

        return None

    tdf = data[ticker].copy()

    return tdf.dropna(how="all")