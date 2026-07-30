"""
export_membership_to_csv.py

One-off (or occasional, e.g. after an S&P 500 reconstitution) export of the
`index_membership` table from Postgres to a static CSV. This CSV is what
gets committed to the public repo, so that anyone cloning it can run
survivorship-bias-free backtests without setting up a database.

Usage:
    python export_membership_to_csv.py
    python export_membership_to_csv.py --index-name SP500 --out data/sp500_membership.csv

Requires DATABASE_URL to be set (same as the rest of the pipeline) — this
script only reads from your DB, it never writes to it.
"""

import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from db import get_engine


def export_membership(index_name: str, out_path: str) -> Path:
    engine = get_engine()
    index_name = index_name.upper()

    sql = text("""
        SELECT ticker, date_added, date_removed
        FROM index_membership
        WHERE index_name = :index_name
        ORDER BY ticker, date_added
    """)

    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"index_name": index_name})

    if df.empty:
        raise RuntimeError(
            f"No rows found for index_name='{index_name}'. "
            f"Check that load_universe_to_db.py has been run against this database."
        )

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_file, index=False)

    print(f"Exported {len(df):,} rows for {index_name} -> {out_file}")
    return out_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-name", default="SP500")
    parser.add_argument("--out", default="data/sp500_membership.csv")
    args = parser.parse_args()

    export_membership(index_name=args.index_name, out_path=args.out)
