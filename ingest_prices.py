"""
Daily ingestion job: prices, dividends, and splits.

For each active ticker, finds the last date already stored and only asks Yahoo
Finance for data after that (so re-runs are cheap), then upserts into Postgres
in batches (not row-by-row - row-by-row over a pooled connection is extremely
slow for tickers with years of daily history).

Usage:
    DATABASE_URL="postgresql://..." python ingest_prices.py

This is the script the GitHub Actions workflow .github/workflows/daily_prices.yml
runs every night.
"""
import os
import sys
import time
from datetime import timedelta

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL)

BATCH_SIZE = 40             # tickers per yfinance call - smaller batches are more reliable than one huge call
SLEEP_BETWEEN_BATCHES = 3   # seconds - be polite to Yahoo's servers, reduces the odds of getting rate-limited
DEFAULT_START = "2000-01-01"


def get_active_tickers():
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker FROM tickers WHERE active = TRUE ORDER BY ticker")).fetchall()
    return [r[0] for r in rows]


def get_last_date(ticker):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT MAX(date) FROM prices_daily WHERE ticker = :t"), {"t": ticker}).fetchone()
    return row[0]


def upsert_prices(cur, df, ticker):
    if df is None or df.empty:
        return 0
    df = df.reset_index()
    df["ticker"] = ticker
    df = df.rename(columns={
        "Date": "date", "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
    })
    df = df[["ticker", "date", "open", "high", "low", "close", "adj_close", "volume"]].dropna(subset=["date"])
    df = df.where(pd.notnull(df), None)
    rows = list(df.itertuples(index=False, name=None))
    if not rows:
        return 0
    execute_values(cur, """
        INSERT INTO prices_daily (ticker, date, open, high, low, close, adj_close, volume)
        VALUES %s
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, adj_close = EXCLUDED.adj_close, volume = EXCLUDED.volume
    """, rows, page_size=1000)
    return len(rows)


def upsert_dividends(cur, series, ticker):
    if series is None or series.empty:
        return 0
    rows = [(ticker, d.date(), float(v)) for d, v in series.items() if v]
    if not rows:
        return 0
    execute_values(cur, """
        INSERT INTO dividends (ticker, ex_date, amount) VALUES %s
        ON CONFLICT (ticker, ex_date) DO UPDATE SET amount = EXCLUDED.amount
    """, rows, page_size=1000)
    return len(rows)


def upsert_splits(cur, series, ticker):
    if series is None or series.empty:
        return 0
    rows = [(ticker, d.date(), float(v)) for d, v in series.items() if v]
    if not rows:
        return 0
    execute_values(cur, """
        INSERT INTO splits (ticker, ex_date, ratio) VALUES %s
        ON CONFLICT (ticker, ex_date) DO UPDATE SET ratio = EXCLUDED.ratio
    """, rows, page_size=1000)
    return len(rows)


def log(job, ticker, status, message=""):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ingestion_log (job_name, ticker, status, message)
            VALUES (:job, :ticker, :status, :message)
        """), {"job": job, "ticker": ticker, "status": status, "message": (message or "")[:500]})


def main():
    tickers = get_active_tickers()
    if not tickers:
        print("No tickers found in the 'tickers' table. Run seed_tickers.py first.")
        sys.exit(1)

    print(f"Starting ingestion for {len(tickers)} tickers, in batches of {BATCH_SIZE}.")

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(tickers) - 1) // BATCH_SIZE + 1
        print(f"\nBatch {batch_num}/{total_batches}: downloading {len(batch)} tickers from Yahoo Finance...")

        starts = [get_last_date(t) for t in batch]
        known_starts = [s for s in starts if s is not None]
        # small 5-day overlap so we re-confirm the last few rows (covers late dividend/adjustment updates)
        start = (min(known_starts) - timedelta(days=5)).isoformat() if known_starts else DEFAULT_START

        t0 = time.time()
        try:
            data = yf.download(batch, interval="1d", start=start, group_by="ticker",
                                actions=True, rounding=True, threads=True, progress=False,
                                auto_adjust=False)
        except Exception as e:
            for t in batch:
                log("ingest_prices", t, "error", f"batch download failed: {e}")
            print(f"Batch {batch_num} download FAILED: {e}")
            time.sleep(SLEEP_BETWEEN_BATCHES)
            continue
        print(f"  download done in {time.time() - t0:.1f}s, writing to database...")

        conn = engine.raw_connection()
        try:
            with conn.cursor() as cur:
                for t in batch:
                    try:
                        sub = data[t] if len(batch) > 1 else data
                        sub = sub.dropna(how="all")
                        if sub.empty:
                            print(f"  [{t}] no new rows")
                            log("ingest_prices", t, "ok", "no new rows")
                            continue
                        n_prices = upsert_prices(cur, sub[["Open", "High", "Low", "Close", "Adj Close", "Volume"]], t)
                        n_divs = upsert_dividends(cur, sub["Dividends"][sub["Dividends"] != 0], t) if "Dividends" in sub.columns else 0
                        n_splits = upsert_splits(cur, sub["Stock Splits"][sub["Stock Splits"] != 0], t) if "Stock Splits" in sub.columns else 0
                        print(f"  [{t}] ok - {n_prices} price rows, {n_divs} dividends, {n_splits} splits")
                        log("ingest_prices", t, "ok", f"{n_prices} price rows")
                    except Exception as e:
                        print(f"  [{t}] ERROR: {e}")
                        log("ingest_prices", t, "error", str(e))
            conn.commit()
        finally:
            conn.close()

        time.sleep(SLEEP_BETWEEN_BATCHES)

    print("\nDone.")


if __name__ == "__main__":
    main()
