"""
CVM - descoberta do plano de contas de INSTITUIÇÕES FINANCEIRAS.

POR QUE ESTE SCRIPT EXISTE
--------------------------
Bancos, seguradoras e outras instituições financeiras usam um plano de contas
DIFERENTE na CVM. A conta 2.03, que em uma indústria é "Patrimônio Líquido",
significa outra coisa (ou nem existe) num banco. Por isso o de-para atual
(cvm_ingestor.py) produz ~72% de concordância no setor financeiro, contra
~93% no resto - e esses dados entrariam TORTOS, não é diferença de critério.

Em vez de adivinhar os códigos a partir de documentação de terceiros, este
script DESCOBRE a estrutura a partir dos próprios arquivos: pega as empresas
que sabemos ser do setor financeiro, lista quais contas elas realmente usam,
com as descrições oficiais (DS_CONTA), e mostra lado a lado com o que as
empresas gerais usam.

Foi essa mesma abordagem empírica que revelou, na Fase 1, que Patrimônio
Líquido e Lucro Líquido precisavam da variante "controladores" - uma coisa
que nenhuma documentação teria dito.

SAÍDA
-----
Imprime no terminal e grava 'plano_contas_financeiro.csv' com:
  - código da conta, descrição, quantas empresas financeiras usam
  - se aquela conta também é usada por empresas gerais
Com isso dá para montar o de-para específico com evidência, não com chute.

Uso:
    DATABASE_URL="..." python cvm_descobrir_plano_bancos.py --ano 2024
"""
import argparse
import io
import os
import zipfile

import pandas as pd
import requests

import cvm_fonte
from sqlalchemy import create_engine, text

URL_DFP = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip"
SETORES_FINANCEIROS = ("Financial Services", "Insurance")
CSV_SAIDA = "plano_contas_financeiro.csv"

# quantos níveis do código mostrar (2.03.09 = 3 níveis). Mais níveis = mais
# detalhe e mais ruído; 2-3 costuma ser o suficiente para o de-para.
NIVEL_MAX = 3


def ler_demonstrativo(zf, ano, sigla):
    nome = f"dfp_cia_aberta_{sigla}_con_{ano}.csv"
    if nome not in zf.namelist():
        return pd.DataFrame()
    with zf.open(nome) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", decimal=".",
                          dtype={"CD_CONTA": str, "CNPJ_CIA": str, "CD_CVM": str})
    if "CD_CVM" in df.columns:
        df["CD_CVM"] = df["CD_CVM"].str.strip().str.lstrip("0")
    if "CD_CONTA" in df.columns:
        df["CD_CONTA"] = df["CD_CONTA"].str.strip()
    if "ORDEM_EXERC" in df.columns:
        df = df[df["ORDEM_EXERC"] == "ÚLTIMO"]
    return df


def nivel_do_codigo(cd):
    return len(str(cd).split("."))


def dump_empresa(zf, ano, cd_cvm_alvo, nivel_max):
    """Despeja o plano de contas COMPLETO de uma empresa específica.

    A visão agregada esconde estrutura: contas usadas por poucas empresas
    ficam abaixo do corte percentual e somem do relatório. Para entender o
    plano de um banco, é preciso ver TODAS as contas que ELE declara."""
    print("\n" + "=" * 78)
    print(f"PLANO DE CONTAS COMPLETO - empresa CD_CVM {cd_cvm_alvo}")
    print("=" * 78)

    achou = False
    for sigla in ["BPA", "BPP", "DRE", "DFC_MI"]:
        df = ler_demonstrativo(zf, ano, sigla)
        if df.empty:
            continue
        sub = df[df["CD_CVM"] == str(cd_cvm_alvo)]
        if sub.empty:
            continue
        achou = True
        nome = sub["DENOM_CIA"].iloc[0] if "DENOM_CIA" in sub.columns else "?"
        sub = sub[sub["CD_CONTA"].apply(nivel_do_codigo) <= nivel_max]
        sub = sub.sort_values("CD_CONTA")
        print(f"\n--- {sigla} ({nome}) ---")
        for _, r in sub.iterrows():
            valor = r.get("VL_CONTA")
            try:
                valor_fmt = f"{float(valor):>20,.0f}"
            except (TypeError, ValueError):
                valor_fmt = f"{str(valor):>20}"
            print(f"  {r['CD_CONTA']:<12} {str(r['DS_CONTA'])[:45]:<47} {valor_fmt}")

    if not achou:
        print(f"  Nenhum demonstrativo encontrado para CD_CVM {cd_cvm_alvo} em {ano}.")
        print("  Confira o código (a coluna cd_cvm da tabela ticker_cvm_map).")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ano", type=int, default=2024)
    p.add_argument("--nivel-max", type=int, default=NIVEL_MAX)
    p.add_argument("--min-pct", type=float, default=30.0,
                    help="Só mostra contas usadas por pelo menos esta %% do setor. "
                         "Baixe para 5 ou 10 para ver contas de subgrupos menores. Padrão: 30.")
    p.add_argument("--dump", nargs="*", default=None,
                    help="Despeja o plano de contas COMPLETO dos CD_CVM informados "
                         "(ex: --dump 906 1023). Ignora a análise agregada.")
    args = p.parse_args()

    # modo dump: não precisa de classificação setorial, vai direto ao arquivo
    if args.dump:
        # usa o cache local (dados_cvm/) - o servidor da CVM bloqueia
        # conexões vindas do Codespace por faixa de IP
        zf = cvm_fonte.obter_dfp(args.ano)
        if zf is None:
            raise SystemExit(f"DFP {args.ano} não disponível. Veja: python cvm_fonte.py --listar")
        for cd in args.dump:
            dump_empresa(zf, args.ano, str(cd).strip().lstrip("0"), args.nivel_max)
        return

    engine = create_engine(os.environ["DATABASE_URL"])

    # quais CD_CVM são do setor financeiro (via nosso mapeamento + company_info)
    with engine.connect() as conn:
        df_setor = pd.read_sql(text("""
            SELECT m.cd_cvm, m.ticker, ci.info->>'sector' AS setor
            FROM ticker_cvm_map m
            LEFT JOIN company_info ci ON ci.ticker = m.ticker
        """), conn)

    df_setor["cd_cvm"] = df_setor["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    financeiros = set(df_setor[df_setor["setor"].isin(SETORES_FINANCEIROS)]["cd_cvm"])
    gerais = set(df_setor[~df_setor["setor"].isin(SETORES_FINANCEIROS)
                           & df_setor["setor"].notna()]["cd_cvm"])

    print(f"{len(financeiros)} empresas do setor financeiro mapeadas")
    print(f"{len(gerais)} empresas de outros setores (grupo de comparação)")
    if not financeiros:
        raise SystemExit("Nenhuma empresa financeira mapeada - confira company_info e ticker_cvm_map.")

    zf = cvm_fonte.obter_dfp(args.ano)
    if zf is None:
        raise SystemExit(f"DFP {args.ano} não disponível. Veja: python cvm_fonte.py --listar")

    linhas = []
    for sigla in ["BPA", "BPP", "DRE", "DFC_MI"]:
        df = ler_demonstrativo(zf, args.ano, sigla)
        if df.empty:
            continue
        df = df[df["CD_CONTA"].apply(nivel_do_codigo) <= args.nivel_max]

        fin = df[df["CD_CVM"].isin(financeiros)]
        ger = df[df["CD_CVM"].isin(gerais)]

        # para cada conta: quantas financeiras usam, quantas gerais usam
        uso_fin = fin.groupby(["CD_CONTA", "DS_CONTA"])["CD_CVM"].nunique()
        uso_ger = ger.groupby("CD_CONTA")["CD_CVM"].nunique()

        for (cd, ds), n_fin in uso_fin.items():
            n_ger = int(uso_ger.get(cd, 0))
            linhas.append({
                "demonstrativo": sigla, "cd_conta": cd, "descricao": ds,
                "empresas_financeiras": int(n_fin),
                "empresas_gerais": n_ger,
                "pct_financeiras": round(100 * n_fin / len(financeiros), 1),
                "exclusiva_do_setor_financeiro": "SIM" if n_ger == 0 else "não",
            })

    out = pd.DataFrame(linhas).sort_values(
        ["demonstrativo", "cd_conta"]).reset_index(drop=True)
    out.to_csv(CSV_SAIDA, index=False, encoding="utf-8-sig")

    for sigla in ["BPA", "BPP", "DRE", "DFC_MI"]:
        sub = out[out["demonstrativo"] == sigla]
        if sub.empty:
            continue
        print("\n" + "=" * 78)
        print(f"{sigla} - contas usadas por instituições financeiras")
        print("=" * 78)
        # só as usadas por uma fatia relevante do setor - o resto é cauda longa
        relevantes = sub[sub["pct_financeiras"] >= args.min_pct]
        print(relevantes[["cd_conta", "descricao", "pct_financeiras",
                           "exclusiva_do_setor_financeiro"]].to_string(index=False))

    print("\n" + "=" * 78)
    print("CONTAS EXCLUSIVAS DO SETOR FINANCEIRO (não aparecem em empresas gerais)")
    print("Estas são a evidência de que o plano de contas é mesmo diferente:")
    print("=" * 78)
    excl = out[(out["exclusiva_do_setor_financeiro"] == "SIM") & (out["pct_financeiras"] >= args.min_pct)]
    if excl.empty:
        print("  Nenhuma - o plano pode ser mais parecido do que se supunha.")
    else:
        print(excl[["demonstrativo", "cd_conta", "descricao", "pct_financeiras"]].to_string(index=False))

    print(f"\nCSV completo em: {CSV_SAIDA}")
    print("\nPRÓXIMO PASSO: com esta lista, monta-se o de-para específico do setor")
    print("financeiro no cvm_ingestor.py, com evidência do que cada conta significa.")


if __name__ == "__main__":
    main()