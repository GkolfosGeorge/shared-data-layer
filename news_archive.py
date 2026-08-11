"""
news_archive.py
────────────────────────────────────────────────────────────────────────────
Appends each day's raw news scan (from NewsScanner.to_dataframe()) to a
local, date-partitioned Parquet archive — same Template A pattern as
options_archive.py.

WHY LOCAL PARQUET, NOT THE NEON DATABASE:
  Same reasoning as options: news volume at S&P 500 scale (multiple
  articles per ticker per day, over a multi-day lookback window) would
  eat into Neon's free tier fast, and this is raw archival data, not
  relational query data. Parquet is free, columnar, and self-contained
  per day.

WHY THE LOOKBACK WINDOW MEANS DUPLICATES ACROSS FILES (by design):
  news_scanner.py re-requests a multi-day window on every run (a safety
  net against a missed cron run), so the SAME article can legitimately
  appear in 2-3 consecutive daily files. That redundancy is intentional
  and cheap to store — it is resolved at LOAD time here via dedup on
  finnhub_id, never at save time (the raw daily files themselves are
  never deduplicated against each other, keeping each day's file an
  honest, standalone snapshot of what was fetched that day).

Usage:
    from news_archive import save_news_snapshot
    save_news_snapshot(news_df)
"""

from pathlib import Path
from typing import Optional

import pandas as pd

DEFAULT_ARCHIVE_FOLDER = "news_archive"


def save_news_snapshot(
    news_df: pd.DataFrame,
    snapshot_date=None,
    folder_path: str = DEFAULT_ARCHIVE_FOLDER,
) -> Optional[Path]:
    """
    Saves today's news_df to the local archive as one Parquet file per day:
        news_archive/news_YYYY-MM-DD.parquet

    Safe to call multiple times on the same day (overwrites that day's
    file) — e.g. if you re-run the scanner after a failed run.

    Returns the path written, or None if news_df was empty.
    """
    if news_df is None or news_df.empty:
        print("⚠️ news_df is empty — nothing to save.")
        return None

    if snapshot_date is None:
        snapshot_date = pd.Timestamp.today().normalize()
    else:
        snapshot_date = pd.Timestamp(snapshot_date).normalize()

    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)

    date_str = snapshot_date.strftime("%Y-%m-%d")
    file_path = folder / f"news_{date_str}.parquet"

    df = news_df.copy()
    # Stamp the fetch date explicitly in the data itself, not just the
    # filename — this is the "when did we fetch this" field, distinct
    # from published_at ("when was it actually published").
    df["snapshot_date"] = snapshot_date

    df.to_parquet(file_path, index=False)

    size_kb = file_path.stat().st_size / 1024
    print(f"💾 News snapshot saved: {file_path}  ({len(df):,} rows, {size_kb:.0f} KB)")

    return file_path


def load_news_archive(
    folder_path: str = DEFAULT_ARCHIVE_FOLDER,
    start=None,
    end=None,
    dedup: bool = True,
) -> pd.DataFrame:
    """
    Loads and concatenates daily archive files into one DataFrame,
    optionally filtered to a [start, end] date range (inclusive, on
    snapshot_date — the fetch date, not published_at).

    dedup=True (default): drops duplicate articles across overlapping
    lookback windows, keyed on finnhub_id, keeping the EARLIEST-fetched
    copy (snapshot_date ascending) — i.e. the first time we ever saw the
    article, not the most recent re-fetch of it.

    Rows with a null finnhub_id are never deduplicated (kept as-is),
    since there's no reliable key to match them on.

    Returns an empty DataFrame if no files are found in range.
    """
    folder = Path(folder_path)
    if not folder.exists():
        print(f"⚠️ Folder {folder_path} does not exist yet — nothing has been saved.")
        return pd.DataFrame()

    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None

    frames = []
    for file_path in sorted(folder.glob("news_*.parquet")):
        date_str = file_path.stem.replace("news_", "")
        try:
            file_date = pd.Timestamp(date_str)
        except ValueError:
            continue

        if start_ts is not None and file_date < start_ts:
            continue
        if end_ts is not None and file_date > end_ts:
            continue

        frames.append(pd.read_parquet(file_path))

    if not frames:
        print("⚠️ No files found in the requested date range.")
        return pd.DataFrame()

    result = pd.concat(frames, ignore_index=True)
    total_before = len(result)

    if dedup:
        result = result.sort_values("snapshot_date")

        has_id = result["finnhub_id"].notna()
        with_id = result[has_id].drop_duplicates(subset="finnhub_id", keep="first")
        without_id = result[~has_id]

        result = pd.concat([with_id, without_id], ignore_index=True)
        result = result.sort_values(["published_at", "ticker"], na_position="last").reset_index(drop=True)

        dropped = total_before - len(result)
        print(f"📂 Loaded {len(frames)} days, {total_before:,} raw rows -> {len(result):,} after dedup ({dropped:,} removed).")
    else:
        print(f"📂 Loaded {len(frames)} days, {total_before:,} total rows (no dedup).")

    return result


def archive_summary(folder_path: str = DEFAULT_ARCHIVE_FOLDER) -> None:
    """Quick sanity check: how many days are archived, total size, date range."""
    folder = Path(folder_path)
    if not folder.exists():
        print(f"⚠️ Folder {folder_path} does not exist yet.")
        return

    files = sorted(folder.glob("news_*.parquet"))
    if not files:
        print("⚠️ Empty archive — no days saved yet.")
        return

    total_size_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
    dates = [f.stem.replace("news_", "") for f in files]

    print("── News archive summary ────────────────────────────────────")
    print(f"  Days archived: {len(files)}")
    print(f"  Range: {dates[0]} → {dates[-1]}")
    print(f"  Total size: {total_size_mb:.1f} MB")
