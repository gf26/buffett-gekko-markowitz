"""
Computes two well-known quality+value metrics and adds them to
'fundamental_ratios' (via UPDATE - assumes compute_fundamental_ratios.py
already ran):

- Magic Formula (Joel Greenblatt): ranks every ticker by Earnings Yield
  (EBIT/Enterprise Value - cheapness) and by Return on Capital
  (EBIT/(Net Working Capital + Net Fixed Assets) - quality), then combines
  the two rankings into one. Lower magic_formula_rank = better ("good
  companies at a good price").

- Gross Profitability (Robert Novy-Marx): Gross Profit / Total Assets, a
  single, simple quality metric that's hard to manipulate with accounting
  choices. Higher = better.

Note: neither metric applies well to banks/insurers (no "Gross Profit" or
clean "EBIT" concept for them) - those tickers naturally end up with NULL
here, same as gross_margin_pct already does. That's expected, not a bug.

Usage:
    DATABASE_URL="postgresql://..." python compute_magic_formula.py
"""
import os

import pandas as pd
from sqlalchemy import create_engine, text

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL)

FLOW_STATEMENTS = {"income_statement", "cashflow"}
EBIT_FIELDS = ("EBIT", "Operating Income", "Total Operating Income As Reported", "Pretax Income")


def get_ratio_tickers():
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker FROM fundamental_ratios")).fetchall()
    return [r[0] for r in rows]


def load_statement(ticker, statement, period_type):
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


def get_lfy(ticker, statement):
    """Latest-fiscal-year row for a statement, falling back to a TTM built
    from quarterly data (or the latest quarterly snapshot for the balance
    sheet) when annual data isn't available."""
    annual = load_statement(ticker, statement, "annual")
    if not annual.empty:
        return annual.iloc[0]
    quarterly = load_statement(ticker, statement, "quarterly")
    if quarterly.empty:
        return None
    if statement not in FLOW_STATEMENTS:
        return quarterly.iloc[0]
    if len(quarterly) < 4:
        return None
    return quarterly.iloc[0:4].sum(numeric_only=True, min_count=1)


def g(row, *names, default=None):
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


def load_price_and_dividend_fields(ticker):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT market_cap FROM fundamental_ratios WHERE ticker = :t
        """), {"t": ticker}).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def compute_for_ticker(ticker):
    bs = get_lfy(ticker, "balance_sheet")
    inc = get_lfy(ticker, "income_statement")
    if bs is None or inc is None:
        return None

    market_cap = load_price_and_dividend_fields(ticker)
    total_debt = g(bs, "Total Debt", default=0)
    cash = g(bs, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments", default=0)
    current_assets = g(bs, "Current Assets")
    current_liab = g(bs, "Current Liabilities")
    net_ppe = g(bs, "Net PPE")
    total_assets = g(bs, "Total Assets")
    gross_profit = g(inc, "Gross Profit")
    ebit = g(inc, *EBIT_FIELDS)

    result = {"ticker": ticker}

    if market_cap is not None and ebit is not None:
        enterprise_value = market_cap + total_debt - cash
        result["enterprise_value"] = round(enterprise_value, 2)
        result["earnings_yield_pct"] = round(safe_div(ebit, enterprise_value) * 100, 2) if enterprise_value else None
    else:
        result["enterprise_value"] = None
        result["earnings_yield_pct"] = None

    if None not in (current_assets, current_liab, net_ppe) and ebit is not None:
        capital_employed = (current_assets - current_liab) + net_ppe
        result["return_on_capital_pct"] = round(safe_div(ebit, capital_employed) * 100, 2) if capital_employed else None
    else:
        result["return_on_capital_pct"] = None

    if gross_profit is not None and total_assets:
        result["gross_profitability_pct"] = round(safe_div(gross_profit, total_assets) * 100, 2)
    else:
        result["gross_profitability_pct"] = None

    return result


def rank_and_update(rows):
    df = pd.DataFrame(rows).set_index("ticker")

    valid_magic = df.dropna(subset=["earnings_yield_pct", "return_on_capital_pct"])
    ey_rank = valid_magic["earnings_yield_pct"].rank(ascending=False, method="min")
    roc_rank = valid_magic["return_on_capital_pct"].rank(ascending=False, method="min")
    magic_score = (ey_rank + roc_rank)
    magic_rank = magic_score.rank(ascending=True, method="min")

    gp_rank = df["gross_profitability_pct"].rank(ascending=False, method="min")

    def as_float(x):
        return None if pd.isna(x) else float(x)

    def as_int(series, key):
        return int(series[key]) if key in series.index and pd.notna(series[key]) else None

    with engine.begin() as conn:
        for ticker, row in df.iterrows():
            conn.execute(text("""
                UPDATE fundamental_ratios SET
                    enterprise_value = :ev,
                    earnings_yield_pct = :ey,
                    return_on_capital_pct = :roc,
                    magic_formula_score = :mscore,
                    magic_formula_rank = :mrank,
                    gross_profitability_pct = :gp,
                    gross_profitability_rank = :gprank
                WHERE ticker = :t
            """), {
                "ev": as_float(row["enterprise_value"]),
                "ey": as_float(row["earnings_yield_pct"]),
                "roc": as_float(row["return_on_capital_pct"]),
                "mscore": as_int(magic_score, ticker),
                "mrank": as_int(magic_rank, ticker),
                "gp": as_float(row["gross_profitability_pct"]),
                "gprank": as_int(gp_rank, ticker),
                "t": ticker,
            })


def main():
    tickers = get_ratio_tickers()
    print(f"Calculando Magic Formula e Gross Profitability para {len(tickers)} tickers.")
    rows = []
    for i, t in enumerate(tickers, start=1):
        r = compute_for_ticker(t)
        if r is not None:
            rows.append(r)
        if i % 50 == 0:
            print(f"  ...{i}/{len(tickers)} processados")

    rank_and_update(rows)
    ranked = sum(1 for r in rows if r["earnings_yield_pct"] is not None and r["return_on_capital_pct"] is not None)
    print(f"Done. {len(rows)} tickers com dados, {ranked} entraram no ranking da Magic Formula.")


if __name__ == "__main__":
    main()
