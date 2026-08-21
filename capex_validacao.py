"""
Nosso CapEx e investimento total contra a brapi.

O QUE SE MEDE
-------------
A brapi não expõe CapEx diretamente. Mas expõe `freeCashFlow` e o fluxo
operacional, e a diferença entre eles é o que ela subtraiu:

    subtraido = FCO_brapi - FCF_brapi

A pergunta é o que esse valor representa. Duas hipóteses:

H1. É o CAPEX (aquisição de imobilizado e intangível). Nesse caso deve bater
    com o nosso `Capital Expenditure`, extraído das subcontas de 6.02 por
    descrição - hoje com 97% de cobertura.

H2. É o INVESTIMENTO TOTAL (conta 6.02 inteira), que inclui aplicações
    financeiras, aquisição de participações e dividendos recebidos. A
    engenharia reversa anterior sugeriu isso: `FCO + inv` bateu com o FCF dela
    em 100% dos casos.

Se H2 se confirmar, os dois conceitos NÃO são intercambiáveis, e o CapEx da
brapi seria mais amplo que o nosso - o que importa saber antes de comparar
qualquer indicador derivado.

⚠️ CONFERIR O PERÍODO PRIMEIRO. Uma investigação anterior queimou duas rodadas
comparando o DFP de 2024 com o exercício de 2025 da brapi. O script checa o
alinhamento ANTES de qualquer conclusão, e descarta os pares desalinhados.

Uso:
    BRAPI_TOKEN="..." DATABASE_URL="..." python capex_validacao.py --ano 2025 --n 80
"""
import argparse
import os
import re
import sys
import time

import numpy as np
import pandas as pd
import requests
from sqlalchemy import create_engine, text

import cvm_fluxo_caixa as fc


def brapi_fluxo(ticker, token):
    try:
        r = requests.get(f"https://brapi.dev/api/quote/{ticker}",
                          params={"token": token, "modules": "cashflowHistory"}, timeout=40)
    except requests.exceptions.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        d = (r.json().get("results") or [{}])[0].get("cashflowHistory")
    except ValueError:
        return None
    it = d if isinstance(d, list) else (d or {}).get(list(d.keys())[0] if d else "", [])
    if not it:
        return None
    r0 = it[0]
    fco = next((r0.get(k) for k in ("operatingCashFlow", "cashGeneratedInOperations",
                                     "incomeFromOperations")
                if r0.get(k) not in (None, 0)), None)
    return {"b_fco": fco, "b_inv": r0.get("investmentCashFlow"),
            "b_fcf": r0.get("freeCashFlow"), "b_fim": str(r0.get("endDate") or "")[:10]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ano", type=int, default=2025)
    p.add_argument("--n", type=int, default=80)
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--pausa", type=float, default=0.25)
    args = p.parse_args()
    if not args.token:
        sys.exit("Token não informado.")

    print(f"Lendo o fluxo de caixa de {args.ano} pelos nossos scripts...")
    regs = fc.extrair_fluxo(args.ano)
    nosso = pd.DataFrame(regs)
    if nosso.empty:
        sys.exit("Nada extraído.")
    nosso = nosso.rename(columns={
        "Operating Cash Flow": "fco", "Investing Cash Flow": "inv",
        "Free Cash Flow": "fcf", "Capital Expenditure": "capex"})
    print(f"  {len(nosso)} empresas, {int(nosso.get('capex', pd.Series()).notna().sum())} "
          f"com CapEx identificado")

    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.connect() as conn:
        mapa = pd.read_sql(text("SELECT ticker, cd_cvm FROM ticker_cvm_map"), conn)
    mapa["cd_cvm"] = mapa["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    mapa["tk"] = mapa["ticker"].str.replace(".SA", "", regex=False)
    mapa = mapa.drop_duplicates("cd_cvm")

    alvo = nosso.merge(mapa[["cd_cvm", "tk"]], on="cd_cvm").head(args.n)
    print(f"\nConsultando {len(alvo)} tickers na brapi...")

    res = []
    for _, r in alvo.iterrows():
        d = brapi_fluxo(r["tk"], args.token)
        time.sleep(args.pausa)
        if d and d.get("b_fcf") is not None and d.get("b_fco"):
            res.append({**r.to_dict(), **d})
    if not res:
        sys.exit("Nenhum resultado.")
    c = pd.DataFrame(res)

    # --- alinhamento de período, ANTES de qualquer comparação ---
    c["_fim"] = pd.to_datetime(c["fiscal_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    alinhado = c["_fim"] == c["b_fim"]
    print(f"\n{len(c)} respostas | {int(alinhado.sum())} com o MESMO exercício "
          f"({alinhado.mean()*100:.0f}%)")
    if alinhado.sum() < 10:
        print("\n⚠️ Poucos pares alinhados. Amostra dos desalinhados:")
        print(c[~alinhado][["tk", "_fim", "b_fim"]].head(8).to_string(index=False))
        print("\nTente outro --ano. Comparar exercícios diferentes não mede nada.")
        return
    c = c[alinhado].copy()

    c["b_subtraido"] = c["b_fco"] - c["b_fcf"]
    c["inv_abs"] = c["inv"].abs()

    print("\n" + "=" * 78)
    print("O QUE A BRAPI SUBTRAI DO FCO?")
    print("=" * 78)
    print(f"{len(c)} empresas com exercício alinhado\n")

    for nome, col in [("nosso CapEx (subcontas 6.02 por descrição)", "capex"),
                       ("nosso investimento total (6.02 inteiro)", "inv_abs")]:
        if col not in c.columns:
            continue
        d = c[c[col].notna() & (c["b_subtraido"].abs() > 1e6)]
        if d.empty:
            continue
        r = d[col] / d["b_subtraido"].abs()
        r = r[np.isfinite(r)]
        if r.empty:
            continue
        print(f"  {nome}")
        print(f"    n={len(r):>3}  mediana {r.median():>6.3f}  "
              f"dentro de ±5%: {r.between(0.95,1.05).mean()*100:>5.1f}%  "
              f"±20%: {r.between(0.8,1.2).mean()*100:>5.1f}%")

    print("\n" + "=" * 78)
    print("CONFERÊNCIA: nosso FCO e FCF batem com os dela?")
    print("=" * 78)
    for nome, a, b in [("FCO", "fco", "b_fco"), ("FCF", "fcf", "b_fcf")]:
        d = c[c[a].notna() & c[b].notna() & (c[b].abs() > 1e6)]
        if d.empty:
            continue
        r = (d[a] / d[b])
        r = r[np.isfinite(r)]
        print(f"  {nome}: n={len(r):>3}  mediana {r.median():.3f}  "
              f"dentro de ±5%: {r.between(0.95,1.05).mean()*100:.1f}%")

    print("\n" + "=" * 78)
    print("AMOSTRA")
    print("=" * 78)
    cols = ["tk", "fco", "capex", "inv_abs", "b_subtraido"]
    cols = [x for x in cols if x in c.columns]
    amostra = c[c["capex"].notna()].head(15) if "capex" in c.columns else c.head(15)
    print(amostra[cols].to_string(index=False))

    print("\nLEITURA:")
    print("  Se o INVESTIMENTO TOTAL bater e o CapEx não, a brapi usa o 6.02")
    print("  inteiro - que inclui aplicações financeiras e aquisição de")
    print("  participações. Nesse caso os dois conceitos NÃO são")
    print("  intercambiáveis, e o 'CapEx' dela é mais amplo que o nosso.")


if __name__ == "__main__":
    main()
