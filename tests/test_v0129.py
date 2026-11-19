"""v0.1.29: filings with a wrong (fy, fp) label are no longer lost. Synthetic companyfacts."""

import pandas as pd

import fundamentals as F

T = pd.Timestamp


def obs(accn, fy, fp, form, start, end, filed, val):
    return {"accn": accn, "fy": fy, "fp": fp, "form": form, "start": start, "end": end,
            "filed": filed, "val": val}


def facts(*observations, tag="EarningsPerShareDiluted", unit="USD/shares"):
    return {"facts": {"us-gaap": {tag: {"units": {unit: list(observations)}}}}}


# CRM-like: fiscal year ends Jan 31; the 10-K for 2026-01-31 is labeled fy=2025 like the year before
CRM = facts(
    obs("K25", 2025, "FY", "10-K", "2024-02-01", "2025-01-31", "2025-03-05", 6.4),
    obs("K25", 2025, "FY", "10-K", "2023-02-01", "2024-01-31", "2025-03-05", 4.2),   # comparative
    obs("K26", 2025, "FY", "10-K", "2025-02-01", "2026-01-31", "2026-03-02", 7.7),   # WRONG label
    obs("K26", 2025, "FY", "10-K", "2024-02-01", "2025-01-31", "2026-03-02", 6.4),   # comparative
)


def rows(df):
    return sorted((int(r.fiscal_year), r.fiscal_period, r.period_end.date().isoformat(), r.value)
                  for r in df.itertuples())


def test_old_parser_loses_the_new_10k():
    df = F.parse_companyfacts("CRM", 1, CRM, relabel=False)
    assert rows(df) == [(2025, "FY", "2025-01-31", 6.4)]


def test_new_parser_keeps_both_years():
    df = F.parse_companyfacts("CRM", 1, CRM)
    assert rows(df) == [(2025, "FY", "2025-01-31", 6.4), (12026, "FY", "2026-01-31", 7.7)]
    assert df.attrs["unresolved"] == []
    assert df.loc[df["period_end"] == T("2025-01-31"), "_relabeled"].tolist() == [False]


def test_rereported_same_period_is_not_relabeled():
    f = facts(
        obs("Q1", 2025, "Q1", "10-Q", "2025-01-01", "2025-03-31", "2025-05-01", 1.0),
        obs("Q2", 2025, "Q1", "10-Q", "2025-01-01", "2025-03-31", "2025-08-01", 1.0),  # same period again
        obs("K", 2025, "FY", "10-K", "2025-01-01", "2025-12-31", "2026-02-20", 4.0),
    )
    df = F.parse_companyfacts("X", 1, f)
    assert rows(df) == [(2025, "FY", "2025-12-31", 4.0), (2025, "Q1", "2025-03-31", 1.0)]
    assert not df["_relabeled"].any()


def test_quarterly_labels_one_year_behind():
    # WDAY-like: fye Jan 31; Q1 of FY2026 (ends 2025-04-30) labeled fy=2025 Q1 like Q1 of FY2025
    f = facts(
        obs("K", 2025, "FY", "10-K", "2024-02-01", "2025-01-31", "2025-03-10", 5.0),
        obs("A", 2025, "Q1", "10-Q", "2024-02-01", "2024-04-30", "2024-05-29", 1.0),
        obs("B", 2025, "Q1", "10-Q", "2025-02-01", "2025-04-30", "2025-05-23", 1.5),
    )
    df = F.parse_companyfacts("WDAY", 1, f)
    q1 = [r for r in rows(df) if r[1] == "Q1"]
    assert q1 == [(2025, "Q1", "2024-04-30", 1.0), (12026, "Q1", "2025-04-30", 1.5)]


def test_relabel_never_collides_with_a_filed_label():
    # the "true" label 2026 Q1 already belongs to ANOTHER period with its own SEC label:
    # the reserved 12026 avoids it, both periods are kept
    f = facts(
        obs("K", 2024, "FY", "10-K", "2024-01-01", "2024-12-31", "2025-02-10", 5.0),
        obs("A", 2025, "Q1", "10-Q", "2025-01-01", "2025-03-31", "2025-05-01", 1.0),
        obs("B", 2025, "Q1", "10-Q", "2026-01-01", "2026-03-31", "2026-05-01", 2.0),  # → 12026 Q1
        obs("C", 2026, "Q1", "10-Q", "2025-07-01", "2025-09-30", "2025-11-01", 3.0),  # holds 2026 Q1
    )
    df = F.parse_companyfacts("X", 1, f)
    q1 = [r for r in rows(df) if r[1] == "Q1"]
    assert q1 == [(2025, "Q1", "2025-03-31", 1.0), (2026, "Q1", "2025-09-30", 3.0), (12026, "Q1", "2026-03-31", 2.0)]
    assert df.attrs["unresolved"] == []
    # the output must still merge/concat like any DataFrame (attrs are compared there)
    assert len(df.merge(df[["metric", "period_end"]], on=["metric", "period_end"])) >= len(df)
    assert len(pd.concat([df, df])) == 2 * len(df)


def test_two_relabeled_periods_on_one_key_are_reported():
    # FERG-like change of fiscal year end: Jul-2025 and Dec-2025 both labeled "2025 FY" after
    # Jul-2024 did; both fall in fiscal year 2025 → the later one cannot get its own key
    f = facts(
        obs("A", 2025, "FY", "10-K", "2023-08-01", "2024-07-31", "2024-09-25", 1.0),
        obs("B", 2025, "FY", "10-K", "2024-08-01", "2025-07-31", "2025-09-26", 2.0),
        obs("C", 2025, "FY", "10-K", "2025-08-01", "2025-12-31", "2026-02-27", 3.0),
    )
    df = F.parse_companyfacts("FERG", 1, f)
    assert rows(df) == [(2025, "FY", "2024-07-31", 1.0), (12025, "FY", "2025-07-31", 2.0)]
    u = df.attrs["unresolved"]
    assert len(u) == 1 and u[0]["period_end"] == T("2025-12-31")


def test_no_annual_filing_means_no_relabel():
    f = facts(
        obs("A", 2025, "Q1", "10-Q", "2025-01-01", "2025-03-31", "2025-05-01", 1.0),
        obs("B", 2025, "Q1", "10-Q", "2026-01-01", "2026-03-31", "2026-05-01", 2.0),
    )
    df = F.parse_companyfacts("NEW", 1, f)
    assert rows(df) == [(2025, "Q1", "2025-03-31", 1.0)]          # unchanged, as before v0.1.29


def test_inconsistent_naming_within_one_company():
    # KR-like: "fiscal 2023" ends 2024-02-03 (named by start year), the next 10-K is named by
    # its end year, and the 10-K for 2026-01-31 repeats the 2025 label → 12026, no guessing
    f = facts(
        obs("K23", 2023, "FY", "10-K", "2023-01-29", "2024-02-03", "2024-04-01", 3.0),
        obs("K24", 2025, "FY", "10-K", "2024-02-04", "2025-02-01", "2025-04-01", 3.5),
        obs("K25", 2025, "FY", "10-K", "2025-02-02", "2026-01-31", "2026-03-31", 4.0),   # repeats 2025
    )
    df = F.parse_companyfacts("KR", 1, f)
    assert [r for r in rows(df) if r[1] == "FY"] == [
        (2023, "FY", "2024-02-03", 3.0), (2025, "FY", "2025-02-01", 3.5), (12026, "FY", "2026-01-31", 4.0)]


def test_derived_fiscal_year():
    assert F.derived_fiscal_year(T("2026-01-31"), (1, 31)) == 2026
    assert F.derived_fiscal_year(T("2025-04-30"), (1, 31)) == 2026
    assert F.derived_fiscal_year(T("2025-06-30"), (12, 31)) == 2025
    assert F.derived_fiscal_year(T("2025-02-01"), (1, 31)) == 2025     # 52/53-week year end
    assert F.derived_fiscal_year(T("2025-09-27"), (9, 30)) == 2025
