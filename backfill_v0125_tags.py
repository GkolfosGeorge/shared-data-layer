"""
backfill_v0125_tags.py — writes ONLY the 12 metrics added in v0.1.25 for
every ticker already in the database. Existing metrics are not touched:
rows are filtered to NEW_METRICS_V0125 before the upsert, and the upsert
itself is ON CONFLICT DO NOTHING.

Run from the shared data layer folder, AFTER check_tagmap_v0125.py was clean:
    python backfill_v0125_tags.py            # dry run: 10 tickers, nothing written
    python backfill_v0125_tags.py --write    # the real thing (~30-40 min)

- Resumable: tickers marked ok in backfill_v0125_log.csv are skipped.
- CIK from the database, so delisted tickers work too.
- Safety brake: stops if the database grows past STOP_AT_MB (Neon free tier = 512 MB).
- Connection with pre-ping/recycle/keepalives (Sept 2026: a plain one died silently overnight).
"""

import csv
import os
import sys
import time

sys.path.insert(0, os.getcwd())

import pandas as pd
from sqlalchemy import create_engine, text

import fundamentals as F

LOG = "backfill_v0125_log.csv"
LOG_COLS = ["ticker", "status", "rows", "metrics", "inserted", "error"]   # same columns on EVERY line
STOP_AT_MB = 470
DRY_RUN_TICKERS = 10


def make_engine():
    try:
        from db import get_database_url
        return create_engine(
            get_database_url(), pool_pre_ping=True, pool_recycle=300,
            connect_args={"keepalives": 1, "keepalives_idle": 30,
                          "keepalives_interval": 10, "keepalives_count": 5},
        )
    except ImportError:
        from db import get_engine
        print("⚠️  db.get_database_url δεν βρέθηκε: χρησιμοποιώ get_engine() χωρίς pre-ping")
        return get_engine()


def db_mb(eng) -> float:
    with eng.connect() as c:
        return c.execute(text("SELECT pg_database_size(current_database())")).scalar() / 1e6


def main(write: bool):
    assert hasattr(F, "NEW_METRICS_V0125"), "fundamentals.py δεν είναι το v0.1.25"
    new = set(F.NEW_METRICS_V0125)
    eng = make_engine()
    with eng.connect() as c:
        tc = c.execute(text(
            "SELECT DISTINCT ON (ticker) ticker, cik FROM fundamentals ORDER BY ticker, filed_date DESC"
        )).fetchall()

    done = set()
    if write and os.path.exists(LOG):
        # read raw: logs written before the column fix have ragged lines,
        # so "ok anywhere on the line" is the robust test
        with open(LOG, newline="", encoding="utf-8") as fh:
            done = {r[0] for r in csv.reader(fh) if r and "ok" in r[1:]}
    todo = [(t, cik) for t, cik in tc if t not in done]
    if not write:
        todo = todo[:DRY_RUN_TICKERS]
    start_mb = db_mb(eng)
    print(f"{'ΕΓΓΡΑΦΗ' if write else 'DRY RUN (τίποτα δεν γράφεται)'} | {len(tc)} tickers, "
          f"{len(done)} ήδη έτοιμα, {len(todo)} σε αυτό το run | βάση {start_mb:.0f} MB\n")

    caches = F.load_lookup_caches(eng) if write else None
    t0, total = time.time(), 0
    for i, (t, cik) in enumerate(todo, 1):
        rec = {c: None for c in LOG_COLS}
        rec["ticker"] = t
        try:
            facts = F.fetch_companyfacts(int(cik))
            df = F.parse_companyfacts(t, int(cik), facts) if facts else pd.DataFrame()
            df = df[df["metric"].isin(new)] if not df.empty else df
            rec["rows"] = len(df)
            rec["metrics"] = df["metric"].nunique() if len(df) else 0
            if write and len(df):
                r = F.upsert_fundamentals(df, eng, caches=caches)
                rec["inserted"] = r["inserted"]
                total += r["inserted"]
            rec["status"] = "ok"
        except Exception as e:
            rec["status"], rec["error"] = "error", str(e)[:200]
        if write:
            pd.DataFrame([rec], columns=LOG_COLS).to_csv(LOG, mode="a", header=not os.path.exists(LOG),
                                                         index=False)
        flag = "" if rec["status"] == "ok" else "   <-- " + (rec["error"] or "")
        dash = lambda v: "-" if v is None else v
        print(f"[{i}/{len(todo)}] {t:6s} γραμμές={dash(rec['rows']):>4} "
              f"μεγέθη={dash(rec['metrics'])} εγγράφηκαν={dash(rec['inserted'])}{flag}")
        time.sleep(F.REQUEST_SLEEP)
        if write and i % 50 == 0:
            mb = db_mb(eng)
            el = time.time() - t0
            print(f"  ... {el / 60:.0f} min, ~{el / i * (len(todo) - i) / 60:.0f} min ακόμα | "
                  f"βάση {mb:.0f} MB (+{mb - start_mb:.1f})")
            if mb > STOP_AT_MB:
                print(f"🛑 Η βάση πέρασε τα {STOP_AT_MB} MB — σταματάω. Στείλε αυτή την έξοδο.")
                return

    print(f"\n── Τέλος: {total:,} νέες γραμμές | βάση {db_mb(eng):.0f} MB (ξεκίνησε {start_mb:.0f}) ──")
    if write:
        with open(LOG, newline="", encoding="utf-8") as fh:
            lines = [r for r in csv.reader(fh) if r and r[0] != "ticker"]
        ok = {r[0] for r in lines if "ok" in r[1:]}
        still_bad = sorted({r[0] for r in lines} - ok)
        print(f"ok: {len(ok)}  ακόμα με σφάλμα: {len(still_bad)}"
              + (f"  → {', '.join(still_bad)}" if still_bad else ""))
        if still_bad:
            print("Ξανατρέξε το ίδιο command: θα δοκιμάσει μόνο όσα απέτυχαν.")
        with eng.connect() as c:
            per = pd.read_sql(text("""
                SELECT m.name AS metric, COUNT(DISTINCT f.ticker) AS tickers, COUNT(*) AS rows
                FROM fundamentals f JOIN metric_lookup m ON m.id = f.metric_id
                WHERE m.name = ANY(:m) GROUP BY 1 ORDER BY 2 DESC"""), c, params={"m": list(new)})
        print("\n" + per.to_string(index=False))


if __name__ == "__main__":
    main(write="--write" in sys.argv)
