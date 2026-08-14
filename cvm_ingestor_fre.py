"""
Ingestor do FRE - quantidade de ações por classe.

O QUE RESOLVE
-------------
Três problemas de uma vez (itens 1, 2 e 3 do PENDENCIAS.md):

1. AUSÊNCIA DE DADOS ANTES DE 2020. A CVM só publica `composicao_capital` a
   partir de 2020. Sem número de ações não há valor de mercado, e sem valor
   de mercado não existem P/L, P/VPA, Earnings Yield nem FCF Yield - todo o
   lado de valuation do screener. O FRE cobre 2010-2025.

2. ESCALA INCONSISTENTE. O `composicao_capital` mistura unidades sem avisar:
   no mesmo arquivo de 2024, Banco do Brasil aparece com 5.730.834.040
   (unidades) e Lojas Renner com 1.059.550 (milhares). O FRE foi verificado
   em 2010, 2015, 2020 e 2024 e é consistente.

3. VALOR DE MERCADO DE UNITS E DE EMPRESAS COM DUAS CLASSES. O FRE separa
   ordinárias de preferenciais, o que permite calcular
   `market_cap = (qtd_ON x preço_ON) + (qtd_PN x preço_PN)` em vez de
   multiplicar o total por um preço só.

DECISÕES DE PROJETO
-------------------
- Usa APENAS `Tipo_Capital = 'Capital Integralizado'`. O arquivo traz quatro
  tipos e a diferença é grande: a Vale tem 5,37 bi de integralizado contra
  10,8 bi de AUTORIZADO - que é um teto estatutário, não ações existentes.
- Deduplica por versão: o arquivo repete a mesma empresa várias vezes.
- Grava com `source = 'cvm_fre'`, separado do resto, para permitir comparar
  com o que veio de `composicao_capital` antes de descartá-lo.

Uso:
    DATABASE_URL="..." python cvm_ingestor_fre.py --de 2010 --ate 2025 --dry-run
    DATABASE_URL="..." python cvm_ingestor_fre.py --de 2010 --ate 2025
"""
import argparse
import os
import warnings
import zipfile

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

import cvm_fonte

warnings.filterwarnings("ignore", category=pd.errors.DtypeWarning)

PASTA_CACHE = "dados_cvm"
TIPO_CAPITAL = "Capital Integralizado"

COLUNAS = {
    "Quantidade_Acoes_Ordinarias": "Ordinary Shares Number",
    "Quantidade_Acoes_Preferenciais": "Preferred Shares Number",
    "Quantidade_Total_Acoes": "Total Shares Number",
}


def abrir_fre(ano):
    caminho = os.path.join(PASTA_CACHE, f"fre_cia_aberta_{ano}.zip")
    if not os.path.exists(caminho):
        return None
    return zipfile.ZipFile(caminho)


def ler_capital_social(ano):
    zf = abrir_fre(ano)
    if zf is None:
        print(f"  {ano}: arquivo FRE não encontrado em {PASTA_CACHE}/")
        return pd.DataFrame()
    nome = f"fre_cia_aberta_capital_social_{ano}.csv"
    if nome not in zf.namelist():
        print(f"  {ano}: sem capital_social no zip")
        return pd.DataFrame()

    with zf.open(nome) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)

    if "Tipo_Capital" not in df.columns:
        print(f"  {ano}: coluna Tipo_Capital ausente - colunas: {list(df.columns)}")
        return pd.DataFrame()

    df = df[df["Tipo_Capital"] == TIPO_CAPITAL].copy()
    if df.empty:
        print(f"  {ano}: nenhuma linha com Tipo_Capital = {TIPO_CAPITAL!r}")
        return df

    for c in COLUNAS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # versão mais recente por empresa/data (o arquivo repete linhas)
    if "Versao" in df.columns:
        df["Versao"] = pd.to_numeric(df["Versao"], errors="coerce")
        chaves = [c for c in ["CNPJ_Companhia", "Data_Referencia"] if c in df.columns]
        if chaves:
            df = df.sort_values("Versao").drop_duplicates(chaves, keep="last")

    df["_ano_arquivo"] = ano
    return df


def mapa_cnpj_cd_cvm(anos):
    """CNPJ -> CD_CVM, montado a partir dos arquivos DFP (que têm os dois).

    O FRE identifica empresas só por CNPJ; nossa `ticker_cvm_map` usa CD_CVM.
    Percorre vários anos porque uma empresa pode não estar em todos."""
    mapa = {}
    # o cadastro traz TODAS as empresas já registradas, inclusive as que
    # pararam de entregar DFP - cobre os poucos casos que os arquivos DFP
    # sozinhos deixariam sem CD_CVM
    cad = os.path.join(PASTA_CACHE, "cad_cia_aberta.csv")
    if os.path.exists(cad):
        d = pd.read_csv(cad, sep=";", encoding="latin-1", dtype=str)
        c_cnpj = next((c for c in d.columns if "CNPJ" in c.upper()), None)
        c_cd = next((c for c in d.columns if c.upper() in ("CD_CVM", "CODIGO_CVM")), None)
        if c_cnpj and c_cd:
            for cnpj, cd in zip(d[c_cnpj].astype(str).str.strip(),
                                 d[c_cd].astype(str).str.strip().str.lstrip("0")):
                mapa.setdefault(cnpj, cd)

    for ano in anos:
        try:
            zf = cvm_fonte.obter_dfp(ano, permitir_download=False)
        except (FileNotFoundError, SystemExit):
            continue
        if zf is None:
            continue
        nome = f"dfp_cia_aberta_{ano}.csv"
        if nome not in zf.namelist():
            continue
        with zf.open(nome) as f:
            d = pd.read_csv(f, sep=";", encoding="latin-1",
                             dtype={"CNPJ_CIA": str, "CD_CVM": str})
        if "CNPJ_CIA" not in d.columns or "CD_CVM" not in d.columns:
            continue
        d["CD_CVM"] = d["CD_CVM"].str.strip().str.lstrip("0")
        for cnpj, cd in zip(d["CNPJ_CIA"], d["CD_CVM"]):
            mapa.setdefault(str(cnpj).strip(), cd)
    return mapa


def carregar_tickers(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker, cd_cvm FROM ticker_cvm_map")).fetchall()
    por_cd = {}
    for ticker, cd in rows:
        por_cd.setdefault(str(cd).strip().lstrip("0"), []).append(ticker)
    return por_cd


def qualidade_referencia(data_referencia):
    """Quão direta é a associação entre a data de referência e o exercício.

    Serve de desempate quando DOIS arquivos descrevem o mesmo exercício. Sem
    isso, os poucos registros "atrasados" de um arquivo posterior (empresas
    com referência no meio do ano) sobrescrevem os ~650 registros do arquivo
    principal daquele exercício.

      2 = referência em 31/12  -> é o próprio fechamento (convenção nova)
      1 = referência em 01/01  -> fechamento do ano anterior (convenção antiga)
      0 = referência no meio do ano -> exercício inferido, menos confiável
    """
    d = pd.to_datetime(data_referencia, errors="coerce")
    if pd.isna(d):
        return 0
    if d.month == 12 and d.day == 31:
        return 2
    if d.month == 1 and d.day == 1:
        return 1
    return 0


def fiscal_date_de(data_referencia, ano_arquivo):
    """Exercício a que a posição de capital se refere.

    O FRE do ano N NÃO descreve o fechamento de N. A Data_Referencia varia por
    empresa (2021-01-01, 2021-02-28, 2021-05-01...) e indica a posição NAQUELE
    momento. Verificado com a Petrobras: o FRE 2021 traz Data_Referencia
    2021-01-01, ou seja, o fechamento de 2020.

    Regra: o exercício é o último 31/12 anterior ou igual à data de
    referência. Assim uma referência de janeiro/2021 vai para 2020-12-31, e
    uma de dezembro/2021 vai para 2021-12-31 - sem precisar de regra fixa por
    ano, que erraria dependendo do mês em que a empresa entregou."""
    d = pd.to_datetime(data_referencia, errors="coerce")
    if pd.isna(d):
        # sem data de referência, assume o fechamento do ano anterior ao
        # arquivo - o comportamento mais comum observado
        return pd.Timestamp(year=ano_arquivo - 1, month=12, day=31).date()
    ano_fiscal = d.year if (d.month == 12 and d.day == 31) else d.year - 1
    return pd.Timestamp(year=ano_fiscal, month=12, day=31).date()


def montar_linhas(df, cnpj_para_cd, cd_para_tickers, ano):
    """Uma linha por (ticker, classe de ação)."""
    linhas, sem_cd, sem_ticker = [], set(), set()
    datas_usadas = {}

    # DEDUPLICAÇÃO POR EXERCÍCIO, não por Data_Referencia.
    # Uma mesma empresa pode aparecer no arquivo com duas datas de referência
    # diferentes (ex: 2013-01-01 e 2013-06-30) que caem no MESMO exercício
    # fiscal. Sem isso, o INSERT recebe duas linhas com a mesma chave e o
    # Postgres recusa o lote inteiro (CardinalityViolation). Fica a referência
    # mais recente, que é a informação mais atualizada daquele exercício.
    df = df.copy()
    df["_fiscal_date"] = [fiscal_date_de(d, ano) for d in df.get("Data_Referencia", [None] * len(df))]
    ordem = [c for c in ["Versao", "Data_Referencia"] if c in df.columns]
    if ordem:
        df = df.sort_values(ordem)
    df = df.drop_duplicates(["CNPJ_Companhia", "_fiscal_date"], keep="last")

    for _, r in df.iterrows():
        fiscal_date = r["_fiscal_date"]
        datas_usadas[fiscal_date] = datas_usadas.get(fiscal_date, 0) + 1
        cnpj = str(r.get("CNPJ_Companhia", "")).strip()
        cd = cnpj_para_cd.get(cnpj)
        if not cd:
            sem_cd.add(cnpj)
            continue
        tickers = cd_para_tickers.get(cd)
        if not tickers:
            sem_ticker.add(cd)
            continue
        for col, line_item in COLUNAS.items():
            valor = r.get(col)
            if pd.isna(valor) or valor is None:
                continue
            q = qualidade_referencia(r.get("Data_Referencia"))
            for tk in tickers:
                linhas.append((tk, "balance_sheet", "annual", fiscal_date,
                                line_item, float(valor), None, "cvm_fre", q))
    return linhas, sem_cd, sem_ticker, datas_usadas


def gravar(engine, linhas):
    if not linhas:
        return 0
    # rede de segurança: garante chave única no lote, independentemente da
    # origem da duplicata. A chave é (ticker, statement, period_type,
    # fiscal_date, line_item) - posições 0,1,2,3,4 da tupla.
    # Um mesmo exercício pode ser descrito por DOIS arquivos. Fica o registro
    # de melhor qualidade de referência (ver qualidade_referencia): o
    # fechamento explícito vence o exercício inferido de uma data do meio do
    # ano. Sem esse critério, 11 registros atrasados do arquivo de 2024
    # substituiriam os 647 registros corretos do arquivo de 2023.
    melhor = {}
    for l in linhas:
        k = (l[0], l[1], l[2], l[3], l[4])
        if k not in melhor or l[8] > melhor[k][8]:
            melhor[k] = l
    if len(melhor) < len(linhas):
        print(f"  ({len(linhas) - len(melhor)} duplicatas resolvidas por qualidade da referência)")
    linhas = [l[:8] for l in melhor.values()]
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM financials WHERE source = 'cvm_fre'")
            if cur.rowcount:
                print(f"  (removidas {cur.rowcount} linhas de uma carga anterior)")
            execute_values(cur, """
                INSERT INTO financials
                    (ticker, statement, period_type, fiscal_date, line_item, value, published_date, source)
                VALUES %s
                ON CONFLICT (ticker, statement, period_type, fiscal_date, line_item)
                DO UPDATE SET value = EXCLUDED.value, source = EXCLUDED.source, fetched_at = now()
            """, linhas, page_size=1000)
        conn.commit()
    finally:
        conn.close()
    return len(linhas)


def main():
    p = argparse.ArgumentParser(description="Ingestor do FRE (quantidade de ações por classe).")
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2025)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    cd_para_tickers = carregar_tickers(engine)
    if not cd_para_tickers:
        raise SystemExit("ticker_cvm_map vazia.")
    print(f"{len(cd_para_tickers)} empresas mapeadas para tickers\n")

    print("Montando de-para CNPJ -> CD_CVM a partir dos arquivos DFP...")
    anos = list(range(args.de, args.ate + 1))
    cnpj_para_cd = mapa_cnpj_cd_cvm(anos)
    print(f"  {len(cnpj_para_cd)} CNPJs mapeados\n")
    if not cnpj_para_cd:
        raise SystemExit("Não consegui montar o de-para CNPJ->CD_CVM. "
                          "Confira se os arquivos DFP estão em dados_cvm/.")

    total, resumo, todas_linhas = 0, [], []
    for ano in anos:
        df = ler_capital_social(ano)
        if df.empty:
            continue
        linhas, sem_cd, sem_ticker, datas = montar_linhas(df, cnpj_para_cd, cd_para_tickers, ano)
        n_tickers = len({l[0] for l in linhas})
        exercicios = ", ".join(f"{d.year}({n})" for d, n in sorted(datas.items()))
        print(f"  arquivo {ano}: {len(df):>5} empresas -> {len(linhas):>5} linhas, {n_tickers:>3} tickers"
              f"  (sem CD_CVM: {len(sem_cd)}, fora do universo: {len(sem_ticker)})")
        print(f"              exercícios gravados: {exercicios}")
        resumo.append({"ano": ano, "empresas_fre": len(df), "tickers": n_tickers})
        todas_linhas.extend(linhas)
        total += len(linhas)

    # grava TUDO de uma vez. Gravar arquivo a arquivo com DELETE por exercício
    # fazia um arquivo posterior apagar o que o arquivo principal daquele
    # exercício já tinha escrito - foi assim que os exercícios 2022, 2023 e
    # 2024 ficaram com 5 tickers em vez de ~320.
    if not args.dry_run and todas_linhas:
        gravos = gravar(engine, todas_linhas)
        print(f"\n{gravos} linhas gravadas.")

    print(f"\nTotal processado: {total} linhas" + (" (dry-run, nada gravado)" if args.dry_run else ""))

    if resumo:
        r = pd.DataFrame(resumo)
        print("\nCobertura por ano:")
        print(r.to_string(index=False))
        mediana = r["tickers"].median()
        fracos = r[r["tickers"] < mediana * 0.6]
        if not fracos.empty:
            print(f"\n  ATENÇÃO: anos com cobertura bem abaixo da mediana ({mediana:.0f} tickers):")
            print(fracos.to_string(index=False))

    if not args.dry_run:
        with engine.connect() as conn:
            comp = pd.read_sql(text("""
                SELECT source, line_item, COUNT(*) AS linhas,
                       COUNT(DISTINCT ticker) AS tickers,
                       MIN(fiscal_date) AS de, MAX(fiscal_date) AS ate
                FROM financials
                WHERE line_item IN ('Ordinary Shares Number','Preferred Shares Number','Total Shares Number')
                GROUP BY source, line_item ORDER BY source, line_item
            """), conn)
        print("\nContagem de ações no banco, por fonte:")
        print(comp.to_string(index=False))
        print("\nPróximo passo: comparar 'cvm_fre' com 'cvm' nos anos 2020-2025 para")
        print("confirmar a correção de escala, e então reescrever o cálculo de")
        print("market cap por classe em scoring.py (item 1 do PENDENCIAS.md).")


if __name__ == "__main__":
    main()