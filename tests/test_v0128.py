"""v0.1.28: share-class tickers kept, `companies` kept current. No network, no database."""

import fundamentals as F
import ticker_provider as TP


# ── ticker cleaning ────────────────────────────────────────────────────────

def test_us_share_class_is_kept_as_a_dash():
    got = TP._clean_tickers(["AAPL", "BRK.B", "BF.B", " MSFT ", "GOOGL"], "sp500", None)
    assert got == ["AAPL", "BF-B", "BRK-B", "GOOGL", "MSFT"]


def test_european_exchange_suffix_is_still_replaced():
    assert TP._clean_tickers(["SAP.DE", "ALV"], "dax40", ".DE") == ["ALV.DE", "SAP.DE"]
    assert TP._clean_tickers(["ASML.AS", "MC.PA"], "eurostoxx50", None) == ["ASML", "MC"]


def test_empty_symbols_are_dropped():
    assert TP._clean_tickers([".", ""], "sp500", None) == []


# ── companies kept current ─────────────────────────────────────────────────

EXISTING = {"AAPL": (320193, "Apple Inc."), "XOM": (34088, "EXXON MOBIL CORP"),
            "FB": (1326801, "Meta Platforms, Inc.")}
SEC = {"AAPL": (320193, "Apple Inc."), "XOM": (2115436, "ExxonMobil Holdings Corp"),
       "VMRK": (906107, "VIVMARK RESIDENTIAL"), "META": (1326801, "Meta Platforms, Inc.")}


def test_companies_changes():
    ch = {c["ticker"]: c for c in F.companies_changes(EXISTING, SEC)}
    assert set(ch) == {"XOM", "VMRK", "META"}                  # AAPL unchanged, FB left alone
    assert ch["VMRK"]["kind"] == "νέα" and ch["META"]["kind"] == "νέα"
    assert ch["XOM"]["kind"] == "άλλο CIK" and ch["XOM"]["old_cik"] == 34088
    title_only = F.companies_changes({"AAPL": (320193, "APPLE INC")}, {"AAPL": (320193, "Apple Inc.")})
    assert [c["kind"] for c in title_only] == ["τίτλος"]


class FakeConn:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        self.log.append((str(stmt), params))
        return type("R", (), {"fetchall": lambda s: self.rows})()


class FakeEngine:
    def __init__(self, rows):
        self.log, self.rows = [], rows

    def connect(self):
        return FakeConn(self.log, self.rows)

    def begin(self):
        return FakeConn(self.log, self.rows)


def test_sync_companies_writes_only_the_changes(monkeypatch):
    monkeypatch.setattr(F, "get_sec_companies", lambda folder_path="cache": SEC)
    eng = FakeEngine(rows=[("AAPL", 320193, "Apple Inc."), ("XOM", 34088, "EXXON MOBIL CORP")])
    ch = F.sync_companies(["AAPL", "XOM", "VMRK", "BRK"], eng)          # BRK: not on the SEC list → ignored
    assert sorted(c["ticker"] for c in ch) == ["VMRK", "XOM"]
    writes = [p for s, p in eng.log if "INSERT INTO companies" in s]
    assert len(writes) == 1 and sorted(r["ticker"] for r in writes[0]) == ["VMRK", "XOM"]


def test_sync_companies_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(F, "get_sec_companies", lambda folder_path="cache": SEC)
    eng = FakeEngine(rows=[])
    assert len(F.sync_companies(["VMRK"], eng, write=False)) == 1
    assert not [s for s, _ in eng.log if "INSERT" in s]


def test_universe_ingest_never_blocked_by_companies(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("SEC down")
    monkeypatch.setattr(F, "sync_companies", boom)
    monkeypatch.setattr(F, "load_lookup_caches", lambda e: {})
    monkeypatch.setattr(F, "ingest_fundamentals_for_ticker",
                        lambda t, e, skip_if_no_new_filing, caches: {"ticker": t, "status": "ok"})
    out = F.ingest_fundamentals_for_universe(["AAPL"], engine=None, sleep=0)
    assert out["status"].tolist() == ["ok"]
