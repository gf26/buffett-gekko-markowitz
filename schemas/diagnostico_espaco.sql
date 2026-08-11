-- Diagnóstico de uso de espaço no Supabase.
-- Rode no SQL Editor. Só lê, não altera nada.

-- ============================================================
-- 1. TAMANHO POR TABELA (dados vs índices)
-- ============================================================
SELECT
    C.relname                                                 AS tabela,
    pg_size_pretty(pg_total_relation_size(C.oid))             AS total,
    pg_size_pretty(pg_relation_size(C.oid))                   AS dados,
    pg_size_pretty(pg_total_relation_size(C.oid) - pg_relation_size(C.oid)) AS indices,
    S.n_live_tup                                              AS linhas_vivas,
    S.n_dead_tup                                              AS linhas_mortas
FROM pg_class C
LEFT JOIN pg_namespace N ON N.oid = C.relnamespace
LEFT JOIN pg_stat_user_tables S ON S.relid = C.oid
WHERE N.nspname = 'public' AND C.relkind = 'r'
ORDER BY pg_total_relation_size(C.oid) DESC;

-- LEITURA:
--   'linhas_mortas' alto = espaço desperdiçado por UPDATEs. Todo upsert que
--   atualiza uma linha deixa a versão antiga como "tupla morta" até um VACUUM.
--   Como seus scripts rodam ON CONFLICT DO UPDATE todo dia, isso acumula.
--   Se linhas_mortas for uma fração grande de linhas_vivas, VACUUM FULL
--   pode devolver bastante espaço (ver seção 4).


-- ============================================================
-- 2. ÍNDICES QUE NUNCA FORAM USADOS
-- ============================================================
-- Índice não usado ocupa espaço e ainda deixa a escrita mais lenta.
SELECT
    schemaname,
    relname       AS tabela,
    indexrelname  AS indice,
    pg_size_pretty(pg_relation_size(indexrelid)) AS tamanho,
    idx_scan      AS vezes_usado
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY idx_scan ASC, pg_relation_size(indexrelid) DESC;

-- LEITURA: 'vezes_usado' = 0 em índice grande é candidato a DROP.
-- CUIDADO: índices de chave primária/única aparecem aqui mesmo sendo
-- essenciais - não remova esses.


-- ============================================================
-- 3. COLUNAS DE prices_daily QUE TALVEZ NÃO SEJAM USADAS
-- ============================================================
-- Confere se open/high/low têm dado. O sistema hoje usa apenas adj_close
-- (retornos), close e volume (liquidez) - open/high/low são ingeridos mas
-- nunca lidos por nenhum script.
SELECT
    COUNT(*)                                  AS total_linhas,
    COUNT(open)                               AS com_open,
    COUNT(high)                               AS com_high,
    COUNT(low)                                AS com_low,
    COUNT(close)                              AS com_close,
    COUNT(adj_close)                          AS com_adj_close,
    COUNT(volume)                             AS com_volume,
    MIN(date)                                 AS data_mais_antiga,
    MAX(date)                                 AS data_mais_recente
FROM prices_daily;

-- Quanto do histórico de preço é anterior a 2010 (que é o começo do
-- histórico de fundamentos da CVM - antes disso não dá para rodar o
-- screener de qualquer forma):
SELECT
    COUNT(*) FILTER (WHERE date <  '2010-01-01') AS linhas_antes_2010,
    COUNT(*) FILTER (WHERE date >= '2010-01-01') AS linhas_desde_2010,
    ROUND(100.0 * COUNT(*) FILTER (WHERE date < '2010-01-01') / COUNT(*), 1) AS pct_antes_2010
FROM prices_daily;


-- ============================================================
-- 4. RECUPERAR ESPAÇO (rode só depois de olhar o diagnóstico acima)
-- ============================================================
-- VACUUM FULL reescreve a tabela sem as tuplas mortas. Devolve espaço ao
-- sistema operacional (VACUUM normal só marca como reutilizável).
--
-- ATENÇÃO: bloqueia a tabela enquanto roda e precisa de espaço temporário.
-- Faça uma tabela por vez, fora do horário dos workflows agendados.
--
--   VACUUM FULL ANALYZE prices_daily;
--   VACUUM FULL ANALYZE financials;
--   VACUUM FULL ANALYZE fundamental_ratios;
--
-- Depois, rode a consulta 1 de novo para ver quanto voltou.