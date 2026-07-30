-- ─────────────────────────────────────────────────────────────────────────
-- schema.sql
-- Core tables for the point-in-time universe layer (Phase 1).
-- Safe to re-run: every statement is idempotent (IF NOT EXISTS).
-- ─────────────────────────────────────────────────────────────────────────

-- One row per known ticker, mapping it to its SEC CIK and company name.
-- `ticker` is the natural key here because index_membership and, later,
-- price data are keyed by ticker. A ticker can only map to one CIK at a
-- time in this table; if a ticker is ever reused by a different company
-- after the original delists, that historical nuance is out of scope for
-- now (flagged as a known limitation, not silently ignored).
CREATE TABLE IF NOT EXISTS companies (
    ticker      TEXT PRIMARY KEY,
    cik         INTEGER,
    title       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_companies_cik ON companies (cik);


-- Point-in-time index membership. One row per (index, ticker, stint).
-- A ticker can appear multiple times for the same index if it left and
-- rejoined (this happens, e.g. after a spin-off/merger).
CREATE TABLE IF NOT EXISTS index_membership (
    id            BIGSERIAL PRIMARY KEY,
    index_name    TEXT NOT NULL,           -- e.g. 'SP500'
    ticker        TEXT NOT NULL,
    date_added    DATE NOT NULL,
    date_removed  DATE,                    -- NULL = still a current member
    source        TEXT NOT NULL DEFAULT 'fja05680/sp500',
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (index_name, ticker, date_added)
);

-- The index used by every point-in-time query:
-- "who was in <index_name> on <date>?"
CREATE INDEX IF NOT EXISTS idx_membership_pit
    ON index_membership (index_name, date_added, date_removed);

CREATE INDEX IF NOT EXISTS idx_membership_ticker
    ON index_membership (ticker);
-- ─────────────────────────────────────────────────────────────────────────
-- Phase 2 v2: normalized fundamentals schema.
-- Replaces the TEXT columns (metric, unit, form_type, source_tag) — which
-- repeated the same ~30-character strings over 1.2M+ rows — with small
-- lookup tables + SMALLINT foreign keys. Same data, much less disk space.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS metric_lookup (
    id   SMALLSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_lookup (
    id   SMALLSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS form_type_lookup (
    id   SMALLSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS source_tag_lookup (
    id   SMALLSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS fundamentals (
    id            BIGSERIAL PRIMARY KEY,
    ticker        TEXT NOT NULL,
    cik           INTEGER NOT NULL,
    metric_id     SMALLINT NOT NULL REFERENCES metric_lookup(id),
    value         NUMERIC,
    unit_id       SMALLINT REFERENCES unit_lookup(id),
    fiscal_year   SMALLINT NOT NULL,
    fiscal_period TEXT NOT NULL,        -- 'Q1','Q2','Q3','Q4','FY' — already short, left as-is
    period_start  DATE,
    period_end    DATE NOT NULL,
    filed_date    DATE NOT NULL,
    form_type_id  SMALLINT REFERENCES form_type_lookup(id),
    source_tag_id SMALLINT NOT NULL REFERENCES source_tag_lookup(id),
    UNIQUE (ticker, metric_id, fiscal_year, fiscal_period, form_type_id)
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker_metric
    ON fundamentals (ticker, metric_id, period_end);

CREATE INDEX IF NOT EXISTS idx_fundamentals_filed
    ON fundamentals (filed_date);