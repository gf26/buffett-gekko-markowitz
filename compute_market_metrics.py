"""
Computes market-based screener metrics (return, volatility, Sharpe, Sortino,
max drawdown) from the cached price history in 'prices_daily', and upserts
one summary row per ticker into 'market_metrics'.

Uses the trailing LOOKBACK_DAYS of trading days (default: ~3 years). Assumes
a risk-free rate of RISK_FREE_RATE_ANNUAL (annualized) for Sharpe/Sortino -
this is a simplification; swap in the CDI or SELIC rate later if you want
Sharpe/Sortino benchmarked against the real Brazilian risk-free rate instead
of an approximate constant.

Usage:
    DATABASE_URL="postgresql://..." python compute_market_metrics.py
"""
import os

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL)

LOOKBACK_DAYS = 252 * 3       # ~3 years of trading days
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE_ANNUAL = 0.0   # simplification - see note above
MIN_OBSERVATIONS = 60         # skip tickers with too little history to be meaningful


def get_active_tickers():
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker FROM tickers WHERE active = TRUE ORDER BY ticker")).fetchall()
    return [r[0] for r in rows]


def load_prices(ticker):
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT date, adj_close FROM prices_daily
                WHERE ticker = :t AND adj_close IS NOT NULL
                ORDER BY date DESC LIMIT :n
            """),
            conn, params={"t": ticker, "n": LOOKBACK_DAYS},
        )
    return df.sort_values("date").reset_index(drop=True)


def compute_metrics(df):
    if len(df) < MIN_OBSERVATIONS:
        return None

    prices = df["adj_close"].astype(float)
    returns = prices.pct_change().dropna()
    if returns.empty or returns.std() == 0:
        return None

    n_days = len(returns)
    total_return = prices.iloc[-1] / prices.iloc[0] - 1
    years = n_days / TRADING_DAYS_PER_YEAR
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else None

    ann_vol = returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR)
    excess_return = cagr - RISK_FREE_RATE_ANNUAL if cagr is not None else None
    sharpe = excess_return / ann_vol if ann_vol and excess_return is not None else None

    downside_returns = returns[returns < 0]
    downside_dev = downside_returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) if not downside_returns.empty else None
    sortino = excess_return / downside_dev if downside_dev and excess_return is not None else None

    cumulative = (1 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = cumulative / running_max - 1
    max_drawdown = drawdown.min()

    return {
        "obs_start": df["date"].iloc[0],
        "obs_end": df["date"].iloc[-1],
        "trading_days": n_days,
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(cagr * 100, 2) if cagr is not None else None,
        "ann_volatility_pct": round(ann_vol * 100, 2),
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "sortino_ratio": round(sortino, 3) if sortino is not None else None,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
    }


def upsert_metrics(rows):
    if not rows:
        return
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO market_metrics (
                    ticker, obs_start, obs_end, trading_days, total_return_pct,
                    cagr_pct, ann_volatility_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct
                ) VALUES %s
                ON CONFLICT (ticker) DO UPDATE SET
                    calculated_at = now(),
                    obs_start = EXCLUDED.obs_start,
                    obs_end = EXCLUDED.obs_end,
                    trading_days = EXCLUDED.trading_days,
                    total_return_pct = EXCLUDED.total_return_pct,
                    cagr_pct = EXCLUDED.cagr_pct,
                    ann_volatility_pct = EXCLUDED.ann_volatility_pct,
                    sharpe_ratio = EXCLUDED.sharpe_ratio,
                    sortino_ratio = EXCLUDED.sortino_ratio,
                    max_drawdown_pct = EXCLUDED.max_drawdown_pct
            """, rows, page_size=500)
        conn.commit()
    finally:
        conn.close()


def main():
    tickers = get_active_tickers()
    print(f"Computing market metrics for {len(tickers)} tickers.")
    rows = []
    skipped = 0

    for i, t in enumerate(tickers, start=1):
        df = load_prices(t)
        m = compute_metrics(df)
        if m is None:
            skipped += 1
            continue
        rows.append((
            t, m["obs_start"], m["obs_end"], m["trading_days"], m["total_return_pct"],
            m["cagr_pct"], m["ann_volatility_pct"], m["sharpe_ratio"], m["sortino_ratio"], m["max_drawdown_pct"],
        ))
        if i % 50 == 0:
            print(f"  ...{i}/{len(tickers)} processados")

    upsert_metrics(rows)
    print(f"Done. {len(rows)} tickers com métricas calculadas, {skipped} pulados (histórico insuficiente).")


if __name__ == "__main__":
    main()
