"""
CVM - FASE 2/3: ingestor histórico completo.

Baixa os demonstrativos da CVM (DFP anual desde 2010, ITR trimestral desde
2011), traduz o plano de contas da CVM para os nomes que o resto do sistema
já usa (herdados do Yahoo), e grava na tabela `financials`.

O QUE ISTO RESOLVE
------------------
- Histórico: de ~3-4 anos (limite do Yahoo) para 15+ anos.
- Point-in-time REAL: a CVM informa a data de entrega do documento
  (DT_RECEB), então `published_date` deixa de ser estimativa.
- Qualidade: onde CVM e Yahoo divergem, a CVM é a fonte oficial. Isso
  corrige, entre outros, empresas que o Yahoo reporta em dólar rotulando
  como real (Vale, Embraer).

DECISÕES DE PROJETO
-------------------
1. Onde há sobreposição, a CVM SOBRESCREVE o Yahoo. A coluna `source`
   registra a origem de cada linha. O código de leitura não muda.
2. Usamos sempre os demonstrativos CONSOLIDADOS (_con_), que equivalem ao
   que o Yahoo reporta. Empresas que só publicam individual ficam de fora.
3. Patrimônio Líquido e Lucro Líquido usam a variante ATRIBUÍVEL AOS
   CONTROLADORES (validado na Fase 1: 91,6% e 80,2% de concordância, contra
   62,6% e 63,0% da variante consolidada).
4. Uma empresa pode ter vários tickers (PETR3/PETR4). Os fundamentos são da
   empresa, então cada ticker mapeado recebe uma cópia.

LIMITAÇÃO CONHECIDA: CapEx
--------------------------
"Capital Expenditure" não tem código fixo na CVM - fica em subcontas de 6.02
cujo nome varia por empresa. Este ingestor tenta localizar por descrição
("aquisição de imobilizado", "aquisições de imobilizado"...), mas a cobertura
é parcial. Isso afeta o FCF Yield. Onde não encontrar, o campo simplesmente
não é gravado e o Yahoo continua sendo a fonte para aquele item.

Uso:
    DATABASE_URL="..." python cvm_ingestor.py --de 2010 --ate 2024
    DATABASE_URL="..." python cvm_ingestor.py --de 2024 --ate 2024 --dry-run
"""
import argparse
import os
import re

import warnings

import pandas as pd

# Os CSVs da CVM têm colunas de tipo misto e são lidos em fatias, o que gera
# DtypeWarning/SettingWithCopyWarning a cada arquivo. São previsíveis e já
# tratados (dtype explícito em CD_CONTA/CD_CVM, .copy() antes de atribuir) -
# silenciar aqui evita que centenas de avisos escondam a saída útil.
# filtra por MENSAGEM em vez de por classe: SettingWithCopyWarning existe em
# pandas 2.x mas foi removida em 3.x - referenciar a classe quebraria o import
# dependendo da versão instalada.
warnings.filterwarnings("ignore", category=pd.errors.DtypeWarning)
warnings.filterwarnings("ignore", message=".*value is trying to be set on a copy.*")
warnings.filterwarnings("ignore", message=".*only bool and object.*")
warnings.filterwarnings("ignore", message=".*invalid value encountered.*")
from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

import cvm_fonte

# URLs e cache local ficam em cvm_fonte.py - o servidor da CVM bloqueia
# conexões vindas do Codespace, então os arquivos são baixados pelo
# navegador e lidos de dados_cvm/. Ver ATUALIZACAO_MANUAL_CVM.md

ESCALA = {"UNIDADE": 1, "MIL": 1_000, "MILHAR": 1_000, "MILHÃO": 1_000_000, "MILHAO": 1_000_000}

# ---------------------------------------------------------------
# DE-PARA: código de conta da CVM -> nome usado no resto do sistema
# ---------------------------------------------------------------
# Balanço Patrimonial Ativo
CONTAS_BPA = {
    "1": "Total Assets",
    "1.01": "Current Assets",
    "1.01.01": "Cash And Cash Equivalents",
    "1.01.02": "Other Short Term Investments",
    "1.01.03": "Accounts Receivable",
    "1.01.04": "Inventory",
    "1.02": "Total Non Current Assets",
    "1.02.03": "Net PPE",
    "1.02.04": "Goodwill And Other Intangible Assets",
}
# Balanço Patrimonial Passivo
CONTAS_BPP = {
    "2.01": "Current Liabilities",
    "2.01.04": "Current Debt",
    "2.02": "Total Non Current Liabilities Net Minority Interest",
    "2.02.01": "Long Term Debt",
}
# Demonstração do Resultado
CONTAS_DRE = {
    "3.01": "Total Revenue",
    "3.02": "Cost Of Revenue",
    "3.03": "Gross Profit",
    "3.04": "Operating Expense",
    "3.05": "EBIT",
    "3.06": "Net Non Operating Interest Income Expense",
    "3.07": "Pretax Income",
    "3.08": "Tax Provision",
}
# Fluxo de Caixa (método indireto)
CONTAS_DFC = {
    "6.01": "Operating Cash Flow",
    "6.02": "Investing Cash Flow",
    "6.03": "Financing Cash Flow",
}

DEMONSTRATIVOS = [
    ("BPA", CONTAS_BPA, "balance_sheet"),
    ("BPP", CONTAS_BPP, "balance_sheet"),
    ("DRE", CONTAS_DRE, "income_statement"),
    ("DFC_MI", CONTAS_DFC, "cashflow"),
]

# ---------------------------------------------------------------
# DE-PARA ALTERNATIVO: instituições financeiras
# ---------------------------------------------------------------
# Bancos usam um plano de contas DIFERENTE na CVM, e o mesmo código significa
# coisas opostas. Descoberto empiricamente (cvm_descobrir_plano_bancos.py),
# comparando Bradesco (CD_CVM 906) com empresas do plano padrão:
#
#   código   plano padrão              plano de banco
#   1.01     Ativo Circulante          Caixa e Equivalentes
#   1.02     Ativo Não Circulante      Ativos Financeiros
#   2.01     Passivo Circulante        Passivos Financeiros a Valor Justo
#   2.03     PATRIMÔNIO LÍQUIDO        PROVISÕES              <- o mais perigoso
#   2.07     (não existe)              Patrimônio Líquido
#   3.05     EBIT                      Resultado antes dos Tributos
#   3.06     Resultado Financeiro      Imposto de Renda
#
# Sem esta separação, o PL do Bradesco entraria como R$ 402 bi (as provisões)
# em vez dos R$ 168 bi reais - erro de 140% com número plausível.
CONTAS_BPA_BANCO = {
    "1": "Total Assets",
    "1.01": "Cash And Cash Equivalents",
    "1.06": "Net PPE",
    "1.07": "Goodwill And Other Intangible Assets",
    # Bancos NÃO classificam ativo por liquidez - não existe "Ativo
    # Circulante". Deixar Current Assets ausente é correto: melhor NULL do
    # que um número que não significa o que o nome diz.
}
CONTAS_BPP_BANCO = {
}
CONTAS_DRE_BANCO = {
    "3.01": "Total Revenue",            # Receitas de Intermediação Financeira
    "3.02": "Cost Of Revenue",          # Despesas de Intermediação Financeira
    "3.03": "Gross Profit",             # Resultado Bruto de Intermediação
    "3.04": "Operating Expense",
    "3.05": "Pretax Income",            # atenção: NÃO é EBIT como no padrão
    "3.06": "Tax Provision",
}
# O fluxo de caixa é idêntico nos dois planos (6.01/6.02/6.03).
CONTAS_DFC_BANCO = dict(CONTAS_DFC)

DEMONSTRATIVOS_BANCO = [
    ("BPA", CONTAS_BPA_BANCO, "balance_sheet"),
    ("BPP", CONTAS_BPP_BANCO, "balance_sheet"),
    ("DRE", CONTAS_DRE_BANCO, "income_statement"),
    ("DFC_MI", CONTAS_DFC_BANCO, "cashflow"),
]


def detectar_empresas_plano_banco(zf, ano, prefixo):
    """Quais CD_CVM usam o plano de contas de instituição financeira.

    Detecta pela AUSÊNCIA de "Ativo Circulante" no BPA. Instituições
    financeiras não classificam o balanço por liquidez (circulante x não
    circulante) - essa é a assinatura estrutural do plano delas.

    A versão anterior procurava a conta 2.07 descrita como Patrimônio
    Líquido. Isso falhava para BTG, Itaú, BMG e Pine, que declaram o
    patrimônio em 2.08 (existem DUAS variantes do plano de banco). Como
    consequência, esses quatro caíam no plano padrão e liam contas erradas -
    o patrimônio do Itaú aparecia como R$ 2,1 trilhões em vez de R$ 211 bi.

    Detectar pela ausência de Ativo Circulante pega as duas variantes, e
    qualquer outra que apareça.

    Não usar o setor do Yahoo: a B3 (bolsa) é 'Financial Services' lá, mas
    usa o plano PADRÃO - aplicar o de banco nela geraria dados errados."""
    bpa = ler_csv(zf, f"{prefixo}_cia_aberta_BPA_con_{ano}.csv")
    if bpa.empty or "DS_CONTA" not in bpa.columns:
        return set()
    bpa = bpa[bpa["ORDEM_EXERC"] == "ÚLTIMO"] if "ORDEM_EXERC" in bpa.columns else bpa

    todas = set(bpa["CD_CVM"].astype(str).str.strip())
    com_circulante = set(bpa[
        (bpa["CD_CONTA"].str.strip() == "1.01")
        & (bpa["DS_CONTA"].astype(str).str.contains(r"ativo\s+circulante", case=False, na=False, regex=True))
    ]["CD_CVM"].astype(str).str.strip())
    return todas - com_circulante

# CapEx: sem código fixo na CVM, procuramos por descrição em subcontas de 6.02
# Localização por DESCRIÇÃO (ver _localizar_por_descricao). O mesmo conceito
# fica em códigos diferentes conforme o plano de contas da empresa.
RE_PL_TOTAL = re.compile(r"patrim[oô]nio\s+l[ií]quido\s+consolidado", re.IGNORECASE)
RE_LUCRO = re.compile(r"(lucro|preju[ií]zo).*consolidad", re.IGNORECASE)
RE_POR_ACAO = re.compile(r"por\s+a[cç][aã]o", re.IGNORECASE)
RE_NAO_CONTROLADOR = re.compile(r"n[aã]o\s+controlador", re.IGNORECASE)
RE_CONTROLADOR = re.compile(r"controlador", re.IGNORECASE)

PADRAO_CAPEX = re.compile(
    r"(?:aquisi\w*|compra\w*|adi\w*)\s+.*(?:imobilizado|ativo\s+imobilizado|intang)", re.IGNORECASE
)


def ler_csv(zf, nome):
    if nome not in zf.namelist():
        return pd.DataFrame()
    with zf.open(nome) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", decimal=".",
                          dtype={"CD_CONTA": str, "CNPJ_CIA": str, "CD_CVM": str})
    if "CD_CVM" in df.columns:
        # os arquivos detalhados (BPA/BPP/DRE/DFC) trazem o código CVM com
        # zeros à esquerda ("001023"); o arquivo resumo e a tabela de
        # mapeamento não ("1023") - normalizando aqui, uma vez só, evita que
        # todo o resto do pipeline precise se preocupar com isso.
        df["CD_CVM"] = df["CD_CVM"].str.strip().str.lstrip("0")
        df.loc[df["CD_CVM"] == "", "CD_CVM"] = "0"
    return df


def preparar(df):
    """Aplica os três tratamentos obrigatórios: exercício corrente, versão mais
    recente (reapresentações) e normalização de escala monetária."""
    if df.empty:
        return df
    if "ORDEM_EXERC" in df.columns:
        df = df[df["ORDEM_EXERC"] == "ÚLTIMO"].copy()
    if "CD_CONTA" in df.columns:
        df["CD_CONTA"] = df["CD_CONTA"].str.strip()
    if "VERSAO" in df.columns and not df.empty:
        df["VERSAO"] = pd.to_numeric(df["VERSAO"], errors="coerce")
        chaves = [c for c in ["CNPJ_CIA", "DT_FIM_EXERC", "CD_CONTA"] if c in df.columns]
        if chaves:
            df = df.sort_values("VERSAO").drop_duplicates(chaves, keep="last")
    if "ESCALA_MOEDA" in df.columns:
        fator = df["ESCALA_MOEDA"].str.upper().map(ESCALA).fillna(1)
        df["VL_CONTA"] = pd.to_numeric(df["VL_CONTA"], errors="coerce") * fator
    return df


def datas_publicacao(zf, ano, prefixo):
    """CD_CVM -> data de entrega do documento (point-in-time de verdade)."""
    df = ler_csv(zf, f"{prefixo}_cia_aberta_{ano}.csv")
    if df.empty:
        return {}
    col_data = next((c for c in ["DT_RECEB", "DT_RECEBIMENTO"] if c in df.columns), None)
    if not col_data or "CD_CVM" not in df.columns:
        return {}
    df = df.dropna(subset=[col_data])
    df["_d"] = pd.to_datetime(df[col_data], errors="coerce")
    df = df.dropna(subset=["_d"]).sort_values("_d")
    chave = ["CD_CVM", "DT_REFER"] if "DT_REFER" in df.columns else ["CD_CVM"]
    ultimo = df.drop_duplicates(chave, keep="last")
    if "DT_REFER" in df.columns:
        return {(r["CD_CVM"], str(r["DT_REFER"])[:10]): r["_d"].date() for _, r in ultimo.iterrows()}
    return {r["CD_CVM"]: r["_d"].date() for _, r in ultimo.iterrows()}


def extrair_capex(zf, ano, prefixo):
    """Tenta achar CapEx nas subcontas de 6.02 por descrição."""
    df = preparar(ler_csv(zf, f"{prefixo}_cia_aberta_DFC_MI_con_{ano}.csv"))
    if df.empty or "DS_CONTA" not in df.columns:
        return pd.DataFrame()
    sub = df[df["CD_CONTA"].str.startswith("6.02", na=False)].copy()
    sub = sub[sub["DS_CONTA"].astype(str).str.contains(PADRAO_CAPEX, na=False)]
    if sub.empty:
        return pd.DataFrame()
    agg = sub.groupby(["CD_CVM", "DT_FIM_EXERC"], as_index=False)["VL_CONTA"].sum()
    agg["line_item"] = "Capital Expenditure"
    return agg


def acoes_em_circulacao(zf, ano, prefixo):
    """Número de ações, do arquivo de composição de capital."""
    df = ler_csv(zf, f"{prefixo}_cia_aberta_composicao_capital_{ano}.csv")
    if df.empty:
        return pd.DataFrame()
    col_qtd = next((c for c in df.columns if "QTD" in c.upper() and "ACAO" in c.upper()
                     and "TESOU" not in c.upper()), None)
    if not col_qtd or "CD_CVM" not in df.columns:
        return pd.DataFrame()
    df[col_qtd] = pd.to_numeric(df[col_qtd], errors="coerce")
    col_data = "DT_FIM_EXERC" if "DT_FIM_EXERC" in df.columns else "DT_REFER"
    agg = df.groupby(["CD_CVM", col_data], as_index=False)[col_qtd].sum()
    agg = agg.rename(columns={col_qtd: "VL_CONTA", col_data: "DT_FIM_EXERC"})
    agg["line_item"] = "Ordinary Shares Number"
    return agg


def _localizar_por_descricao(g, re_pai, re_excluir=None):
    """Localiza um conceito pela DESCRIÇÃO da conta, não pelo código.

    POR QUE: o mesmo conceito fica em códigos diferentes conforme o plano.
    Patrimônio Líquido aparece em 2.03 (padrão), 2.07 (banco variante A) ou
    2.08 (banco variante B); Lucro Líquido em 3.09, 3.11 ou 3.13. Fixar
    códigos exigiria enumerar todas as variantes - e uma nova quebraria
    silenciosamente. A descrição, essa, a CVM mantém padronizada.

    Devolve (valor_dos_controladores, codigo_raiz, como_obtido)."""
    cand = g[g["DS_CONTA"].str.contains(re_pai, na=False)]
    if re_excluir is not None and not cand.empty:
        cand = cand[~cand["DS_CONTA"].str.contains(re_excluir, na=False)]
    if cand.empty:
        return None, None, "não encontrado"

    # a raiz é a de menor profundidade (2.03 antes de 2.03.01)
    pai = cand.loc[cand["CD_CONTA"].str.count(r"\.").idxmin()]
    raiz, total = pai["CD_CONTA"], pai["VL_CONTA"]

    # SÓ filhas diretas: uma conta neta cuja descrição menciona "controlador"
    # (ex: 2.03.02.09 "Opções Outorgadas a Não Controladores") seria escolhida
    # por engano, quebrando empresas hoje corretas (TOTS3, EMBJ3, PASS3).
    filhas = g[g["CD_CONTA"].str.startswith(raiz + ".")
               & (g["CD_CONTA"].str.count(r"\.") == raiz.count(".") + 1)]

    # 1ª opção: filha que já traz o valor dos controladores pronto
    contr = filhas[filhas["DS_CONTA"].str.contains(RE_CONTROLADOR, na=False)
                   & ~filhas["DS_CONTA"].str.contains(RE_NAO_CONTROLADOR, na=False)]
    if not contr.empty:
        return float(contr.iloc[0]["VL_CONTA"]), raiz, f"direto:{contr.iloc[0]['CD_CONTA']}"

    # 2ª opção: total menos a participação dos não controladores
    nao = filhas[filhas["DS_CONTA"].str.contains(RE_NAO_CONTROLADOR, na=False)]
    minor = float(nao.iloc[0]["VL_CONTA"]) if not nao.empty else 0.0
    return float(total) - minor, raiz, f"{raiz}-minoritarios"


def extrair_pl_e_lucro(zf, ano, prefixo):
    """Patrimônio Líquido e Lucro Líquido (atribuíveis aos controladores),
    localizados por descrição - funciona em qualquer variante de plano."""
    linhas = []
    for sigla, statement, re_pai, re_excl, nome_total, nome_contr in [
        ("BPP", "balance_sheet", RE_PL_TOTAL, None,
         "Total Equity Gross Minority Interest", "Stockholders Equity"),
        ("DRE", "income_statement", RE_LUCRO, RE_POR_ACAO,
         "Net Income Including Noncontrolling Interests", "Net Income"),
    ]:
        df = preparar(ler_csv(zf, f"{prefixo}_cia_aberta_{sigla}_con_{ano}.csv"))
        if df.empty or "DS_CONTA" not in df.columns:
            continue
        df["DS_CONTA"] = df["DS_CONTA"].astype(str)
        for (cd, dt_ref, dt_fim), g in df.groupby(["CD_CVM", "DT_REFER", "DT_FIM_EXERC"]):
            valor, raiz, _ = _localizar_por_descricao(g, re_pai, re_excl)
            if valor is None:
                continue
            total = g[g["CD_CONTA"] == raiz]["VL_CONTA"]
            if not total.empty and pd.notna(total.iloc[0]):
                linhas.append({"CD_CVM": cd, "DT_REFER": dt_ref, "DT_FIM_EXERC": dt_fim,
                               "statement": statement, "line_item": nome_total,
                               "VL_CONTA": float(total.iloc[0])})
            linhas.append({"CD_CVM": cd, "DT_REFER": dt_ref, "DT_FIM_EXERC": dt_fim,
                           "statement": statement, "line_item": nome_contr,
                           "VL_CONTA": valor})
    return pd.DataFrame(linhas)


def processar_ano(zf, ano, prefixo, period_type, mapa_ticker, debug=False):
    """Devolve as linhas prontas para inserir em `financials`."""
    pubs = datas_publicacao(zf, ano, prefixo)
    bancos = detectar_empresas_plano_banco(zf, ano, prefixo)
    if debug or bancos:
        print(f"    {len(bancos)} empresas usam o plano de contas de instituição financeira")
    frames = []

    for grupo, demonstrativos in [("padrão", DEMONSTRATIVOS), ("banco", DEMONSTRATIVOS_BANCO)]:
        for sigla, contas, statement in demonstrativos:
            nome_arq = f"{prefixo}_cia_aberta_{sigla}_con_{ano}.csv"
            bruto = ler_csv(zf, nome_arq)
            df = preparar(bruto)
            if df.empty:
                continue

            # cada empresa é processada APENAS com o plano que ela de fato usa
            if grupo == "banco":
                df = df[df["CD_CVM"].astype(str).str.strip().isin(bancos)]
            else:
                df = df[~df["CD_CVM"].astype(str).str.strip().isin(bancos)]
            if df.empty:
                continue

            df = df[df["CD_CONTA"].isin(contas)].copy()
            if df.empty:
                continue
            df["line_item"] = df["CD_CONTA"].map(contas)
            df["statement"] = statement
            frames.append(df[["CD_CVM", "DT_REFER", "DT_FIM_EXERC", "statement", "line_item", "VL_CONTA"]])

    if not frames:
        if debug:
            print("    nenhum demonstrativo produziu linhas - retornando vazio")
        return []

    dados = pd.concat(frames, ignore_index=True)
    if debug:
        print(f"    total após concat: {len(dados)} linhas, {dados['CD_CVM'].nunique()} empresas")

    # Patrimônio Líquido e Lucro Líquido vêm por DESCRIÇÃO, não por código -
    # o mesmo conceito fica em 2.03/2.07/2.08 e 3.09/3.11/3.13 conforme o
    # plano de contas da empresa. Ver _localizar_por_descricao.
    pl_lucro = extrair_pl_e_lucro(zf, ano, prefixo)
    if not pl_lucro.empty:
        dados = pd.concat([dados, pl_lucro], ignore_index=True)
        if debug:
            n_pl = (pl_lucro["line_item"] == "Stockholders Equity").sum()
            n_li = (pl_lucro["line_item"] == "Net Income").sum()
            print(f"    por descrição: {n_pl} patrimônios, {n_li} lucros líquidos")

    piv = dados[dados["statement"] == "balance_sheet"].pivot_table(
        index=["CD_CVM", "DT_REFER", "DT_FIM_EXERC"], columns="line_item",
        values="VL_CONTA", aggfunc="first").reset_index()

    # Passivo total.
    # Plano PADRÃO: circulante + não circulante.
    # Plano de BANCO: essas contas não existem (bancos não classificam o
    # balanço por liquidez), então usamos a identidade contábil
    # Passivo = Ativo - Patrimônio Líquido.
    #
    # O código anterior somava as duas com .fillna(0) e, para bancos - onde
    # AMBAS faltam - gravava ZERO. O passivo do Banco do Brasil aparecia como
    # R$ 0 em vez de ~R$ 2,2 trilhões, o que quebraria qualquer indicador de
    # alavancagem (Debt/Equity, por exemplo).
    if {"Current Liabilities", "Total Non Current Liabilities Net Minority Interest"} <= set(piv.columns):
        cl = pd.to_numeric(piv["Current Liabilities"], errors="coerce")
        ncl = pd.to_numeric(piv["Total Non Current Liabilities Net Minority Interest"], errors="coerce")
        # só soma onde PELO MENOS UMA existe - se as duas faltam, o resultado
        # é NaN (desconhecido), não zero
        soma = cl.fillna(0) + ncl.fillna(0)
        soma = soma.where(cl.notna() | ncl.notna())
    else:
        soma = pd.Series(pd.NA, index=piv.index, dtype="float64")

    if {"Total Assets", "Stockholders Equity"} <= set(piv.columns):
        ta = pd.to_numeric(piv["Total Assets"], errors="coerce")
        se = pd.to_numeric(piv["Stockholders Equity"], errors="coerce")
        por_identidade = ta - se
    else:
        por_identidade = pd.Series(pd.NA, index=piv.index, dtype="float64")

    passivo = soma.fillna(por_identidade)
    if passivo.notna().any():
        tot = piv[["CD_CVM", "DT_REFER", "DT_FIM_EXERC"]].copy()
        tot["VL_CONTA"] = passivo
        tot["line_item"] = "Total Liabilities Net Minority Interest"
        tot["statement"] = "balance_sheet"
        tot = tot.dropna(subset=["VL_CONTA"])
        if not tot.empty:
            dados = pd.concat([dados, tot], ignore_index=True)
        if debug:
            print(f"    Passivo total: {int(soma.notna().sum())} por soma, "
                  f"{int((soma.isna() & por_identidade.notna()).sum())} por identidade contábil")

    # CapEx e número de ações (fontes auxiliares)
    capex = extrair_capex(zf, ano, prefixo)
    if not capex.empty:
        capex["statement"] = "cashflow"
        capex["DT_REFER"] = capex["DT_FIM_EXERC"]
        dados = pd.concat([dados, capex[["CD_CVM", "DT_REFER", "DT_FIM_EXERC",
                                          "statement", "line_item", "VL_CONTA"]]], ignore_index=True)
    acoes = acoes_em_circulacao(zf, ano, prefixo)
    if not acoes.empty:
        acoes["statement"] = "balance_sheet"
        acoes["DT_REFER"] = acoes["DT_FIM_EXERC"]
        dados = pd.concat([dados, acoes[["CD_CVM", "DT_REFER", "DT_FIM_EXERC",
                                          "statement", "line_item", "VL_CONTA"]]], ignore_index=True)

    dados = dados.dropna(subset=["VL_CONTA"])

    # Descarta empresa/exercício em que TODOS os valores são zero. Uma empresa
    # com ativo, patrimônio, receita e lucro todos exatamente 0 não é uma
    # empresa sem valor - é um documento entregue sem dados (empresa em
    # liquidação, ou entrega apenas formal). Gravar zeros seria pior que não
    # gravar: eles entrariam nos cálculos como se fossem informação real,
    # distorcendo percentis e rankings.
    if not dados.empty:
        chave = ["CD_CVM", "DT_FIM_EXERC"]
        tem_valor = dados.groupby(chave)["VL_CONTA"].transform(lambda s: (s != 0).any())
        n_descartados = int((~tem_valor).sum())
        if n_descartados:
            empresas_zeradas = dados.loc[~tem_valor, "CD_CVM"].nunique()
            print(f"    descartando {n_descartados} linhas de {empresas_zeradas} empresa(s) "
                  f"com TODOS os valores zerados (documento sem dados)")
        dados = dados[tem_valor]
    if debug:
        print(f"    após dropna(VL_CONTA): {len(dados)} linhas")
    dados["fiscal_date"] = pd.to_datetime(dados["DT_FIM_EXERC"], errors="coerce").dt.date
    antes = len(dados)
    dados = dados.dropna(subset=["fiscal_date"])
    if debug:
        print(f"    após parse de fiscal_date: {len(dados)} linhas (de {antes} - "
              f"{antes - len(dados)} tinham DT_FIM_EXERC ilegível)")

    # explode empresa -> tickers (PETR3 e PETR4 compartilham os fundamentos)
    linhas = []
    cd_cvm_sem_mapa = set()
    for _, r in dados.iterrows():
        cd = str(r["CD_CVM"]).strip()
        tickers = mapa_ticker.get(cd)
        if not tickers:
            cd_cvm_sem_mapa.add(cd)
            continue
        pub = pubs.get((cd, str(r["DT_REFER"])[:10])) or pubs.get(cd)
        for tk in tickers:
            linhas.append((tk, r["statement"], period_type, r["fiscal_date"],
                            r["line_item"], float(r["VL_CONTA"]), pub, "cvm"))
    if debug:
        cds_no_dados = set(str(c).strip() for c in dados["CD_CVM"].unique())
        cds_no_mapa = set(mapa_ticker.keys())
        print(f"    códigos CVM nos dados: {len(cds_no_dados)}  |  códigos CVM no mapa: {len(cds_no_mapa)}")
        print(f"    intersecção (deveriam gerar linhas): {len(cds_no_dados & cds_no_mapa)}")
        if cd_cvm_sem_mapa:
            exemplos = sorted(cd_cvm_sem_mapa)[:5]
            print(f"    {len(cd_cvm_sem_mapa)} códigos CVM nos dados SEM mapeamento - exemplos: {exemplos}")
        exemplos_mapa = sorted(cds_no_mapa)[:5]
        print(f"    exemplos de códigos que ESTÃO no mapa: {exemplos_mapa}")
    return linhas


def gravar(engine, linhas, ano=None, period_type=None):
    """Grava as linhas, substituindo por completo o que já existia da CVM
    naquele ano/periodicidade.

    POR QUE APAGAR ANTES: o ON CONFLICT DO UPDATE só toca nas linhas que o
    código ESCREVE. Linhas gravadas por uma versão anterior - e que o código
    atual não produz mais - ficariam órfãs no banco, com valores errados.

    Foi o que aconteceu quando o BTG era tratado pelo plano padrão: gravou-se
    Current Assets, EBIT e afins, contas que não existem no plano de banco.
    Depois de corrigir a detecção, essas linhas continuaram lá e faziam o BTG
    ainda parecer uma empresa comum nas validações.

    Apagar o ano antes torna cada carga uma substituição limpa - e o problema
    não volta em nenhuma recarga futura."""
    if not linhas:
        return 0
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            if ano is not None:
                cur.execute("""
                    DELETE FROM financials
                    WHERE source = 'cvm'
                      AND EXTRACT(YEAR FROM fiscal_date) = %s
                      AND (%s IS NULL OR period_type = %s)
                """, (ano, period_type, period_type))
                if cur.rowcount:
                    print(f"    (removidas {cur.rowcount} linhas anteriores de {ano} para recarga limpa)")
            execute_values(cur, """
                INSERT INTO financials
                    (ticker, statement, period_type, fiscal_date, line_item, value, published_date, source)
                VALUES %s
                ON CONFLICT (ticker, statement, period_type, fiscal_date, line_item)
                DO UPDATE SET value = EXCLUDED.value,
                              published_date = COALESCE(EXCLUDED.published_date, financials.published_date),
                              source = EXCLUDED.source,
                              fetched_at = now()
            """, linhas, page_size=1000)
        conn.commit()
    finally:
        conn.close()
    return len(linhas)


def carregar_mapa_ticker(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker, cd_cvm FROM ticker_cvm_map")).fetchall()
    mapa = {}
    for ticker, cd in rows:
        mapa.setdefault(str(cd).strip(), []).append(ticker)
    return mapa


def main():
    p = argparse.ArgumentParser(description="Ingestor histórico da CVM.")
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2024)
    p.add_argument("--trimestral", action="store_true", help="Também ingere ITR (trimestral).")
    p.add_argument("--dry-run", action="store_true", help="Processa mas não grava no banco.")
    p.add_argument("--debug", action="store_true", help="Mostra diagnóstico detalhado de cada etapa do processamento.")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    mapa = carregar_mapa_ticker(engine)
    if not mapa:
        raise SystemExit("Tabela ticker_cvm_map vazia - rode cvm_mapear_tickers.py primeiro.")
    print(f"{len(mapa)} empresas mapeadas, cobrindo {sum(len(v) for v in mapa.values())} tickers.\n")

    total = 0
    for ano in range(args.de, args.ate + 1):
        print(f"--- {ano} ---")
        zf = cvm_fonte.obter_dfp(ano)
        if zf:
            linhas = processar_ano(zf, ano, "dfp", "annual", mapa, debug=args.debug)
            print(f"  DFP: {len(linhas)} linhas")
            if not args.dry_run:
                gravar(engine, linhas, ano=ano, period_type="annual")
                print("    (gravadas)")
            else:
                print(" (dry-run, não gravadas)")
            total += len(linhas)

        if args.trimestral:
            zfi = cvm_fonte.obter_itr(ano)
            if zfi:
                linhas = processar_ano(zfi, ano, "itr", "quarterly", mapa, debug=args.debug)
                print(f"  ITR: {len(linhas)} linhas")
                if not args.dry_run:
                    gravar(engine, linhas, ano=ano, period_type="quarterly")
                    print("    (gravadas)")
                else:
                    print(" (dry-run)")
                total += len(linhas)

    print(f"\nTotal: {total} linhas processadas.")
    if not args.dry_run:
        with engine.connect() as conn:
            r = conn.execute(text("""
                SELECT source, COUNT(*), MIN(fiscal_date), MAX(fiscal_date)
                FROM financials GROUP BY source ORDER BY COUNT(*) DESC
            """)).fetchall()
        print("\nComposição da tabela financials agora:")
        for src, n, dmin, dmax in r:
            print(f"  {src or '(yahoo, antigo)':<20} {n:>8} linhas   {dmin} a {dmax}")


if __name__ == "__main__":
    main()