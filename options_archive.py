"""
options_archive.py
────────────────────────────────────────────────────────────────────────────
Appends each day's full options-chain scan (from OptionsScanner.to_dataframe())
to a local, date-partitioned Parquet archive.

WHY LOCAL PARQUET, NOT THE NEON DATABASE:
  - Options chain snapshots are much larger per-row-count than price data
    (~hundreds to low-thousands of rows PER TICKER, per day). At S&P500
    scale this would blow through Neon's 0.5GB free tier in weeks.
  - Parquet is columnar + compressed — typically 5-10x smaller on disk than
    the equivalent relational rows, and completely free (local disk).
  - One file per day means each day's data is independently readable/
    shareable (e.g. if you ever want to publish a curated subset later).

WHY THIS CAN'T WAIT:
  Unlike prices (yfinance still has the full daily history for a still-
  active ticker, so re-running the pipeline later doesn't lose anything),
  an options chain is a snapshot of THAT SPECIFIC DAY's market — once the
  day passes, there is no free or paid source that reconstructs it
  retroactively. This has to be captured going forward, starting now.

Usage (add ONE line after your existing options_scanner cell — no changes
needed to options_scanner.py itself):

    from options_archive import save_options_snapshot
    save_options_snapshot(options_df)
"""

from pathlib import Path
from typing import Optional

import pandas as pd

DEFAULT_ARCHIVE_FOLDER = "options_archive"


def save_options_snapshot(
    options_df: pd.DataFrame,
    snapshot_date=None,
    folder_path: str = DEFAULT_ARCHIVE_FOLDER,
) -> Optional[Path]:
    """
    Appends today's options_df (as produced by OptionsScanner.to_dataframe())
    to the local archive as one Parquet file per day:
        options_archive/options_YYYY-MM-DD.parquet

    Safe to call multiple times on the same day (overwrites that day's file
    rather than duplicating rows) — e.g. if you re-run the scanner mid-day.

    Returns the path written, or None if options_df was empty (nothing to save).
    """
    if options_df is None or options_df.empty:
        print("⚠️ options_df is empty — nothing to save.")
        return None

    if snapshot_date is None:
        snapshot_date = pd.Timestamp.today().normalize()
    else:
        snapshot_date = pd.Timestamp(snapshot_date).normalize()

    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)

    date_str = snapshot_date.strftime("%Y-%m-%d")
    file_path = folder / f"options_{date_str}.parquet"

    df = options_df.copy()
    # Stamp the snapshot date explicitly in the data itself, not just the
    # filename — makes the archive self-describing once you start
    # concatenating many days together for analysis.
    df["snapshot_date"] = snapshot_date

    df.to_parquet(file_path, index=False)

    size_kb = file_path.stat().st_size / 1024
    print(f"💾 Options snapshot saved: {file_path}  ({len(df):,} rows, {size_kb:.0f} KB)")

    return file_path


def load_options_archive(
    folder_path: str = DEFAULT_ARCHIVE_FOLDER,
    start=None,
    end=None,
) -> pd.DataFrame:
    """
    Loads and concatenates the daily archive files into one DataFrame,
    optionally filtered to a [start, end] date range (inclusive).

    Returns an empty DataFrame if no files are found in range.
    """
    folder = Path(folder_path)
    if not folder.exists():
        print(f"⚠️ Folder {folder_path} does not exist yet — nothing has been saved.")
        return pd.DataFrame()

    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None

    frames = []
    for file_path in sorted(folder.glob("options_*.parquet")):
        date_str = file_path.stem.replace("options_", "")
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
    print(f"📂 Loaded {len(frames)} days, {len(result):,} total rows.")

    return result


def archive_summary(folder_path: str = DEFAULT_ARCHIVE_FOLDER) -> None:
    """Quick sanity check: how many days are archived, total size, date range."""
    folder = Path(folder_path)
    if not folder.exists():
        print(f"⚠️ Folder {folder_path} does not exist yet.")
        return

    files = sorted(folder.glob("options_*.parquet"))
    if not files:
        print("⚠️ Empty archive — no days saved yet.")
        return

    total_size_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
    dates = [f.stem.replace("options_", "") for f in files]

    print("── Options archive summary ─────────────────────────────────")
    print(f"  Days archived: {len(files)}")
    print(f"  Range: {dates[0]} → {dates[-1]}")
    print(f"  Total size: {total_size_mb:.1f} MB")
