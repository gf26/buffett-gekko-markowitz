"""
Builds a single composite ranking that's comparable across two different
peer groups:

- 'geral' (most companies): ranked on Earnings Yield + Return on Capital
  (Magic Formula), already computed by compute_magic_formula.py.
- 'financeiro_utility' (Financial Services + Utilities): the Magic Formula's
  "capital employed" concept doesn't fit these sectors, so they're ranked on
  ROE + Price-to-Book instead - a standard substitute for capital-intensive,
  regulated, or balance-sheet-driven businesses.

Within each group, every metric is converted to a PERCENTILE (0-100, higher
= better) before combining - this is what makes the two groups comparable:
"top 10% of your peer group" means the same thing whether the group has 20
companies or 250, whereas raw rank position (like magic_formula_rank) does
not.

Sector classification comes from company_info->>'sector' (Yahoo Finance's
own sector labels), not from which balance sheet fields happen to be
present - more reliable than inferring it from data gaps.

Usage:
    DATABASE_URL="postgresql://..." python compute_composite_score.py
"""
import os

import pandas as pd
from sqlalchemy import create_engine, text

DB_URL = os.environ["DATABASE_URL"]
engine = create_engine(DB_URL)

FINANCEIRO_UTILITY_SECTORS = {"Financial Services", "Utilities"}


def load_data():
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT
                fr.ticker,
                ci.info->>'sector' AS sector,
                fr.net_earnings,
                fr.book_value,
                fr.price_to_book,
                fr.earnings_yield_pct,
                fr.return_on_capital_pct,
                fr.piotroski_f_score
            FROM fundamental_ratios fr
            LEFT JOIN company_info ci ON ci.ticker = fr.ticker
        """), conn)
    return df.set_index("ticker")


# F-Score mínimo pra "entrar" no ranking (0-9). 7+ é um corte de qualidade
# comum na prática - ajuste aqui se quiser mais ou menos rigoroso.
PIOTROSKI_MIN_SCORE = 7


def classify_and_score(df):
    df["peer_group"] = df["sector"].apply(
        lambda s: "financeiro_utility" if s in FINANCEIRO_UTILITY_SECTORS else "geral"
    )
    df["roe_pct"] = ((df["net_earnings"] / df["book_value"]) * 100).round(2)
    df.loc[df["book_value"].isna() | (df["book_value"] == 0), "roe_pct"] = None

    df["composite_percentile"] = None
    df["ranking_status"] = "dados_insuficientes"

    passed_gate = df["piotroski_f_score"] >= PIOTROSKI_MIN_SCORE
    # quem tem os dados de valuation/qualidade mas não passou no F-Score fica marcado,
    # em vez de cair no genérico "dados_insuficientes"
    has_metrics = (
        df[["earnings_yield_pct", "return_on_capital_pct"]].notna().all(axis=1)
        | df[["roe_pct", "price_to_book"]].notna().all(axis=1)
    )
    df.loc[has_metrics & df["piotroski_f_score"].notna() & ~passed_gate, "ranking_status"] = "reprovado_piotroski"

    geral = df[(df["peer_group"] == "geral") & passed_gate].dropna(subset=["earnings_yield_pct", "return_on_capital_pct"])
    if not geral.empty:
        ey_pct = geral["earnings_yield_pct"].rank(pct=True)
        roc_pct = geral["return_on_capital_pct"].rank(pct=True)
        df.loc[geral.index, "composite_percentile"] = ((ey_pct + roc_pct) / 2 * 100).round(1)
        df.loc[geral.index, "ranking_status"] = "ok"

    fin = df[(df["peer_group"] == "financeiro_utility") & passed_gate].dropna(subset=["roe_pct", "price_to_book"])
    if not fin.empty:
        roe_pct = fin["roe_pct"].rank(pct=True)
        pb_pct = fin["price_to_book"].rank(pct=True, ascending=False)  # lower P/B = better
        df.loc[fin.index, "composite_percentile"] = ((roe_pct + pb_pct) / 2 * 100).round(1)
        df.loc[fin.index, "ranking_status"] = "ok"

    ranked = df.dropna(subset=["composite_percentile"])
    df["composite_rank"] = ranked["composite_percentile"].rank(ascending=False, method="min")
    return df


def as_float(x):
    return None if pd.isna(x) else float(x)


def as_int(x):
    return None if pd.isna(x) else int(x)


def update_scores(df):
    with engine.begin() as conn:
        for ticker, row in df.iterrows():
            conn.execute(text("""
                UPDATE fundamental_ratios SET
                    peer_group = :pg, roe_pct = :roe,
                    composite_percentile = :cp, composite_rank = :cr,
                    ranking_status = :status
                WHERE ticker = :t
            """), {
                "pg": row["peer_group"],
                "roe": as_float(row["roe_pct"]),
                "cp": as_float(row["composite_percentile"]),
                "cr": as_int(row["composite_rank"]),
                "status": row["ranking_status"],
                "t": ticker,
            })


def main():
    df = load_data()
    print(f"Classificando e rankeando {len(df)} tickers.")
    df = classify_and_score(df)

    n_geral = (df["peer_group"] == "geral").sum()
    n_fin = (df["peer_group"] == "financeiro_utility").sum()
    print(f"  {n_geral} no grupo 'geral', {n_fin} no grupo 'financeiro_utility'")
    for status, count in df["ranking_status"].value_counts().items():
        print(f"  {status}: {count}")

    update_scores(df)
    print("Done.")


if __name__ == "__main__":
    main()
