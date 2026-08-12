"""
CVM - FASE 1: prova de conceito e validação de compatibilidade.

OBJETIVO
--------
Antes de investir dias construindo um ingestor completo da CVM, responder uma
pergunta só: OS NÚMEROS DA CVM BATEM COM OS DO YAHOO para os mesmos tickers e
exercícios? Se baterem, a fonte é compatível e vale seguir para as fases
seguintes. Se não baterem, descobrimos agora - barato - e repensamos.

Este script NÃO grava nada no banco. Só baixa, parseia, compara e relata.

O QUE ELE FAZ
-------------
1. Baixa o arquivo DFP (anual) da CVM para o ano pedido - dados oficiais,
   públicos, sem chave de API.
2. Parseia os demonstrativos (BPA, BPP, DRE, DFC).
3. Tenta casar as empresas da CVM (identificadas por CNPJ/código CVM) com os
   tickers que já temos no banco (identificados por nome vindo do Yahoo).
4. Para os que casaram, compara valores-chave (Ativo Total, Patrimônio
   Líquido, Receita, Lucro Líquido) contra o que o Yahoo nos deu.
5. Relata a taxa de concordância.

ESTRUTURA DOS ARQUIVOS DA CVM (para referência)
-----------------------------------------------
URL: .../DFP/DADOS/dfp_cia_aberta_AAAA.zip
Dentro do zip, um CSV por demonstrativo, em duas versões:
  _con_ = consolidado (o grupo todo)   <- usamos este, equivale ao Yahoo
  _ind_ = individual (só a controladora)
Colunas relevantes: CNPJ_CIA, DT_REFER, DENOM_CIA, CD_CVM, ORDEM_EXERC,
DT_FIM_EXERC, CD_CONTA, DS_CONTA, VL_CONTA, ESCALA_MOEDA.

Dois detalhes que quebram quem não presta atenção:
  - ORDEM_EXERC: cada arquivo traz o exercício corrente ('ÚLTIMO') E o
    anterior ('PENÚLTIMO'). Filtrar, ou os valores duplicam.
  - ESCALA_MOEDA: pode vir 'MIL' (valores em milhares) ou 'UNIDADE'.
    Normalizar, ou os números saem 1000x errados.

Uso:
    DATABASE_URL="postgresql://..." python cvm_fase1_validacao.py --ano 2024
"""
import argparse
import io
import os
import unicodedata
import zipfile

import pandas as pd
import requests
from sqlalchemy import create_engine, text

import cvm_fonte

CVM_DFP_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip"

# Contas CVM que vamos comparar com o Yahoo nesta prova de conceito.
# (o de-para completo vem na Fase 2 - aqui só o suficiente para validar)
#
# Sobre Patrimônio Líquido e Lucro Líquido: a CVM reporta o valor CONSOLIDADO
# (2.03 e 3.11), que INCLUI a participação de não-controladores (minoritários).
# O Yahoo usa a convenção de reportar o atribuível aos CONTROLADORES. Por isso
# testamos as duas variantes: a conta cheia e a subconta dos controladores.
CONTAS_TESTE = {
    "BPA": {"1": "Total Assets", "1.01": "Current Assets"},
    "BPP": {
        "2.01": "Current Liabilities",
        "2.03": "Stockholders Equity (consolidado)",
        "2.03.09": "_minoritarios_pl",
    },
    "DRE": {
        "3.01": "Total Revenue",
        "3.03": "Gross Profit",
        "3.11": "Net Income (consolidado)",
        "3.11.01": "Net Income (controladores)",
    },
}

# Como cada variante da CVM deve ser comparada com o Yahoo
EQUIVALENCIAS = {
    "Total Assets": "Total Assets",
    "Current Assets": "Current Assets",
    "Current Liabilities": "Current Liabilities",
    "Total Revenue": "Total Revenue",
    "Gross Profit": "Gross Profit",
    "Stockholders Equity (consolidado)": "Stockholders Equity",
    "Stockholders Equity (controladores)": "Stockholders Equity",
    "Net Income (consolidado)": "Net Income",
    "Net Income (controladores)": "Net Income",
}

ESCALA = {"UNIDADE": 1, "MIL": 1_000, "MILHAR": 1_000, "MILHÃO": 1_000_000, "MILHAO": 1_000_000}


def baixar_dfp(ano):
    """Lê do cache local (dados_cvm/) - o servidor da CVM bloqueia conexões
    vindas do Codespace. Ver ATUALIZACAO_MANUAL_CVM.md"""
    zf = cvm_fonte.obter_dfp(ano)
    if zf is None:
        raise SystemExit(f"DFP {ano} não disponível. Veja: python cvm_fonte.py --listar")
    return zf


def ler_demonstrativo(zf, ano, sigla, consolidado=True):
    """Lê um CSV de dentro do zip. sigla: BPA, BPP, DRE, DFC_MI..."""
    tipo = "con" if consolidado else "ind"
    nome = f"dfp_cia_aberta_{sigla}_{tipo}_{ano}.csv"
    if nome not in zf.namelist():
        print(f"  aviso: {nome} não encontrado no zip")
        return pd.DataFrame()
    with zf.open(nome) as f:
        # dtype=str em CD_CONTA é essencial: sem isso o pandas lê o código da
        # conta ("1", "1.01") como float (1.0, 1.01), e NENHUM mapeamento de
        # conta casa - falha silenciosa, sem erro, com resultado vazio.
        df = pd.read_csv(f, sep=";", encoding="latin-1", decimal=".",
                          dtype={"CD_CONTA": str, "CNPJ_CIA": str, "CD_CVM": str})
    if "CD_CONTA" in df.columns:
        df["CD_CONTA"] = df["CD_CONTA"].str.strip()
    # só o exercício corrente (o arquivo também traz o anterior, o que
    # duplicaria tudo se não filtrássemos)
    if "ORDEM_EXERC" in df.columns:
        df = df[df["ORDEM_EXERC"] == "ÚLTIMO"]
    # Reapresentações: quando a empresa corrige um demonstrativo já publicado,
    # o arquivo passa a conter mais de uma VERSAO para o mesmo período. Ficamos
    # com a mais recente (a versão corrigida) - sem isso, valores concorrentes
    # convivem e o pivot escolhe um arbitrariamente.
    if "VERSAO" in df.columns and not df.empty:
        df["VERSAO"] = pd.to_numeric(df["VERSAO"], errors="coerce")
        chaves = ["CNPJ_CIA", "DT_FIM_EXERC", "CD_CONTA"]
        chaves = [c for c in chaves if c in df.columns]
        if chaves:
            df = df.sort_values("VERSAO").drop_duplicates(chaves, keep="last")
    # normaliza a escala: alguns documentos vêm em milhares
    if "ESCALA_MOEDA" in df.columns:
        fator = df["ESCALA_MOEDA"].str.upper().map(ESCALA).fillna(1)
        df["VL_CONTA"] = df["VL_CONTA"] * fator
    return df


def normalizar_nome(nome):
    """Normaliza razão social para tentar casar CVM x Yahoo.

    A remoção de acentos é essencial: a CVM grava sem acento ('PETROLEO
    BRASILEIRO') e o Yahoo com ('Petróleo Brasileiro'). Sem isso, boa parte
    das empresas não casaria - e a falha seria silenciosa."""
    if not isinstance(nome, str):
        return ""
    n = unicodedata.normalize("NFKD", nome)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.upper()
    for lixo in [" S.A.", " S/A", " SA ", " S.A", " LTDA", " HOLDING", " PARTICIPACOES",
                  " CIA", " COMPANHIA", ".", ",", "-", "  "]:
        n = n.replace(lixo, " ")
    return " ".join(n.split()).strip()


def carregar_tickers_do_banco(engine):
    """Além do nome (para casar com a CVM), traz a MOEDA em que o Yahoo
    reporta os demonstrativos e o setor - as duas causas suspeitas das
    divergências restantes."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT t.ticker, t.name,
                   ci.info->>'financialCurrency' AS moeda_yahoo,
                   ci.info->>'sector'            AS setor
            FROM tickers t
            LEFT JOIN company_info ci ON ci.ticker = t.ticker
            WHERE t.name IS NOT NULL
        """), conn)
    df["nome_norm"] = df["name"].apply(normalizar_nome)
    return df


def carregar_yahoo_do_banco(engine, ano):
    """Valores que o Yahoo nos deu para o exercício encerrado no ano pedido."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, statement, fiscal_date, line_item, value
            FROM financials
            WHERE period_type = 'annual'
              AND EXTRACT(YEAR FROM fiscal_date) = :ano
              AND line_item IN ('Total Assets','Current Assets','Current Liabilities',
                                'Stockholders Equity','Total Revenue','Gross Profit','Net Income')
        """), conn, params={"ano": ano})
    return df


def main():
    parser = argparse.ArgumentParser(description="Fase 1: valida compatibilidade CVM x Yahoo.")
    parser.add_argument("--ano", type=int, default=2024, help="Exercício a validar (padrão: 2024)")
    parser.add_argument("--tolerancia", type=float, default=2.0,
                         help="Diferença percentual aceitável para considerar 'bate' (padrão: 2%%)")
    args = parser.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])

    zf = baixar_dfp(args.ano)
    print(f"\nArquivos dentro do zip ({len(zf.namelist())}):")
    for n in sorted(zf.namelist())[:20]:
        print(f"  {n}")

    # --- parse dos demonstrativos ---
    frames = []
    for sigla, contas in CONTAS_TESTE.items():
        df = ler_demonstrativo(zf, args.ano, sigla)
        if df.empty:
            continue
        df = df[df["CD_CONTA"].isin(contas.keys())].copy()
        df["line_item"] = df["CD_CONTA"].map(contas)
        frames.append(df[["CNPJ_CIA", "DENOM_CIA", "CD_CVM", "DT_FIM_EXERC", "line_item", "VL_CONTA"]])

    if not frames:
        raise SystemExit("Nenhum demonstrativo lido - confira se o ano existe na CVM.")

    cvm = pd.concat(frames, ignore_index=True)
    print(f"\nCVM: {cvm['CD_CVM'].nunique()} empresas, {len(cvm)} linhas de conta lidas.")

    # Deriva o PL atribuível aos CONTROLADORES = consolidado - minoritários.
    # (o Lucro Líquido dos controladores já vem pronto na subconta 3.11.01)
    wide = cvm.pivot_table(index=["CNPJ_CIA", "DENOM_CIA", "CD_CVM", "DT_FIM_EXERC"],
                           columns="line_item", values="VL_CONTA", aggfunc="first").reset_index()
    if "Stockholders Equity (consolidado)" in wide.columns:
        minor = wide["_minoritarios_pl"] if "_minoritarios_pl" in wide.columns else 0
        wide["Stockholders Equity (controladores)"] = (
            wide["Stockholders Equity (consolidado)"] - pd.to_numeric(minor, errors="coerce").fillna(0)
        )
    cvm = wide.melt(id_vars=["CNPJ_CIA", "DENOM_CIA", "CD_CVM", "DT_FIM_EXERC"],
                    var_name="line_item", value_name="VL_CONTA").dropna(subset=["VL_CONTA"])
    cvm = cvm[cvm["line_item"] != "_minoritarios_pl"]
    cvm["equiv_yahoo"] = cvm["line_item"].map(EQUIVALENCIAS)
    cvm = cvm.dropna(subset=["equiv_yahoo"])

    # --- casamento por nome ---
    tickers = carregar_tickers_do_banco(engine)
    cvm["nome_norm"] = cvm["DENOM_CIA"].apply(normalizar_nome)

    casados = cvm.merge(tickers, on="nome_norm", how="inner")
    n_empresas_casadas = casados["CD_CVM"].nunique()
    print(f"Casamento por nome: {n_empresas_casadas} empresas da CVM casaram com tickers do banco "
          f"(de {len(tickers)} tickers com nome).")
    if n_empresas_casadas == 0:
        raise SystemExit("Nenhuma empresa casou por nome - o casamento precisa de outra estratégia (Fase 2).")

    # --- comparação com o Yahoo ---
    yahoo = carregar_yahoo_do_banco(engine, args.ano)
    if yahoo.empty:
        raise SystemExit(f"Sem dados do Yahoo para exercícios de {args.ano} no banco - tente outro ano.")

    comp = casados.merge(
        yahoo, left_on=["ticker", "equiv_yahoo"], right_on=["ticker", "line_item"],
        how="inner", suffixes=("_cvm", "_yahoo")
    )
    if comp.empty:
        raise SystemExit("Nenhum par (ticker, conta) em comum entre CVM e Yahoo para comparar.")

    comp["valor_cvm"] = comp["VL_CONTA"].astype(float)
    comp["valor_yahoo"] = comp["value"].astype(float)
    # pd.NA (do guarda contra divisão por zero) transforma a coluna inteira em
    # object; to_numeric devolve float com NaN, que nlargest/median aceitam
    comp["dif_pct"] = pd.to_numeric(
        (comp["valor_cvm"] - comp["valor_yahoo"]).abs()
        / comp["valor_yahoo"].abs().replace(0, pd.NA) * 100,
        errors="coerce",
    )
    comp["bate"] = comp["dif_pct"] <= args.tolerancia

    print("\n" + "=" * 78)
    print(f"VALIDAÇÃO CVM x YAHOO - exercício {args.ano} (tolerância {args.tolerancia}%)")
    print("=" * 78)
    print(f"Pares comparados: {len(comp)}  |  tickers distintos: {comp['ticker'].nunique()}")

    print("\nPor conta da CVM (as variantes 'consolidado' e 'controladores' competem entre si -")
    print("a que tiver MAIOR concordância é a que devemos usar no de-para da Fase 2):")
    por_conta = comp.groupby("line_item_cvm").agg(
        pares=("bate", "size"), concordancia_pct=("bate", lambda s: round(s.mean() * 100, 1)),
        dif_mediana_pct=("dif_pct", lambda s: round(s.median(), 2)),
    ).sort_values("concordancia_pct")
    print(por_conta.to_string())

    # taxa geral considerando, para cada conceito, só a melhor variante
    melhor_por_conceito = (comp.groupby(["equiv_yahoo", "line_item_cvm"])["bate"].mean()
                             .reset_index().sort_values("bate", ascending=False)
                             .drop_duplicates("equiv_yahoo"))
    escolhidas = set(melhor_por_conceito["line_item_cvm"])
    comp_melhor = comp[comp["line_item_cvm"].isin(escolhidas)]
    taxa = comp_melhor["bate"].mean() * 100
    print(f"\nConcordância usando a MELHOR variante de cada conceito: {taxa:.1f}%")
    print("Mapeamento vencedor (é isto que vai para o de-para da Fase 2):")
    for _, r in melhor_por_conceito.iterrows():
        print(f"    {r['equiv_yahoo']:<22} <- CVM: {r['line_item_cvm']}  ({r['bate']*100:.1f}%)")

    print("\n10 maiores divergências (para inspeção manual):")
    piores = comp_melhor.nlargest(10, "dif_pct")[
        ["ticker", "DENOM_CIA", "line_item_cvm", "valor_cvm", "valor_yahoo", "dif_pct"]]
    print(piores.to_string(index=False))

    # ---- diagnóstico das causas ----
    print("\n" + "-" * 78)
    print("DIAGNÓSTICO DAS DIVERGÊNCIAS RESTANTES")
    print("-" * 78)

    comp_melhor = comp_melhor.copy()
    comp_melhor["razao"] = comp_melhor["valor_cvm"] / comp_melhor["valor_yahoo"].replace(0, pd.NA)

    print("\n1) Divergências com RAZÃO SISTEMÁTICA (mesmo fator em várias contas do mesmo")
    print("   ticker) - sinal de diferença de MOEDA ou ESCALA, não de dado errado:")
    ruins = comp_melhor[~comp_melhor["bate"]].dropna(subset=["razao"])
    if not ruins.empty:
        por_ticker = ruins.groupby("ticker").agg(
            contas_divergentes=("razao", "size"),
            razao_mediana=("razao", "median"),
            razao_desvio=("razao", "std"),
        )
        # razão parecida em >=3 contas do mesmo ticker = fator sistemático
        sistematicos = por_ticker[(por_ticker["contas_divergentes"] >= 3)
                                   & (por_ticker["razao_desvio"].fillna(0) < 0.05)]
        if not sistematicos.empty:
            print(sistematicos.sort_values("contas_divergentes", ascending=False).head(15).to_string())
            print(f"\n   -> {len(sistematicos)} tickers com fator sistemático. Se a razão estiver perto")
            print("      de 5-6, é BRL/USD (empresa reporta em dólar no Yahoo, em real na CVM).")
            print("      Nesses casos o dado da CVM é o correto para um sistema em BRL.")
        else:
            print("   nenhum fator sistemático claro encontrado.")
        tickers_sistematicos = set(sistematicos.index)
    else:
        tickers_sistematicos = set()

    print("\n2) Por setor (bancos/seguradoras usam plano de contas próprio na CVM):")
    por_setor = comp_melhor.groupby(comp_melhor["setor"].fillna("(sem info)")).agg(
        pares=("bate", "size"), concordancia_pct=("bate", lambda s: round(s.mean() * 100, 1)),
    ).sort_values("concordancia_pct")
    print(por_setor.head(8).to_string())

    # taxa "limpa": fora do setor financeiro e sem os casos de moeda/escala
    limpo = comp_melhor[
        (~comp_melhor["ticker"].isin(tickers_sistematicos))
        & (~comp_melhor["setor"].fillna("").isin(["Financial Services"]))
    ]
    if not limpo.empty:
        taxa_limpa = limpo["bate"].mean() * 100
        print(f"\n3) Concordância excluindo casos de moeda/escala e setor financeiro:")
        print(f"   {taxa_limpa:.1f}%  ({len(limpo)} pares, {limpo['ticker'].nunique()} tickers)")
        print("   -> este é o número que indica se o parsing em si está correto.")

    print("\n" + "=" * 78)
    if taxa >= 90:
        print("VEREDITO: alta concordância. A fonte é compatível - vale seguir para a Fase 2.")
    elif taxa >= 70:
        print("VEREDITO: concordância parcial. Veja o diagnóstico acima: se as divergências se")
        print("concentram em moeda estrangeira e setor financeiro, são casos tratáveis na Fase 2")
        print("(converter moeda; plano de contas específico para bancos) - não impedimento.")
    else:
        print("VEREDITO: concordância baixa. NÃO seguir para a Fase 2 antes de entender a causa -")
        print("pode ser erro de parsing (escala, ORDEM_EXERC) ou casamento errado de empresas.")
    print("=" * 78)


if __name__ == "__main__":
    main()