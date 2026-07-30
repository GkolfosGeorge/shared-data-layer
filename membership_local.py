"""
membership_local.py

Local, file-based replacement for db.get_universe_as_of(). Reads the static
CSV exported by export_membership_to_csv.py instead of querying Postgres,
so that anyone cloning the repo can run point-in-time (survivorship-bias-
free) backtests with no database setup at all.

Same filtering logic as db.get_universe_as_of() — a ticker is included if
it was a member at any point during [start, end]:
    date_added <= end AND (date_removed IS NULL OR date_removed >= start)
"""

from pathlib import Path

import pandas as pd

DEFAULT_MEMBERSHIP_CSV = "data/sp500_membership.csv"


def get_universe_as_of_local(
    start,
    end,
    csv_path: str = DEFAULT_MEMBERSHIP_CSV,
) -> list[str]:
    """
    Returns tickers that were EVER members within [start, end], including
    delisted/removed ones — the point-in-time universe for a backtest.

    Drop-in replacement for db.get_universe_as_of(index_name, start, end):
    same filtering semantics, same return type, no `index_name` argument
    since the CSV is already scoped to one index at export time.
    """
    csv_file = Path(csv_path)
    if not csv_file.exists():
        raise FileNotFoundError(
            f"{csv_path} not found. Run export_membership_to_csv.py once "
            f"(if you have DB access) or download the CSV included in this repo."
        )

    df = pd.read_csv(csv_file, parse_dates=["date_added", "date_removed"])

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    mask = (df["date_added"] <= end_ts) & (
        df["date_removed"].isna() | (df["date_removed"] >= start_ts)
    )

    tickers = sorted(df.loc[mask, "ticker"].unique().tolist())
    return tickers
