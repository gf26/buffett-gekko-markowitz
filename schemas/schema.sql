-- Buffet Gekko Markowitz - persistent data cache schema
-- Run this once in the Supabase SQL editor (or any Postgres instance) to create the tables.

-- Master list of tickers you want to track. This is what the ingestion jobs loop over.
CREATE TABLE IF NOT EXISTS tickers (
    ticker     TEXT PRIMARY KEY,
    name       TEXT,
    active     BOOLEAN DEFAULT TRUE,   -- set to FALSE to pause a ticker without deleting its history
    added_at   TIMESTAMPTZ DEFAULT now()
);

-- Daily OHLCV + adjusted close. One row per ticker per day.
CREATE TABLE IF NOT EXISTS prices_daily (
    ticker      TEXT NOT NULL REFERENCES tickers(ticker),
    date        DATE NOT NULL,
    open        NUMERIC,
    high        NUMERIC,
    low         NUMERIC,
    close       NUMERIC,
    adj_close   NUMERIC,
    volume      BIGINT,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_daily_date ON prices_daily(date);

-- Cash dividends (ex-date + amount per share).
CREATE TABLE IF NOT EXISTS dividends (
    ticker   TEXT NOT NULL REFERENCES tickers(ticker),
    ex_date  DATE NOT NULL,
    amount   NUMERIC NOT NULL,
    PRIMARY KEY (ticker, ex_date)
);

-- Stock splits / inplits.
CREATE TABLE IF NOT EXISTS splits (
    ticker   TEXT NOT NULL REFERENCES tickers(ticker),
    ex_date  DATE NOT NULL,
    ratio    NUMERIC NOT NULL,
    PRIMARY KEY (ticker, ex_date)
);

-- Fundamentals (balance sheet / income statement / cashflow), stored "tidy" (long) format
-- so it survives Yahoo Finance changing which line items it reports.
CREATE TABLE IF NOT EXISTS financials (
    ticker       TEXT NOT NULL REFERENCES tickers(ticker),
    statement    TEXT NOT NULL CHECK (statement IN ('balance_sheet','income_statement','cashflow')),
    period_type  TEXT NOT NULL CHECK (period_type IN ('annual','quarterly')),
    fiscal_date  DATE NOT NULL,
    line_item    TEXT NOT NULL,
    value        NUMERIC,
    fetched_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (ticker, statement, period_type, fiscal_date, line_item)
);

-- Company snapshot info (sector, shares outstanding, market cap, etc.) - stored as JSON
-- because yfinance's "info" dict shape varies a lot between tickers/versions.
CREATE TABLE IF NOT EXISTS company_info (
    ticker      TEXT PRIMARY KEY REFERENCES tickers(ticker),
    info        JSONB,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Simple run log so you can see, from the Supabase table editor, whether last night's job worked.
CREATE TABLE IF NOT EXISTS ingestion_log (
    id        SERIAL PRIMARY KEY,
    job_name  TEXT NOT NULL,
    ticker    TEXT,
    status    TEXT,          -- 'ok' or 'error'
    message   TEXT,
    run_at    TIMESTAMPTZ DEFAULT now()
);
