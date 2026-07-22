"""
Computes valuation ratios (P/B, P/S, P/E, P/Cash, Debt/Equity, growth, dividend
yield/payout, margins) and the Piotroski F-Score, using the last two annual
fiscal periods available in 'financials', the latest cached price, and
'marketCap' from 'company_info'. Upserts one summary row per ticker into
'fundamental_ratios'.

If a ticker is missing a line item it needs, that specific ratio is simply
left NULL (and, for the Piotroski score, that criterion isn't counted) rather
than failing the whole ticker - Yahoo Finance's coverage varies a lot,
especially for smaller B3 companies.

Usage:
    DATABASE_URL="postgresql://..." python compute_fundamental_ratios.py
"""
import os
import json
from datetime import timedelta

import pandas as pd
from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL)


def get_active_tickers():
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker FROM tickers WHERE active = TRUE ORDER BY ticker")).fetchall()
    return [r[0] for r in rows]


def load_statement(ticker, statement, period_type):
    """Returns a DataFrame indexed by fiscal_date (desc), columns = line_item."""
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT fiscal_date, line_item, value FROM financials
                WHERE ticker = :t AND statement = :s AND period_type = :p
            """),
            conn, params={"t": ticker, "s": statement, "p": period_type},
        )
    if df.empty:
        return pd.DataFrame()
    wide = df.pivot_table(index="fiscal_date", columns="line_item", values="value", aggfunc="first")
    return wide.sort_index(ascending=False)


# Statements that represent a flow over a period (income statement, cashflow) get
# summed across 4 quarters to build a TTM (trailing twelve months) figure when
# annual data isn't available. Balance sheet is a point-in-time snapshot, so it's
# never summed - we just use the most recent quarter available instead.
FLOW_STATEMENTS = {"income_statement", "cashflow"}


def get_lfy_pair(ticker, statement):
    """
    Returns (row_lfy, row_lfy1, date_lfy, date_lfy1, source) for a statement.
    Prefers real annual data; falls back to quarterly (summed into TTM for flow
    statements, or just the latest snapshot for the balance sheet) when annual
    isn't available - some tickers only have partial annual coverage on Yahoo.
    """
    annual = load_statement(ticker, statement, "annual")
    if len(annual) >= 2:
        return annual.iloc[0], annual.iloc[1], annual.index[0], annual.index[1], "annual"
    if len(annual) == 1:
        return annual.iloc[0], None, annual.index[0], None, "annual (1 período)"

    quarterly = load_statement(ticker, statement, "quarterly")
    if quarterly.empty:
        return None, None, None, None, "sem dados"

    if statement not in FLOW_STATEMENTS:  # balance sheet: point-in-time snapshot
        row_lfy, date_lfy = quarterly.iloc[0], quarterly.index[0]
        if len(quarterly) >= 5:
            row_lfy1, date_lfy1 = quarterly.iloc[4], quarterly.index[4]  # ~1 year back
        elif len(quarterly) >= 2:
            row_lfy1, date_lfy1 = quarterly.iloc[-1], quarterly.index[-1]
        else:
            row_lfy1, date_lfy1 = None, None
        return row_lfy, row_lfy1, date_lfy, date_lfy1, "trimestral (mais recente)"

    # flow statement: sum quarters into TTM windows
    if len(quarterly) < 4:
        ttm = quarterly.sum(numeric_only=True, min_count=1)
        return ttm, None, quarterly.index[0], None, "trimestral parcial (menos de 4 tri)"
    ttm = quarterly.iloc[0:4].sum(numeric_only=True, min_count=1)
    date_ttm = quarterly.index[0]
    if len(quarterly) >= 8:
        ttm1 = quarterly.iloc[4:8].sum(numeric_only=True, min_count=1)
        date_ttm1 = quarterly.index[4]
    else:
        ttm1, date_ttm1 = None, None
    return ttm, ttm1, date_ttm, date_ttm1, "TTM (soma de 4 trimestres)"


def load_latest_price(ticker):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT date, adj_close FROM prices_daily
            WHERE ticker = :t AND adj_close IS NOT NULL
            ORDER BY date DESC LIMIT 1
        """), {"t": ticker}).fetchone()
    return (row[0], float(row[1])) if row else (None, None)


def load_market_cap(ticker):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT (info->>'marketCap')::NUMERIC FROM company_info WHERE ticker = :t
        """), {"t": ticker}).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def load_trailing_dividends(ticker, price_date):
    if price_date is None:
        return 0.0
    start = price_date - timedelta(days=365)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT COALESCE(SUM(amount), 0) FROM dividends
            WHERE ticker = :t AND ex_date > :start AND ex_date <= :end
        """), {"t": ticker, "start": start, "end": price_date}).fetchone()
    return float(row[0])


def g(row, *names, default=None):
    """Gets the first present, non-null value among the given line-item names."""
    if row is None:
        return default
    for name in names:
        if name in row and pd.notna(row[name]):
            return float(row[name])
    return default


def safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def compute_piotroski(bs_lfy, bs_lfy1, inc_lfy, inc_lfy1, cf_lfy):
    """Returns (score, breakdown_dict). Each criterion is True/False/None (None = data missing)."""
    ni_lfy = g(inc_lfy, "Net Income")
    ni_lfy1 = g(inc_lfy1, "Net Income")
    assets_lfy = g(bs_lfy, "Total Assets")
    assets_lfy1 = g(bs_lfy1, "Total Assets")
    cfo_lfy = g(cf_lfy, "Operating Cash Flow")
    ltd_lfy = g(bs_lfy, "Long Term Debt", default=0)
    ltd_lfy1 = g(bs_lfy1, "Long Term Debt", default=0)
    ca_lfy = g(bs_lfy, "Current Assets")
    ca_lfy1 = g(bs_lfy1, "Current Assets")
    cl_lfy = g(bs_lfy, "Current Liabilities")
    cl_lfy1 = g(bs_lfy1, "Current Liabilities")
    shares_lfy = g(bs_lfy, "Ordinary Shares Number", "Share Issued")
    shares_lfy1 = g(bs_lfy1, "Ordinary Shares Number", "Share Issued")
    gm_lfy = safe_div(g(inc_lfy, "Gross Profit"), g(inc_lfy, "Total Revenue"))
    gm_lfy1 = safe_div(g(inc_lfy1, "Gross Profit"), g(inc_lfy1, "Total Revenue"))
    sales_lfy = g(inc_lfy, "Total Revenue")
    sales_lfy1 = g(inc_lfy1, "Total Revenue")

    roa_lfy = safe_div(ni_lfy, assets_lfy)
    roa_lfy1 = safe_div(ni_lfy1, assets_lfy1)
    leverage_lfy = safe_div(ltd_lfy, assets_lfy)
    leverage_lfy1 = safe_div(ltd_lfy1, assets_lfy1)
    current_ratio_lfy = safe_div(ca_lfy, cl_lfy)
    current_ratio_lfy1 = safe_div(ca_lfy1, cl_lfy1)
    turnover_lfy = safe_div(sales_lfy, assets_lfy)
    turnover_lfy1 = safe_div(sales_lfy1, assets_lfy1)

    def cmp(a, b, op):
        if a is None or b is None:
            return None
        return op(a, b)

    breakdown = {
        "f1_lucro_positivo": cmp(ni_lfy, 0, lambda a, b: a > b),
        "f2_caixa_operacional_positivo": cmp(cfo_lfy, 0, lambda a, b: a > b),
        "f3_roa_melhorou": cmp(roa_lfy, roa_lfy1, lambda a, b: a > b),
        "f4_caixa_operacional_maior_que_lucro": cmp(cfo_lfy, ni_lfy, lambda a, b: a > b),
        "f5_alavancagem_caiu": cmp(leverage_lfy, leverage_lfy1, lambda a, b: a < b),
        "f6_liquidez_corrente_melhorou": cmp(current_ratio_lfy, current_ratio_lfy1, lambda a, b: a > b),
        "f7_sem_diluicao_de_acoes": cmp(shares_lfy, shares_lfy1, lambda a, b: a <= b),
        "f8_margem_bruta_melhorou": cmp(gm_lfy, gm_lfy1, lambda a, b: a > b),
        "f9_giro_de_ativos_melhorou": cmp(turnover_lfy, turnover_lfy1, lambda a, b: a > b),
    }
    known = [v for v in breakdown.values() if v is not None]
    score = sum(1 for v in known if v) if known else None
    breakdown["criterios_avaliados"] = len(known)
    return score, breakdown


def compute_for_ticker(ticker):
    bs_lfy, bs_lfy1, bs_date_lfy, bs_date_lfy1, bs_source = get_lfy_pair(ticker, "balance_sheet")
    inc_lfy, inc_lfy1, inc_date_lfy, inc_date_lfy1, inc_source = get_lfy_pair(ticker, "income_statement")
    cf_lfy, cf_lfy1, cf_date_lfy, cf_date_lfy1, cf_source = get_lfy_pair(ticker, "cashflow")

    if bs_lfy is None or inc_lfy is None:
        return None, f"sem balanço ({bs_source}) ou DRE ({inc_source}) suficiente"

    fiscal_date_lfy = bs_date_lfy
    fiscal_date_lfy1 = bs_date_lfy1
    data_sources = {"balance_sheet": bs_source, "income_statement": inc_source, "cashflow": cf_source}

    price_date, price = load_latest_price(ticker)
    market_cap = load_market_cap(ticker)
    trailing_div = load_trailing_dividends(ticker, price_date)

    book_value = g(bs_lfy, "Stockholders Equity", "Common Stock Equity")
    total_liab = g(bs_lfy, "Total Liabilities Net Minority Interest")
    cash = g(bs_lfy, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")
    sales = g(inc_lfy, "Total Revenue")
    net_income = g(inc_lfy, "Net Income")
    gross_profit = g(inc_lfy, "Gross Profit")
    operating_income = g(inc_lfy, "Operating Income")
    eps_diluted = g(inc_lfy, "Diluted EPS", "Basic EPS")

    net_income_lfy1 = g(inc_lfy1, "Net Income")
    operating_income_lfy1 = g(inc_lfy1, "Operating Income")

    dividend_per_share = trailing_div  # yfinance dividends are already per-share
    row = {
        "ticker": ticker,
        "fiscal_date_lfy": fiscal_date_lfy,
        "fiscal_date_lfy1": fiscal_date_lfy1,
        "price_used": price,
        "price_date": price_date,
        "market_cap": market_cap,
        "price_to_book": safe_div(market_cap, book_value),
        "price_to_sales": safe_div(market_cap, sales),
        "price_to_earnings": safe_div(market_cap, net_income),
        "price_to_cash": safe_div(market_cap, cash),
        "debt_to_equity": safe_div(total_liab, book_value),
        "net_income_growth_pct": round(safe_div(net_income - net_income_lfy1, abs(net_income_lfy1)) * 100, 2)
            if net_income is not None and net_income_lfy1 not in (None, 0) else None,
        "operating_income_growth_pct": round(safe_div(operating_income - operating_income_lfy1, abs(operating_income_lfy1)) * 100, 2)
            if operating_income is not None and operating_income_lfy1 not in (None, 0) else None,
        "dividend_yield_pct": round(safe_div(dividend_per_share, price) * 100, 2) if price else None,
        "dividend_payout_ratio_pct": round(safe_div(dividend_per_share, eps_diluted) * 100, 2) if eps_diluted else None,
        "book_value": book_value,
        "sales": sales,
        "gross_earnings": gross_profit,
        "operating_earnings": operating_income,
        "net_earnings": net_income,
        "cash_on_hand": cash,
        "gross_margin_pct": round(safe_div(gross_profit, sales) * 100, 2) if gross_profit is not None and sales else None,
        "operating_margin_pct": round(safe_div(operating_income, sales) * 100, 2) if operating_income is not None and sales else None,
        "net_margin_pct": round(safe_div(net_income, sales) * 100, 2) if net_income is not None and sales else None,
    }
    score, breakdown = compute_piotroski(bs_lfy, bs_lfy1, inc_lfy, inc_lfy1, cf_lfy)
    row["piotroski_f_score"] = score
    row["piotroski_breakdown"] = json.dumps(breakdown)
    row["data_sources"] = json.dumps(data_sources)
    return row, None


COLUMNS = [
    "ticker", "fiscal_date_lfy", "fiscal_date_lfy1", "price_used", "price_date", "market_cap",
    "price_to_book", "price_to_sales", "price_to_earnings", "price_to_cash", "debt_to_equity",
    "net_income_growth_pct", "operating_income_growth_pct", "dividend_yield_pct", "dividend_payout_ratio_pct",
    "book_value", "sales", "gross_earnings", "operating_earnings", "net_earnings", "cash_on_hand",
    "gross_margin_pct", "operating_margin_pct", "net_margin_pct", "piotroski_f_score", "piotroski_breakdown",
    "data_sources",
]


def upsert_rows(rows):
    if not rows:
        return
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            values = [tuple(r[c] for c in COLUMNS) for r in rows]
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "ticker")
            execute_values(cur, f"""
                INSERT INTO fundamental_ratios ({", ".join(COLUMNS)})
                VALUES %s
                ON CONFLICT (ticker) DO UPDATE SET calculated_at = now(), {set_clause}
            """, values, page_size=200)
        conn.commit()
    finally:
        conn.close()


def main():
    tickers = get_active_tickers()
    print(f"Calculando índices fundamentalistas para {len(tickers)} tickers.")
    rows, skipped_list = [], []

    for i, t in enumerate(tickers, start=1):
        row, reason = compute_for_ticker(t)
        if row is None:
            skipped_list.append((t, reason))
            print(f"  [{t}] pulado - {reason}")
            continue
        rows.append(row)
        if i % 50 == 0:
            print(f"  ...{i}/{len(tickers)} processados")

    upsert_rows(rows)
    print(f"\nDone. {len(rows)} tickers calculados, {len(skipped_list)} pulados.")
    if skipped_list:
        print("Pulados:", ", ".join(t for t, _ in skipped_list))


if __name__ == "__main__":
    main()
