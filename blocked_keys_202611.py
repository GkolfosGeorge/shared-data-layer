"""
blocked_keys_202611.py — READ-ONLY: rows the parser produces correctly but the
database REFUSES, because their key (ticker, metric, fy, fp, form) is already held
by a row for ANOTHER period (ON CONFLICT DO NOTHING keeps the old one). 20/11/2026.
Run from the shared data layer folder.

    python blocked_keys_202611.py XOM
    python blocked_keys_202611.py XOM C V
    python blocked_keys_202611.py --sp500          (~1 parse per company, slow)

Why: XOM's database rows labeled "2025 Q2" and "2025 Q3" are the PRIOR-year
comparative columns (2024-01-01 → 2024-06-30, filed 2025-08-04), taken from its old
CIK 34088 after the 2025 reorganization. If the new CIK's data has the right 2025
row under the same label, the database can never take it. Example line:
    EPS  2025 Q2 10-Q · parser: 2025-01-01→2025-06-30 (8.18)
         ✗ held by 2024-01-01→2024-06-30, CIK 34088, filed 2025-08-04
It also shows, per company, how many database rows look like that holder:
a period ~1 year older than the label suggests (a comparative stored as own).
"""

import sys
import time

import pandas as pd
from sqlalchemy import text

from db import get_engine
from fundamentals import fetch_companyfacts, parse_companyfacts
from ticker_provider import get_cik_for_ticker, get_tickers

TOL = pd.Timedelta(days=7)
KEY = ["metric", "fiscal_year", "fiscal_period", "form_type"]


def db_rows(eng, ticker: str) -> pd.DataFrame:
    sql = text("""SELECT m.name, f.fiscal_year, f.fiscal_period, ft.name, f.period_start, f.period_end,
                         f.filed_date, f.cik, f.value
                  FROM fundamentals f JOIN metric_lookup m ON m.id = f.metric_id
                  JOIN form_type_lookup ft ON ft.id = f.form_type_id WHERE f.ticker = :t""")
    with eng.connect() as c:
        df = pd.DataFrame(c.execute(sql, {"t": ticker}).fetchall(),
                          columns=KEY + ["period_start", "period_end", "filed_date", "cik", "value"])
    for col in ("period_start", "period_end", "filed_date"):
        df[col] = pd.to_datetime(df[col])
    return df


def blocked(parsed: pd.DataFrame, db: pd.DataFrame) -> pd.DataFrame:
    """Parsed rows whose key is in the database with a period_end more than 7 days away."""
    if parsed.empty or db.empty:
        return parsed.iloc[0:0]
    m = parsed.merge(db, on=KEY, suffixes=("", "_db"))
    return m[(m["period_end"] - m["period_end_db"]).abs() > TOL]


def selftest() -> None:
    T = pd.Timestamp
    p = pd.DataFrame([("EPS", 2025, "Q2", "10-Q", T("2025-01-01"), T("2025-06-30"), T("2026-08-03"), 2, 8.2)],
                     columns=KEY + ["period_start", "period_end", "filed_date", "cik", "value"])
    d = p.assign(period_start=T("2024-01-01"), period_end=T("2024-06-30"), filed_date=T("2025-08-04"), cik=1)
    assert len(blocked(p, d)) == 1
    assert blocked(p, p).empty                     # same period → not blocked
    print("selftest: όλα τα τεστ πέρασαν ✓")


def main(tickers: list[str]) -> None:
    eng = get_engine()
    tot = 0
    for t in tickers:
        cik = get_cik_for_ticker(t)
        if cik is None:
            continue
        try:
            facts = fetch_companyfacts(cik)
            parsed = parse_companyfacts(t, cik, facts) if facts else pd.DataFrame()
        except Exception as e:
            print(f"  {t}: σφάλμα {type(e).__name__}: {e}")
            continue
        b = blocked(parsed, db_rows(eng, t))
        if b.empty:
            if len(tickers) <= 20:
                print(f"══ {t} (CIK {cik}): κανένα μπλοκαρισμένο κλειδί")
            continue
        tot += len(b)
        print(f"══ {t} (CIK {cik}): {len(b)} μπλοκαρισμένες γραμμές ══")
        for (fy, fp, form), g in b.groupby(["fiscal_year", "fiscal_period", "form_type"]):
            r = g.iloc[0]
            print(f"  {int(fy)} {fp} {form} · {len(g)} μεγέθη · parser {r.period_start:%Y-%m-%d}→{r.period_end:%Y-%m-%d}"
                  f" (κατάθ. {r.filed_date:%Y-%m-%d}) ✗ στη βάση {r.period_start_db:%Y-%m-%d}→{r.period_end_db:%Y-%m-%d}"
                  f", CIK {int(r.cik_db) if pd.notna(r.cik_db) else '—'}, κατάθ. {r.filed_date_db:%Y-%m-%d}")
        print()
        time.sleep(0.15)
    print(f"Σύνολο μπλοκαρισμένων γραμμών: {tot}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--sp500" in sys.argv:
        main(get_tickers("sp500", folder_path="cache"))
    else:
        main([a.upper() for a in sys.argv[1:]] or ["XOM"])
