"""
fundamentals.py — Point-in-time fundamentals ingest & read layer.

Part of shared-data-layer / trading-shared-data. Pulls company financial
facts from SEC EDGAR's XBRL `companyfacts` API and writes them into the
`fundamentals` table (see schema.sql), anchored on `filed_date` — NEVER
`period_end` — so downstream backtests never see a number before it was
actually available to the market.

── Immutability / no-lookahead-corruption design ─────────────────────────
A single XBRL concept for a given period is often RE-REPORTED verbatim as
comparative data in later filings (e.g. Q1 numbers reappear inside the Q3
10-Q, and again inside next year's 10-K). Two consequences drive the
design here:

1. Within one company's parsed facts, if the same
   (metric, fiscal_year, fiscal_period, form_type) tuple appears more than
   once, we keep the row with the EARLIEST `filed_date` — that's the true
   "first known to the market" value.
2. Once a (ticker, metric, fiscal_year, fiscal_period, form_type) row
   exists in the table, it is never overwritten (ON CONFLICT DO NOTHING).
   A genuine restatement arrives under a DIFFERENT form_type (10-K/A vs
   10-K) and lands as a new row rather than silently mutating the
   original — so re-running the ingest is always safe, and the table
   never "changes its mind" about what was known on a given date.

Usage:
    from db import get_engine
    from fundamentals import ingest_fundamentals_for_universe, get_fundamentals

    engine = get_engine()
    ingest_fundamentals_for_universe(["AAPL", "MSFT", "XOM"], engine)

    df = get_fundamentals("AAPL", engine)                    # everything filed to date
    df_pit = get_fundamentals("AAPL", engine, as_of="2023-06-30")  # point-in-time snapshot
"""

import time
from datetime import date

import pandas as pd
import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

from ticker_provider import get_cik_for_ticker, SEC_HEADERS

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# SEC's fair-access policy expects a well-behaved client; well under their
# informal ~10 req/sec ceiling.
REQUEST_SLEEP = 0.15

# ── metric -> candidate XBRL us-gaap tags, priority order ────────────────
# IMPORTANT: canonical metric names here are reverse-engineered from the
# EXISTING database (found pre-built, ~1.29M rows already in it) so that
# new ingests land on the SAME metric_id as what's already there instead
# of fragmenting into parallel, disconnected metrics. Verified against
# check_fundamentals_coverage.py output: summing the source_tag row counts
# within each group reproduces the existing metric's total exactly (e.g.
# Revenues=41,594 == Revenues(21,073) + RevenueFromContractWith...(10,043)
# + SalesRevenueNet(7,433) + the other 3 revenue tags).
# Companies drift between tags across years (taxonomy changes, company-
# specific extensions), so each metric pulls from ALL candidate tags, not
# just the first one found.
XBRL_TAG_MAP: dict[str, list[str]] = {
    "Revenues": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
    ],
    "CostOfRevenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfServices",
    ],
    "GrossProfit": ["GrossProfit"],
    "OperatingIncomeLoss": ["OperatingIncomeLoss"],          # EBIT proxy
    "OperatingExpenses": ["CostsAndExpenses", "OperatingExpenses"],
    "SellingGeneralAndAdministrativeExpense": ["SellingGeneralAndAdministrativeExpense"],
    "ResearchAndDevelopmentExpense": ["ResearchAndDevelopmentExpense"],
    "NetIncomeLoss": ["NetIncomeLoss", "ProfitLoss"],
    "NetIncomeLossAvailableToCommonStockholdersBasic": [
        "NetIncomeLossAvailableToCommonStockholdersBasic"
    ],
    "InterestExpense": ["InterestExpense", "InterestExpenseDebt", "InterestIncomeExpenseNet"],
    "IncomeTaxExpenseBenefit": ["IncomeTaxExpenseBenefit"],
    "EarningsPerShareBasic": ["EarningsPerShareBasic"],
    "EarningsPerShareDiluted": ["EarningsPerShareDiluted"],
    "CommonStockSharesOutstanding": ["CommonStockSharesOutstanding"],
    "WeightedAverageNumberOfSharesOutstandingBasic": [
        "WeightedAverageNumberOfSharesOutstandingBasic"
    ],
    "WeightedAverageNumberOfDilutedSharesOutstanding": [
        "WeightedAverageNumberOfDilutedSharesOutstanding"
    ],
    "CommonStockDividendsPerShareDeclared": ["CommonStockDividendsPerShareDeclared"],
    "Assets": ["Assets"],
    "AssetsCurrent": ["AssetsCurrent"],
    "Liabilities": ["Liabilities"],
    "LiabilitiesCurrent": ["LiabilitiesCurrent"],
    "LongTermDebtNoncurrent": ["LongTermDebtNoncurrent"],
    "LongTermDebtCurrent": ["LongTermDebtCurrent"],
    "DebtCurrent": ["DebtCurrent"],
    "StockholdersEquity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "RetainedEarningsAccumulatedDeficit": ["RetainedEarningsAccumulatedDeficit"],
    "CashAndCashEquivalentsAtCarryingValue": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
    ],
    "ShortTermInvestments": ["ShortTermInvestments"],
    "InventoryNet": ["InventoryNet"],
    "AccountsReceivableNetCurrent": ["AccountsReceivableNetCurrent"],
    "AccountsPayableCurrent": ["AccountsPayableCurrent"],
    "PropertyPlantAndEquipmentNet": ["PropertyPlantAndEquipmentNet"],
    "Goodwill": ["Goodwill"],
    "IntangibleAssetsNetExcludingGoodwill": [
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ],
    "DepreciationDepletionAndAmortization": [
        "DepreciationDepletionAndAmortization",
        "Depreciation",
        "DepreciationAmortizationAndAccretionNet",
    ],
    "NetCashProvidedByUsedInOperatingActivities": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "NetCashProvidedByUsedInInvestingActivities": [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ],
    "NetCashProvidedByUsedInFinancingActivities": [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ],
    "PaymentsToAcquirePropertyPlantAndEquipment": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "PaymentsForRepurchaseOfCommonStock": ["PaymentsForRepurchaseOfCommonStock"],
    "PaymentsOfDividends": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
}


# ── lookup table get-or-create ────────────────────────────────────────────
# `table` is always one of the four hardcoded lookup table names from our
# own call sites below — never user input — so the f-string is safe here.
def _get_or_create_id(engine: Engine, table: str, name: str, cache: dict) -> int:
    if name in cache:
        return cache[name]

    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT id FROM {table} WHERE name = :name"), {"name": name}
        ).fetchone()
        if row is None:
            row = conn.execute(
                text(
                    f"INSERT INTO {table} (name) VALUES (:name) "
                    f"ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                    f"RETURNING id"
                ),
                {"name": name},
            ).fetchone()

    cache[name] = row[0]
    return row[0]


# ── fetch ──────────────────────────────────────────────────────────────────
def fetch_companyfacts(cik: int) -> dict | None:
    """Downloads the raw companyfacts JSON for one CIK. Returns None (not
    raise) on 404 — some CIKs (very new listings, some foreign filers)
    have no XBRL facts on file yet."""
    url = COMPANYFACTS_URL.format(cik=cik)
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def get_latest_filing_date(cik: int) -> "date | None":
    """
    Fetches ONLY the lightweight `submissions` endpoint (a list of recent
    filings — tiny compared to the full companyfacts blob, which can be
    several MB for large companies) and returns the most recent filing
    date on record. Used to cheaply decide "is there anything new here
    at all?" before paying for a full companyfacts download.
    """
    from datetime import datetime

    url = SUBMISSIONS_URL.format(cik=cik)
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    dates = r.json().get("filings", {}).get("recent", {}).get("filingDate", [])
    if not dates:
        return None
    return max(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)


# ── parse ──────────────────────────────────────────────────────────────────
def parse_companyfacts(ticker: str, cik: int, facts: dict) -> pd.DataFrame:
    """
    Flattens one company's companyfacts JSON into rows ready for the
    `fundamentals` table. One row per (metric, fiscal_year, fiscal_period,
    form_type) — see module docstring for the earliest-filed_date-wins
    dedup rule.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    rows = []

    def _extract(tag_map: dict, taxonomy: dict):
        for metric, candidate_tags in tag_map.items():
            for tag in candidate_tags:
                concept = taxonomy.get(tag)
                if not concept:
                    continue
                for unit_name, observations in concept.get("units", {}).items():
                    for obs in observations:
                        rows.append({
                            "ticker": ticker,
                            "cik": cik,
                            "metric": metric,
                            "source_tag": tag,
                            "value": obs.get("val"),
                            "unit": unit_name,
                            "fiscal_year": obs.get("fy"),
                            "fiscal_period": obs.get("fp"),
                            "period_start": obs.get("start"),
                            "period_end": obs.get("end"),
                            "filed_date": obs.get("filed"),
                            "form_type": obs.get("form"),
                        })

    _extract(XBRL_TAG_MAP, us_gaap)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Required NOT NULL columns in the schema.
    df = df.dropna(subset=["fiscal_year", "fiscal_period", "period_end", "filed_date"])
    if df.empty:
        return df

    df["period_start"] = pd.to_datetime(df["period_start"])
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["filed_date"] = pd.to_datetime(df["filed_date"])

    # ── Stage 1: resolve SAME-FILING duplicates ─────────────────────────
    # A single filing often reports MORE than one observation under the
    # same nominal (metric, fy, fp, form) label — e.g. a 10-Q's income
    # statement shows both the 3-month (quarterly) AND 9-month (YTD
    # cumulative) figure for the same line item, both with the same
    # `end` date and the SAME filed_date (same filing). A balance sheet
    # similarly often shows the current AND a prior comparative column
    # under the same label. Because these ties share filed_date, sorting
    # by filed_date alone can't separate them — it would keep whichever
    # happened to appear first in the raw JSON, effectively at random.
    # Duration facts (have period_start): keep the observation whose
    # length is closest to the expected quarter/year length.
    # Instant facts (no period_start, e.g. Assets): keep the one with
    # the LATEST end date — the filing's own current-period figure,
    # not an earlier comparative column.
    def _resolve_same_filing_duplicates(group: pd.DataFrame) -> pd.DataFrame:
        # pandas excludes groupby-key columns from `group` inside .apply();
        # `group.name` carries the key tuple in groupby-column order:
        # (metric, fiscal_year, fiscal_period, form_type, filed_date).
        fiscal_period = group.name[2]
        if group["period_start"].notna().all():
            expected_days = 365 if fiscal_period == "FY" else 91
            duration_days = (group["period_end"] - group["period_start"]).dt.days
            group = group.assign(_duration_diff=(duration_days - expected_days).abs())
            group = group.sort_values(["_duration_diff", "period_end"], ascending=[True, False])
        else:
            group = group.sort_values("period_end", ascending=False)
        return group.iloc[[0]]

    df = (
        df.groupby(
            ["metric", "fiscal_year", "fiscal_period", "form_type", "filed_date"],
            group_keys=True,
        )
        .apply(_resolve_same_filing_duplicates, include_groups=False)
        .drop(columns=["_duration_diff"], errors="ignore")
        .reset_index(level=[0, 1, 2, 3, 4])
        .reset_index(drop=True)
    )

    # ── Stage 2: resolve ACROSS-FILING duplicates ───────────────────────
    # Now that each filing contributes at most one observation per
    # (metric, fy, fp, form), the same tuple can still recur across
    # LATER filings (e.g. Q1 numbers reappear as comparative data in the
    # Q3 10-Q). Keep the EARLIEST filed_date — the true "first known to
    # the market" value (see module docstring).
    df = df.sort_values("filed_date")
    df = df.drop_duplicates(
        subset=["ticker", "metric", "fiscal_year", "fiscal_period", "form_type"],
        keep="first",
    )

    df["fiscal_year"] = df["fiscal_year"].astype(int)

    return df.reset_index(drop=True)


# ── upsert ─────────────────────────────────────────────────────────────────
# Rows are written in multi-row batches (one INSERT with many VALUES
# tuples per batch), not one INSERT per row. A single busy company can
# have 1000+ facts; against a hosted DB (Neon), one network round-trip
# per row turns into minutes of pure latency. Batching cuts that to a
# handful of round-trips regardless of row count.
UPSERT_COLUMNS = [
    "ticker", "cik", "metric_id", "value", "unit_id", "fiscal_year",
    "fiscal_period", "period_start", "period_end", "filed_date",
    "form_type_id", "source_tag_id",
]
UPSERT_BATCH_SIZE = 500


def _upsert_batch(conn, batch: list[dict]) -> int:
    """Builds and executes one multi-row INSERT for a batch of records."""
    values_sql = ", ".join(
        "(" + ", ".join(f":{col}_{i}" for col in UPSERT_COLUMNS) + ")"
        for i in range(len(batch))
    )
    stmt = text(f"""
        INSERT INTO fundamentals ({", ".join(UPSERT_COLUMNS)})
        VALUES {values_sql}
        ON CONFLICT (ticker, metric_id, fiscal_year, fiscal_period, form_type_id)
        DO NOTHING
    """)
    params = {
        f"{col}_{i}": rec[col]
        for i, rec in enumerate(batch)
        for col in UPSERT_COLUMNS
    }
    result = conn.execute(stmt, params)
    return result.rowcount


def upsert_fundamentals(df: pd.DataFrame, engine: Engine) -> dict:
    """
    Resolves lookup ids and writes rows into `fundamentals` in batches.
    ON CONFLICT DO NOTHING — see module docstring for why existing rows
    are treated as immutable. Returns {"attempted": N, "inserted": N};
    inserted < attempted just means those rows already existed from a
    prior run (expected/healthy on repeat ingests, not an error).
    """
    if df.empty:
        return {"attempted": 0, "inserted": 0}

    metric_cache, unit_cache, form_cache, source_cache = {}, {}, {}, {}

    def _to_date_or_none(v):
        if pd.isna(v):
            return None
        return v.date() if hasattr(v, "date") else v

    records = []
    for _, r in df.iterrows():
        records.append({
            "ticker": r["ticker"],
            "cik": int(r["cik"]),
            "metric_id": _get_or_create_id(engine, "metric_lookup", r["metric"], metric_cache),
            "value": r["value"],
            "unit_id": _get_or_create_id(engine, "unit_lookup", r["unit"], unit_cache),
            "fiscal_year": int(r["fiscal_year"]),
            "fiscal_period": r["fiscal_period"],
            "period_start": _to_date_or_none(r["period_start"]),
            "period_end": _to_date_or_none(r["period_end"]),
            "filed_date": _to_date_or_none(r["filed_date"]),
            "form_type_id": _get_or_create_id(engine, "form_type_lookup", r["form_type"], form_cache),
            "source_tag_id": _get_or_create_id(engine, "source_tag_lookup", r["source_tag"], source_cache),
        })

    inserted = 0
    with engine.begin() as conn:
        for i in range(0, len(records), UPSERT_BATCH_SIZE):
            batch = records[i : i + UPSERT_BATCH_SIZE]
            inserted += _upsert_batch(conn, batch)

    return {"attempted": len(records), "inserted": inserted}


# ── orchestration ────────────────────────────────────────────────────────
def _get_latest_known_filed_date(ticker: str, engine: Engine):
    """MAX(filed_date) already on file for this ticker, or None if we
    have nothing yet (e.g. brand-new spin-off — must always do a full
    fetch in that case, never skip)."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT MAX(filed_date) FROM fundamentals WHERE ticker = :ticker"),
            {"ticker": ticker},
        ).fetchone()
    return row[0] if row else None


def ingest_fundamentals_for_ticker(
    ticker: str, engine: Engine, skip_if_no_new_filing: bool = False
) -> dict:
    cik = get_cik_for_ticker(ticker)
    if cik is None:
        print(f"⚠️  {ticker}: no CIK found, skipping.")
        return {"ticker": ticker, "status": "no_cik"}

    if skip_if_no_new_filing:
        known = _get_latest_known_filed_date(ticker, engine)
        if known is not None:
            try:
                latest = get_latest_filing_date(cik)
            except Exception:
                latest = None  # δεν ξέρουμε -> προχωράμε στο πλήρες fetch, ποτέ σιωπηλό skip σε αβεβαιότητα
            if latest is not None and latest <= known:
                print(f"⏭️  {ticker}: κανένα νέο filing (τελευταίο γνωστό {known}), παραλείπεται.")
                return {"ticker": ticker, "status": "up_to_date", "attempted": 0, "inserted": 0}

    facts = fetch_companyfacts(cik)
    if facts is None:
        print(f"⚠️  {ticker}: no XBRL facts on file (CIK {cik}).")
        return {"ticker": ticker, "status": "no_facts"}

    df = parse_companyfacts(ticker, cik, facts)
    result = upsert_fundamentals(df, engine)
    print(f"✅ {ticker}: {result['inserted']}/{result['attempted']} rows inserted.")
    return {"ticker": ticker, "status": "ok", **result}


def ingest_fundamentals_for_universe(
    tickers: list[str],
    engine: Engine,
    sleep: float = REQUEST_SLEEP,
    skip_if_no_new_filing: bool = False,
) -> pd.DataFrame:
    """Sequential ingest across a ticker universe (e.g. the full S&P 500).

    skip_if_no_new_filing=True checks a lightweight SEC endpoint first
    and skips the full (multi-MB) companyfacts download entirely for
    tickers with nothing new since our last known filed_date — the
    normal case in a weekly recurring run, where most companies haven't
    filed anything since last week. Leave False for full
    backfills/repairs where you deliberately want to re-process
    everything regardless of freshness.
    """
    results = []
    for i, ticker in enumerate(tickers):
        try:
            results.append(
                ingest_fundamentals_for_ticker(
                    ticker, engine, skip_if_no_new_filing=skip_if_no_new_filing
                )
            )
        except Exception as e:
            print(f"❌ {ticker}: {e}")
            results.append({"ticker": ticker, "status": "error", "error": str(e)})
        time.sleep(sleep)
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(tickers)} tickers done")
    return pd.DataFrame(results)


# ── read (point-in-time) ────────────────────────────────────────────────
def get_fundamentals(
    ticker: str,
    engine: Engine,
    metrics: list[str] | None = None,
    as_of: str | date | None = None,
) -> pd.DataFrame:
    """
    Point-in-time fundamentals reader — the helper single-asset-analyzer's
    Βήμα 2 was waiting on.

    `as_of`, if given, restricts to facts with filed_date <= as_of. THIS
    is the look-ahead-bias guard: it answers "what did the market actually
    know about this company's fundamentals as of this date?" — not
    "what turned out to be true for this fiscal period." Omit `as_of` to
    get everything filed to date.
    """
    where = ["f.ticker = :ticker"]
    params: dict = {"ticker": ticker}

    if metrics:
        where.append("m.name = ANY(:metrics)")
        params["metrics"] = metrics

    if as_of:
        where.append("f.filed_date <= :as_of")
        params["as_of"] = as_of

    sql = text(f"""
        SELECT
            f.ticker, m.name AS metric, f.value, u.name AS unit,
            f.fiscal_year, f.fiscal_period, f.period_start, f.period_end,
            f.filed_date, ft.name AS form_type, st.name AS source_tag
        FROM fundamentals f
        JOIN metric_lookup m ON m.id = f.metric_id
        LEFT JOIN unit_lookup u ON u.id = f.unit_id
        LEFT JOIN form_type_lookup ft ON ft.id = f.form_type_id
        JOIN source_tag_lookup st ON st.id = f.source_tag_id
        WHERE {' AND '.join(where)}
        ORDER BY f.period_end, f.metric_id
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    return pd.DataFrame(rows, columns=[
        "ticker", "metric", "value", "unit", "fiscal_year", "fiscal_period",
        "period_start", "period_end", "filed_date", "form_type", "source_tag",
    ])


if __name__ == "__main__":
    # Quick manual smoke test on a couple of well-known tickers before
    # running the full universe.
    from db import get_engine, init_schema

    eng = get_engine()
    init_schema(eng)

    test_tickers = ["AAPL", "MSFT"]
    summary = ingest_fundamentals_for_universe(test_tickers, eng)
    print("\n── Summary ──────────────────────────────────")
    print(summary)

    print("\n── Sample read (AAPL, revenue) ────────────────")
    print(get_fundamentals("AAPL", eng, metrics=["revenue"]).tail(8))
