"""
Daily ingestion job: prices, dividends, and splits.

Tickers with no price history yet always get the FULL history (from
DEFAULT_START), processed as their own batches - they are never mixed into a
batch with already-loaded tickers, which would otherwise drag their start
date forward (that was a real bug: a shared per-batch start date, computed
from tickers that already had data, was being applied to brand-new tickers
too, truncating their history to just the incremental window).

Tickers that already have history keep using the incremental approach: only
fetch days after the latest date already stored (with a small overlap).

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


def get_last_dates(tickers):
    """Returns {ticker: last_date_or_None} for all given tickers in one query."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ticker, MAX(date) FROM prices_daily
            WHERE ticker = ANY(:tickers) GROUP BY ticker
        """), {"tickers": tickers}).fetchall()
    last = {t: None for t in tickers}
    last.update({r[0]: r[1] for r in rows})
    return last


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


def process_batch(batch, start):
    """Downloads one batch of tickers from `start` onward and upserts everything."""
    print(f"  baixando {len(batch)} tickers a partir de {start}...")
    t0 = time.time()
    try:
        data = yf.download(batch, interval="1d", start=start, group_by="ticker",
                            actions=True, rounding=True, threads=True, progress=False,
                            auto_adjust=False)
    except Exception as e:
        for t in batch:
            log("ingest_prices", t, "error", f"batch download failed: {e}")
        print(f"  FALHOU: {e}")
        return
    print(f"  download concluído em {time.time() - t0:.1f}s, gravando no banco...")

    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            for t in batch:
                try:
                    sub = data[t] if len(batch) > 1 else data
                    sub = sub.dropna(how="all")
                    if sub.empty:
                        print(f"  [{t}] sem linhas novas")
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


def run_group(tickers, get_start):
    """get_start(sub_batch, last_dates) -> start date string for that sub-batch."""
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(tickers) - 1) // BATCH_SIZE + 1
        print(f"\nLote {batch_num}/{total_batches} ({len(batch)} tickers)")
        start = get_start(batch)
        process_batch(batch, start)


def main():
    tickers = get_active_tickers()
    if not tickers:
        print("No tickers found in the 'tickers' table. Run seed_tickers.py first.")
        sys.exit(1)

    last_dates = get_last_dates(tickers)
    new_tickers = [t for t in tickers if last_dates[t] is None]
    existing_tickers = [t for t in tickers if last_dates[t] is not None]

    print(f"{len(new_tickers)} tickers sem histórico ainda -> baixando desde {DEFAULT_START}")
    print(f"{len(existing_tickers)} tickers com histórico -> baixando só o incremental")

    if new_tickers:
        print("\n=== Backfill completo (tickers novos) ===")
        run_group(new_tickers, lambda batch: DEFAULT_START)

    if existing_tickers:
        print("\n=== Atualização incremental (tickers existentes) ===")
        run_group(
            existing_tickers,
            lambda batch: (min(last_dates[t] for t in batch) - timedelta(days=5)).isoformat(),
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
