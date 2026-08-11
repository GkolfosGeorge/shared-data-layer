from db import get_engine
from sqlalchemy import text
from fundamentals import ingest_fundamentals_for_universe

eng = get_engine()

with eng.connect() as conn:
    rows = conn.execute(text("""
        SELECT ticker FROM index_membership
        WHERE index_name = 'SP500' AND date_removed IS NULL
        ORDER BY ticker;
    """)).fetchall()

tickers = [r[0] for r in rows]
print(f"Θα γίνει ingest για {len(tickers)} tickers.\n")

summary = ingest_fundamentals_for_universe(tickers, eng)

print("\n── Summary ──────────────────────────────────")
print(summary["status"].value_counts())
print("\nΤυχόν προβληματικά tickers:")
print(summary[summary["status"] != "ok"])

summary.to_csv("fundamentals_ingest_run.csv", index=False)
print("\nΑποθηκεύτηκε πλήρες log: fundamentals_ingest_run.csv")
