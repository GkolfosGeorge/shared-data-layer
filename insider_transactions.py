"""
insider_transactions.py
────────────────────────────────────────────────────────────────────────────
Core fetch/parse logic for SEC Form 4 (insider transactions). No archiving
or dedup/save logic here — that lives in weekly_insider_transactions_archive.py
and backfill_insider_transactions_history.py, which both import this module.

Data flow (per ticker/CIK):
    1. data.sec.gov/submissions/CIK{cik:010d}.json
         -> list of ALL filings for the company, filtered to form == '4'
            (4/A amendments are intentionally skipped for now — flagged as
            a known scope limitation, can be added later)
    2. For each Form 4 filing:
         www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primaryDocument}
         -> the raw ownership XML (NOT the xslF345X05/... human-readable
            rendering — we want the machine-readable structured document)
    3. Parse <nonDerivativeTransaction> entries out of the XML.
         Derivative transactions (options, RSUs-as-derivatives, etc.) are
         OUT OF SCOPE for v1 — flagged as a future extension, same as the
         4/A decision above. Non-derivative (straight stock buy/sell) is
         where the Lynch-style "insider confirmation" signal lives anyway.

Rate limit: SEC EDGAR allows ~10 requests/second, and REQUIRES a descriptive
User-Agent with a real contact email (see SEC_HEADERS below, reused from
ticker_provider.py's convention). Every network call in this module sleeps
afterward to stay comfortably under that limit.
"""

import time
import xml.etree.ElementTree as ET
from datetime import date
from typing import Optional

import pandas as pd
import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

# Single source of truth for the SEC contact-email header — same constant
# fundamentals.py imports, so there's only one place to update it.
from ticker_provider import SEC_HEADERS

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{doc}"

SLEEP_BETWEEN_REQUESTS = 0.15  # ~6-7 req/sec, safely under the 10/sec cap

FORM_TYPES_INCLUDED = {"4"}  # 4/A amendments excluded for now (see module docstring)

TRANSACTION_COLUMNS = [
    "ticker", "cik", "accession_number", "filing_date",
    "insider_name", "insider_title", "is_director", "is_officer", "is_ten_pct_owner",
    "transaction_date", "transaction_code",
    "shares", "price_per_share", "shares_owned_after", "ownership_type",
    "transaction_index",
]


# ─────────────────────────────────────────────────────────────
# Universe: tickers + CIKs, straight from the existing `companies` table
# (no need to re-derive from SEC's company_tickers.json — you already have
# a verified 768-ticker CIK mapping sitting in Postgres).
# ─────────────────────────────────────────────────────────────

def fetch_universe_with_cik(engine: Engine) -> pd.DataFrame:
    """
    Returns DataFrame[ticker, cik] for the fundamentals universe (~768
    tickers) — NOT the full `companies` table, which contains a much
    broader set of CIKs (10,000+) inherited from the pre-existing dataset
    and is not scoped to tickers we actually have fundamentals for.
    Joining against `fundamentals` (and taking DISTINCT) restricts the
    insider-transactions universe to the same 768 tickers used elsewhere
    in the ecosystem, as intended.
    """
    sql = text("""
        SELECT DISTINCT c.ticker, c.cik
        FROM companies c
        JOIN fundamentals f ON f.ticker = c.ticker
        WHERE c.cik IS NOT NULL
        ORDER BY c.ticker
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return pd.DataFrame(rows, columns=["ticker", "cik"])


# ─────────────────────────────────────────────────────────────
# Step 1: submissions index -> list of Form 4 filings for one CIK
# ─────────────────────────────────────────────────────────────

def _get_json(url: str, max_retries: int = 3) -> Optional[dict]:
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=SEC_HEADERS, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None  # no submissions file for this CIK — not an error
            # 403/429 etc — back off harder, SEC may be throttling.
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s, 4s — instead of a flat 1s each time
    return None


def fetch_form4_index(cik: int, since_date: Optional[str] = None) -> list[dict]:
    """
    Returns a list of dicts, one per Form 4 filing for `cik`:
        {accession_number, filing_date, primary_document}

    `since_date` (YYYY-MM-DD string), if given, filters out filings filed
    before that date — used by the weekly archiver to avoid re-scanning a
    company's entire filing history every run.

    Handles the (common for active filers) case where recent filings live
    in the main submissions JSON but older ones are paginated into
    `filings.files[]` — only fetched if `since_date` requires looking back
    far enough that the recent-only page isn't sufficient.
    """
    url = SUBMISSIONS_URL.format(cik=cik)
    data = _get_json(url)
    time.sleep(SLEEP_BETWEEN_REQUESTS)
    if data is None:
        return []

    filings_out = []

    def _extract(recent: dict):
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])

        for i in range(len(forms)):
            if forms[i] not in FORM_TYPES_INCLUDED:
                continue
            f_date = filing_dates[i]
            if since_date and f_date < since_date:
                continue
            filings_out.append({
                "accession_number": accessions[i],
                "filing_date": f_date,
                "primary_document": primary_docs[i],
            })

    recent = data.get("filings", {}).get("recent", {})
    _extract(recent)

    # Older filings, paginated. Each shard descriptor already carries
    # filingFrom/filingTo — we can decide WITHOUT downloading whether a
    # shard could possibly contain anything >= since_date, and skip it
    # entirely if not. This is the key fix for the slow runs seen in
    # practice: large/old companies can have dozens of historical shards,
    # and downloading-then-checking every one of them (the previous
    # approach) was needlessly slow. Now we only fetch shards whose date
    # range actually overlaps the window we care about.
    oldest_recent = min(recent.get("filingDate", ["9999-99-99"]), default="9999-99-99")
    need_older_pages = (since_date and since_date < oldest_recent) or since_date is None

    if need_older_pages:
        for page in data.get("filings", {}).get("files", []):
            page_to = page.get("filingTo", "9999-99-99")
            if since_date and page_to < since_date:
                continue  # this entire shard predates our cutoff — skip the download
            page_url = f"https://data.sec.gov/submissions/{page['name']}"
            page_data = _get_json(page_url)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            if page_data:
                _extract(page_data)

    return filings_out


# ─────────────────────────────────────────────────────────────
# Step 2 + 3: fetch raw ownership XML, parse non-derivative transactions
# ─────────────────────────────────────────────────────────────

def fetch_ownership_xml(cik: int, accession_number: str, primary_document: str) -> Optional[str]:
    """
    IMPORTANT: `primaryDocument` from the submissions API, for ownership
    forms (3/4/5), points to the XSLT-RENDERED HTML view — e.g.
    "xslF345X06/form4.xml". Despite the .xml extension, fetching that path
    returns HTML (Content-Type: text/html), not the machine-readable XML.
    The actual raw XML lives in the SAME accession folder, under the SAME
    filename, WITHOUT the "xslF345Xnn/" prefix — so we strip it here.
    (Confirmed against a live AAPL filing during development: the prefixed
    path served an HTML page that failed XML parsing on every single
    filing, which is what caused the original 0-rows-for-everyone bug.)
    """
    accession_nodash = accession_number.replace("-", "")
    raw_filename = primary_document.rsplit("/", 1)[-1]  # drop any "xslF345Xnn/" prefix
    url = ARCHIVES_BASE.format(cik=cik, accession_nodash=accession_nodash, doc=raw_filename)
    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=30)
        time.sleep(SLEEP_BETWEEN_REQUESTS)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException:
        pass
    return None


def _text_or_none(el, path) -> Optional[str]:
    found = el.find(path)
    if found is None:
        return None
    # Ownership XML values are typically wrapped like <value>123</value>
    val = found.find("value")
    if val is not None:
        return val.text
    return found.text


def parse_form4_xml(
    xml_text: str, ticker: str, cik: int, accession_number: str, filing_date: str,
) -> pd.DataFrame:
    """
    Parses <nonDerivativeTransaction> entries. Derivative transactions are
    skipped (out of scope for v1 — see module docstring). Filings with
    multiple reportingOwners (joint filings) produce one row set per owner.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return pd.DataFrame(columns=TRANSACTION_COLUMNS)

    owners = []
    for owner_el in root.findall(".//reportingOwner"):
        name = _text_or_none(owner_el, "reportingOwnerId/rptOwnerName")
        rel = owner_el.find("reportingOwnerRelationship")
        title = None
        is_director = is_officer = is_ten_pct = False
        if rel is not None:
            title = _text_or_none(rel, "officerTitle") or _text_or_none(owner_el, "officerTitle")
            is_director = (_text_or_none(rel, "isDirector") or "0") == "1"
            is_officer = (_text_or_none(rel, "isOfficer") or "0") == "1"
            is_ten_pct = (_text_or_none(rel, "isTenPercentOwner") or "0") == "1"
        owners.append({
            "name": name, "title": title,
            "is_director": is_director, "is_officer": is_officer, "is_ten_pct": is_ten_pct,
        })

    if not owners:
        owners = [{"name": None, "title": None, "is_director": False, "is_officer": False, "is_ten_pct": False}]

    rows = []
    non_deriv = root.find(".//nonDerivativeTable")
    if non_deriv is not None:
        for idx, tx in enumerate(non_deriv.findall("nonDerivativeTransaction")):
            tx_date = _text_or_none(tx, "transactionDate")
            code = _text_or_none(tx, "transactionCoding/transactionCode")
            shares = _text_or_none(tx, "transactionAmounts/transactionShares")
            price = _text_or_none(tx, "transactionAmounts/transactionPricePerShare")
            shares_after = _text_or_none(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")
            own_type = _text_or_none(tx, "ownershipNature/directOrIndirectOwnership")

            for owner in owners:
                rows.append({
                    "ticker": ticker,
                    "cik": cik,
                    "accession_number": accession_number,
                    "filing_date": filing_date,
                    "insider_name": owner["name"],
                    "insider_title": owner["title"],
                    "is_director": owner["is_director"],
                    "is_officer": owner["is_officer"],
                    "is_ten_pct_owner": owner["is_ten_pct"],
                    "transaction_date": tx_date,
                    "transaction_code": code,
                    "shares": float(shares) if shares else None,
                    "price_per_share": float(price) if price else None,
                    "shares_owned_after": float(shares_after) if shares_after else None,
                    "ownership_type": own_type,
                    "transaction_index": idx,
                })

    return pd.DataFrame(rows, columns=TRANSACTION_COLUMNS)


def fetch_transactions_for_ticker(
    ticker: str, cik: int, since_date: Optional[str] = None,
) -> pd.DataFrame:
    """Full pipeline for one ticker: index -> fetch each filing -> parse -> concat."""
    filings = fetch_form4_index(cik, since_date=since_date)
    if not filings:
        return pd.DataFrame(columns=TRANSACTION_COLUMNS)

    frames = []
    for f in filings:
        xml_text = fetch_ownership_xml(cik, f["accession_number"], f["primary_document"])
        if xml_text is None:
            continue
        df = parse_form4_xml(xml_text, ticker, cik, f["accession_number"], f["filing_date"])
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame(columns=TRANSACTION_COLUMNS)
    return pd.concat(frames, ignore_index=True)
