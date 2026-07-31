-- Etapa 2, Parte B (2/2): Altman Z-Score, Beneish M-Score, Value Trap Indicator.
-- Rode uma vez no SQL Editor do Supabase.

ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS altman_z_score       NUMERIC;
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS altman_z_zone        TEXT;   -- 'segura' | 'cinzenta' | 'risco'
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS beneish_m_score      NUMERIC;
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS beneish_flag         TEXT;   -- 'provável manipulação' | 'normal'
ALTER TABLE fundamental_ratios ADD COLUMN IF NOT EXISTS value_trap_indicator NUMERIC; -- menor = melhor (menos "armadilha de valor")
