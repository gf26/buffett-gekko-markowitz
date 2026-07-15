"""
Weekly ingestion job: balance sheet, income statement, cashflow, and company info.

These change quarterly at most, so running this weekly (instead of daily) is plenty,
and keeps well clear of Yahoo Finance rate limits.

Each piece (each statement, and company info) is fetched and upserted independently,
so a failure in one (e.g. yfinance's .info sometimes raises internal errors for
thinly-covered tickers) doesn't discard the rest of that ticker's data.

Usage:
    DATABASE_URL="postgresql://..." python ingest_fundamentals.py
"""
import os
import json
import time

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL)
SLEEP_BETWEEN_TICKERS = 1.5

# yfinance attribute -> (statement name, period type) stored in the 'financials' table
STATEMENTS = {
    "balance_sheet": ("balance_sheet", "annual"),
    "quarterly_balance_sheet": ("balance_sheet", "quarterly"),
    "financials": ("income_statement", "annual"),
    "quarterly_financials": ("income_statement", "quarterly"),
    "cashflow": ("cashflow", "annual"),
    "quarterly_cashflow": ("cashflow", "quarterly"),
}


def get_active_tickers():
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker FROM tickers WHERE active = TRUE ORDER BY ticker")).fetchall()
    return [r[0] for r in rows]


def upsert_financials(cur, df, ticker, statement, period_type):
    if df is None:
        return 0
    if isinstance(df, pd.Series):  # yfinance occasionally returns a Series instead of a DataFrame
        df = df.to_frame()
    if df.empty:
        return 0
    long_df = df.reset_index().melt(id_vars=df.reset_index().columns[0], var_name="fiscal_date", value_name="value")
    long_df.columns = ["line_item", "fiscal_date", "value"]
    long_df = long_df.dropna(subset=["value"])
    long_df["fiscal_date"] = pd.to_datetime(long_df["fiscal_date"], errors="coerce").dt.date
    long_df = long_df.dropna(subset=["fiscal_date"])
    if long_df.empty:
        return 0
    rows = [(ticker, statement, period_type, r.fiscal_date, r.line_item, float(r.value))
            for r in long_df.itertuples(index=False)]
    execute_values(cur, """
        INSERT INTO financials (ticker, statement, period_type, fiscal_date, line_item, value)
        VALUES %s
        ON CONFLICT (ticker, statement, period_type, fiscal_date, line_item)
        DO UPDATE SET value = EXCLUDED.value, fetched_at = now()
    """, rows, page_size=1000)
    return len(rows)


def upsert_info(cur, ticker, info):
    if not info:
        return
    cur.execute("""
        INSERT INTO company_info (ticker, info, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (ticker) DO UPDATE SET info = EXCLUDED.info, updated_at = now()
    """, (ticker, json.dumps(info, default=str)))

    name = info.get("longName") or info.get("shortName")
    if name:
        cur.execute("UPDATE tickers SET name = %s WHERE ticker = %s", (name, ticker))


def log(job, ticker, status, message=""):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ingestion_log (job_name, ticker, status, message)
            VALUES (:job, :ticker, :status, :message)
        """), {"job": job, "ticker": ticker, "status": status, "message": (message or "")[:500]})


def main():
    tickers = get_active_tickers()
    print(f"Starting fundamentals ingestion for {len(tickers)} tickers.")

    for idx, t in enumerate(tickers, start=1):
        tk = yf.Ticker(t)
        total_rows = 0
        issues = []

        conn = engine.raw_connection()
        try:
            with conn.cursor() as cur:
                for attr, (statement, period_type) in STATEMENTS.items():
                    try:
                        df = getattr(tk, attr, None)
                        total_rows += upsert_financials(cur, df, t, statement, period_type)
                    except Exception as e:
                        issues.append(f"{attr}: {e}")

                try:
                    upsert_info(cur, t, tk.info)
                except Exception as e:
                    issues.append(f"info: {e}")

            conn.commit()  # commits whatever succeeded above, even if some pieces raised
        except Exception as e:
            conn.rollback()
            issues.append(f"fatal: {e}")
        finally:
            conn.close()

        status = "ok" if not issues else ("partial" if total_rows else "error")
        message = f"{total_rows} rows" + (f" - issues: {'; '.join(issues)}" if issues else "")
        print(f"[{idx}/{len(tickers)}] [{t}] {status} - {message}")
        log("ingest_fundamentals", t, status, message)

        time.sleep(SLEEP_BETWEEN_TICKERS)
    print("Done.")


if __name__ == "__main__":
    main()
