from db import get_engine
from sqlalchemy import text

eng = get_engine()
with eng.connect() as conn:
    total = conn.execute(text("SELECT COUNT(*) FROM fundamentals;")).scalar()
    dupes = conn.execute(text("""
        SELECT ticker, metric_id, fiscal_year, fiscal_period, form_type_id, COUNT(*)
        FROM fundamentals
        GROUP BY ticker, metric_id, fiscal_year, fiscal_period, form_type_id
        HAVING COUNT(*) > 1;
    """)).fetchall()

print("Συνολικά rows στη βάση τώρα:", total)
print("Πραγματικά duplicate keys (θα έπρεπε να είναι 0):", len(dupes))
