"""
Computes three scores and adds them to 'fundamental_ratios' (via UPDATE, not
INSERT - this script assumes compute_fundamental_ratios.py already ran):

- Altman Z-Score (classic 5-variable, bankruptcy/distress risk - higher is safer)
- Beneish M-Score (8-variable, earnings-manipulation risk - values above about
  -1.78 are the traditional flag threshold; higher = more suspicious)
- Value Trap Indicator (VTI) - a custom composite the project's earlier script
  used, built from valuation ratios already in 'fundamental_ratios'; lower =
  less of a "cheap for a reason" trap

Altman Z and Beneish M need balance sheet / income statement / cashflow data
for the last two annual periods (or the TTM-from-quarterly fallback, same
approach as compute_fundamental_ratios.py). If a required line item is
missing, that specific score is left NULL for that ticker rather than guessed.

Usage:
    DATABASE_URL="postgresql://..." python compute_quality_scores.py
"""
import os
import json

import pandas as pd
from sqlalchemy import create_engine, text

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL)

FLOW_STATEMENTS = {"income_statement", "cashflow"}


def get_ratio_tickers():
    """Tickers that already have a fundamental_ratios row (from the other script)."""
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


def get_lfy_pair(ticker, statement):
    annual = load_statement(ticker, statement, "annual")
    if len(annual) >= 2:
        return annual.iloc[0], annual.iloc[1], "annual"
    quarterly = load_statement(ticker, statement, "quarterly")
    if quarterly.empty:
        return None, None, "sem dados"
    if statement not in FLOW_STATEMENTS:
        row_lfy = quarterly.iloc[0]
        row_lfy1 = quarterly.iloc[4] if len(quarterly) >= 5 else (quarterly.iloc[-1] if len(quarterly) >= 2 else None)
        return row_lfy, row_lfy1, "trimestral (mais recente)"
    if len(quarterly) < 8:
        return None, None, "trimestral insuficiente para TTM duplo"
    ttm = quarterly.iloc[0:4].sum(numeric_only=True, min_count=1)
    ttm1 = quarterly.iloc[4:8].sum(numeric_only=True, min_count=1)
    return ttm, ttm1, "TTM (soma de 4 trimestres)"


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


def load_market_cap(ticker):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT market_cap FROM fundamental_ratios WHERE ticker = :t"), {"t": ticker}).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def compute_altman_z(ticker):
    bs_lfy, _, _ = get_lfy_pair(ticker, "balance_sheet")
    inc_lfy, _, _ = get_lfy_pair(ticker, "income_statement")
    if bs_lfy is None or inc_lfy is None:
        return None, None

    total_assets = g(bs_lfy, "Total Assets")
    current_assets = g(bs_lfy, "Current Assets")
    current_liab = g(bs_lfy, "Current Liabilities")
    retained_earnings = g(bs_lfy, "Retained Earnings")
    total_liab = g(bs_lfy, "Total Liabilities Net Minority Interest")
    ebit = g(inc_lfy, "EBIT")
    sales = g(inc_lfy, "Total Revenue")
    market_cap = load_market_cap(ticker)

    if None in (total_assets, current_assets, current_liab, retained_earnings, total_liab, ebit, sales, market_cap) or total_assets == 0:
        return None, None

    a = (current_assets - current_liab) / total_assets
    b = retained_earnings / total_assets
    c = ebit / total_assets
    d = safe_div(market_cap, total_liab) or 0
    e = sales / total_assets

    z = 1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e
    zone = "segura" if z > 2.99 else ("cinzenta" if z >= 1.81 else "risco")
    return round(z, 3), zone


def compute_beneish_m(ticker):
    bs_lfy, bs_lfy1, _ = get_lfy_pair(ticker, "balance_sheet")
    inc_lfy, inc_lfy1, _ = get_lfy_pair(ticker, "income_statement")
    cf_lfy, cf_lfy1, _ = get_lfy_pair(ticker, "cashflow")
    if any(x is None for x in (bs_lfy, bs_lfy1, inc_lfy, inc_lfy1, cf_lfy, cf_lfy1)):
        return None, None

    def year_components(bs, inc, cf):
        receivables = g(bs, "Accounts Receivable")
        sales = g(inc, "Total Revenue")
        gross_profit = g(inc, "Gross Profit")
        current_assets = g(bs, "Current Assets")
        net_ppe = g(bs, "Net PPE")
        investments = g(bs, "Investments And Advances", "Long Term Equity Investment", default=0)
        total_assets = g(bs, "Total Assets")
        depreciation = g(cf, "Depreciation", "Depreciation Amortization Depletion")
        sga = g(inc, "Selling General And Administration")
        current_liab = g(bs, "Current Liabilities")
        lt_debt = g(bs, "Long Term Debt", default=0)
        net_income = g(inc, "Net Income Continuous Operations", "Net Income")
        cfo = g(cf, "Operating Cash Flow")

        if None in (receivables, sales, gross_profit, current_assets, net_ppe, total_assets,
                    depreciation, sga, current_liab, net_income, cfo) or sales == 0 or total_assets == 0:
            return None

        return {
            "dsri_raw": safe_div(receivables, sales),
            "gm": safe_div(gross_profit, sales),
            "aqi_raw": (current_assets + net_ppe + investments) / total_assets,
            "sales": sales,
            "depi_raw": safe_div(depreciation, (net_ppe + depreciation)) if (net_ppe + depreciation) else None,
            "sgai_raw": safe_div(sga, sales),
            "lvgi_raw": safe_div(current_liab + lt_debt, total_assets),
            "tata": safe_div(net_income - cfo, total_assets),
        }

    y0 = year_components(bs_lfy, inc_lfy, cf_lfy)
    y1 = year_components(bs_lfy1, inc_lfy1, cf_lfy1)
    if y0 is None or y1 is None:
        return None, None

    dsri = safe_div(y0["dsri_raw"], y1["dsri_raw"])
    gmi = safe_div(y1["gm"], y0["gm"])
    aqi = safe_div(1 - y0["aqi_raw"], 1 - y1["aqi_raw"]) if y0["aqi_raw"] != 1 and y1["aqi_raw"] != 1 else None
    sgi = safe_div(y0["sales"], y1["sales"])
    depi = safe_div(y1["depi_raw"], y0["depi_raw"])
    sgai = safe_div(y0["sgai_raw"], y1["sgai_raw"])
    lvgi = safe_div(y0["lvgi_raw"], y1["lvgi_raw"])
    tata = y0["tata"]

    components = [dsri, gmi, aqi, sgi, depi, sgai, lvgi, tata]
    if any(c is None for c in components):
        return None, None

    m = (-4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
         + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
    flag = "provável manipulação" if m > -1.78 else "normal"
    return round(m, 3), flag


# Thresholds and clamps below reproduce this project's original VTI formula as-is.
def compute_vti(row):
    pb, ps, pe, pc, de, gr_pct, dy_pct, dpr_pct = (
        row["price_to_book"], row["price_to_sales"], row["price_to_earnings"], row["price_to_cash"],
        row["debt_to_equity"], row["net_income_growth_pct"], row["dividend_yield_pct"], row["dividend_payout_ratio_pct"],
    )
    if None in (pb, ps, pe, pc, de, gr_pct, dy_pct, dpr_pct):
        return None
    gr, dy, dpr = gr_pct / 100, dy_pct / 100, dpr_pct / 100
    if gr == 0 or dy == 0:
        return None

    x1 = (pb / 1.5) ** 2
    x1 = 43 if x1 <= 0 else min(x1, 100000)
    x2 = (ps / 1.25) ** 2
    x2 = min(x2, 100000)
    x3 = pe / 15
    x3 = 43 if x3 <= 0 else x3
    x4 = pc / 10
    x5 = de / 0.25
    x5 = 43 if x5 <= 0 else x5
    x6 = 0.038 / gr
    x6 = 4 if x6 < 0.01 else x6
    x7 = (1 / 3) * ((0.03 / dy) + 2 * (dpr / 0.6))
    x7 = 43 if x7 <= 0 else min(x7, 17.5)

    vti = 14.286 * (x1 + x2 + x3 + x4 + x5 + x6 + x7)
    return round(vti, 2)


def update_scores(ticker, z, zone, m, flag, vti):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE fundamental_ratios SET
                altman_z_score = :z, altman_z_zone = :zone,
                beneish_m_score = :m, beneish_flag = :flag,
                value_trap_indicator = :vti
            WHERE ticker = :t
        """), {"z": z, "zone": zone, "m": m, "flag": flag, "vti": vti, "t": ticker})


def main():
    tickers = get_ratio_tickers()
    print(f"Calculando Altman Z, Beneish M e VTI para {len(tickers)} tickers.")

    with engine.connect() as conn:
        vti_rows = pd.read_sql(text("""
            SELECT ticker, price_to_book, price_to_sales, price_to_earnings, price_to_cash,
                   debt_to_equity, net_income_growth_pct, dividend_yield_pct, dividend_payout_ratio_pct
            FROM fundamental_ratios
        """), conn).set_index("ticker")

    for i, t in enumerate(tickers, start=1):
        z, zone = compute_altman_z(t)
        m, flag = compute_beneish_m(t)
        vti = compute_vti(vti_rows.loc[t]) if t in vti_rows.index else None
        update_scores(t, z, zone, m, flag, vti)
        if i % 50 == 0:
            print(f"  ...{i}/{len(tickers)} processados")

    print("Done.")


if __name__ == "__main__":
    main()
