"""
check_tagmap_v0127.py — READ-ONLY regression for v0.1.27 (6 new metrics).

Question 1: does adding metrics to XBRL_TAG_MAP change ANY row of the EXISTING
metrics? It could: Stage 0 picks each filing's own period by counting how many
metrics use each period_end, so new metrics add "votes". We parse each company
twice from the same SEC data, with and without the new metrics, and compare.
Any difference = stop and look.

Question 2: do the new tags survive the real ingest filters with the amounts
we measured? Example: PG's newest cash incl. restricted should be ~15.37 bn (Yahoo, 2026-09-30).

Run from the shared data layer folder, with the new fundamentals.py in place:
    python check_tagmap_v0127.py --selftest     (1 sec, no network, no database)
    python check_tagmap_v0127.py                (~3 min, nothing is written)
"""

import os
import random
import sys
import time

sys.path.insert(0, os.getcwd())

import pandas as pd

import fundamentals as F

NEW = list(getattr(F, "NEW_METRICS_V0127", []))
# Previous regression's list + the companies of the cash and convertibles work
FIXED = ["KO", "HD", "MSFT", "AAPL", "NVDA", "CRM", "KR", "PEP", "COST", "DPZ", "KDP",
         "AAL", "XOM", "CVX", "DUK", "NEE", "JPM", "O", "AVB", "T", "MAR", "UPS",
         "GOOGL", "KLAC", "DXCM", "PD", "ADP", "PFE", "MIR", "VIAV", "AKAM", "TGT",
         "PG", "MMM", "HSY", "LULU", "GEV", "GME", "OMC", "HST", "LKQ", "ILMN"]
SPOT = ["PG", "MMM", "GEV", "GME", "OMC", "HST", "LKQ", "ILMN"]
N_RANDOM = 40
SEED = 20261104
COLS = ["metric", "source_tag", "value", "unit", "fiscal_year", "fiscal_period",
        "period_start", "period_end", "filed_date", "form_type"]
KEY = ["metric", "fiscal_year", "fiscal_period", "form_type"]


def parse_without_new(ticker: str, cik: int, facts: dict) -> pd.DataFrame:
    """The parser exactly as v0.1.25 had it: only the tag map differs between the versions."""
    full = F.XBRL_TAG_MAP
    F.XBRL_TAG_MAP = {k: v for k, v in full.items() if k not in NEW}
    try:
        return F.parse_companyfacts(ticker, cik, facts)
    finally:
        F.XBRL_TAG_MAP = full


def compare(ticker: str, cik: int, facts: dict) -> tuple[dict | None, pd.DataFrame]:
    """→ (differences in existing metrics or None, rows of the new metrics)."""
    a = parse_without_new(ticker, cik, facts)
    b = F.parse_companyfacts(ticker, cik, facts)
    new_rows = b[b["metric"].isin(NEW)] if not b.empty else b
    kept = b[~b["metric"].isin(NEW)] if not b.empty else b
    if a.empty and kept.empty:
        return None, new_rows
    a2 = a[COLS].sort_values(KEY).reset_index(drop=True)
    b2 = kept[COLS].sort_values(KEY).reset_index(drop=True)
    if a2.equals(b2):
        return None, new_rows
    m = a2.merge(b2, on=KEY, how="outer", suffixes=("_old", "_new"), indicator=True)
    changed = m[(m["_merge"] == "both") & ((m["value_old"] != m["value_new"])
                                           | (m["period_end_old"] != m["period_end_new"]))]
    return {"ticker": ticker, "μόνο_στο_παλιό": int((m["_merge"] == "left_only").sum()),
            "μόνο_στο_νέο": int((m["_merge"] == "right_only").sum()),
            "αλλαγμένες": len(changed)}, new_rows


def check_map() -> None:
    assert len(NEW) == 6 and len(set(NEW)) == 6, "fundamentals.py δεν είναι το v0.1.27"
    for m in NEW:
        assert F.XBRL_TAG_MAP.get(m) == [m], f"{m}: κάθε νέο μέγεθος έχει μόνο το δικό του tag"
    seen = {}
    for metric, tags in F.XBRL_TAG_MAP.items():
        for tag in tags:
            assert tag not in seen, f"{tag} σε δύο μεγέθη: {seen[tag]}, {metric}"
            seen[tag] = metric
    for older in (F.NEW_METRICS_V0125, F.NEW_METRICS_V0126):
        assert set(older) <= set(F.XBRL_TAG_MAP) and not set(NEW) & set(older)


def selftest() -> None:
    check_map()

    def fact(val, end, fy, fp, form, accn, filed, start=None):
        o = {"val": val, "end": end, "fy": fy, "fp": fp, "form": form, "accn": accn, "filed": filed}
        return {**o, "start": start} if start else o

    # One 10-Q with its own date and the year-end comparative column
    q = [("2026-09-30", 1), ("2025-12-31", 2)]
    usd = lambda vals: {"units": {"USD": [
        fact(v, e, 2026, "Q3", "10-Q", "A1", "2026-10-30") for (e, _), v in zip(q, vals)]}}
    facts = {"facts": {"us-gaap": {
        "CashAndCashEquivalentsAtCarryingValue": usd([100, 90]),
        "Assets": usd([500, 480]), "AssetsCurrent": usd([300, 280]), "Liabilities": usd([200, 190]),
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": usd([40, 30]),
        "LongTermNotesPayable": usd([35, 25]),
        "NotesPayable": usd([70, 70]),
    }}}
    diff, rows = compare("X", 1, facts)
    assert diff is None
    assert set(rows["metric"]) == {"CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "NotesPayable", "LongTermNotesPayable"}
    # the comparative column stays out, each tag keeps its own value: nothing merged
    assert list(rows["period_end"].astype(str).unique()) == ["2026-09-30"] and len(rows) == 3
    assert rows.set_index("metric")["value"].to_dict()["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"] == 40

    # A tag seen only as a comparative is dropped by the ingest, like every other metric
    facts["facts"]["us-gaap"]["NotesPayableCurrent"] = {"units": {"USD": [
        fact(9, "2025-12-31", 2026, "Q3", "10-Q", "A1", "2026-10-30")]}}
    _, rows = compare("X", 1, facts)
    assert "NotesPayableCurrent" not in set(rows["metric"])

    # The regression really does notice a difference in an existing metric
    full = F.parse_companyfacts
    try:
        def broken(t, c, f):
            df = full(t, c, f)
            if "NotesPayable" in F.XBRL_TAG_MAP:
                df.loc[df["metric"] == "Assets", "value"] = 999.0
            return df
        F.parse_companyfacts = broken
        diff, _ = compare("X", 1, facts)
        assert diff is not None and diff["αλλαγμένες"] == 1
    finally:
        F.parse_companyfacts = full

    # The tag map is restored even if a parse fails
    n = len(F.XBRL_TAG_MAP)
    F.parse_companyfacts = lambda *a: 1 / 0
    try:
        parse_without_new("X", 1, facts)
    except ZeroDivisionError:
        pass
    finally:
        F.parse_companyfacts = full
    assert len(F.XBRL_TAG_MAP) == n
    print("✅ selftest: όλα τα συνθετικά σενάρια πέρασαν")


def main() -> None:
    from sqlalchemy import text
    from db import get_engine

    check_map()
    eng = get_engine()
    with eng.connect() as c:
        tc = pd.DataFrame(c.execute(text(
            "SELECT DISTINCT ON (ticker) ticker, cik FROM fundamentals ORDER BY ticker, filed_date DESC"
        )).fetchall(), columns=["ticker", "cik"]).set_index("ticker")["cik"]
        size, n_rows = c.execute(text(
            "SELECT pg_total_relation_size('fundamentals'), COUNT(*) FROM fundamentals")).fetchone()
    rest = sorted(set(tc.index) - set(FIXED))
    tickers = [t for t in FIXED if t in tc.index] + random.Random(SEED).sample(rest, N_RANDOM)
    print(f"{len(tickers)} εταιρείες ({len(tickers) - N_RANDOM} επιλεγμένες + {N_RANDOM} τυχαίες)\n")

    same, diffs, parts, spot = 0, [], [], []
    for i, t in enumerate(tickers, 1):
        facts = F.fetch_companyfacts(int(tc[t]))
        time.sleep(F.REQUEST_SLEEP)
        if not facts:
            print(f"  {t}: χωρίς δεδομένα στη SEC")
            continue
        diff, rows = compare(t, int(tc[t]), facts)
        if diff:
            diffs.append(diff)
        else:
            same += 1
        if not rows.empty:
            parts.append(rows.assign(ticker=t))
        if t in SPOT and not rows.empty:
            last = rows.sort_values(["period_end", "filed_date"]).groupby("metric").tail(1)
            spot.append(last.assign(ticker=t))
        if i % 10 == 0:
            print(f"  ... {i}/{len(tickers)}")

    print(f"\n── Υπάρχοντα μεγέθη: πανομοιότυπα σε {same}/{same + len(diffs)} εταιρείες ──")
    if diffs:
        print("⚠️  ΔΙΑΦΟΡΕΣ — ΣΤΑΜΑΤΑΜΕ, στείλε αυτόν τον πίνακα:")
        print(pd.DataFrame(diffs).to_string(index=False))
    else:
        print("✅ Καμία διαφορά: τα νέα μεγέθη δεν επηρεάζουν τα υπάρχοντα.")

    print("\n── Νεότερη τιμή των νέων tags στις μετοχές του ελέγχου (δισ.) ──")
    if spot:
        s = pd.concat(spot)
        s = pd.DataFrame({"ticker": s["ticker"], "metric": s["metric"],
                          "ημ/νία": s["period_end"].astype(str).str[:10], "ποσό": (s["value"] / 1e9).round(3)})
        print(s.sort_values(["ticker", "ημ/νία"], ascending=[True, False]).to_string(index=False))

    if parts:
        r = pd.concat(parts)
        per = r.groupby("metric").agg(εταιρείες=("ticker", "nunique"), γραμμές=("value", "size"))
        scale = len(tc) / (same + len(diffs))
        total = len(r) * scale
        print(f"\n── Νέες γραμμές σε {same + len(diffs)} εταιρείες ──")
        print(per.sort_values("γραμμές", ascending=False).to_string())
        print(f"\n  Αναγωγή σε {len(tc)} εταιρείες ≈ {total:,.0f} γραμμές ≈ {total * size / n_rows / 1e6:.1f} MB"
              " (οι επιλεγμένες είναι βαρύτερες, άρα ανώτατη εκτίμηση)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
