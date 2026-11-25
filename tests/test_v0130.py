"""v0.1.30: filings missing from companyfacts are read from their own XBRL instance. Synthetic XML."""

import pytest

import fundamentals as F
import xbrl_instance as X

CIK = 1326160
ACCN = "0001326160-26-000099"


def instance(body: str, fy="2026", fp="Q3", cik="0001326160") -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:dei="http://xbrl.sec.gov/dei/2025"
    xmlns:duk="http://www.duke-energy.com/20260930" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
    xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <xbrli:context id="q3"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="ytd"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="q3ly"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-07-01</xbrli:startDate><xbrli:endDate>2025-09-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="bs"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-09-30</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="sub"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">duk:DukeEnergyCarolinasMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="other"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000037637</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
  <xbrli:unit id="eps"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
  <dei:DocumentFiscalYearFocus contextRef="ytd">{fy}</dei:DocumentFiscalYearFocus>
  <dei:DocumentFiscalPeriodFocus contextRef="ytd">{fp}</dei:DocumentFiscalPeriodFocus>
  <dei:DocumentType contextRef="ytd">10-Q</dei:DocumentType>
  {body}
</xbrli:xbrl>""".encode()


BODY = """
  <us-gaap:NetIncomeLoss contextRef="q3" unitRef="usd" decimals="-6">1550000000</us-gaap:NetIncomeLoss>
  <us-gaap:NetIncomeLoss contextRef="ytd" unitRef="usd" decimals="-6">4100000000</us-gaap:NetIncomeLoss>
  <us-gaap:NetIncomeLoss contextRef="q3ly" unitRef="usd" decimals="-6">1400000000</us-gaap:NetIncomeLoss>
  <us-gaap:NetIncomeLoss contextRef="sub" unitRef="usd" decimals="-6">700000000</us-gaap:NetIncomeLoss>
  <us-gaap:NetIncomeLoss contextRef="other" unitRef="usd" decimals="-6">900000000</us-gaap:NetIncomeLoss>
  <us-gaap:EarningsPerShareDiluted contextRef="q3" unitRef="eps" decimals="2">1.97</us-gaap:EarningsPerShareDiluted>
  <us-gaap:EarningsPerShareDiluted contextRef="q3ly" unitRef="eps" decimals="2">1.80</us-gaap:EarningsPerShareDiluted>
  <us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding contextRef="q3" unitRef="shares" decimals="-5">779000000</us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding>
  <us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding contextRef="q3" unitRef="shares" decimals="-6">779000000</us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding>
  <us-gaap:Revenues contextRef="q3" unitRef="usd" decimals="-6">8000000000</us-gaap:Revenues>
  <us-gaap:Revenues contextRef="q3" unitRef="usd" decimals="-3">8000400000</us-gaap:Revenues>
  <us-gaap:LongTermDebt contextRef="bs" unitRef="usd" decimals="-6">80000000000</us-gaap:LongTermDebt>
  <us-gaap:OperatingIncomeLoss contextRef="q3" unitRef="usd" xsi:nil="true"/>
  <us-gaap:ProfitLoss contextRef="q3" unitRef="usd" decimals="-6">-25000000</us-gaap:ProfitLoss>
  <us-gaap:AccountingPoliciesTextBlock contextRef="q3">some text</us-gaap:AccountingPoliciesTextBlock>
  <duk:CompanyOwnTag contextRef="q3" unitRef="usd" decimals="-6">5</duk:CompanyOwnTag>
"""


def obs(facts, tag, unit, tax="us-gaap"):
    return facts["facts"][tax][tag]["units"][unit]


def test_own_entity_no_dimensions_and_units():
    f = X.instance_to_facts(instance(BODY), CIK, ACCN, "10-Q", "2026-11-05")
    ni = obs(f, "NetIncomeLoss", "USD")
    # subsidiary (LegalEntityAxis) and the other CIK are dropped; comparative kept (Stage 0 drops it later)
    assert sorted((o["start"], o["val"]) for o in ni) == [
        ("2025-07-01", 1400000000), ("2026-01-01", 4100000000), ("2026-07-01", 1550000000)]
    eps = obs(f, "EarningsPerShareDiluted", "USD/shares")
    assert {o["val"] for o in eps} == {1.97, 1.80}
    o = [x for x in eps if x["end"] == "2026-09-30"][0]
    assert o == {"start": "2026-07-01", "end": "2026-09-30", "val": 1.97, "accn": ACCN,
                 "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-05"}
    assert obs(f, "LongTermDebt", "USD") == [{"end": "2026-09-30", "val": 80000000000, "accn": ACCN,
                                             "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-05"}]
    assert obs(f, "ProfitLoss", "USD")[0]["val"] == -25000000


def test_duplicates_keep_most_precise_nil_text_and_own_tags_dropped():
    f = X.instance_to_facts(instance(BODY), CIK, ACCN, "10-Q", "2026-11-05")
    assert [o["val"] for o in obs(f, "Revenues", "USD")] == [8000400000]
    assert len(obs(f, "WeightedAverageNumberOfDilutedSharesOutstanding", "shares")) == 1
    assert "OperatingIncomeLoss" not in f["facts"]["us-gaap"]
    assert "AccountingPoliciesTextBlock" not in f["facts"]["us-gaap"]
    assert set(f["facts"]) == {"us-gaap"}          # company's own taxonomy ignored, dei has no numbers here


def test_missing_fiscal_focus_or_doctype_refused():
    with pytest.raises(X.InstanceError):
        X.instance_to_facts(instance(BODY, fy=""), CIK, ACCN, "10-Q", "2026-11-05")
    bad = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'
    with pytest.raises(X.InstanceError):
        X.instance_to_facts(bad, CIK, ACCN, "10-Q", "2026-11-05")


def test_merge_does_not_touch_base():
    base = {"cik": CIK, "facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [{"accn": "OLD", "val": 1}]}}}}}
    extra = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [{"accn": "NEW", "val": 2}]}},
                                   "Revenues": {"units": {"USD": [{"accn": "NEW", "val": 3}]}}},
                       "dei": {"X": {"units": {"shares": [{"accn": "NEW", "val": 4}]}}}}}
    m = X.merge_facts(base, extra)
    assert [o["accn"] for o in m["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]] == ["OLD", "NEW"]
    assert base["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"] == [{"accn": "OLD", "val": 1}]
    assert "Revenues" not in base["facts"]["us-gaap"] and "dei" not in base["facts"]
    assert m["cik"] == CIK


def test_missing_filings_only_recent_10q_10k_not_in_companyfacts():
    facts = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [{"accn": "Q1"}]}}}}}
    sub = {"filings": {"recent": {
        "form": ["10-Q", "10-Q", "8-K", "10-Q", "10-K", "10-Q/A"],
        "reportDate": ["2026-09-30", "2026-06-30", "2026-10-01", "2026-03-31", "2022-12-31", "2026-06-30"],
        "filingDate": ["2026-11-05", "2026-08-04", "2026-10-02", "2026-05-05", "2023-02-20", "2026-09-01"],
        "accessionNumber": ["Q3", "Q2", "8K", "Q1", "OLDK", "AMEND"]}}}
    got = F.missing_filings(sub, facts, since="2023-11-25")
    assert [g["accn"] for g in got] == ["Q2", "Q3"]       # sorted by filing date
    assert F.missing_filings(None, facts, "2023-01-01") == []


def test_instance_name():
    idx = {"directory": {"item": [{"name": "duk-20260930.htm"}, {"name": "duk-20260930_htm.xml"},
                                  {"name": "duk-20260930_cal.xml"}, {"name": "FilingSummary.xml"}]}}
    assert F.instance_name(idx) == "duk-20260930_htm.xml"
    assert F.instance_name({"directory": {"item": [{"name": "a.htm"}]}}) is None


def test_end_to_end_parser_keeps_the_new_quarter():
    """Companyfacts has Q3 2025 only; the fallback brings Q3 2026; the parser keeps the own period."""
    cf = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
        {"start": "2025-07-01", "end": "2025-09-30", "val": 1.80, "accn": "OLDQ3", "fy": 2025, "fp": "Q3",
         "form": "10-Q", "filed": "2025-11-05"}]}}}}}
    extra = X.instance_to_facts(instance(BODY), CIK, ACCN, "10-Q", "2026-11-05")
    df = F.parse_companyfacts("DUK", CIK, X.merge_facts(cf, extra))
    eps = df[df["metric"] == "EarningsPerShareDiluted"].sort_values("period_end")
    assert [(int(r.fiscal_year), r.fiscal_period, r.period_end.date().isoformat(), r.value)
            for r in eps.itertuples()] == [(2025, "Q3", "2025-09-30", 1.80), (2026, "Q3", "2026-09-30", 1.97)]
    ni = df[df["metric"] == "NetIncomeLoss"]
    # own period only: the 2026 quarter and/or YTD, never the 2025 comparative of the new filing
    assert set(ni["period_end"].dt.date.astype(str)) == {"2026-09-30"}


def test_add_missing_filings_skips_a_broken_filing(monkeypatch):
    sub = {"filings": {"recent": {"form": ["10-Q", "10-Q"], "reportDate": ["2026-06-30", "2026-09-30"],
                                  "filingDate": ["2026-08-04", "2026-11-05"], "accessionNumber": ["Q2", "Q3"]}}}
    base = {"facts": {}}

    def fake(cik, f, sleep=0):
        if f["accn"] == "Q2":
            raise X.InstanceError("no …_htm.xml instance in the filing folder")
        return X.instance_to_facts(instance(BODY), cik, f["accn"], f["form"], f["filed"])

    monkeypatch.setattr(F, "fetch_filing_facts", fake)
    from datetime import date
    merged, rep = F.add_missing_filings(CIK, base, sub, today=date(2026, 11, 25))
    assert [(r["accn"], bool(r["error"])) for r in rep] == [("Q2", True), ("Q3", False)]
    assert rep[1]["n_facts"] > 0 and base == {"facts": {}}
    assert "NetIncomeLoss" in merged["facts"]["us-gaap"]
