-- View "screener": junta índices fundamentalistas + indicadores de mercado
-- numa única planilha, uma linha por ticker. Rode uma vez no SQL Editor.

CREATE OR REPLACE VIEW vw_screener AS
SELECT
    t.ticker,
    t.name,
    fr.price_used,
    fr.market_cap,

    fr.price_to_book,
    fr.price_to_sales,
    fr.price_to_earnings,
    fr.price_to_cash,
    fr.debt_to_equity,

    fr.dividend_yield_pct,
    fr.dividend_payout_ratio_pct,

    fr.net_income_growth_pct,
    fr.operating_income_growth_pct,
    fr.gross_margin_pct,
    fr.operating_margin_pct,
    fr.net_margin_pct,

    fr.piotroski_f_score,
    fr.altman_z_score,
    fr.altman_z_zone,
    fr.beneish_m_score,
    fr.beneish_flag,
    fr.value_trap_indicator,

    mm.cagr_pct,
    mm.ann_volatility_pct,
    mm.sharpe_ratio,
    mm.sortino_ratio,
    mm.max_drawdown_pct,

    fr.fiscal_date_lfy,
    fr.data_sources,
    fr.calculated_at   AS fundamentos_calculados_em,
    mm.calculated_at   AS mercado_calculado_em
FROM tickers t
LEFT JOIN fundamental_ratios fr ON fr.ticker = t.ticker
LEFT JOIN market_metrics mm     ON mm.ticker = t.ticker
WHERE t.active = TRUE
ORDER BY t.ticker;
