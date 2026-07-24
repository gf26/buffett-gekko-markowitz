-- Ranking combinado (percentil dentro do grupo de pares), unindo Magic
-- Formula (empresas "gerais") com ROE + P/VPA (financeiras e utilities).
-- Rode uma vez no SQL Editor do Supabase.

ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS peer_group           TEXT;    -- 'geral' | 'financeiro_utility'
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS roe_pct               NUMERIC; -- Lucro Líquido / Patrimônio Líquido, usado só no grupo financeiro_utility
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS composite_percentile   NUMERIC; -- 0 a 100, comparável entre grupos - maior = melhor
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS composite_rank        INTEGER; -- posição geral por composite_percentile, 1 = melhor

-- Adição: filtro de qualidade (Piotroski F-Score) como "porta de entrada"
-- do ranking, sem esconder os tickers reprovados da view.
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS ranking_status TEXT;
-- valores possíveis: 'ok' | 'reprovado_piotroski' | 'dados_insuficientes'
