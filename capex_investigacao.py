"""
CapEx: viabilidade da aproximação pelo balanço, e engenharia reversa da brapi.

O PROBLEMA
----------
O FCF Yield precisa de CapEx. A CVM traz o investimento na DFC, conta 6.02,
mas SEM PADRONIZAÇÃO: são 2.303 descrições distintas para 3.530 linhas em
2024, e nenhuma cobre mais de 11% das empresas. Localizar por descrição - o
que funcionou para o plano de contas de bancos - não resolve aqui.

A brapi entrega `freeCashFlow` para 10 de 10 tickers testados, mas não
conseguimos reproduzir o número: a Suzano aparece com FCF de R$ 8,32 bi contra
FCO de R$ 1,06 bi, o que é impossível se FCF = FCO - CapEx.

O QUE ESTE SCRIPT FAZ
---------------------
TESTE A - Engenharia reversa. Para uma amostra grande, calcula o CapEx
  implícito (FCO - FCF_brapi) e testa contra várias definições candidatas:
  linhas de imobilizado na DFC, 6.02 inteiro, variação do imobilizado no
  balanço. Se alguma bater consistentemente, descobrimos o método.

TESTE B - Viabilidade da aproximação pelo balanço:

      CapEx ≈ Δ Imobilizado + Depreciação

  Usa contas universais e auditáveis, com point-in-time. A imprecisão vem de
  reavaliações e baixas de ativo. O teste mede esse erro comparando com o
  CapEx da DFC nas empresas em que ele está identificável.

Uso:
    BRAPI_TOKEN="..." python capex_investigacao.py --ano 2024 --n 60
"""
import argparse
import os
import re
import sys
import time

import pandas as pd
import requests

import cvm_fonte

# descrições que identificam CapEx com razoável confiança
RE_CAPEX = re.compile(
    r"(aquisi|adi[cç]|aplica|compra|investiment).{0,40}(imobiliz|intang)"
    r"|(imobiliz|intang).{0,30}(aquisi|adi[cç]|adquir)",
    re.IGNORECASE)


def ler(ano, nome):
    zf = cvm_fonte.obter_dfp(ano)
    if zf is None:
        sys.exit(f"DFP de {ano} não encontrado em dados_cvm/")
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


def montar_painel(ano):
    dfc = ler(ano, "DFC_MI_con")
    bpa = ler(ano, "BPA_con")
    dre = ler(ano, "DRE_con")

    linhas = []
    for cd, g in dfc.groupby("CD_CVM"):
        nome = g["DENOM_CIA"].iloc[0]
        fco = g[g["CD_CONTA"] == "6.01"]["v"]
        inv = g[g["CD_CONTA"] == "6.02"]["v"]
        if fco.empty:
            continue
        sub = g[g["CD_CONTA"].str.startswith("6.02.")]
        capex_dfc = sub[sub["DS_CONTA"].astype(str).str.contains(RE_CAPEX, na=False)]["v"].sum()
        linhas.append({
            "cd_cvm": cd, "nome": nome,
            "fco": float(fco.iloc[0]),
            "inv_total": float(inv.iloc[0]) if not inv.empty else None,
            "capex_dfc": abs(float(capex_dfc)) if capex_dfc else None,
            "n_sub": len(sub),
        })
    p = pd.DataFrame(linhas)

    # imobilizado (1.02.03) para a aproximação pelo balanço
    if not bpa.empty:
        imob = (bpa[bpa["CD_CONTA"] == "1.02.03"]
                .groupby("CD_CVM")["v"].first().rename("imobilizado"))
        p = p.merge(imob, left_on="cd_cvm", right_index=True, how="left")

    # depreciação: aparece na DFC como ajuste ao lucro
    if not dfc.empty:
        dep = (dfc[dfc["DS_CONTA"].astype(str).str.contains("deprecia", case=False, na=False)]
               .groupby("CD_CVM")["v"].sum().rename("depreciacao"))
        p = p.merge(dep, left_on="cd_cvm", right_index=True, how="left")
    return p


def fcf_brapi(ticker, token):
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
    reg = it[0]
    return {"fcf": reg.get("freeCashFlow"),
            "fco": reg.get("cashGeneratedInOperations"),
            "inv": reg.get("investmentCashFlow"),
            "endDate": reg.get("endDate")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ano", type=int, default=2024)
    p.add_argument("--n", type=int, default=60, help="quantos tickers consultar na brapi")
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--pausa", type=float, default=0.3)
    args = p.parse_args()

    print(f"Lendo DFP de {args.ano}...")
    painel = montar_painel(args.ano)
    print(f"  {len(painel)} empresas com FCO")
    tem_capex = painel["capex_dfc"].notna() & (painel["capex_dfc"] > 0)
    print(f"  {int(tem_capex.sum())} com CapEx identificável por descrição "
          f"({tem_capex.mean()*100:.0f}%)")

    # --- TESTE B: aproximação pelo balanço, sem depender da brapi ---
    print("\n" + "=" * 78)
    print("TESTE B - aproximação pelo balanço vs CapEx da DFC")
    print("=" * 78)
    print("CapEx ≈ variação do imobilizado + depreciação.")
    print("Aqui só dá para medir o segundo termo: precisa de dois anos para a")
    print("variação. Este teste compara a DEPRECIAÇÃO com o CapEx da DFC, que")
    print("é a parte estável da aproximação.\n")
    b = painel[tem_capex & painel["depreciacao"].notna()].copy()
    if not b.empty:
        b["razao"] = b["depreciacao"].abs() / b["capex_dfc"]
        b = b[b["razao"].between(0.01, 100)]
        print(f"{len(b)} empresas comparáveis")
        print(f"  razão depreciação/CapEx - mediana {b['razao'].median():.2f}, "
              f"p25 {b['razao'].quantile(.25):.2f}, p75 {b['razao'].quantile(.75):.2f}")
        print("\n  Se a dispersão for alta, a depreciação não substitui o CapEx -")
        print("  e a aproximação depende inteiramente da variação do imobilizado.")

    if not args.token:
        print("\n(sem token: pulando a engenharia reversa da brapi)")
        return

    # --- TESTE A: engenharia reversa ---
    print("\n" + "=" * 78)
    print("TESTE A - engenharia reversa do freeCashFlow da brapi")
    print("=" * 78)

    # precisa do de-para cd_cvm -> ticker
    from sqlalchemy import create_engine, text
    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.connect() as conn:
        mapa = pd.read_sql(text("SELECT ticker, cd_cvm FROM ticker_cvm_map"), conn)
    mapa["cd_cvm"] = mapa["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    mapa["tk"] = mapa["ticker"].str.replace(".SA", "", regex=False)
    # um ticker por empresa
    mapa = mapa.drop_duplicates("cd_cvm")

    alvo = painel.merge(mapa[["cd_cvm", "tk"]], on="cd_cvm", how="inner")
    alvo = alvo[alvo["fco"].abs() > 1e7].head(args.n)
    print(f"consultando {len(alvo)} tickers...\n")

    res = []
    for _, r in alvo.iterrows():
        d = fcf_brapi(r["tk"], args.token)
        time.sleep(args.pausa)
        if not d or d.get("fcf") in (None, 0):
            continue
        res.append({**r.to_dict(), **{f"b_{k}": v for k, v in d.items()}})

    if not res:
        print("Nenhum resultado.")
        return
    c = pd.DataFrame(res)
    c["capex_implicito"] = c["fco"] - c["b_fcf"]

    print(f"{len(c)} empresas com FCF da brapi\n")
    print("Testando definições candidatas (razão perto de 1 = acertou):\n")

    testes = {
        "FCO - CapEx(descrição)": c["fco"] - c["capex_dfc"],
        "FCO + investimento total": c["fco"] + c["inv_total"],
        "FCO da brapi - CapEx(descrição)": c["b_fco"] - c["capex_dfc"],
        "FCO da brapi + inv. da brapi": c["b_fco"] + c["b_inv"],
    }
    for nome, calc in testes.items():
        r = (calc / c["b_fcf"]).replace([float("inf"), float("-inf")], pd.NA).dropna()
        if r.empty:
            continue
        dentro = r.between(0.95, 1.05).mean() * 100
        print(f"  {nome:<34} mediana {r.median():>7.3f}  dentro de ±5%: {dentro:>5.1f}%")

    print("\nNosso FCO bate com o da brapi?")
    rr = (c["fco"] / c["b_fco"]).replace([float("inf"), float("-inf")], pd.NA).dropna()
    if not rr.empty:
        print(f"  mediana {rr.median():.3f}, dentro de ±5%: {rr.between(0.95,1.05).mean()*100:.1f}%")
        print("  (se não bater, a brapi usa outro período ou a demonstração individual)")

    print("\n10 maiores divergências:")
    c["dif"] = (c["fco"] - c["b_fcf"]).abs()
    cols = ["tk", "fco", "b_fco", "b_fcf", "capex_dfc", "capex_implicito"]
    print(c.nlargest(10, "dif")[cols].to_string(index=False))


if __name__ == "__main__":
    main()
