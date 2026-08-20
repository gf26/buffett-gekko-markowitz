"""
Validação da série ajustada contra a brapi.

A PERGUNTA QUE ESTE SCRIPT RESPONDE
-----------------------------------
A série que construímos (COTAHIST bruto + ajuste próprio por proventos,
splits e cisões) é confiável o suficiente para substituir o Yahoo?

Isso importa porque o Yahoo cobre 351 tickers e a nossa série cobre 925 - os
~600 a mais são empresas que saíram da bolsa, e são exatamente elas que
causam o viés de sobrevivência no backtest. Mas só faz sentido usá-las se o
método de ajuste estiver correto.

A brapi é o árbitro natural: tem preços ajustados desde 2000 e faz o próprio
ajuste, de forma independente da nossa.

O QUE SE COMPARA
----------------
RETORNOS, não preços. Duas séries ajustadas corretamente podem ter níveis
diferentes (o ajuste retroativo depende de onde a série começa), mas os
retornos diários têm que coincidir - é isso que entra em qualquer cálculo de
fator ou backtest.

COMO LER O RESULTADO
--------------------
1. CORRELAÇÃO ALTA E ERRO BAIXO -> método validado. Aplicar aos deslistados.

2. DIVERGÊNCIA EM POUCAS DATAS ISOLADAS -> evento faltando ou com fator
   errado. O script lista as datas, e elas apontam onde procurar.

3. DIVERGÊNCIA SISTEMÁTICA DE UM PREGÃO -> convenção de data-ex diferente. A
   brapi usa `lastDatePrior` (última data COM direito); se ela ajustar a
   partir dela em vez de depois dela, aparece um deslocamento constante.

4. DIVERGÊNCIA ERRÁTICA -> problema mais profundo, e o padrão dos piores
   casos indica se é por ticker, por período ou por tipo de evento.

Uso:
    BRAPI_TOKEN="..." DATABASE_URL="..." python validar_ajuste.py
    BRAPI_TOKEN="..." DATABASE_URL="..." python validar_ajuste.py --tickers PETR4 VALE3
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import requests
from sqlalchemy import create_engine, text

URL = "https://brapi.dev/api/quote/{ticker}"

# tickers líquidos, de setores diferentes, com histórico longo e eventos
# societários variados - são os que dão o teste mais informativo
PADRAO = ["PETR4", "VALE3", "ITUB4", "BBDC4", "ABEV3", "WEGE3",
          "MGLU3", "BBAS3", "SUZB3", "ITSA4", "RADL3", "EGIE3"]

# tolerância para considerar dois retornos diários iguais. 0,1% acomoda
# arredondamento de centavos em papéis baratos sem esconder erro real.
TOL = 0.001


def serie_brapi(ticker, token):
    try:
        r = requests.get(URL.format(ticker=ticker),
                          params={"token": token, "range": "max", "interval": "1d"},
                          timeout=60)
    except requests.exceptions.RequestException as e:
        return None, f"rede: {type(e).__name__}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    res = (r.json().get("results") or [{}])[0]
    h = res.get("historicalDataPrice") or []
    if not h:
        return None, "sem histórico"
    d = pd.DataFrame(h)
    d["date"] = pd.to_datetime(d["date"], unit="s").dt.normalize()
    col = "adjustedClose" if "adjustedClose" in d.columns else "close"
    d["preco"] = pd.to_numeric(d[col], errors="coerce")
    s = d.dropna(subset=["preco"]).set_index("date")["preco"].sort_index()
    return s[~s.index.duplicated(keep="last")], None


def serie_nossa(engine, ticker):
    with engine.connect() as conn:
        d = pd.read_sql(text("""
            SELECT date, adj_close FROM prices_ajustados
            WHERE ticker = :tk AND adj_close IS NOT NULL AND adj_close > 0
            ORDER BY date
        """), conn, params={"tk": ticker})
    if d.empty:
        return None
    d["date"] = pd.to_datetime(d["date"])
    return d.set_index("date")["adj_close"]


def comparar(nossa, deles):
    """Compara RETORNOS diários nas datas em comum."""
    comum = nossa.index.intersection(deles.index)
    if len(comum) < 60:
        return None
    rn = nossa.loc[comum].pct_change().dropna()
    rd = deles.loc[comum].pct_change().dropna()
    idx = rn.index.intersection(rd.index)
    rn, rd = rn.loc[idx], rd.loc[idx]
    dif = (rn - rd).abs()
    return pd.DataFrame({"nosso": rn, "brapi": rd, "dif": dif})


def analisar(tk, c):
    n = len(c)
    iguais = (c["dif"] <= TOL).mean() * 100
    corr = c["nosso"].corr(c["brapi"])
    piores = c.nlargest(3, "dif")
    return {
        "ticker": tk, "pregoes": n,
        "iguais_pct": round(iguais, 1),
        "correlacao": round(float(corr), 5) if pd.notna(corr) else np.nan,
        "dif_mediana": round(float(c["dif"].median()), 6),
        "dif_max": round(float(c["dif"].max()), 4),
        "piores_datas": ", ".join(str(d.date()) for d in piores.index),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--tickers", nargs="*", default=PADRAO)
    p.add_argument("--pausa", type=float, default=0.4)
    args = p.parse_args()
    if not args.token:
        sys.exit("Token não informado.")

    engine = create_engine(os.environ["DATABASE_URL"])
    print(f"Comparando {len(args.tickers)} tickers "
          f"({len(args.tickers)} requisições de 500.000/mês)\n")

    linhas, detalhes = [], {}
    for tk in args.tickers:
        nossa = serie_nossa(engine, tk)
        if nossa is None:
            print(f"  {tk:<8} não está em prices_ajustados")
            continue
        deles, err = serie_brapi(tk, args.token)
        if err:
            print(f"  {tk:<8} brapi: {err}")
            time.sleep(args.pausa)
            continue
        c = comparar(nossa, deles)
        if c is None:
            print(f"  {tk:<8} menos de 60 pregões em comum")
            time.sleep(args.pausa)
            continue
        r = analisar(tk, c)
        detalhes[tk] = c
        linhas.append(r)
        print(f"  {tk:<8} {r['pregoes']:>5} pregões, {r['iguais_pct']:>5.1f}% iguais, "
              f"corr {r['correlacao']:.5f}")
        time.sleep(args.pausa)

    if not linhas:
        sys.exit("\nNenhuma comparação possível.")

    df = pd.DataFrame(linhas)
    print("\n" + "=" * 78)
    print("RESULTADO")
    print("=" * 78)
    print(df[["ticker", "pregoes", "iguais_pct", "correlacao", "dif_mediana", "dif_max"]]
          .to_string(index=False))

    med_ig = df["iguais_pct"].median()
    med_corr = df["correlacao"].median()
    print(f"\nMediana: {med_ig:.1f}% dos retornos iguais (±{TOL*100:.1f}%), "
          f"correlação {med_corr:.5f}")

    print("\nDatas com maior divergência, por ticker:")
    for r in linhas:
        print(f"  {r['ticker']:<8} máx {r['dif_max']:.4f} em {r['piores_datas']}")

    # datas que divergem em VÁRIOS tickers ao mesmo tempo sugerem problema
    # sistêmico (feriado, convenção de data), não evento pontual
    todas = []
    for tk, c in detalhes.items():
        ruins = c[c["dif"] > 0.01]
        todas.extend((d.date(), tk) for d in ruins.index)
    if todas:
        cnt = pd.Series([d for d, _ in todas]).value_counts()
        multi = cnt[cnt >= 3]
        if not multi.empty:
            print(f"\n⚠️ {len(multi)} datas divergem em 3+ tickers simultaneamente:")
            for d, n in multi.head(8).items():
                print(f"    {d}: {n} tickers")
            print("  Isso sugere causa sistêmica (calendário, convenção de data),")
            print("  não evento societário específico.")

    print("\n" + "=" * 78)
    if med_ig >= 95 and med_corr >= 0.999:
        print("VEREDITO: método validado.")
        print("A série ajustada reproduz a da brapi nos tickers líquidos. Isso")
        print("autoriza usá-la nos ~600 deslistados que só o COTAHIST tem - e")
        print("aí o viés de sobrevivência fica resolvido.")
    elif med_corr >= 0.99:
        print("VEREDITO: quase lá.")
        print("A correlação é alta, mas há divergências. Confira as datas acima:")
        print("se forem poucas e isoladas, são eventos faltando; se forem muitas")
        print("e espalhadas, há problema de convenção.")
    else:
        print("VEREDITO: NÃO usar ainda.")
        print("A divergência é grande demais para atribuir a eventos pontuais.")
        print("O padrão dos piores casos indica onde investigar.")
    print("=" * 78)


if __name__ == "__main__":
    main()
