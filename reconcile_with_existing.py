"""
reconcile_with_existing.py — Ελέγχει ότι το fundamentals.py parsing
αναπαράγει ΑΚΡΙΒΩΣ τα ίδια values/units/dates με ό,τι ήδη υπάρχει στη
βάση, για rows που επικαλύπτονται (ίδιο ticker+metric+fiscal_year+
fiscal_period+form_type). Δεν γράφει τίποτα — μόνο fetch + compare.

Usage:
    python reconcile_with_existing.py
"""

import pandas as pd
from sqlalchemy import text

from db import get_engine
from fundamentals import fetch_companyfacts, parse_companyfacts
from ticker_provider import get_cik_for_ticker

# Μείγμα κλάδων/μεγεθών — όχι μόνο tech, ώστε να πιάσουμε πιθανές
# ιδιαιτερότητες ανά sector (π.χ. τράπεζες συχνά δεν έχουν InventoryNet).
SAMPLE_TICKERS = ["AAPL", "MSFT", "JPM", "XOM", "WMT", "JNJ"]


def reconcile_ticker(ticker: str, engine) -> pd.DataFrame:
    cik = get_cik_for_ticker(ticker)
    facts = fetch_companyfacts(cik)
    df_new = parse_companyfacts(ticker, cik, facts)

    with engine.connect() as conn:
        existing = pd.read_sql(
            text("""
                SELECT
                    m.name AS metric, f.value, u.name AS unit,
                    f.fiscal_year, f.fiscal_period, f.period_end,
                    f.filed_date, ft.name AS form_type
                FROM fundamentals f
                JOIN metric_lookup m ON m.id = f.metric_id
                LEFT JOIN unit_lookup u ON u.id = f.unit_id
                LEFT JOIN form_type_lookup ft ON ft.id = f.form_type_id
                WHERE f.ticker = :ticker
            """),
            conn,
            params={"ticker": ticker},
        )

    if existing.empty or df_new.empty:
        return pd.DataFrame()

    merged = df_new.merge(
        existing,
        on=["metric", "fiscal_year", "fiscal_period", "form_type"],
        suffixes=("_new", "_db"),
        how="inner",
    )

    if merged.empty:
        print(f"  {ticker}: 0 επικαλυπτόμενα rows")
        return pd.DataFrame()

    # merge() already suffixed the overlapping columns (value, unit,
    # period_end, filed_date) to *_new / *_db automatically — no manual
    # renaming needed here.
    merged["period_end_new"] = pd.to_datetime(merged["period_end_new"])
    merged["period_end_db"] = pd.to_datetime(merged["period_end_db"])

    mismatch = (
        (merged["value_new"].round(2) != merged["value_db"].round(2))
        | (merged["unit_new"] != merged["unit_db"])
        | (merged["period_end_new"] != merged["period_end_db"])
    )

    diffs = merged[mismatch][[
        "metric", "fiscal_year", "fiscal_period", "form_type",
        "value_new", "value_db", "unit_new", "unit_db",
        "period_end_new", "period_end_db",
    ]]

    print(f"  {ticker}: {len(merged)} επικαλυπτόμενα rows, {len(diffs)} mismatches")
    return diffs


def main():
    eng = get_engine()
    print(f"Reconciling {len(SAMPLE_TICKERS)} tickers...\n")

    all_diffs = []
    for ticker in SAMPLE_TICKERS:
        diffs = reconcile_ticker(ticker, eng)
        if not diffs.empty:
            diffs.insert(0, "ticker", ticker)
            all_diffs.append(diffs)

    print("\n── Αποτέλεσμα ──────────────────────────────")
    if all_diffs:
        result = pd.concat(all_diffs, ignore_index=True)
        print(f"⚠️  Βρέθηκαν {len(result)} mismatches συνολικά:")
        print(result.to_string())
        result.to_csv("reconciliation_mismatches.csv", index=False)
        print("\nΑποθηκεύτηκε: reconciliation_mismatches.csv")
    else:
        print("✅ Μηδέν mismatches — το νέο parsing αναπαράγει ακριβώς τα ίδια values/units/dates με ό,τι ήδη υπάρχει.")


if __name__ == "__main__":
    main()
