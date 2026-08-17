"""
Ingestor do COTAHIST - carrega as cotações históricas da B3.

TABELA SEPARADA, DE PROPÓSITO
-----------------------------
Grava em `prices_cotahist`, não em `prices_daily`. A ideia é comparar as duas
fontes antes de trocar: o Yahoo tem erros conhecidos (AZEV3 cotada a R$ 0,0001
por 740 pregões) e remove tickers deslistados, mas trocar às cegas seria
substituir um conjunto de problemas conhecidos por outro desconhecido.

Depois da comparação, os dados migram para `prices_daily` com uma coluna
`source` distinguindo a origem.

O QUE É FILTRADO
----------------
Só mercado à vista em lote padrão (TPMERC=010, CODBDI=02). Sem isso o arquivo
de 2015 traz 26.286 "tickers" - opções, fundos, termo e fracionário. Com o
filtro, 555, que são as ações de fato.

BDRs (espécie DRN, DR1, DR2, DR3) são descartados: são ações estrangeiras
negociadas aqui, fora do escopo de um screener da B3.

PREÇOS NÃO SÃO AJUSTADOS
------------------------
O COTAHIST traz o preço NEGOCIADO na data, sem ajuste por proventos ou
desdobramentos. A série fica com degraus em cada evento. O ajuste é trabalho
do próximo módulo, usando as tabelas `dividends` e `splits` - e a vantagem
dessa separação é que o ajuste fica auditável, em vez de ser uma caixa-preta
que às vezes devolve R$ 0,0001.

Uso:
    DATABASE_URL="..." python cotahist_ingestor.py --de 2010 --ate 2025 --dry-run
    DATABASE_URL="..." python cotahist_ingestor.py --de 2010 --ate 2025
"""
import argparse
import os

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

from cotahist_parser import ler_cotahist

PASTA = "dados_cvm"

# BDR = ação estrangeira negociada no Brasil. Fora do escopo.
ESPECIES_BDR = ("DRN", "DR1", "DR2", "DR3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices_cotahist (
    ticker        TEXT NOT NULL,
    date          DATE NOT NULL,
    open          NUMERIC,
    high          NUMERIC,
    low           NUMERIC,
    close         NUMERIC,
    media         NUMERIC,
    volume        NUMERIC,
    negocios      INTEGER,
    quantidade    BIGINT,
    nome_pregao   TEXT,
    especie       TEXT,
    isin          TEXT,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_cotahist_date ON prices_cotahist(date);
CREATE INDEX IF NOT EXISTS idx_prices_cotahist_isin ON prices_cotahist(isin);
"""


def caminho_ano(ano):
    for nome in (f"COTAHIST_A{ano}.ZIP", f"COTAHIST_A{ano}.zip", f"COTAHIST_A{ano}.TXT"):
        p = os.path.join(PASTA, nome)
        if os.path.exists(p):
            return p
    return None


def processar_ano(ano, sem_bdr=True):
    p = caminho_ano(ano)
    if not p:
        print(f"  {ano}: arquivo não encontrado em {PASTA}/")
        return pd.DataFrame()

    df = ler_cotahist(p)
    if df.empty:
        return df

    antes_tk = df["ticker"].nunique()
    if sem_bdr:
        eh_bdr = df["especie"].str.strip().str.upper().str.startswith(ESPECIES_BDR)
        df = df[~eh_bdr]

    print(f"  {ano}: {len(df):>7,} registros, {df['ticker'].nunique():>4} tickers"
          f"  ({antes_tk - df['ticker'].nunique()} BDRs descartados)")
    return df


def gravar(engine, df, ano):
    if df.empty:
        return 0
    cols = ["ticker", "date", "open", "high", "low", "close", "media",
            "volume", "negocios", "quantidade", "nome_pregao", "especie", "isin"]
    d = df[cols].copy()
    d["date"] = pd.to_datetime(d["date"]).dt.date
    # numpy -> tipos nativos: o psycopg2 não sabe gravar np.float64/np.int64
    linhas = [tuple(None if pd.isna(v) else (float(v) if isinstance(v, float)
                    else int(v) if hasattr(v, "item") and isinstance(v.item(), int)
                    else v)
                    for v in row)
              for row in d.itertuples(index=False, name=None)]

    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prices_cotahist WHERE EXTRACT(YEAR FROM date) = %s", (ano,))
            if cur.rowcount:
                print(f"      (removidas {cur.rowcount:,} linhas anteriores de {ano})")
            execute_values(cur, f"""
                INSERT INTO prices_cotahist ({','.join(cols)})
                VALUES %s
                ON CONFLICT (ticker, date) DO UPDATE SET
                    close = EXCLUDED.close, volume = EXCLUDED.volume,
                    nome_pregao = EXCLUDED.nome_pregao, isin = EXCLUDED.isin
            """, linhas, page_size=5000)
        conn.commit()
    finally:
        conn.close()
    return len(linhas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2025)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--com-bdr", action="store_true", help="Mantém BDRs (padrão: descarta)")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    if not args.dry_run:
        with engine.begin() as conn:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    conn.execute(text(stmt))
        print("Tabela prices_cotahist pronta.\n")

    total, resumo = 0, []
    for ano in range(args.de, args.ate + 1):
        df = processar_ano(ano, sem_bdr=not args.com_bdr)
        if df.empty:
            continue
        resumo.append({"ano": ano, "registros": len(df),
                        "tickers": df["ticker"].nunique(),
                        "pregoes": df["date"].nunique()})
        if not args.dry_run:
            gravar(engine, df, ano)
        total += len(df)

    print(f"\nTotal: {total:,} registros" + (" (dry-run)" if args.dry_run else " gravados"))

    if resumo:
        r = pd.DataFrame(resumo)
        print("\nCobertura por ano:")
        print(r.to_string(index=False))

    if args.dry_run or not resumo:
        return

    # comparação com o que já existe, que é o motivo de a tabela ser separada
    with engine.connect() as conn:
        comp = pd.read_sql(text("""
            SELECT
              (SELECT COUNT(DISTINCT ticker) FROM prices_cotahist)         AS tickers_cotahist,
              (SELECT COUNT(DISTINCT ticker) FROM prices_daily)            AS tickers_yahoo,
              (SELECT COUNT(*) FROM prices_cotahist)                       AS linhas_cotahist,
              (SELECT COUNT(*) FROM prices_daily)                          AS linhas_yahoo
        """), conn)
        print("\n" + comp.to_string(index=False))

        so_cotahist = pd.read_sql(text("""
            SELECT c.ticker, MIN(c.date) AS de, MAX(c.date) AS ate, COUNT(*) AS pregoes
            FROM prices_cotahist c
            WHERE NOT EXISTS (
                SELECT 1 FROM prices_daily p
                WHERE REPLACE(p.ticker, '.SA', '') = c.ticker)
            GROUP BY c.ticker ORDER BY COUNT(*) DESC LIMIT 20
        """), conn)
    if not so_cotahist.empty:
        print(f"\nTickers que EXISTEM no COTAHIST e não na base atual "
              f"(amostra dos 20 com mais pregões):")
        print(so_cotahist.to_string(index=False))
        print("\nEstes são o viés de sobrevivência ficando visível: papéis que")
        print("negociaram de verdade e sumiram da fonte atual.")


if __name__ == "__main__":
    main()
