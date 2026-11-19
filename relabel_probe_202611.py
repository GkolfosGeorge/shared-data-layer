"""
relabel_probe_202611.py — READ-ONLY: what do a company's fiscal labels look like,
and what does the v0.1.29 relabel do with them? (18/11/2026)
Run from the shared data layer folder:

    python relabel_probe_202611.py KR
    python relabel_probe_202611.py KR CRWD FERG REG

For ONE common metric (net income) it prints:
  1. the fiscal year end, and how the SEC labels differ from the year each fiscal year ends
  2. every 10-K FY row: label, period, filed, year it ends, difference
  3. every label shared by DIFFERENT periods (the cases the relabel handles):
     which period kept the label, which got a new one, and which stayed lost
     because the new label already belongs to another period
"""

import sys

import pandas as pd

import fundamentals as F
from ticker_provider import get_cik_for_ticker

METRIC = "NetIncomeLoss"
YEARS = 10


def probe(ticker: str) -> None:
    cik = get_cik_for_ticker(ticker)
    facts = F.fetch_companyfacts(cik)
    seen = {}
    original = F._relabel_fiscal_years

    def spy(df):                         # keeps a copy of the parser's table just before relabel
        seen["before"] = df.copy()
        out, unresolved = original(df)
        seen["after"], seen["unresolved"] = out.copy(), unresolved
        return out, unresolved

    F._relabel_fiscal_years = spy
    try:
        F.parse_companyfacts(ticker, cik, facts)
    finally:
        F._relabel_fiscal_years = original

    before, after = seen["before"], seen["after"]
    fye = F._fiscal_year_end(before)
    cut = pd.Timestamp.today() - pd.DateOffset(years=YEARS)
    print(f"══ {ticker} · CIK {cik} · τέλος χρονιάς {fye} ══")

    fy = before[(before["fiscal_period"] == "FY") & before["form_type"].astype(str).str.startswith("10-K")]
    if fye:
        d = fy["fiscal_year"].astype(int) - fy["period_end"].map(lambda e: F.derived_fiscal_year(e, fye))
        print("  διαφορά ετικέτας − έτους λήξης, σε όλα τα 10-K FY (όλα τα μεγέθη): "
              + ", ".join(f"{k:+d}: {v}" for k, v in d.value_counts().sort_index().items()))
    m = fy[(fy["metric"] == METRIC) & (fy["period_end"] >= cut)].sort_values("period_end")
    print(f"\n  10-K FY, {METRIC}:")
    for r in m.itertuples():
        ends = F.derived_fiscal_year(r.period_end, fye) if fye else None
        print(f"    ετικέτα {int(r.fiscal_year)} · περίοδος {r.period_end:%Y-%m-%d} · κατάθ. {r.filed_date:%Y-%m-%d}"
              f" · λήγει {ends} · διαφορά {int(r.fiscal_year) - ends:+d}")

    keys = ["metric", "fiscal_year", "fiscal_period", "form_type"]
    b = before[(before["metric"] == METRIC) & (before["period_end"] >= cut)]
    a = after.loc[b.index]
    lost = seen["unresolved"]
    print(f"\n  ετικέτες με ΔΙΑΦΟΡΕΤΙΚΕΣ περιόδους ({METRIC}):")
    n = 0
    for key, g in b.groupby(keys):
        if g["period_end"].max() - g["period_end"].min() <= pd.Timedelta(days=7):
            continue
        n += 1
        print(f"    {key[1]} {key[2]} {key[3]}:")
        for i, r in g.sort_values("period_end").iterrows():
            new = int(a.at[i, "fiscal_year"])
            status = "κράτησε την ετικέτα" if not a.at[i, "_relabeled"] else f"→ {new}"
            if len(lost) and ((lost["metric"] == METRIC) & (lost["period_end"] == r.period_end)).any():
                occ = after[(after["metric"] == METRIC) & (after["fiscal_year"] == new)
                            & (after["fiscal_period"] == key[2]) & (after["form_type"] == key[3])
                            & ~after["_relabeled"]]
                status += (f" ✗ πιασμένη από {occ['period_end'].iloc[0]:%Y-%m-%d}" if len(occ) else " ✗ πιασμένη")
            print(f"       {r.period_end:%Y-%m-%d} ({(r.period_end - r.period_start).days if pd.notna(r.period_start) else '—'} μέρες)"
                  f" · κατάθ. {r.filed_date:%Y-%m-%d} · {status}")
    if not n:
        print("    καμία")
    print()


if __name__ == "__main__":
    for t in [a.upper() for a in sys.argv[1:]] or ["KR"]:
        probe(t)
