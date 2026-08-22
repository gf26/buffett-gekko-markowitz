"""
Revisão do CapEx - o que ainda escapa da classificação automática.

PARA QUE SERVE
--------------
A classificação por descrição cobre 97% dos tickers com FCF. Este script
mostra o que sobra, para revisão manual, e permite chegar a 100%.

Ele NÃO altera nada. Gera dois CSVs:

  capex_revisar.csv       descrições não classificadas, com valor e empresa
  capex_classificadas.csv o que a regra atual já captura, para conferência

COMO USAR
---------
1. Rode sem argumentos e veja o resumo.
2. Abra `capex_revisar.csv` e marque na coluna `decisao`:
       S  = é CapEx
       N  = não é
       (vazio = indefinido, fica de fora)
3. Rode com --aplicar para gerar o trecho de código com os termos novos.

POR QUE REVISÃO MANUAL E NÃO MAIS REGEX
---------------------------------------
A CVM permite 2.303 descrições distintas nas subcontas de 6.02. Cada rodada de
ampliação do regex captura menos casos novos e aumenta o risco de pegar o que
não deve - "Investimentos" e "Aumento de Capital em Controladas" parecem CapEx
e são participação societária.

A partir de certo ponto, olhar as descrições que sobraram é mais barato e mais
seguro que adivinhar padrões.

Uso:
    python capex_revisao.py --ano 2025
    python capex_revisao.py --ano 2025 --todos-anos
    python capex_revisao.py --aplicar
"""
import argparse
import os

import pandas as pd

import cvm_fluxo_caixa as fc

CSV_REVISAR = "capex_revisar.csv"
CSV_OK = "capex_classificadas.csv"


def coletar(anos, prefixo="dfp"):
    linhas = []
    for ano in anos:
        d = fc.ler(ano, "DFC_MI_con", prefixo)
        if d.empty:
            continue
        sub = d[d["CD_CONTA"].str.startswith("6.02.")].copy()
        if sub.empty:
            continue
        sub["ano"] = ano
        sub["eh_capex"] = [fc.eh_capex(ds, v) for ds, v in zip(sub["DS_CONTA"], sub["v"])]
        sub["excluida"] = sub["DS_CONTA"].astype(str).str.contains(fc.RE_NAO_CAPEX, na=False)
        linhas.append(sub[["ano", "CD_CVM", "DENOM_CIA", "CD_CONTA", "DS_CONTA",
                            "v", "eh_capex", "excluida"]])
    return pd.concat(linhas, ignore_index=True) if linhas else pd.DataFrame()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ano", type=int, default=2025)
    p.add_argument("--todos-anos", action="store_true",
                    help="Varre 2010-2025 (mais lento, cobertura maior)")
    p.add_argument("--aplicar", action="store_true",
                    help="Lê capex_revisar.csv preenchido e gera o código")
    args = p.parse_args()

    if args.aplicar:
        if not os.path.exists(CSV_REVISAR):
            raise SystemExit(f"{CSV_REVISAR} não encontrado. Rode sem --aplicar primeiro.")
        d = pd.read_csv(CSV_REVISAR)
        if "decisao" not in d.columns:
            raise SystemExit("A coluna `decisao` não existe no CSV.")
        sim = d[d["decisao"].astype(str).str.upper().str.strip() == "S"]
        nao = d[d["decisao"].astype(str).str.upper().str.strip() == "N"]
        print(f"{len(sim)} marcadas como CapEx, {len(nao)} como não-CapEx\n")
        if sim.empty:
            print("Nada a aplicar.")
            return
        termos = sorted({str(x).strip() for x in sim["DS_CONTA"] if str(x).strip()})
        print("Acrescente ao cvm_fluxo_caixa.py:\n")
        print("# descrições confirmadas manualmente como CapEx")
        print("DESCRICOES_CAPEX_MANUAIS = {")
        for t in termos:
            print(f'    "{t}",')
        print("}")
        print("\nE em eh_capex(), antes das demais regras:")
        print("    if str(descricao).strip() in DESCRICOES_CAPEX_MANUAIS:")
        print("        return True")
        if not nao.empty:
            print(f"\n# e {len(nao)} descrições confirmadas como NÃO-CapEx:")
            print("DESCRICOES_NAO_CAPEX_MANUAIS = {")
            for t in sorted({str(x).strip() for x in nao["DS_CONTA"]}):
                print(f'    "{t}",')
            print("}")
        return

    anos = list(range(2010, 2026)) if args.todos_anos else [args.ano]
    print(f"Lendo a DFC de {anos[0]}" + (f" a {anos[-1]}" if len(anos) > 1 else "") + "...")
    d = coletar(anos)
    if d.empty:
        raise SystemExit("Nada lido.")

    empresas = d["CD_CVM"].nunique()
    com = d[d["eh_capex"]]["CD_CVM"].nunique()
    print(f"\n{len(d):,} linhas em subcontas de 6.02, {empresas} empresas")
    print(f"  {com} com CapEx identificado ({com/empresas*100:.1f}%)")
    print(f"  {empresas - com} SEM - são estas que precisam de revisão")

    # só interessa o que é saída de caixa e não foi classificado nem excluído
    sem = set(d[d["eh_capex"]]["CD_CVM"])
    resta = d[~d["CD_CVM"].isin(sem) & ~d["excluida"] & (d["v"] < 0)].copy()

    if resta.empty:
        print("\nNenhuma descrição pendente: cobertura completa.")
    else:
        g = (resta.groupby("DS_CONTA")
             .agg(ocorrencias=("v", "size"),
                  empresas=("CD_CVM", "nunique"),
                  valor_medio=("v", "mean"))
             .sort_values("ocorrencias", ascending=False).reset_index())
        g["decisao"] = ""
        g.to_csv(CSV_REVISAR, index=False, encoding="utf-8-sig")
        print(f"\n{len(g)} descrições distintas a revisar -> {CSV_REVISAR}")
        print("\nAs 30 mais frequentes:")
        print(g.head(30).to_string(index=False))
        print("\nMarque S ou N na coluna `decisao` e rode com --aplicar.")
        print("Dica: valores muito pequenos ou de empresa única podem ficar em")
        print("branco - o ganho de cobertura não compensa o risco de erro.")

    # o que a regra captura hoje, para conferir falso positivo
    ok = (d[d["eh_capex"]].groupby("DS_CONTA")
          .agg(ocorrencias=("v", "size"), empresas=("CD_CVM", "nunique"))
          .sort_values("ocorrencias", ascending=False).reset_index())
    ok.to_csv(CSV_OK, index=False, encoding="utf-8-sig")
    print(f"\n{len(ok)} descrições JÁ classificadas como CapEx -> {CSV_OK}")
    print("Vale conferir se alguma não deveria estar lá (falso positivo é pior")
    print("que cobertura incompleta: contamina o indicador sem avisar).")
    print("\nAs 15 mais frequentes:")
    print(ok.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
