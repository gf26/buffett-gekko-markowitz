"""
Ingestor de proventos e splits da brapi.

POR QUE SUBSTITUI AS TABELAS ATUAIS
-----------------------------------
As tabelas `dividends` e `splits` vieram do Yahoo e têm dois problemas
comprovados:

1. VALORES TRUNCADOS. O Yahoo reporta 0,0100 - exatamente esse valor - para
   BBDC3, BBDC4, ITSA3, ITSA4, POMO3, LREN3 e EUCA3. Valor idêntico em
   empresas diferentes é truncamento em dois decimais, não coincidência. A
   brapi devolve `rate` com 8 casas.

2. COBERTURA. 308 tickers em `dividends` e 244 em `splits`, contra 1.080 no
   COTAHIST.

Verificado no teste de aceitação: Itaú com 483 proventos desde 1995 e 12
splits; Petrobras com 168 desde 1996. Bem além dos "10+ anos" anunciados.

O CAMPO QUE MUDA TUDO
---------------------
A resposta traz `lastDatePrior` - a DATA-EX, última data com direito ao
provento. É ela que afeta o preço e que serve para ajustar a série.

Isso torna desnecessário o módulo de casamento entre os marcadores ED/EJ do
COTAHIST (que dão a data) e os montantes do FRE (que dão o valor). Onde a
brapi cobre, data-ex e valor vêm na mesma linha.

O QUE ELA NÃO RESOLVE
---------------------
Empresas que FECHARAM CAPITAL de vez (APER3, HGTX3, CIEL3, ENBR3) dão 404. As
que passaram por fusão ou mudança de classe (VALE5, CCRO3, BIDI11, SULA11)
estão lá. Para as primeiras, o FRE continua sendo a fonte de proventos - por
isso `cvm_proventos.py` não é descartado.

Uso:
    BRAPI_TOKEN="..." DATABASE_URL="..." python brapi_proventos.py --dry-run
    BRAPI_TOKEN="..." DATABASE_URL="..." python brapi_proventos.py
    BRAPI_TOKEN="..." DATABASE_URL="..." python brapi_proventos.py --tickers PETR4 VALE3
"""
import argparse
import os
import sys
import time

import pandas as pd
import requests
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

URL = "https://brapi.dev/api/quote/{ticker}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proventos_brapi (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    ex_date         DATE NOT NULL,
    tipo            TEXT NOT NULL,
    valor           NUMERIC,
    fator           NUMERIC,
    data_pagamento  DATE,
    data_aprovacao  DATE,
    isin            TEXT
);
-- Chave natural via índice único, não via PRIMARY KEY: eventos
-- proporcionais (GRUPAMENTO, DESDOBRAMENTO, BONIFICACAO) têm `valor` NULO
-- por definição - eles alteram a quantidade de ações, não distribuem
-- dinheiro. Coluna de PRIMARY KEY não aceita nulo no Postgres, o que fazia a
-- carga falhar em NotNullViolation.
--
-- COALESCE resolve: nulo vira -1, que nunca colide com valor real (sempre
-- positivo) nem com fator.
CREATE UNIQUE INDEX IF NOT EXISTS uq_proventos_brapi
    ON proventos_brapi (ticker, ex_date, tipo,
                        COALESCE(valor, -1), COALESCE(fator, -1));
CREATE INDEX IF NOT EXISTS idx_proventos_brapi_ticker ON proventos_brapi(ticker);
CREATE INDEX IF NOT EXISTS idx_proventos_brapi_ex ON proventos_brapi(ex_date);
"""


def _data(v):
    if not v:
        return None
    d = pd.to_datetime(str(v)[:10], errors="coerce")
    return None if pd.isna(d) else d.date()


def buscar(ticker, token, timeout=40):
    """Proventos e splits de um ticker.

    `dividends=true` é obrigatório: sem ele a resposta vem sem dividendsData,
    e PETR4 aparece com zero proventos - o que parece limitação da fonte e é
    parâmetro faltando."""
    try:
        r = requests.get(URL.format(ticker=ticker),
                          params={"token": token, "dividends": "true", "range": "max"},
                          timeout=timeout)
    except requests.exceptions.RequestException as e:
        return None, f"rede: {type(e).__name__}"
    if r.status_code == 404:
        return None, "404"
    if r.status_code != 200:
        try:
            msg = r.json().get("message", r.text[:70])
        except ValueError:
            msg = r.text[:70]
        return None, f"HTTP {r.status_code}: {msg}"
    try:
        res = r.json().get("results") or []
    except ValueError:
        return None, "resposta inválida"
    return (res[0] if res else None), None


def extrair(ticker, dados):
    dd = (dados or {}).get("dividendsData") or {}
    linhas = []

    for c in dd.get("cashDividends") or []:
        # a data-ex é o que importa para ajuste de preço; o pagamento vem
        # semanas depois e não afeta a cotação
        ex = _data(c.get("lastDatePrior")) or _data(c.get("paymentDate"))
        if not ex:
            continue
        try:
            valor = float(c.get("rate") or 0)
        except (TypeError, ValueError):
            continue
        if valor <= 0:
            continue
        linhas.append((ticker, ex, str(c.get("label") or "DIVIDENDO")[:40], valor,
                        None, _data(c.get("paymentDate")), _data(c.get("approvedOn")),
                        str(c.get("isinCode") or "")[:20] or None))

    for s in dd.get("stockDividends") or []:
        ex = _data(s.get("lastDatePrior")) or _data(s.get("approvedOn"))
        if not ex:
            continue
        # o campo do fator varia; tenta os nomes conhecidos
        fator = None
        for k in ("factor", "ratio", "rate"):
            v = s.get(k)
            if v not in (None, ""):
                try:
                    fator = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        linhas.append((ticker, ex, str(s.get("label") or "SPLIT")[:40], None,
                        fator, _data(s.get("paymentDate")), _data(s.get("approvedOn")),
                        str(s.get("isinCode") or "")[:20] or None))

    return linhas


def tickers_do_banco(engine, todos):
    """Tickers a buscar. Por padrão os ativos; com --todos, também o histórico
    do COTAHIST (que tem 1.080, incluindo deslistados)."""
    with engine.connect() as conn:
        if todos:
            rows = conn.execute(text("""
                SELECT DISTINCT ticker FROM prices_cotahist
                UNION
                SELECT REPLACE(ticker, '.SA', '') FROM tickers WHERE active
            """)).fetchall()
        else:
            rows = conn.execute(text(
                "SELECT REPLACE(ticker, '.SA', '') FROM tickers WHERE active")).fetchall()
    return sorted({r[0] for r in rows if r[0] and r[0] != "^BVSP"})


def gravar(engine, linhas):
    with engine.begin() as conn:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    # chave inclui `valor`; duas distribuições distintas na mesma data-ex com
    # o mesmo valor seriam indistinguíveis - descartar a repetida é correto
    vistos, unicas = set(), []
    for l in linhas:
        # inclui o fator na chave: dois eventos proporcionais na mesma data
        # com fatores diferentes são eventos distintos
        k = (l[0], l[1], l[2], l[3], l[4])
        if k not in vistos:
            vistos.add(k)
            unicas.append(l)
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM proventos_brapi")
            execute_values(cur, """
                INSERT INTO proventos_brapi
                    (ticker, ex_date, tipo, valor, fator, data_pagamento, data_aprovacao, isin)
                VALUES %s
                ON CONFLICT (ticker, ex_date, tipo, COALESCE(valor, -1), COALESCE(fator, -1))
                DO NOTHING
            """, unicas, page_size=2000)
        conn.commit()
    finally:
        conn.close()
    return len(unicas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--todos", action="store_true",
                    help="Inclui os 1.080 tickers do COTAHIST, não só os ativos.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--pausa", type=float, default=0.25)
    args = p.parse_args()

    if not args.token:
        sys.exit("Token não informado. Use --token ou defina BRAPI_TOKEN.")
    engine = create_engine(os.environ["DATABASE_URL"])

    alvos = ([t.upper().replace(".SA", "") for t in args.tickers]
             if args.tickers else tickers_do_banco(engine, args.todos))
    print(f"{len(alvos)} tickers a consultar (~{len(alvos)} requisições de 500.000/mês)\n")

    linhas, falhas, sem_dados = [], {}, []
    for i, tk in enumerate(alvos, 1):
        d, err = buscar(tk, args.token)
        if err:
            falhas[err.split(":")[0]] = falhas.get(err.split(":")[0], 0) + 1
        else:
            novas = extrair(tk, d)
            if novas:
                linhas.extend(novas)
            else:
                sem_dados.append(tk)
        if i % 50 == 0:
            print(f"  {i}/{len(alvos)}... {len(linhas)} registros até aqui")
        time.sleep(args.pausa)

    if not linhas:
        sys.exit("Nenhum provento obtido.")

    df = pd.DataFrame(linhas, columns=["ticker", "ex_date", "tipo", "valor", "fator",
                                        "data_pagamento", "data_aprovacao", "isin"])
    print(f"\n{len(df)} registros, {df['ticker'].nunique()} tickers")
    print(f"  {int(df['valor'].notna().sum())} proventos em dinheiro")
    print(f"  {int(df['fator'].notna().sum())} eventos proporcionais (split/bonificação)")
    print(f"  período: {df['ex_date'].min()} a {df['ex_date'].max()}")
    if falhas:
        print(f"\nFalhas: {falhas}")
    if sem_dados:
        print(f"{len(sem_dados)} tickers sem proventos: {', '.join(sem_dados[:10])}"
              + (f" e mais {len(sem_dados)-10}" if len(sem_dados) > 10 else ""))

    print("\nPor tipo:")
    print(df["tipo"].value_counts().head(8).to_string())

    # comparação com o Yahoo, que é o que estas tabelas substituem
    with engine.connect() as conn:
        yah = pd.read_sql(text("""
            SELECT COUNT(*) AS linhas, COUNT(DISTINCT ticker) AS tickers FROM dividends
        """), conn)
    print(f"\nYahoo (tabela `dividends`): {int(yah['linhas'][0])} linhas, "
          f"{int(yah['tickers'][0])} tickers")
    print(f"brapi:                      {len(df[df['valor'].notna()])} linhas, "
          f"{df[df['valor'].notna()]['ticker'].nunique()} tickers")

    if args.dry_run:
        print("\n(dry-run - nada gravado)")
        return
    n = gravar(engine, linhas)
    print(f"\n{n} registros gravados em proventos_brapi.")
    print("\nPróximo passo: ajustar os preços do COTAHIST com estes proventos e")
    print("splits, e validar a série ajustada contra a da própria brapi.")


if __name__ == "__main__":
    main()