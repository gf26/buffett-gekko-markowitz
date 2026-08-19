"""
Ajuste de preços do COTAHIST por proventos e eventos societários.

POR QUE PRECISA EXISTIR
-----------------------
O COTAHIST traz o preço NEGOCIADO em cada pregão, sem ajuste. A série fica com
degraus: uma ação que pagou dividendo aparece caindo, e uma que desdobrou
aparece despencando. Sem ajuste, qualquer cálculo de retorno está errado.

O Yahoo já entrega ajustado, mas com dois problemas comprovados: remove
tickers deslistados (causa do viés de sobrevivência) e às vezes quebra - a
AZEV3 aparece cotada a R$ 0,0001 por 740 pregões. Fazer o ajuste aqui torna-o
auditável: erros viram bugs investigáveis em vez de mistérios da fonte.

OS TRÊS TIPOS DE EVENTO
-----------------------
1. DINHEIRO (DIVIDENDO, JCP, RENDIMENTO, REST CAP DIN)
   O preço cai pelo valor distribuído. Fator = 1 - valor/preço_anterior.
   REST CAP DIN entra aqui: devolve capital em dinheiro, e o efeito no preço
   é idêntico ao de um dividendo. A diferença é tributária, não de preço.

2. PROPORCIONAL (DESDOBRAMENTO, GRUPAMENTO, BONIFICACAO)
   Vem com `fator` = razão nova/antiga, verificado contra casos conhecidos:
   MGLU3 1:8 em 2019 -> 8.0; WEGE3 1:2 em 2021 -> 2.0; grupamento do AERI3
   1:20 -> 0.05. O preço é dividido pelo fator.

3. CISÃO (CIS RED CAP) - o caso problemático
   A fonte grava `fator = 100.0` em 24 dos 27 registros: Bradesco, Itaú,
   Cyrela, MRV, Pão de Açúcar, Telebras... Empresas diferentes com o mesmo
   valor exato é preenchimento padrão, não razão real. Aplicá-lo
   multiplicaria o preço do Itaú por 100.

   Verificado no COTAHIST: na cisão do Itaú (2021-10-01), o preço foi de
   R$ 29,67 para R$ 24,34 - queda de 18%, que é o peso do XP Inc., não 1:100.

   SOLUÇÃO: usar a fonte só para saber QUANDO houve cisão, e medir o fator no
   próprio salto de preço. O dado tem a resposta.

   Limitação: o salto mistura o efeito da cisão com o movimento normal do
   mercado naquele dia. Para uma cisão de 18%, o ruído de 1-2% é aceitável.
   Proteção: só ajusta se o salto estiver em faixa plausível; fora disso,
   sinaliza para revisão em vez de aplicar um número duvidoso.

MÉTODO
------
Ajuste retroativo, como é padrão: o preço de HOJE fica intacto e os
anteriores são multiplicados pelo fator acumulado. Assim a série mais recente
é comparável com o que se vê no mercado.

Uso:
    DATABASE_URL="..." python ajustar_precos.py --dry-run
    DATABASE_URL="..." python ajustar_precos.py --tickers ITUB4 MGLU3 PETR4
    DATABASE_URL="..." python ajustar_precos.py
"""
import argparse
import os

import numpy as np
import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

TIPOS_DINHEIRO = {"DIVIDENDO", "JCP", "RENDIMENTO", "REST CAP DIN"}
TIPOS_PROPORCIONAIS = {"DESDOBRAMENTO", "GRUPAMENTO", "BONIFICACAO"}
TIPO_CISAO = "CIS RED CAP"

# faixa em que um salto é plausivelmente uma cisão. Fora dela, o evento é
# sinalizado em vez de ajustado - um salto de 60% pode ser cisão grande ou
# outra coisa acontecendo no mesmo pregão.
CISAO_MIN, CISAO_MAX = 0.30, 0.95

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices_ajustados (
    ticker      TEXT NOT NULL,
    date        DATE NOT NULL,
    close       NUMERIC,
    adj_close   NUMERIC,
    fator_ajuste NUMERIC,
    volume      NUMERIC,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_ajustados_date ON prices_ajustados(date);
"""


def carregar(engine, tickers=None):
    cond, params = ["close IS NOT NULL", "close > 0"], {}
    if tickers:
        cond.append("ticker = ANY(:tk)")
        params["tk"] = tickers
    with engine.connect() as conn:
        px = pd.read_sql(text(f"""
            SELECT ticker, date, close, volume FROM prices_cotahist
            WHERE {' AND '.join(cond)} ORDER BY ticker, date
        """), conn, params=params)
        ev = pd.read_sql(text("""
            SELECT ticker, ex_date, tipo, valor, fator FROM proventos_brapi
            WHERE ex_date <= CURRENT_DATE
        """), conn)
    px["date"] = pd.to_datetime(px["date"])
    ev["ex_date"] = pd.to_datetime(ev["ex_date"])
    return px, ev


def fator_cisao(serie, data_ex):
    """Mede o fator da cisão pelo salto de preço.

    Devolve (fator, motivo). Fator None significa que o salto não é
    plausível como cisão e o evento deve ser sinalizado, não ajustado."""
    antes = serie[serie.index < data_ex]
    depois = serie[serie.index >= data_ex]
    if antes.empty or depois.empty:
        return None, "sem preço antes ou depois"
    p_antes, p_depois = float(antes.iloc[-1]), float(depois.iloc[0])
    if p_antes <= 0:
        return None, "preço anterior inválido"
    r = p_depois / p_antes
    if not (CISAO_MIN <= r <= CISAO_MAX):
        return None, f"salto de {r:.3f} fora da faixa plausível"
    return r, f"medido no salto ({p_antes:.2f} -> {p_depois:.2f})"


def ajustar_ticker(px_t, ev_t):
    """Série ajustada de um ticker. Devolve (df, avisos)."""
    px_t = px_t.sort_values("date").set_index("date")
    close = px_t["close"]
    fator = pd.Series(1.0, index=close.index)
    avisos = []

    for _, e in ev_t.sort_values("ex_date").iterrows():
        d, tipo = e["ex_date"], str(e["tipo"]).upper()
        anteriores = fator.index < d
        if not anteriores.any():
            continue

        if tipo in TIPOS_DINHEIRO:
            v = e["valor"]
            if pd.isna(v) or v <= 0:
                continue
            ant = close[close.index < d]
            if ant.empty:
                continue
            p = float(ant.iloc[-1])
            if p <= 0 or v >= p:
                avisos.append(f"{d.date()} {tipo}: provento {v:.4f} >= preço {p:.2f} - ignorado")
                continue
            fator.loc[anteriores] *= (1 - v / p)

        elif tipo in TIPOS_PROPORCIONAIS:
            f = e["fator"]
            if pd.isna(f) or f <= 0:
                continue
            fator.loc[anteriores] /= f

        elif tipo == TIPO_CISAO:
            f, motivo = fator_cisao(close, d)
            if f is None:
                avisos.append(f"{d.date()} CISÃO: {motivo} - NÃO ajustada")
                continue
            fator.loc[anteriores] *= f

    out = pd.DataFrame({
        "close": close,
        "adj_close": close * fator,
        "fator_ajuste": fator,
        "volume": px_t["volume"],
    })
    return out.reset_index(), avisos


def gravar(engine, df):
    with engine.begin() as conn:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.date
    linhas = [(r.ticker, r.date,
                None if pd.isna(r.close) else float(r.close),
                None if pd.isna(r.adj_close) else float(r.adj_close),
                None if pd.isna(r.fator_ajuste) else float(r.fator_ajuste),
                None if pd.isna(r.volume) else float(r.volume))
              for r in d.itertuples(index=False)]
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prices_ajustados")
            execute_values(cur, """
                INSERT INTO prices_ajustados (ticker, date, close, adj_close, fator_ajuste, volume)
                VALUES %s
                ON CONFLICT (ticker, date) DO UPDATE SET
                    adj_close = EXCLUDED.adj_close, fator_ajuste = EXCLUDED.fator_ajuste
            """, linhas, page_size=5000)
        conn.commit()
    finally:
        conn.close()
    return len(linhas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    alvos = [t.upper().replace(".SA", "") for t in args.tickers] if args.tickers else None
    print("Carregando preços e eventos...")
    px, ev = carregar(engine, alvos)
    print(f"  {len(px):,} preços, {px['ticker'].nunique()} tickers")
    print(f"  {len(ev):,} eventos, {ev['ticker'].nunique()} tickers\n")

    ev_por_ticker = dict(tuple(ev.groupby("ticker")))
    partes, todos_avisos, sem_eventos = [], [], 0

    for tk, g in px.groupby("ticker"):
        e = ev_por_ticker.get(tk)
        if e is None or e.empty:
            sem_eventos += 1
            g = g.copy()
            g["adj_close"] = g["close"]
            g["fator_ajuste"] = 1.0
            partes.append(g[["ticker", "date", "close", "adj_close", "fator_ajuste", "volume"]])
            continue
        out, avisos = ajustar_ticker(g, e)
        out.insert(0, "ticker", tk)
        partes.append(out)
        todos_avisos.extend(f"{tk} {a}" for a in avisos)

    df = pd.concat(partes, ignore_index=True)
    print(f"{len(df):,} preços ajustados")
    print(f"  {sem_eventos} tickers sem eventos (adj_close = close)")

    # quanto o ajuste mexeu
    mexeu = df[df["fator_ajuste"] < 0.999]
    print(f"  {mexeu['ticker'].nunique()} tickers tiveram algum ajuste")
    if not mexeu.empty:
        f = df.groupby("ticker")["fator_ajuste"].min().sort_values()
        print("\n  Maiores ajustes acumulados (fator mínimo por ticker):")
        for tk, v in f.head(8).items():
            print(f"    {tk:<8} {v:.6f}  (preço antigo x {v:.4f})")

    if todos_avisos:
        print(f"\n{len(todos_avisos)} avisos:")
        for a in todos_avisos[:20]:
            print(f"  {a}")
        if len(todos_avisos) > 20:
            print(f"  ... e mais {len(todos_avisos) - 20}")

    if args.dry_run:
        print("\n(dry-run - nada gravado)")
        return
    n = gravar(engine, df)
    print(f"\n{n:,} linhas gravadas em prices_ajustados.")
    print("\nPróximo passo: validar a série ajustada contra a da brapi em tickers")
    print("líquidos. Se bater, o método está provado e pode ser aplicado aos")
    print("deslistados que só o COTAHIST tem.")


if __name__ == "__main__":
    main()
