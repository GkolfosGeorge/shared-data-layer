"""
xbrl_fallback_202611.py — one-off for v0.1.30 (25/11/2026). Run from the shared data layer folder.

    python xbrl_fallback_202611.py DUK NEE          # DRY RUN, only these (seconds)
    python xbrl_fallback_202611.py --from-gaps      # DRY RUN, every company with a gap in
                                                    # ../single_asset_analyzer/data/companyfacts_gaps_202611.csv
    python xbrl_fallback_202611.py --write ...      # same, then inserts ONLY the insertable rows

Why: since 07/2026 some 10-Q/10-K never reach companyfacts (S&P 500: 60 filings,
39 companies, 22/31 utilities). v0.1.30 reads them from the filing's own XBRL
(xbrl_instance.py + fundamentals.add_missing_filings). This script checks it
BEFORE anything is written:

  A. SAME NUMBERS? For each company, the latest 10-Q that IS in companyfacts is
     read the new way too, and every fact of our tags is compared:
     (tag, unit, start, end) → value. Expect 100% equal. Anything else = stop.
  B. WHAT WOULD BE WRITTEN? companyfacts alone vs companyfacts + fallback,
     both through the same parser, compared on (metric, form, period_end):
        new          rows only the fallback brings
          → already in the database (same metric/form/period ±7 days)
          → key taken  (metric, fy, fp, form) already used by another period:
                       ON CONFLICT would skip it
          → insertable what --write inserts
        lost         rows companyfacts alone has but the merged run has not: must be 0
  C. YAHOO: diluted EPS of the newest recovered quarter vs Yahoo's quarterly
     "Diluted EPS" for the same quarter end.
→ xbrl_fallback_202611.csv (one line per company, appended as it goes)
"""

import os
import sys
import time

import pandas as pd
from sqlalchemy import text

from db import get_engine
from fundamentals import (XBRL_TAG_MAP, accessions_in_facts, add_missing_filings, fetch_companyfacts,
                          fetch_filing_facts, fetch_submissions, load_lookup_caches, parse_companyfacts,
                          upsert_fundamentals)
from ticker_provider import get_cik_for_ticker

OUT = "xbrl_fallback_202611.csv"
GAPS = "../single_asset_analyzer/data/companyfacts_gaps_202611.csv"
TOL = pd.Timedelta(days=7)
BYTES_PER_ROW = 295          # measured 17/11 (fundamentals + indexes)
OUR_TAGS = {t for tags in XBRL_TAG_MAP.values() for t in tags}


# ═════════════════════════════════════════════════════════════════════════
# helpers
# ═════════════════════════════════════════════════════════════════════════

def flat(facts: dict, accn: str) -> dict:
    """(tag, unit, start, end) → value, us-gaap tags we ingest, one filing."""
    out = {}
    for tag, body in facts.get("facts", {}).get("us-gaap", {}).items():
        if tag not in OUR_TAGS:
            continue
        for unit, obs in body.get("units", {}).items():
            for o in obs:
                if o.get("accn") == accn:
                    out[(tag, unit, o.get("start"), o.get("end"))] = o.get("val")
    return out


def same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def not_in(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """Rows of a whose (metric, form, period_end ±7 days) has no match in b."""
    if a.empty or b.empty:
        return a
    m = a.reset_index().merge(b[["metric", "form_type", "period_end"]],
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


def yahoo_eps(ticker: str, end: pd.Timestamp) -> float | None:
    try:
        import yfinance as yf
        q = yf.Ticker(ticker.replace(".", "-")).quarterly_income_stmt
        if q is None or "Diluted EPS" not in q.index:
            return None
        row = q.loc["Diluted EPS"].dropna()
        row.index = pd.to_datetime(row.index)
        hit = row[(abs((row.index - end).days) <= 10)]
        return float(hit.iloc[0]) if len(hit) else None
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════
# A. same numbers
# ═════════════════════════════════════════════════════════════════════════

def verify(ticker: str, cik: int, facts: dict, sub: dict) -> dict:
    rec = sub.get("filings", {}).get("recent", {})
    have = accessions_in_facts(facts)
    present = [{"form": f, "report": r, "filed": d, "accn": a}
               for f, r, d, a in zip(rec.get("form", []), rec.get("reportDate", []),
                                     rec.get("filingDate", []), rec.get("accessionNumber", []))
               if f == "10-Q" and a in have]
    if not present:
        return {"verified": "—"}
    f = max(present, key=lambda x: x["filed"])
    ours = flat(fetch_filing_facts(cik, f), f["accn"])
    sec = flat(facts, f["accn"])
    common = set(ours) & set(sec)
    diff = [(k, sec[k], ours[k]) for k in common if not same(sec[k], ours[k])]
    only_sec, only_ours = set(sec) - set(ours), set(ours) - set(sec)
    status = "✓" if not diff and not only_sec else "✗"
    print(f"  A. ίδια νούμερα; 10-Q {f['report']}: {len(common) - len(diff)}/{len(common)} ίσα"
          f" · μόνο SEC {len(only_sec)} · μόνο XBRL {len(only_ours)}  {status}")
    for k, a, b in diff[:3]:
        print(f"       ✗ {k[0]} [{k[1]}] {k[2]}→{k[3]}: SEC {a} · XBRL {b}")
    for k in list(only_sec)[:3]:
        print(f"       μόνο SEC: {k[0]} [{k[1]}] {k[2]}→{k[3]} = {sec[k]}")
    return {"verified": status, "v_compared": len(common), "v_diff": len(diff),
            "v_only_sec": len(only_sec), "v_only_xbrl": len(only_ours)}


# ═════════════════════════════════════════════════════════════════════════
# B + C
# ═════════════════════════════════════════════════════════════════════════

def check(ticker: str, eng) -> tuple[dict, pd.DataFrame]:
    cik = get_cik_for_ticker(ticker)
    if cik is None:
        return {"ticker": ticker, "status": "χωρίς CIK"}, pd.DataFrame()
    facts, sub = fetch_companyfacts(cik), fetch_submissions(cik)
    if facts is None or sub is None:
        return {"ticker": ticker, "status": "χωρίς companyfacts/submissions"}, pd.DataFrame()
    print(f"\n══ {ticker} · CIK {cik} ══")
    res = {"ticker": ticker, "status": "ok", **verify(ticker, cik, facts, sub)}

    merged, rep = add_missing_filings(cik, facts, sub)
    for a in rep:
        print(f"  B. {a['form']} {a['report']} (κατάθ. {a['filed']}): "
              + (f"{a['n_facts']} facts από XBRL" if not a["error"] else f"✗ {a['error']}"))
    if not rep:
        print("  B. καμία κατάθεση δεν λείπει από το companyfacts")
    old = parse_companyfacts(ticker, cik, facts)
    new = parse_companyfacts(ticker, cik, merged)
    fresh = not_in(new, old)
    lost = not_in(old, new)
    db = db_keys(eng, ticker)
    already = len(fresh) - len(not_in(fresh, db))
    fresh = not_in(fresh, db)
    taken = 0
    if not fresh.empty and not db.empty:
        k = fresh.reset_index().merge(db[["metric", "fiscal_year", "fiscal_period", "form_type"]].drop_duplicates(),
                                      on=["metric", "fiscal_year", "fiscal_period", "form_type"], how="inner")
        taken = k["index"].nunique()
        fresh = fresh.drop(index=k["index"].unique())
    res.update(filings=sum(not a["error"] for a in rep), failed=sum(bool(a["error"]) for a in rep),
               new=len(fresh) + already + taken, already_in_db=already, key_taken=taken,
               insertable=len(fresh), lost=len(lost))
    print(f"     γραμμές: νέες {res['new']} · ήδη στη βάση {already} · πιασμένο κλειδί {taken}"
          f" · προς εγγραφή {len(fresh)} · χαμένες {len(lost)}" + ("  ⚠️" if len(lost) else ""))

    if not fresh.empty:
        last = fresh["period_end"].max()
        q = fresh[(fresh["period_end"] == last)]
        def pick(metric, quarter=True):
            x = q[q["metric"] == metric]
            if quarter and "period_start" in x:
                d = (x["period_end"] - x["period_start"]).dt.days
                x = x[d.between(80, 100)] if d.between(80, 100).any() else x
            return x["value"].iloc[0] if len(x) else None
        eps = pick("EarningsPerShareDiluted")
        y = yahoo_eps(ticker, last)
        dev = f"{(eps / y - 1) * 100:+.1f}%" if eps is not None and y else "—"
        ni, cfo = pick("NetIncomeLoss"), pick("NetCashProvidedByUsedInOperatingActivities", quarter=False)
        print(f"  C. {last.date()}: EPS diluted (τρίμηνο) {eps} · Yahoo {y} · απόκλιση {dev}"
              f" · καθαρά κέρδη {ni:,.0f}" if ni is not None else
              f"  C. {last.date()}: EPS diluted (τρίμηνο) {eps} · Yahoo {y} · απόκλιση {dev}")
        if cfo is not None:
            print(f"     CFO (από αρχή χρήσης) {cfo:,.0f}")
        res.update(last_period=str(last.date()), eps=eps, yahoo_eps=y)
    return res, fresh


def main() -> None:
    write = "--write" in sys.argv
    args = [a.upper() for a in sys.argv[1:] if not a.startswith("--")]
    if "--from-gaps" in sys.argv:
        g = pd.read_csv(GAPS, dtype=str)
        args = sorted(g.loc[g["status"] == "λείπει", "ticker"].unique())
    if not args:
        print(__doc__)
        return
    print(("ΕΓΓΡΑΦΗ" if write else "DRY RUN (τίποτα δεν γράφεται)") + f" · {len(args)} εταιρείες")
    eng = get_engine()
    caches = load_lookup_caches(eng) if write else None
    t0, rows = time.time(), []
    for t in args:
        try:
            res, fresh = check(t, eng)
            if write and not fresh.empty and res.get("lost", 0) == 0 and res.get("verified") != "✗":
                w = upsert_fundamentals(fresh, eng, caches=caches)
                res["written"] = w["inserted"]
                print(f"  ✍️  γράφτηκαν {w['inserted']}/{w['attempted']}")
            elif write and not fresh.empty:
                print("  ⛔ δεν γράφτηκε: χαμένες γραμμές ή αποτυχία στο A")
        except Exception as e:
            res = {"ticker": t, "status": f"σφάλμα: {type(e).__name__}: {str(e)[:100]}"}
            print(f"\n══ {t} ══\n  ✗ {res['status']}")
        rows.append(res)
        pd.DataFrame([res]).to_csv(OUT, mode="a", header=not os.path.exists(OUT), index=False)

    d = pd.DataFrame(rows)
    num = lambda c: int(d[c].fillna(0).sum()) if c in d else 0
    print(f"\n── Σύνοψη ({(time.time() - t0) / 60:.0f} λεπτά) ──")
    if "verified" in d:
        v = d["verified"].value_counts()
        print(f"  A. ίδια νούμερα: ✓ {v.get('✓', 0)} · ✗ {v.get('✗', 0)} · — {v.get('—', 0)}")
    print(f"  B. καταθέσεις από XBRL: {num('filings')} (απέτυχαν {num('failed')}) σε "
          f"{int((d.get('filings', pd.Series(dtype=float)).fillna(0) > 0).sum())} εταιρείες")
    print(f"     νέες {num('new')} · ήδη στη βάση {num('already_in_db')} · πιασμένο κλειδί {num('key_taken')}"
          f" · προς εγγραφή {num('insertable')} · χαμένες {num('lost')}")
    print(f"     χώρος: ~{num('insertable') * BYTES_PER_ROW / 1e6:.1f} MB")
    if "eps" in d:
        x = d.dropna(subset=["eps", "yahoo_eps"])
        if len(x):
            dev = (x["eps"].astype(float) / x["yahoo_eps"].astype(float) - 1).abs()
            print(f"  C. EPS έναντι Yahoo: {int((dev <= 0.05).sum())}/{len(x)} εντός 5%")
            for r in x[dev > 0.05].itertuples():
                print(f"     {r.ticker}: δικό μας {r.eps} · Yahoo {r.yahoo_eps}")
    bad = d[~d["status"].eq("ok")] if "status" in d else d.iloc[0:0]
    for r in bad.itertuples():
        print(f"  ✗ {r.ticker}: {r.status}")
    if "written" in d:
        print(f"  ✍️  γράφτηκαν συνολικά: {num('written')}")
    print(f"→ {OUT}")
    if not write:
        print("Για εγγραφή: ίδια εντολή με --write")


if __name__ == "__main__":
    main()
