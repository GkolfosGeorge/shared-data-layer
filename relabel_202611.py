"""
relabel_202611.py — one-off for v0.1.29 (18/11/2026). Run from the shared data layer folder.

    python relabel_202611.py                 # DRY RUN, whole universe (~45–60 min): writes nothing
    python relabel_202611.py --sp500         # DRY RUN, today's S&P 500 only (~30 min)
    python relabel_202611.py CRM WDAY MTD    # DRY RUN, only these (seconds)
    python relabel_202611.py --write ...     # same, then inserts ONLY the recovered rows
    python relabel_202611.py --write --from-csv
                                             # only the companies the last dry run found
                                             # (relabel_202611.csv, insertable > 0) → relabel_202611_write.csv

Why: some filers label a new filing with the same (fiscal year, period) as an
older one (CRM's 10-K for 2026-01-31 says "fy=2025 FY"). The pre-v0.1.29 parser
kept the older and lost the whole new filing. v0.1.29 gives such filings a
fiscal year derived from their own period (fundamentals._relabel_fiscal_years).

For each company it parses the SEC data TWICE — old parser (relabel=False) and
new (relabel=True) — and compares, row by row, on (metric, form, period_end):
    recovered  rows only the new parser has
      → already in the database   (by period, ±7 days): nothing to do
      → key taken                 the database already has (metric, fy, fp, form)
                                  for another period: ON CONFLICT would skip it
      → insertable                what --write inserts
    unresolved periods still lost (the corrected label is taken too)
    lost       rows only the OLD parser has — must be 0 (sanity check)
→ relabel_202611.csv (one line per company, appended as it goes)
"""

import os
import sys
import time

import pandas as pd
from sqlalchemy import text

from db import get_engine
from fundamentals import fetch_companyfacts, load_lookup_caches, parse_companyfacts, upsert_fundamentals
from ticker_provider import get_cik_for_ticker, get_tickers

OUT = "relabel_202611.csv"
TOL = pd.Timedelta(days=7)
BYTES_PER_ROW = 295          # measured 17/11 (fundamentals + indexes)


def periods(df: pd.DataFrame) -> pd.DataFrame:
    return df[["metric", "form_type", "period_end"]].drop_duplicates()


def not_in(a: pd.DataFrame, b: pd.DataFrame, extra=()) -> pd.DataFrame:
    """Rows of a whose (metric, form, period_end ±7 days) has no match in b."""
    if a.empty:
        return a
    if b.empty:
        return a
    m = a.reset_index().merge(b[["metric", "form_type", "period_end", *extra]],
                              on=["metric", "form_type"], how="left", suffixes=("", "_b"))
    m["hit"] = (m["period_end"] - m["period_end_b"]).abs() <= TOL
    hit = m.groupby("index")["hit"].any()
    return a.loc[hit[~hit].index]


def db_keys(eng, ticker: str) -> pd.DataFrame:
    sql = text("""SELECT m.name, f.fiscal_year, f.fiscal_period, ft.name, f.period_end
                  FROM fundamentals f JOIN metric_lookup m ON m.id = f.metric_id
                  JOIN form_type_lookup ft ON ft.id = f.form_type_id WHERE f.ticker = :t""")
    with eng.connect() as conn:
        df = pd.DataFrame(conn.execute(sql, {"t": ticker}).fetchall(),
                          columns=["metric", "fiscal_year", "fiscal_period", "form_type", "period_end"])
    df["period_end"] = pd.to_datetime(df["period_end"])
    return df


def check(ticker: str, cik: int, facts: dict, db: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    old = parse_companyfacts(ticker, cik, facts, relabel=False)
    new = parse_companyfacts(ticker, cik, facts)
    rec = new[new["_relabeled"]] if "_relabeled" in new else new.iloc[0:0]
    rec = not_in(rec, old)
    already = len(rec) - len(not_in(rec, db))
    rec = not_in(rec, db)
    if not rec.empty and not db.empty:
        k = rec.reset_index().merge(db, on=["metric", "fiscal_year", "fiscal_period", "form_type"],
                                    how="left", suffixes=("", "_db"))
        taken_idx = k[k["period_end_db"].notna()]["index"].unique()
    else:
        taken_idx = []
    insertable = rec.drop(index=taken_idx)
    lost = not_in(old, new)
    u = new.attrs.get("unresolved", [])
    r = {"ticker": ticker, "recovered": len(rec) + already, "already_in_db": already,
         "key_taken": len(taken_idx), "insertable": len(insertable),
         "periods": ", ".join(sorted(insertable["period_end"].dt.strftime("%Y-%m-%d").unique())),
         "unresolved": len(u), "lost_sanity": len(lost)}
    return r, insertable


def main() -> None:
    global OUT
    write = "--write" in sys.argv
    args = [a.upper() for a in sys.argv[1:] if not a.startswith("--")]
    eng = get_engine()
    if "--from-csv" in sys.argv:
        prev = pd.read_csv(OUT)
        tickers = prev.loc[prev.get("insertable", 0).fillna(0) > 0, "ticker"].tolist()
        OUT = OUT.replace(".csv", "_write.csv" if write else "_rerun.csv")   # keep the dry run's file
    elif args:
        tickers = args
    elif "--sp500" in sys.argv:
        tickers = get_tickers("sp500", folder_path="cache")
    else:
        with eng.connect() as conn:
            tickers = [r[0] for r in conn.execute(text(
                "SELECT DISTINCT f.ticker FROM fundamentals f JOIN companies c ON c.ticker = f.ticker "
                "ORDER BY 1")).fetchall()]
    print(f"{'ΕΓΓΡΑΦΗ' if write else 'DRY RUN (τίποτα δεν γράφεται)'} · {len(tickers)} εταιρείες\n", flush=True)
    caches = load_lookup_caches(eng) if write else None
    if os.path.exists(OUT):
        os.remove(OUT)

    results, t0 = [], time.time()
    for i, t in enumerate(tickers, 1):
        cik = get_cik_for_ticker(t)
        if cik is None:
            continue
        try:
            facts = fetch_companyfacts(cik)
            if not facts:
                continue
            r, insertable = check(t, cik, facts, db_keys(eng, t))
        except Exception as e:
            r, insertable = {"ticker": t, "error": f"{type(e).__name__}: {e}"}, pd.DataFrame()
        if write and len(insertable):
            res = upsert_fundamentals(insertable, eng, caches=caches)
            r["inserted"] = res["inserted"]
        results.append(r)
        pd.DataFrame([r]).to_csv(OUT, mode="a", header=not os.path.exists(OUT), index=False)
        if r.get("recovered") or r.get("unresolved") or r.get("lost_sanity") or r.get("error"):
            print(f"  {t:6s} ξαναβρέθηκαν {r.get('recovered', 0):4d} · ήδη στη βάση {r.get('already_in_db', 0):4d}"
                  f" · πιασμένο κλειδί {r.get('key_taken', 0):3d} · προς εγγραφή {r.get('insertable', 0):4d}"
                  f" · άλυτα {r.get('unresolved', 0):3d}" + (f" · ⚠️ ΧΑΘΗΚΑΝ {r['lost_sanity']}" if r.get("lost_sanity") else "")
                  + (f" · γράφτηκαν {r['inserted']}" if "inserted" in r else "")
                  + (f" · σφάλμα {r['error']}" if r.get("error") else "")
                  + (f"\n         περίοδοι: {r['periods']}" if r.get("periods") else ""), flush=True)
        if i % 50 == 0:
            print(f"  ... {i}/{len(tickers)} σε {(time.time() - t0) / 60:.0f} λεπτά", flush=True)
        time.sleep(0.15)

    df = pd.DataFrame(results).fillna(0)
    hit = df[df.get("recovered", 0) > 0] if "recovered" in df else df.iloc[0:0]
    print(f"\n── Σύνοψη ({(time.time() - t0) / 60:.0f} λεπτά) ──")
    print(f"  εταιρείες με γραμμές που ξαναβρέθηκαν: {len(hit)}")
    for c in ["recovered", "already_in_db", "key_taken", "insertable", "unresolved", "lost_sanity"]:
        if c in df:
            print(f"  {c:15s} {int(df[c].sum()):7,}")
    if "insertable" in df:
        print(f"  χώρος για όσα θα γραφτούν: ~{df['insertable'].sum() * BYTES_PER_ROW / 1e6:.1f} MB")
    if "error" in df and (df["error"] != 0).any():
        print(f"  σφάλματα: {', '.join(df.loc[df['error'] != 0, 'ticker'])}")
    print(f"→ {OUT}" + ("" if write else "\nΓια εγγραφή: ίδια εντολή με --write"))


if __name__ == "__main__":
    main()
