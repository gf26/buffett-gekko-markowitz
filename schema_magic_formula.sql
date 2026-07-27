-- Magic Formula (Greenblatt) + Gross Profitability (Novy-Marx).
-- Rode uma vez no SQL Editor do Supabase.

ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS enterprise_value          NUMERIC;
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS earnings_yield_pct         NUMERIC;  -- EBIT / Enterprise Value
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS return_on_capital_pct       NUMERIC;  -- EBIT / (Capital de Giro + Ativo Fixo Líquido)
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS magic_formula_score         INTEGER;  -- soma dos 2 rankings (menor = melhor)
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS magic_formula_rank          INTEGER;  -- posição final, 1 = melhor

ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS gross_profitability_pct     NUMERIC;  -- Lucro Bruto / Ativos Totais
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS gross_profitability_rank    INTEGER;  -- posição, 1 = melhor

-- Adição: FCF Yield, segundo índice de "barateza" (visão de caixa, em vez de contábil).
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS fcf_yield_pct NUMERIC;
