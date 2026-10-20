"""
check_tagmap_v0125.py — READ-ONLY regression for v0.1.25 (12 new metrics).

Question: does adding metrics to XBRL_TAG_MAP change ANY row of the
EXISTING metrics? It could, in theory: Stage 0 picks each filing's own
period by counting how many metrics use each period_end, so new metrics
add "votes". For the existing metrics the new parser must produce exactly
the same rows as v0.1.24. Any difference = stop and look.

Setup (Git Bash, in the shared data layer folder), BEFORE replacing fundamentals.py:
    git show HEAD:fundamentals.py > _fundamentals_v0124.py
Then put the new fundamentals.py in place and run:
    python check_tagmap_v0125.py
Nothing is written to the database. ~3 minutes (SEC downloads).
Afterwards: rm _fundamentals_v0124.py   (not for the repo)
"""

import importlib.util
import os
import random
import sys
import time

sys.path.insert(0, os.getcwd())

import pandas as pd
from sqlalchemy import text

from db import get_engine
import fundamentals as NEW

spec = importlib.util.spec_from_file_location("fundamentals_v0124", "_fundamentals_v0124.py")
OLD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OLD)

# Companies chosen for a reason (16-week quarters, mergers, REITs, utilities,
# energy, banks, label problems, heavy leases) + a random sample.
FIXED = ["KO", "HD", "MSFT", "AAPL", "NVDA", "CRM", "KR", "PEP", "COST", "DPZ", "KDP",
         "AAL", "XOM", "CVX", "DUK", "NEE", "JPM", "O", "AVB", "T", "MAR", "UPS"]
N_RANDOM = 40
SEED = 20261020
COLS = ["metric", "source_tag", "value", "unit", "fiscal_year", "fiscal_period",
        "period_start", "period_end", "filed_date", "form_type"]
KEY = ["metric", "fiscal_year", "fiscal_period", "form_type"]


def main():
    assert len(NEW.XBRL_TAG_MAP) == len(OLD.XBRL_TAG_MAP) + 12, \
        "fundamentals.py δεν είναι το v0.1.25 (ή το _fundamentals_v0124.py δεν είναι το παλιό)"
    old_metrics = set(OLD.XBRL_TAG_MAP)

    eng = get_engine()
    with eng.connect() as c:
        tc = pd.DataFrame(c.execute(text(
            "SELECT DISTINCT ON (ticker) ticker, cik FROM fundamentals ORDER BY ticker, filed_date DESC"
        )).fetchall(), columns=["ticker", "cik"]).set_index("ticker")["cik"]
    rest = sorted(set(tc.index) - set(FIXED))
    tickers = [t for t in FIXED if t in tc.index] + random.Random(SEED).sample(rest, N_RANDOM)
    print(f"{len(tickers)} εταιρείες ({len(FIXED)} επιλεγμένες + {N_RANDOM} τυχαίες)\n")

    same, diffs, new_rows = 0, [], []
    for i, t in enumerate(tickers, 1):
        cik = int(tc[t])
        facts = NEW.fetch_companyfacts(cik)
        time.sleep(NEW.REQUEST_SLEEP)
        if not facts:
            print(f"  {t}: χωρίς δεδομένα στη SEC")
            continue
        a = OLD.parse_companyfacts(t, cik, facts)
        b = NEW.parse_companyfacts(t, cik, facts)
        b_old = b[b["metric"].isin(old_metrics)] if not b.empty else b
        if a.empty and b_old.empty:
            same += 1
        else:
            a2 = a[COLS].sort_values(KEY).reset_index(drop=True)
            b2 = b_old[COLS].sort_values(KEY).reset_index(drop=True)
            if a2.equals(b2):
                same += 1
            else:
                m = a2.merge(b2, on=KEY, how="outer", suffixes=("_old", "_new"), indicator=True)
                changed = m[(m["_merge"] == "both") &
                            ((m["value_old"] != m["value_new"]) | (m["period_end_old"] != m["period_end_new"]))]
                diffs.append({"ticker": t,
                              "μόνο_στο_παλιό": int((m["_merge"] == "left_only").sum()),
                              "μόνο_στο_νέο": int((m["_merge"] == "right_only").sum()),
                              "αλλαγμένες": len(changed)})
        if not b.empty:
            nb = b[~b["metric"].isin(old_metrics)]
            for metric, n in nb["metric"].value_counts().items():
                new_rows.append({"ticker": t, "metric": metric, "rows": n})
        if i % 10 == 0:
            print(f"  ... {i}/{len(tickers)}")

    print(f"\n── Υπάρχοντα μεγέθη: πανομοιότυπα σε {same}/{same + len(diffs)} εταιρείες ──")
    if diffs:
        print("⚠️  ΔΙΑΦΟΡΕΣ — ΣΤΑΜΑΤΑΜΕ, στείλε αυτόν τον πίνακα:")
        print(pd.DataFrame(diffs).to_string(index=False))
    else:
        print("✅ Καμία διαφορά: τα νέα μεγέθη δεν επηρεάζουν τα υπάρχοντα.")

    nr = pd.DataFrame(new_rows, columns=["ticker", "metric", "rows"])
    n_done = same + len(diffs)
    print(f"\n── Νέες γραμμές σε αυτές τις {n_done} εταιρείες, και αναγωγή σε {len(tc)} ──")
    if nr.empty:
        print("  καμία")
        return
    per = nr.groupby("metric").agg(εταιρείες=("ticker", "nunique"), γραμμές=("rows", "sum"))
    per["αναγωγή_γραμμών"] = (per["γραμμές"] * len(tc) / n_done).round(-2).astype(int)
    print(per.sort_values("γραμμές", ascending=False).to_string())
    with eng.connect() as c:
        size, rows = c.execute(text(
            "SELECT pg_total_relation_size('fundamentals'), COUNT(*) FROM fundamentals")).fetchone()
    est_mb = per["αναγωγή_γραμμών"].sum() * size / rows / 1e6
    print(f"\n  Σύνολο ≈ {per['αναγωγή_γραμμών'].sum():,} γραμμές ≈ {est_mb:.1f} MB")


if __name__ == "__main__":
    main()
