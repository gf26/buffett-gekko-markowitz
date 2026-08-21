"""
Por que o nosso FCO diverge do da brapi?

O CONTEXTO
----------
A engenharia reversa mostrou que a fórmula do FCF da brapi é simples:

    FCF = fluxo operacional + fluxo de investimento    (100% dentro de ±5%)

Não é caixa-preta. Mas com os NOSSOS números a mesma fórmula falha, porque o
nosso FCO diverge do dela - mediana 0,879, só 11,3% dentro de ±5%.

Se descobrirmos a causa, o FCF vira dado primário calculado por nós, com data
de publicação real, e a brapi deixa de ser necessária para ele.

HIPÓTESES A TESTAR
------------------
H1. CONSOLIDADO x INDIVIDUAL. Lemos DFC_MI_con (consolidada); a brapi pode
    usar DFC_MI_ind. Em holdings a diferença é grande.

H2. MÉTODO INDIRETO x DIRETO. Lemos DFC_MI (indireto). Algumas empresas
    entregam DFC_MD (direto), e os totais podem diferir.

H3. ESCALA. A CVM traz ESCALA_MOEDA que pode ser MIL. Se tratarmos errado em
    parte das empresas, aparece divergência de 1000x - mas a mediana de 0,879
    não sugere isso.

H4. PERÍODO. O endDate da brapi pode não ser o mesmo exercício que lemos.

H5. SUBCONTA x TOTAL. Talvez a brapi some subcontas específicas de 6.01 em
    vez de usar o total da conta.

O teste compara todas de uma vez, para não trocar uma suposição por outra.

Uso:
    BRAPI_TOKEN="..." DATABASE_URL="..." python fco_investigacao.py --n 80
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import requests
from sqlalchemy import create_engine, text

import cvm_fonte


def ler(ano, nome):
    zf = cvm_fonte.obter_dfp(ano)
    if zf is None:
        sys.exit(f"DFP de {ano} não encontrado.")
    alvo = f"dfp_cia_aberta_{nome}_{ano}.csv"
    if alvo not in zf.namelist():
        return pd.DataFrame()
    with zf.open(alvo) as f:
        d = pd.read_csv(f, sep=";", encoding="latin-1",
                         dtype={"CD_CONTA": str, "CD_CVM": str})
    if "ORDEM_EXERC" in d.columns:
        d = d[d["ORDEM_EXERC"] == "ÚLTIMO"]
    d["CD_CONTA"] = d["CD_CONTA"].str.strip()
    d["CD_CVM"] = d["CD_CVM"].str.strip().str.lstrip("0")
    esc = (d["ESCALA_MOEDA"].eq("MIL").map({True: 1000, False: 1})
           if "ESCALA_MOEDA" in d.columns else 1)
    d["v"] = pd.to_numeric(d["VL_CONTA"], errors="coerce") * esc
    return d


def totais(ano):
    """FCO e investimento em cada variante de demonstração."""
    out = {}
    for nome, rotulo in [("DFC_MI_con", "indireto_consolidado"),
                          ("DFC_MI_ind", "indireto_individual"),
                          ("DFC_MD_con", "direto_consolidado"),
                          ("DFC_MD_ind", "direto_individual")]:
        d = ler(ano, nome)
        if d.empty:
            continue
        p = (d[d["CD_CONTA"].isin(["6.01", "6.02"])]
             .pivot_table(index="CD_CVM", columns="CD_CONTA", values="v", aggfunc="first"))
        if p.empty:
            continue
        p.columns = [f"{rotulo}_{c}" for c in p.columns]
        # data de fim do exercício, para conferir o período
        dt = d.groupby("CD_CVM")["DT_FIM_EXERC"].first().rename(f"{rotulo}_fim")
        out[rotulo] = p.join(dt)
    if not out:
        return pd.DataFrame()
    base = None
    for _, v in out.items():
        base = v if base is None else base.join(v, how="outer")
    return base


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
    p.add_argument("--ano", type=int, default=2024)
    p.add_argument("--n", type=int, default=80)
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--pausa", type=float, default=0.25)
    args = p.parse_args()
    if not args.token:
        sys.exit("Token não informado.")

    print(f"Lendo as variantes da DFC de {args.ano}...")
    t = totais(args.ano)
    variantes = sorted({c.rsplit("_", 1)[0] for c in t.columns if c.endswith("6.01")})
    for v in variantes:
        col = f"{v}_6.01"
        print(f"  {v:<24} {int(t[col].notna().sum()):>4} empresas")

    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.connect() as conn:
        mapa = pd.read_sql(text("SELECT ticker, cd_cvm FROM ticker_cvm_map"), conn)
    mapa["cd_cvm"] = mapa["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    mapa["tk"] = mapa["ticker"].str.replace(".SA", "", regex=False)
    mapa = mapa.drop_duplicates("cd_cvm")

    alvo = t.join(mapa.set_index("cd_cvm")["tk"], how="inner")
    alvo = alvo[alvo["indireto_consolidado_6.01"].notna()].head(args.n)
    print(f"\nConsultando {len(alvo)} tickers na brapi...")

    res = []
    for cd, r in alvo.iterrows():
        d = brapi_fluxo(r["tk"], args.token)
        time.sleep(args.pausa)
        if d and d.get("b_fco"):
            res.append({"cd_cvm": cd, **r.to_dict(), **d})
    if not res:
        sys.exit("Nenhum resultado da brapi.")
    c = pd.DataFrame(res)
    print(f"  {len(c)} com fluxo operacional\n")

    print("=" * 78)
    print("QUAL VARIANTE DA DFC A BRAPI USA?")
    print("=" * 78)
    for v in variantes:
        col = f"{v}_6.01"
        if col not in c.columns:
            continue
        r = c[col] / c["b_fco"]
        r = r[np.isfinite(r)]
        if r.empty:
            continue
        print(f"  {v:<24} n={len(r):>3}  mediana {r.median():>6.3f}  "
              f"dentro de ±5%: {r.between(0.95,1.05).mean()*100:>5.1f}%")

    print("\n" + "=" * 78)
    print("O PERÍODO COINCIDE?")
    print("=" * 78)
    if "indireto_consolidado_fim" in c.columns:
        c["_fim_cvm"] = c["indireto_consolidado_fim"].astype(str).str[:10]
        igual = (c["_fim_cvm"] == c["b_fim"]).mean() * 100
        print(f"  {igual:.1f}% com mesma data de fim de exercício")
        dif = c[c["_fim_cvm"] != c["b_fim"]]
        if not dif.empty:
            print(f"\n  {len(dif)} divergentes (amostra):")
            print(dif[["tk", "_fim_cvm", "b_fim"]].head(8).to_string(index=False))

    print("\n" + "=" * 78)
    print("SE O FCO BATER, O FCF BATE?")
    print("=" * 78)
    melhor = None
    for v in variantes:
        col = f"{v}_6.01"
        if col not in c.columns:
            continue
        r = (c[col] / c["b_fco"])
        r = r[np.isfinite(r)]
        if len(r) and (melhor is None or r.between(0.95, 1.05).mean() > melhor[1]):
            melhor = (v, r.between(0.95, 1.05).mean())
    if melhor:
        v = melhor[0]
        print(f"Usando a variante que mais bate: {v}")
        fcf = c[f"{v}_6.01"] + c[f"{v}_6.02"]
        r = fcf / c["b_fcf"]
        r = r[np.isfinite(r)]
        print(f"  FCF calculado vs brapi: mediana {r.median():.3f}, "
              f"dentro de ±5%: {r.between(0.95,1.05).mean()*100:.1f}%")
        if r.between(0.95, 1.05).mean() > 0.8:
            print("\n  ✅ Podemos calcular o FCF por conta própria, com data de")
            print("     publicação real. A brapi deixa de ser necessária para ele.")
        else:
            print("\n  A fórmula bate para a brapi mas não com nossos números -")
            print("  a divergência está no FCO, não na fórmula.")

    print("\n10 maiores divergências de FCO:")
    c["_dif"] = (c["indireto_consolidado_6.01"] - c["b_fco"]).abs()
    cols = ["tk", "indireto_consolidado_6.01", "b_fco"]
    if "indireto_individual_6.01" in c.columns:
        cols.insert(2, "indireto_individual_6.01")
    print(c.nlargest(10, "_dif")[cols].to_string(index=False))


if __name__ == "__main__":
    main()
