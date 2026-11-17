"""
sync_companies_202611.py — one-off for v0.1.28 (17/11/2026). Run from the shared data layer folder.

    python sync_companies_202611.py            # DRY RUN: shows what would change, writes nothing
    python sync_companies_202611.py --write    # writes `companies` + ingests BRK-B, BF-B

What it does:
  1. Re-scrapes today's S&P 500 (the cached list from before v0.1.28 still says
     "BRK" and "BF") and shows that BRK-B / BF-B are now in it.
  2. `companies` vs today's SEC list for those ~503 tickers (fundamentals.sync_companies):
     new rows (VMRK, SKYD, VYLR...), another CIK (XOM?), new names.
     Only `companies` is written: one small row per company, ~0 MB.
  3. BRK-B and BF-B: what we have today, then (with --write) the normal ingest for
     them (fundamentals.ingest_fundamentals_for_ticker, skip if nothing new).
After --write, the weekly ingest keeps `companies` current on its own (v0.1.28).
"""

import sys

from sqlalchemy import text

from db import get_engine
from fundamentals import ingest_fundamentals_for_ticker, sync_companies
from ticker_provider import get_cik_for_ticker, get_tickers

CLASS_SHARES = ["BRK-B", "BF-B"]


def db_mb(eng) -> float:
    with eng.connect() as c:
        return c.execute(text("SELECT pg_database_size(current_database())")).scalar() / 1e6


def main() -> None:
    write = "--write" in sys.argv
    eng = get_engine()
    print(f"{'ΕΓΓΡΑΦΗ' if write else 'DRY RUN (τίποτα δεν γράφεται)'} · βάση {db_mb(eng):.0f} MB\n")

    tickers = get_tickers("sp500", folder_path="cache", max_age_hours=0)     # fresh scrape
    print(f"S&P 500: {len(tickers)} σύμβολα · " + " · ".join(
        f"{t} {'✓' if t in tickers else '✗'}" for t in CLASS_SHARES + ["BRK", "BF"]))

    changes = sync_companies(tickers, eng, write=write)
    print(f"\ncompanies: {len(changes)} αλλαγές")
    for kind in ["νέα", "άλλο CIK", "τίτλος"]:
        sub = [c for c in changes if c["kind"] == kind]
        if not sub:
            continue
        print(f"  {kind} ({len(sub)}):")
        for c in sub:
            was = "" if kind == "νέα" else f"  (ήταν: CIK {c['old_cik']} «{c['old_title']}»)"
            print(f"    {c['ticker']:6s} CIK {c['cik']} «{c['title']}»{was}")

    print("\nΜετοχές κατηγορίας:")
    with eng.connect() as conn:
        for t in CLASS_SHARES:
            n, last = conn.execute(text("SELECT COUNT(*), MAX(filed_date) FROM fundamentals WHERE ticker = :t"),
                                   {"t": t}).fetchone()
            print(f"  {t}: CIK {get_cik_for_ticker(t)} · στη βάση {n:,} γραμμές, τελευταία κατάθεση {last}")
    if write:
        for t in CLASS_SHARES:
            r = ingest_fundamentals_for_ticker(t, eng, skip_if_no_new_filing=True)
            print(f"  → ingest {t}: {r}")
        print(f"\nβάση μετά: {db_mb(eng):.0f} MB")
    else:
        print("\nΓια να γραφτούν: python sync_companies_202611.py --write")


if __name__ == "__main__":
    main()
