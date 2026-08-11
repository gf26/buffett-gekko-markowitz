-- Migração: ticker_cvm_map de 1:1 para 1:N
--
-- POR QUÊ
-- -------
-- Empresas se reestruturam (fusão, cisão, incorporação, mudança de razão
-- social) e ganham um NOVO registro na CVM, com CD_CVM diferente. O histórico
-- pré-reestruturação fica sob o código ANTIGO.
--
-- Com PRIMARY KEY (ticker), carregar o mapeamento de um registro antigo
-- SOBRESCREVERIA o registro atual - você perderia o mapeamento que já
-- funciona e ganharia só o histórico velho. Com a chave composta, o mesmo
-- ticker pode apontar para vários CD_CVM, e o ingestor junta o histórico
-- de todos eles.
--
-- SEGURO DE RODAR: preserva todos os dados já carregados.

BEGIN;

-- 1. Guarda o que já existe
CREATE TEMP TABLE _backup_map AS SELECT * FROM ticker_cvm_map;

-- 2. Recria com chave composta
DROP TABLE ticker_cvm_map;

CREATE TABLE ticker_cvm_map (
    ticker      TEXT NOT NULL REFERENCES tickers(ticker),
    cd_cvm      TEXT NOT NULL,
    nome_cvm    TEXT,
    confianca   TEXT,
    -- TRUE = registro em uso hoje; FALSE = registro histórico (empresa
    -- mudou de CD_CVM depois). Serve para saber de onde veio cada pedaço
    -- do histórico e para depurar sobreposições.
    vigente     BOOLEAN DEFAULT TRUE,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (ticker, cd_cvm)
);

CREATE INDEX idx_ticker_cvm_map_cd ON ticker_cvm_map(cd_cvm);
CREATE INDEX idx_ticker_cvm_map_ticker ON ticker_cvm_map(ticker);

-- 3. Restaura (tudo que já estava lá é, por definição, o registro vigente)
INSERT INTO ticker_cvm_map (ticker, cd_cvm, nome_cvm, confianca, vigente, updated_at)
SELECT ticker, cd_cvm, nome_cvm, confianca, TRUE, updated_at FROM _backup_map;

COMMIT;

-- 4. Confirmação
SELECT
    COUNT(*)                          AS total_mapeamentos,
    COUNT(DISTINCT ticker)            AS tickers_distintos,
    COUNT(DISTINCT cd_cvm)            AS codigos_cvm_distintos,
    COUNT(*) FILTER (WHERE vigente)   AS vigentes,
    COUNT(*) FILTER (WHERE NOT vigente) AS historicos
FROM ticker_cvm_map;

-- Depois de carregar registros históricos, esta consulta mostra os tickers
-- que passaram a ter mais de um registro CVM:
--
--   SELECT ticker, COUNT(*) AS n_registros,
--          string_agg(cd_cvm || CASE WHEN vigente THEN ' (vigente)' ELSE ' (histórico)' END, ', ')
--   FROM ticker_cvm_map GROUP BY ticker HAVING COUNT(*) > 1 ORDER BY ticker;
