"""
db.py — central database connection module. Every script imports its
engine/connection from here.

Configuration is via the DATABASE_URL environment variable:

    postgresql+psycopg2://USER:PASSWORD@HOST/DBNAME?sslmode=require

This file does not read .env — set DATABASE_URL directly, or load it
with python-dotenv before importing this module.
"""

import os
from datetime import date
from pathlib import Path
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Set it to your Postgres connection string, e.g.:\n"
            "  postgresql+psycopg2://USER:PASSWORD@HOST/DBNAME?sslmode=require"
        )
    return url


def get_engine() -> Engine:
    """pool_pre_ping=True so idle connections to serverless Postgres
    (e.g. Neon) reconnect transparently instead of raising a stale-
    connection error."""
    return create_engine(get_database_url(), pool_pre_ping=True)


def init_schema(engine: Engine = None) -> None:
    """Applies schema.sql. Safe to call every run (all statements use
    IF NOT EXISTS)."""
    engine = engine or get_engine()
    ddl = SCHEMA_PATH.read_text()

    # exec_driver_sql supports multiple ;-separated statements in one call;
    # a naive ddl.split(";") would break on semicolons inside SQL comments.
    with engine.begin() as conn:
        conn.exec_driver_sql(ddl)


def get_universe_as_of(index_name: str, start, end, engine: Engine = None) -> list[str]:
    """
    Returns tickers that were EVER members of `index_name` within
    [start, end] — the point-in-time universe for a backtest, including
    delisted/removed tickers (avoids survivorship bias).
    """
    engine = engine or get_engine()

    # index_membership stores index_name in UPPERCASE; normalize so callers
    # don't need to remember the casing convention.
    index_name = index_name.upper()

    sql = text("""
        SELECT DISTINCT ticker
        FROM index_membership
        WHERE index_name = :index_name
          AND date_added <= :end
          AND (date_removed IS NULL OR date_removed >= :start)
        ORDER BY ticker
    """)

    with engine.connect() as conn:
        rows = conn.execute(
            sql, {"index_name": index_name, "start": start, "end": end}
        ).fetchall()

    return [r[0] for r in rows]


def get_membership_table(index_name: str, engine: Engine = None) -> pd.DataFrame:
    """
    Full point-in-time membership table: [ticker, date_added, date_removed],
    one row per interval (date_removed is NaT if still active). Pass to
    backtester.run_backtest(membership=...) to restrict new-position
    candidates to actual index constituents on each review date.
    """
    engine = engine or get_engine()
    index_name = index_name.upper()

    sql = text("""
        SELECT ticker, date_added, date_removed
        FROM index_membership
        WHERE index_name = :index_name
        ORDER BY ticker, date_added
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql, {"index_name": index_name}).fetchall()

    return pd.DataFrame(rows, columns=["ticker", "date_added", "date_removed"])


def _to_date(d):
    """Accepts 'YYYY-MM-DD' strings, datetime.date, or datetime.date-like
    objects (e.g. pandas.Timestamp) and normalizes to datetime.date."""
    if isinstance(d, str):
        return date.fromisoformat(d)
    if hasattr(d, "date") and callable(d.date):
        return d.date()
    return d


def membership_coverage_gap(
    index_name: str,
    start,
    end,
    missing_tickers: list[str],
    engine: Engine = None,
) -> dict:
    """
    Quantifies missing-ticker impact in "company-days" (membership-days
    within [start, end]) rather than raw ticker counts — a ticker missing
    for 3 months matters less than one missing the whole window.
    """
    engine = engine or get_engine()
    index_name = index_name.upper()
    start_d, end_d = _to_date(start), _to_date(end)
    missing_set = set(missing_tickers)

    sql = text("""
        SELECT ticker, date_added, date_removed
        FROM index_membership
        WHERE index_name = :index_name
          AND date_added <= :end
          AND (date_removed IS NULL OR date_removed >= :start)
    """)

    with engine.connect() as conn:
        rows = conn.execute(
            sql, {"index_name": index_name, "start": start, "end": end}
        ).fetchall()

    total_days = 0
    missing_days = 0
    per_ticker_missing: dict[str, int] = {}

    for ticker, date_added, date_removed in rows:
        added = max(date_added, start_d)
        removed = min(date_removed, end_d) if date_removed is not None else end_d
        days = max((removed - added).days, 0)

        total_days += days

        if ticker in missing_set:
            missing_days += days
            per_ticker_missing[ticker] = per_ticker_missing.get(ticker, 0) + days

    missing_pct = (missing_days / total_days * 100) if total_days else 0.0

    return {
        "total_company_days": total_days,
        "missing_company_days": missing_days,
        "missing_pct": missing_pct,
        "per_ticker_missing_days": dict(
            sorted(per_ticker_missing.items(), key=lambda kv: -kv[1])
        ),
    }


def print_coverage_gap_report(result: dict, top_n: int = 15) -> None:
    """Pretty-prints the output of membership_coverage_gap()."""
    print("── Coverage gap report ─────────────────────────────────────")
    print(f"  Total company-days in window: {result['total_company_days']:,}")
    print(f"  Company-days from unavailable tickers: {result['missing_company_days']:,}")
    print(f"  Residual bias: {result['missing_pct']:.2f}%")
    print()
    print(f"  Top {top_n} tickers by impact (most days in window):")
    for ticker, days in list(result["per_ticker_missing_days"].items())[:top_n]:
        print(f"    {ticker:<8} {days:>5} days")


if __name__ == "__main__":
    # Quick manual check: `python db.py` verifies the connection and
    # applies the schema.
    eng = get_engine()
    init_schema(eng)
    with eng.connect() as conn:
        version = conn.execute(text("SELECT version();")).scalar()
    print(f"Connected OK. {version}")
    print("Schema applied (companies, index_membership).")
