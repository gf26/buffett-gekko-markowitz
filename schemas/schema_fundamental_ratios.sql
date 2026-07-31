-- Etapa 2, Parte B (1/2): índices de valuation + Piotroski F-Score.
-- Rode uma vez no SQL Editor do Supabase.

CREATE TABLE IF NOT EXISTS fundamental_ratios (
    ticker                      TEXT PRIMARY KEY REFERENCES tickers(ticker),
    calculated_at               TIMESTAMPTZ DEFAULT now(),
    fiscal_date_lfy              DATE,       -- último exercício fiscal usado (LFY)
    fiscal_date_lfy1             DATE,       -- exercício anterior (LFY-1), usado nos deltas
    price_used                  NUMERIC,
    price_date                  DATE,
    market_cap                  NUMERIC,

    price_to_book                NUMERIC,     -- P/VPA
    price_to_sales                NUMERIC,     -- P/S
    price_to_earnings             NUMERIC,     -- P/L
    price_to_cash                 NUMERIC,     -- P/Caixa
    debt_to_equity                NUMERIC,

    net_income_growth_pct         NUMERIC,     -- crescimento do lucro líquido, LFY vs LFY-1
    operating_income_growth_pct    NUMERIC,

    dividend_yield_pct            NUMERIC,     -- dividendos pagos nos últimos 12 meses / preço atual
    dividend_payout_ratio_pct      NUMERIC,     -- dividendos por ação / lucro por ação

    book_value                   NUMERIC,
    sales                        NUMERIC,
    gross_earnings                NUMERIC,
    operating_earnings            NUMERIC,
    net_earnings                  NUMERIC,
    cash_on_hand                  NUMERIC,

    gross_margin_pct              NUMERIC,
    operating_margin_pct           NUMERIC,
    net_margin_pct                NUMERIC,

    piotroski_f_score             INTEGER,     -- 0 a 9
    piotroski_breakdown            JSONB        -- detalha cada um dos 9 critérios (true/false/null)
);

-- Adição: registra de onde veio cada demonstrativo (anual de verdade vs. TTM trimestral somado)
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS data_sources JSONB;
