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
import io
import os
import re
import zipfile

import pandas as pd
import requests
from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

URL_DFP = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip"
URL_ITR = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_{ano}.zip"

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
    "2.03": "Total Equity Gross Minority Interest",
    "2.03.09": "Minority Interest",
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
    "3.11": "Net Income Including Noncontrolling Interests",
    "3.11.01": "Net Income",  # atribuível aos controladores - variante vencedora na Fase 1
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
    "2.07": "Total Equity Gross Minority Interest",
    "2.07.01": "Stockholders Equity",   # já vem pronto, não precisa subtrair
    "2.07.02": "Minority Interest",
}
CONTAS_DRE_BANCO = {
    "3.01": "Total Revenue",            # Receitas de Intermediação Financeira
    "3.02": "Cost Of Revenue",          # Despesas de Intermediação Financeira
    "3.03": "Gross Profit",             # Resultado Bruto de Intermediação
    "3.04": "Operating Expense",
    "3.05": "Pretax Income",            # atenção: NÃO é EBIT como no padrão
    "3.06": "Tax Provision",
    "3.11": "Net Income Including Noncontrolling Interests",
    "3.11.01": "Net Income",
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

    A detecção é ESTRUTURAL, não por setor: procura quem declara a conta 2.07
    descrita como Patrimônio Líquido. Usar o setor do Yahoo seria frágil - a
    B3 (bolsa) é classificada como 'Financial Services' mas usa o plano
    PADRÃO, e aplicar o plano de banco nela geraria dados errados."""
    df = ler_csv(zf, f"{prefixo}_cia_aberta_BPP_con_{ano}.csv")
    if df.empty or "DS_CONTA" not in df.columns:
        return set()
    marca = df[
        (df["CD_CONTA"].str.strip() == "2.07")
        & (df["DS_CONTA"].astype(str).str.contains("Patrim", case=False, na=False))
    ]
    return set(marca["CD_CVM"].astype(str).str.strip())

# CapEx: sem código fixo na CVM, procuramos por descrição em subcontas de 6.02
PADRAO_CAPEX = re.compile(
    r"(?:aquisi\w*|compra\w*|adi\w*)\s+.*(?:imobilizado|ativo\s+imobilizado|intang)", re.IGNORECASE
)


def baixar_zip(url):
    print(f"  baixando {url.split('/')[-1]} ...", end=" ", flush=True)
    resp = requests.get(url, timeout=300)
    if resp.status_code == 404:
        print("não existe (pulando)")
        return None
    resp.raise_for_status()
    print(f"{len(resp.content) / 1_000_000:.1f} MB")
    return zipfile.ZipFile(io.BytesIO(resp.content))


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
            if debug and grupo == "padrão":
                print(f"    [{sigla}] arquivo={nome_arq!r} existe_no_zip={nome_arq in zf.namelist()} linhas_brutas={len(bruto)}")
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
            if debug:
                print(f"    [{sigla}/{grupo}] {len(df)} linhas aproveitadas")

    if not frames:
        if debug:
            print("    nenhum demonstrativo produziu linhas - retornando vazio")
        return []

    dados = pd.concat(frames, ignore_index=True)
    if debug:
        print(f"    total após concat: {len(dados)} linhas, {dados['CD_CVM'].nunique()} empresas")

    # Patrimônio Líquido dos controladores = total - minoritários.
    # Só para o plano PADRÃO: bancos já declaram esse valor pronto na conta
    # 2.07.01, então derivar de novo sobrescreveria o dado correto por um
    # cálculo redundante (e potencialmente diferente por arredondamento).
    piv = dados[dados["statement"] == "balance_sheet"].pivot_table(
        index=["CD_CVM", "DT_REFER", "DT_FIM_EXERC"], columns="line_item",
        values="VL_CONTA", aggfunc="first").reset_index()
    if "Total Equity Gross Minority Interest" in piv.columns:
        ja_tem = (piv["Stockholders Equity"].notna()
                  if "Stockholders Equity" in piv.columns
                  else pd.Series(False, index=piv.index))
        minor = piv["Minority Interest"] if "Minority Interest" in piv.columns else 0
        derivado = (piv["Total Equity Gross Minority Interest"]
                    - pd.to_numeric(minor, errors="coerce").fillna(0))
        extra = piv.loc[~ja_tem, ["CD_CVM", "DT_REFER", "DT_FIM_EXERC"]].copy()
        extra["VL_CONTA"] = derivado[~ja_tem]
        extra["line_item"] = "Stockholders Equity"
        extra["statement"] = "balance_sheet"
        extra = extra.dropna(subset=["VL_CONTA"])
        if not extra.empty:
            dados = pd.concat([dados, extra], ignore_index=True)
        if debug:
            print(f"    Stockholders Equity: {int(ja_tem.sum())} vieram prontos (bancos), "
                  f"{len(extra)} derivados (plano padrão)")

    # Passivo total = circulante + não circulante
    if {"Current Liabilities", "Total Non Current Liabilities Net Minority Interest"} <= set(piv.columns):
        tot = piv[["CD_CVM", "DT_REFER", "DT_FIM_EXERC"]].copy()
        tot["VL_CONTA"] = (pd.to_numeric(piv["Current Liabilities"], errors="coerce").fillna(0)
                            + pd.to_numeric(piv["Total Non Current Liabilities Net Minority Interest"],
                                            errors="coerce").fillna(0))
        tot["line_item"] = "Total Liabilities Net Minority Interest"
        tot["statement"] = "balance_sheet"
        dados = pd.concat([dados, tot], ignore_index=True)

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


def gravar(engine, linhas):
    if not linhas:
        return 0
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
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
        zf = baixar_zip(URL_DFP.format(ano=ano))
        if zf:
            linhas = processar_ano(zf, ano, "dfp", "annual", mapa, debug=args.debug)
            print(f"  DFP: {len(linhas)} linhas", end="")
            if not args.dry_run:
                gravar(engine, linhas)
                print(" (gravadas)")
            else:
                print(" (dry-run, não gravadas)")
            total += len(linhas)

        if args.trimestral:
            zfi = baixar_zip(URL_ITR.format(ano=ano))
            if zfi:
                linhas = processar_ano(zfi, ano, "itr", "quarterly", mapa, debug=args.debug)
                print(f"  ITR: {len(linhas)} linhas", end="")
                if not args.dry_run:
                    gravar(engine, linhas)
                    print(" (gravadas)")
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