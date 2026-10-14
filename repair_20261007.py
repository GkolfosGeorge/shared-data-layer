"""
repair_20261007.py — one-off repair of quarterly rows written by the pre-v0.1.20
dedup logic (comparative columns stored under the current filing's fy/fp label).

For every ticker in `fundamentals`:
  1. parse SEC companyfacts with the CURRENT fundamentals.py (Stage 0 = filing's own period)
  2. compare with what the DB holds, key = (metric, fiscal_year, fiscal_period, form_type),
     ONLY for metrics this parser owns (XBRL_TAG_MAP). Legacy rows from the pre-built DB
     (snake_case names like `revenue`, `eps_basic`) are out of scope and never touched.
  3. in ONE transaction: save rows that differ / no longer exist to a LOCAL parquet file
     (backup_20261007/<ticker>.parquet -- keeps Neon free-tier storage untouched), then delete them
  4. upsert only the rows that changed or are new

Safety:
  - ticker whose DB cik differs from today's SEC cik (ticker recycling) -> untouched
  - more than MAX_SHARE of a ticker's rows would change                 -> untouched
  - backup / delete / expected counts disagree                          -> ROLLBACK
  - local backup file can't be written                                  -> ROLLBACK (nothing deleted)
Restore a ticker: read its parquet and INSERT the rows back (see restore_ticker below).
Resumable: results are appended to repair_20261007_log.csv after every ticker;
re-running skips tickers already logged as ok / check_inserts.

Run from Git Bash, inside the shared-data-layer folder:
  "/c/Users/georg/AppData/Local/Programs/Python/Python314/python.exe" repair_20261007.py
"""

import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

import fundamentals as F
from db import get_engine

BACKUP_DIR = Path("backup_20261007")
RUN_ID = time.strftime("%H%M%S")      # backup files never overwrite an earlier run's
LOG_COLS = ["ticker", "status", "changed", "filed_only", "deleted_only", "new", "deleted", "inserted", "note"]
# Pass 2 (Oct 8): compare now includes filed_date -> fresh log so every ticker is re-checked.
LOG = os.environ.get("REPAIR_LOG", "repair_20261008_log.csv")
# Safety brake: skip a ticker if more than this share of its rows would change.
# Override for a deliberate run:  MAX_SHARE=1 python repair_20261007.py
# (Oct 2026: tickers never re-ingested since the first, pre-v0.1.20 load --
#  delisted ones and a few like AEP -- hold the comparative column in ~100% of
#  their multi-column facts, e.g. CTL AccountsPayable = 394,687,000 for FY2010,
#  Q2 and Q3 alike, all dated 2009-12-31. The brake rightly stopped them;
#  diagnosis confirmed the new rows are the correct ones.)
MAX_SHARE = float(os.environ.get("MAX_SHARE", "0.4"))
VACUUM_EVERY = 25          # plain VACUUM (NOT FULL): marks deleted rows' space reusable
MAX_DB_MB = 450            # stop if pg_database_size passes this (Neon free tier = 512 MB)
KEY = ["metric", "fiscal_year", "fiscal_period", "form_type"]

ROWS_WHERE = """
    f.ticker = :t AND m.id = f.metric_id AND ft.id = f.form_type_id
    AND (m.name, f.fiscal_year, f.fiscal_period, ft.name) IN (
        SELECT * FROM unnest(CAST(:names AS text[]), CAST(:fys AS int[]),
                             CAST(:fps AS text[]), CAST(:forms AS text[])))
"""


def compare(old: pd.DataFrame, new: pd.DataFrame):
    for d in (old, new):
        for c in ("period_start", "period_end", "filed_date"):
            d[c] = pd.to_datetime(d[c])
        d["fiscal_year"] = d["fiscal_year"].astype(int)
        d["value"] = pd.to_numeric(d["value"], errors="coerce").astype(float)
    m = old.merge(new, on=KEY, how="outer", suffixes=("_db", "_new"), indicator=True)
    both = m[m["_merge"] == "both"]
    # filed_date matters as much as the value: a row with the right period and
    # value but a LATER filed_date (a comparative copy from next year's filing)
    # makes the number "known" a year late in every point-in-time read.
    # (Oct 8: NVDA had 89 such rows -- SEC mislabels its fy, so the
    #  comparative and the original share one key.)
    diff = (
        (both["filed_date_db"] != both["filed_date_new"])
        | (both["period_end_db"] != both["period_end_new"])
        | ~((both["period_start_db"] == both["period_start_new"])
            | (both["period_start_db"].isna() & both["period_start_new"].isna()))
        | ~np.isclose(both["value_db"], both["value_new"], rtol=1e-9, equal_nan=True)
    )
    return both[diff], m[m["_merge"] == "left_only"], m[m["_merge"] == "right_only"]


def filed_only_count(changed: pd.DataFrame) -> int:
    """Rows whose period and value were already right -- only filed_date was wrong."""
    same_val = np.isclose(changed["value_db"], changed["value_new"], rtol=1e-9, equal_nan=True)
    same_end = changed["period_end_db"] == changed["period_end_new"]
    return int((same_val & same_end).sum())


def repair(ticker: str, eng, caches) -> dict:
    cik = F.get_cik_for_ticker(ticker)
    via_db = False
    if cik is None:
        # Delisted / renamed ticker: SEC's ticker map no longer knows it, but a
        # CIK is permanent per company and SEC keeps its companyfacts forever.
        # Use the CIK we stored when the rows were first ingested -- only if
        # there is exactly ONE (two would mean ticker recycling; leave alone).
        with eng.connect() as c:
            ciks = [r[0] for r in c.execute(
                text("SELECT DISTINCT cik FROM fundamentals WHERE ticker = :t"), {"t": ticker})]
        if len(ciks) != 1:
            return {"ticker": ticker, "status": "no_cik", "note": f"db ciks {ciks}"}
        cik, via_db = int(ciks[0]), True
    facts = F.fetch_companyfacts(cik)
    if facts is None:
        return {"ticker": ticker, "status": "no_facts"}
    new = F.parse_companyfacts(ticker, cik, facts)
    if new.empty:
        return {"ticker": ticker, "status": "empty_parse"}

    with eng.connect() as c:
        db_ciks = {r[0] for r in c.execute(
            text("SELECT DISTINCT cik FROM fundamentals WHERE ticker = :t"), {"t": ticker})}
    if db_ciks and db_ciks != {cik}:
        return {"ticker": ticker, "status": "cik_mismatch", "note": f"db {sorted(db_ciks)} vs sec {cik}"}

    old = F.get_fundamentals(ticker, eng)
    old = old[old["metric"].isin(F.XBRL_TAG_MAP)].reset_index(drop=True)   # only what we own
    changed, only_db, only_new = compare(old, new.copy())
    if via_db:
        # Renamed tickers (FB -> META, GPS -> GAP, ...): this CIK's facts also
        # cover years filed under the NEW ticker, which already has its own rows.
        # Fix only what this old ticker already holds; never add new periods.
        only_new = only_new.iloc[0:0]
    to_del = pd.concat([changed[KEY], only_db[KEY]], ignore_index=True)
    if len(old) and len(to_del) > MAX_SHARE * len(old):
        return {"ticker": ticker, "status": "too_many_changes", "note": f"{len(to_del)}/{len(old)}"}

    n_del = 0
    if len(to_del):
        p = {"t": ticker,
             "names": to_del["metric"].tolist(),
             "fys": [int(x) for x in to_del["fiscal_year"]],
             "fps": to_del["fiscal_period"].tolist(),
             "forms": to_del["form_type"].tolist()}
        with eng.begin() as c:  # backup + delete: all or nothing
            rows = c.execute(text(f"""SELECT f.*
                FROM fundamentals f, metric_lookup m, form_type_lookup ft WHERE {ROWS_WHERE}"""), p)
            bk = pd.DataFrame(rows.fetchall(), columns=list(rows.keys()))
            if len(bk) != len(to_del):
                raise RuntimeError(f"backup {len(bk)} / expected {len(to_del)} -> ROLLBACK")
            BACKUP_DIR.mkdir(exist_ok=True)
            bk.to_parquet(BACKUP_DIR / f"{ticker}_{RUN_ID}.parquet", index=False)  # fails -> rollback
            n_del = c.execute(text(f"""DELETE FROM fundamentals f
                USING metric_lookup m, form_type_lookup ft WHERE {ROWS_WHERE}"""), p).rowcount
            if n_del != len(bk):
                raise RuntimeError(f"backup {len(bk)} / delete {n_del} -> ROLLBACK")

    keys = pd.concat([changed[KEY], only_new[KEY]], ignore_index=True).drop_duplicates()
    sub = new.merge(keys, on=KEY, how="inner")
    expected_ins = len(sub)
    res = F.upsert_fundamentals(sub, eng, caches=caches) if len(sub) else {"inserted": 0}
    return {"ticker": ticker,
            "status": "ok" if res["inserted"] == expected_ins else "check_inserts",
            "note": "cik from db (delisted)" if via_db else None,
            "changed": len(changed), "filed_only": filed_only_count(changed),
            "deleted_only": len(only_db), "new": len(only_new),
            "deleted": n_del, "inserted": res["inserted"]}


def db_size_mb(eng) -> float:
    with eng.connect() as c:
        return c.execute(text("SELECT pg_database_size(current_database())")).scalar() / 2**20


def vacuum(eng) -> None:
    # Postgres doesn't reuse a deleted row's space until VACUUM has seen it, and
    # autovacuum only kicks in after ~20% of the table (~260k rows) is dead --
    # i.e. never during this run. Plain VACUUM needs no extra disk space
    # (unlike VACUUM FULL, which rewrites the whole 370 MB table -- never run that).
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("VACUUM fundamentals"))


def main():
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print("fundamentals from:", F.__file__, "| Stage 0:", hasattr(F, "OWN_PERIOD_MIN_SHARE"))
    if not hasattr(F, "OWN_PERIOD_MIN_SHARE"):
        raise SystemExit("Old fundamentals.py loaded -- run from the shared-data-layer folder.")

    eng = get_engine()
    since = os.environ.get("ONLY_INSERTED_SINCE")   # e.g. 2026-10-12: only tickers a run touched
    with eng.connect() as c:
        if since:
            tickers = [r[0] for r in c.execute(text(
                "SELECT DISTINCT ticker FROM fundamentals "
                "WHERE inserted_at >= CAST(:d AS DATE) ORDER BY 1"), {"d": since})]
            print(f"ONLY_INSERTED_SINCE={since}: {len(tickers)} tickers")
        else:
            tickers = [r[0] for r in c.execute(text("SELECT DISTINCT ticker FROM fundamentals ORDER BY 1"))]

    done = set()
    if os.path.exists(LOG):
        prev = pd.read_csv(LOG)
        if "note" not in prev.columns:     # first-version log: notes landed in 'changed'
            numeric = pd.to_numeric(prev["changed"], errors="coerce")
            prev["note"] = prev["changed"].where(numeric.isna() & prev["changed"].notna())
            prev["changed"] = numeric
            prev.reindex(columns=LOG_COLS).to_csv(LOG, index=False)
        done = set(prev.loc[prev["status"].isin(["ok", "check_inserts"]), "ticker"])
    todo = [t for t in tickers if t not in done]
    if limit:
        todo = todo[:limit]
    start_mb = db_size_mb(eng)
    print(f"{len(tickers)} tickers selected, {len(done)} already done (log {LOG}), {len(todo)} in this run")
    print(f"DB size at start: {start_mb:.0f} MB (stop at {MAX_DB_MB} MB) | MAX_SHARE = {MAX_SHARE}\n")

    caches = F.load_lookup_caches(eng)
    t0 = time.time()
    for i, t in enumerate(todo, 1):
        try:
            r = repair(t, eng, caches)
        except Exception as e:
            r = {"ticker": t, "status": "error", "note": str(e)[:300]}
        pd.DataFrame([r]).reindex(columns=LOG_COLS).to_csv(
            LOG, mode="a", header=not os.path.exists(LOG), index=False)
        flag = "" if r["status"] == "ok" else "   <--"
        print(f"[{i}/{len(todo)}] {t:6s} {r['status']:16s} "
              f"changed={r.get('changed', '-')} (filed_only={r.get('filed_only', '-')}) del={r.get('deleted', '-')} "
              f"ins={r.get('inserted', '-')}{flag}")
        time.sleep(F.REQUEST_SLEEP)
        if i % VACUUM_EVERY == 0 or i == len(todo):
            vacuum(eng)
            mb = db_size_mb(eng)
            el = time.time() - t0
            print(f"  ... VACUUM ok | DB {mb:.0f} MB ({mb - start_mb:+.1f}) | "
                  f"{el / 60:.0f} min, ~{el / i * (len(todo) - i) / 60:.0f} min left")
            if mb > MAX_DB_MB:
                print(f"\n🛑 DB passed {MAX_DB_MB} MB -- stopping. Re-run later to resume.")
                break

    log = pd.read_csv(LOG).drop_duplicates("ticker", keep="last")   # retried tickers: latest result
    print("\n── Summary ──")
    print(log["status"].value_counts())
    num = log[["changed", "filed_only", "deleted_only", "new", "deleted", "inserted"]].apply(pd.to_numeric, errors="coerce")
    print("\nTotals:", num.sum().astype(int).to_dict())
    bad = log[~log["status"].isin(["ok"])]
    if len(bad):
        print("\nNeeds a look:")
        print(bad.to_string(index=False))


def restore_ticker(ticker: str, eng) -> int:
    """Put a ticker's backed-up rows back (only needed if something went wrong).
    Run the repair again afterwards ONLY if you want to redo it."""
    bk = pd.read_parquet(BACKUP_DIR / f"{ticker}.parquet")
    cols = [c for c in bk.columns if c != "id"]          # let the DB assign ids if there is one
    bk = bk[cols].astype(object).where(bk[cols].notna(), None)
    stmt = text(f"INSERT INTO fundamentals ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
                "ON CONFLICT DO NOTHING")
    with eng.begin() as c:
        return c.execute(stmt, bk.to_dict("records")).rowcount


if __name__ == "__main__":
    main()
