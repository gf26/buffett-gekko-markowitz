"""
Ingestor do FRE - quantidade de ações por classe, point-in-time.

O QUE MUDOU NESTA VERSÃO (e por quê)
------------------------------------
A versão anterior tratava cada linha do `capital_social` como "a posição da
empresa naquele exercício". Está errado: o arquivo traz o HISTÓRICO DE
APROVAÇÕES DE CAPITAL, uma linha por aprovação, cada uma com sua
`Data_Autorizacao_Aprovacao`.

Exemplo real (CEG, arquivo de 2025) - sete linhas, mesma versão e mesma data
de referência, diferindo só na data de aprovação:

    2013-04-29 ->  51.927.546.473 ações
    2014-04-30 ->  51.927.546.473
    2015-04-28 ->  51.927.546.473
    2016-04-27 ->     259.637.732   <- grupamento
    2017-04-27 ->     259.637.732
    2018-04-27 ->     259.637.732

Pegar "a linha mais recente do arquivo" produzia oscilação: alguns exercícios
recebiam 259 milhões, outros 51,9 bilhões, porque arquivos diferentes listam
conjuntos diferentes de aprovações.

A LÓGICA CORRETA é point-in-time: para o exercício de 2016 vale a aprovação
mais recente ATÉ 31/12/2016; para 2015, a mais recente até 31/12/2015. A CEG
fica com 51,9 bi até 2015 e 259 milhões a partir de 2016 - correto em cada
ponto do tempo e independente de qual arquivo foi lido.

Consequências boas:
  - As aprovações de TODOS os arquivos são acumuladas, então um exercício
    pode ser preenchido por informação vinda de arquivo posterior.
  - Some o buraco de 2022 (item 3b do PENDENCIAS.md): a aprovação vigente em
    31/12/2022 existe, mesmo sem nenhum arquivo com referência naquele ano.
  - A `Data_Referencia` deixa de ser usada para datar o dado, e com ela some
    a necessidade de desempatar por "qualidade da referência".

OUTRAS DECISÕES
---------------
- Só `Tipo_Capital = 'Capital Integralizado'`. O autorizado é teto
  estatutário: a Vale tem 5,37 bi de integralizado contra 10,8 bi autorizado.
- Linhas com quantidade ZERO são descartadas. Apareceram para BBAS3, JHSF3 e
  BNBR3 - Banco do Brasil com zero ações é lacuna de preenchimento, não
  informação.
- Grava com `source = 'cvm_fre'`.

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

warnings.filterwarnings("ignore", category=pd.errors.DtypeWarning)

PASTA_CACHE = "dados_cvm"
# CASCATA DE TIPOS DE CAPITAL, em ordem de preferência.
#
# "Integralizado" é o capital efetivamente pago - é o que queremos. Mas TIM e
# Bardella só declaram "Emitido", e o Banco da Amazônia só "Subscrito"; exigir
# Integralizado deixava essas empresas sem contagem de ações.
#
# Os três costumam coincidir (a Vibra tem 1.119.000.000 nos três). Divergem
# quando há capital subscrito e ainda não integralizado - aí Subscrito e
# Emitido SUPERESTIMAM as ações em circulação, e portanto o valor de mercado.
#
# "Capital Autorizado" fica FORA de propósito: é teto estatutário, não ações
# existentes. A Vale tem 5,37 bi integralizados contra 10,8 bi autorizados.
#
# O tipo efetivamente usado é gravado em `tipo_capital` para auditoria - se
# um dia quiser restringir só aos integralizados, dá para filtrar.
CASCATA_CAPITAL = ["Capital Integralizado", "Capital Subscrito", "Capital Emitido"]

# ver comentário em consolidar_aprovacoes
PISO_ACOES = 100_000

COLUNAS = {
    "Quantidade_Acoes_Ordinarias": "Ordinary Shares Number",
    "Quantidade_Acoes_Preferenciais": "Preferred Shares Number",
    "Quantidade_Total_Acoes": "Total Shares Number",
}


def ler_capital_social(ano):
    caminho = os.path.join(PASTA_CACHE, f"fre_cia_aberta_{ano}.zip")
    if not os.path.exists(caminho):
        return pd.DataFrame()
    zf = zipfile.ZipFile(caminho)
    nome = f"fre_cia_aberta_capital_social_{ano}.csv"
    if nome not in zf.namelist():
        return pd.DataFrame()
    with zf.open(nome) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
    if "Tipo_Capital" not in df.columns:
        return pd.DataFrame()
    df = df[df["Tipo_Capital"].isin(CASCATA_CAPITAL)].copy()
    df["_ano_arquivo"] = ano
    return df


def consolidar_aprovacoes(anos):
    """Todas as aprovações de capital, de todos os arquivos, sem duplicatas.

    A mesma aprovação aparece repetida em vários arquivos (o FRE de 2025 ainda
    lista a aprovação de 2013). Deduplicar por (CNPJ, data de aprovação) deixa
    uma linha por evento real de capital."""
    frames = []
    for ano in anos:
        df = ler_capital_social(ano)
        if not df.empty:
            frames.append(df)
        print(f"  arquivo {ano}: {len(df)} aprovações de capital integralizado")
    if not frames:
        return pd.DataFrame()

    todas = pd.concat(frames, ignore_index=True)
    for c in COLUNAS:
        if c in todas.columns:
            todas[c] = pd.to_numeric(todas[c], errors="coerce")

    antes = len(todas)
    todas = todas[todas["Quantidade_Total_Acoes"].fillna(0) > 0]
    if antes - len(todas):
        print(f"\n  {antes - len(todas)} aprovações descartadas por quantidade zero/ausente")

    # PISO DE SANIDADE: empresa listada não tem 1.000 ações.
    #
    # Três casos apareceram com a mesma assinatura - um valor absurdamente
    # pequeno num exercício em que a empresa já negociava:
    #     RVEE3  1.000 -> 80.094.177
    #     LPSB3  64.000 -> 57.078.658
    #     TECN3  268.565 -> 77.478.413
    # É a primeira aprovação de capital da empresa, de quando ainda era
    # fechada, sendo aplicada a um exercício posterior. Não é escala (não é
    # 1000x exato) nem desdobramento: é aprovação antiga fora de contexto.
    #
    # O piso é conservador DE PROPÓSITO: 100 mil pega RVEE3 (1.000) e LPSB3
    # (64.000), mas NÃO pega TECN3 (268.565). Subir para 1 milhão pegaria a
    # TECN3 e passaria a arriscar descartar empresas pequenas legítimas -
    # trocar um falso positivo por falsos negativos é piorar o problema.
    # A TECN3 e casos semelhantes ficam por conta da verificação de variação
    # entre exercícios (checar_sanidade), que os sinaliza sem descartar.
    antes = len(todas)
    pequenas = todas[todas["Quantidade_Total_Acoes"] < PISO_ACOES]
    if not pequenas.empty:
        print(f"  {len(pequenas)} aprovações descartadas por quantidade abaixo de "
              f"{PISO_ACOES:,} ações (implausível para empresa listada):")
        amostra = (pequenas.groupby("Nome_Companhia")["Quantidade_Total_Acoes"]
                            .agg(["min", "count"]).nsmallest(8, "min"))
        for nome, r in amostra.iterrows():
            print(f"      {str(nome)[:45]:<47} menor: {r['min']:>12,.0f}  ({int(r['count'])} aprovações)")
    todas = todas[todas["Quantidade_Total_Acoes"] >= PISO_ACOES]

    todas["_aprovacao"] = pd.to_datetime(todas.get("Data_Autorizacao_Aprovacao"), errors="coerce")
    sem_data = int(todas["_aprovacao"].isna().sum())
    if sem_data:
        print(f"  {sem_data} aprovações sem data - descartadas (não dá para datar o valor)")
    todas = todas.dropna(subset=["_aprovacao"])

    # aplica a cascata: para cada (empresa, aprovação), fica o tipo de maior
    # preferência disponível
    todas["_pref"] = todas["Tipo_Capital"].map({t: i for i, t in enumerate(CASCATA_CAPITAL)})
    if "Versao" in todas.columns:
        todas["Versao"] = pd.to_numeric(todas["Versao"], errors="coerce")
        todas = todas.sort_values(["_pref", "_ano_arquivo", "Versao"],
                                   ascending=[False, True, True])
    else:
        todas = todas.sort_values("_pref", ascending=False)
    todas = todas.drop_duplicates(["CNPJ_Companhia", "_aprovacao"], keep="last")

    usados = todas["Tipo_Capital"].value_counts()
    if len(usados) > 1:
        print("\n  Tipos de capital utilizados (cascata):")
        for t, n in usados.items():
            print(f"    {t:<26} {n:>6} aprovações")
        nao_integ = todas[todas["Tipo_Capital"] != "Capital Integralizado"]
        if not nao_integ.empty:
            emp = sorted(nao_integ["Nome_Companhia"].dropna().unique())[:8]
            print(f"  {nao_integ['CNPJ_Companhia'].nunique()} empresas sem 'Capital Integralizado' "
                  f"- contagem pode estar superestimada:")
            for e in emp:
                print(f"      {str(e)[:60]}")

    print(f"\n  {len(todas)} aprovações distintas, {todas['CNPJ_Companhia'].nunique()} empresas")
    return todas.sort_values(["CNPJ_Companhia", "_aprovacao"])


def posicao_por_exercicio(aprovacoes, de, ate):
    """Para cada empresa e exercício, a aprovação vigente em 31/12.

    É aqui que o dado vira point-in-time: vale a última aprovação ocorrida
    ATÉ o fechamento daquele exercício."""
    linhas = []
    for cnpj, g in aprovacoes.groupby("CNPJ_Companhia"):
        g = g.sort_values("_aprovacao")
        datas = g["_aprovacao"].to_numpy()
        for ano in range(de, ate + 1):
            corte = pd.Timestamp(year=ano, month=12, day=31)
            idx = datas.searchsorted(corte.to_datetime64(), side="right") - 1
            if idx < 0:
                continue
            r = g.iloc[idx]
            linhas.append({
                "cnpj": cnpj,
                "fiscal_date": corte.date(),
                "aprovacao": r["_aprovacao"].date(),
                "tipo_capital": r.get("Tipo_Capital"),
                **{col: r.get(col) for col in COLUNAS},
            })
    return pd.DataFrame(linhas)


def mapa_cnpj_cd_cvm(anos):
    mapa = {}
    cad = os.path.join(PASTA_CACHE, "cad_cia_aberta.csv")
    if os.path.exists(cad):
        d = pd.read_csv(cad, sep=";", encoding="latin-1", dtype=str)
        c_cnpj = next((c for c in d.columns if "CNPJ" in c.upper()), None)
        c_cd = next((c for c in d.columns if c.upper() in ("CD_CVM", "CODIGO_CVM")), None)
        c_sit = next((c for c in d.columns if c.upper() == "SIT"), None)
        if c_cnpj and c_cd:
            # UM CNPJ PODE TER DOIS REGISTROS NA CVM: um CANCELADO (antigo) e
            # um ATIVO. O cadastro lista o cancelado primeiro, então pegar "o
            # primeiro que aparece" mapeava o Itaú para o código 1279 em vez
            # do 19348 - e como 1279 não está na ticker_cvm_map, o Itaú caía
            # silenciosamente em "fora do universo", sem contagem de ações.
            # Mesmo padrão em Vibra (14249 x 24295) e BMG (1716 x 24600).
            if c_sit:
                d = d.copy()
                d["_ativo"] = d[c_sit].astype(str).str.strip().str.upper().eq("ATIVO")
                d = d.sort_values("_ativo")   # ativos por último -> vencem
            for cnpj, cd in zip(d[c_cnpj].astype(str).str.strip(),
                                 d[c_cd].astype(str).str.strip().str.lstrip("0")):
                mapa[cnpj] = cd   # sem setdefault: o último (ATIVO) prevalece
    for ano in anos:
        caminho = os.path.join(PASTA_CACHE, f"dfp_cia_aberta_{ano}.zip")
        if not os.path.exists(caminho):
            continue
        zf = zipfile.ZipFile(caminho)
        nome = f"dfp_cia_aberta_{ano}.csv"
        if nome not in zf.namelist():
            continue
        with zf.open(nome) as f:
            d = pd.read_csv(f, sep=";", encoding="latin-1", dtype={"CNPJ_CIA": str, "CD_CVM": str})
        if "CNPJ_CIA" in d.columns and "CD_CVM" in d.columns:
            # complemento: só preenche CNPJ que o cadastro não trouxe
            for cnpj, cd in zip(d["CNPJ_CIA"].astype(str).str.strip(),
                                 d["CD_CVM"].astype(str).str.strip().str.lstrip("0")):
                mapa.setdefault(cnpj, cd)
    return mapa


def carregar_tickers(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker, cd_cvm FROM ticker_cvm_map")).fetchall()
    por_cd = {}
    for ticker, cd in rows:
        por_cd.setdefault(str(cd).strip().lstrip("0"), []).append(ticker)
    return por_cd


def montar_linhas(pos, cnpj_para_cd, cd_para_tickers):
    linhas, sem_cd, sem_ticker = [], set(), set()
    for _, r in pos.iterrows():
        cd = cnpj_para_cd.get(r["cnpj"])
        if not cd:
            sem_cd.add(r["cnpj"])
            continue
        tickers = cd_para_tickers.get(cd)
        if not tickers:
            sem_ticker.add(cd)
            continue
        for col, line_item in COLUNAS.items():
            v = r.get(col)
            if pd.isna(v):
                continue
            for tk in tickers:
                linhas.append((tk, "balance_sheet", "annual", r["fiscal_date"],
                                line_item, float(v), None, "cvm_fre"))
    return linhas, sem_cd, sem_ticker


def checar_sanidade(linhas):
    chk = pd.DataFrame([(l[0], l[3], l[5]) for l in linhas if l[4] == "Total Shares Number"],
                       columns=["ticker", "fiscal_date", "valor"])
    if chk.empty:
        return
    chk = chk.sort_values(["ticker", "fiscal_date"])
    chk["anterior"] = chk.groupby("ticker")["valor"].shift(1)
    chk["razao"] = pd.to_numeric(chk["valor"] / chk["anterior"].replace(0, pd.NA), errors="coerce")
    susp = chk[chk["razao"].notna() & ((chk["razao"] > 10) | (chk["razao"] < 0.1))]
    if susp.empty:
        print("\n  Sanidade: nenhuma variação acima de 10x entre exercícios consecutivos.")
        return
    print(f"\n  {len(susp)} variação(ões) acima de 10x entre exercícios consecutivos.")
    print("  Razão redonda (2, 4, 10, 100) costuma ser desdobramento/grupamento real;")
    print("  razão quebrada ou perto de 1000 sugere problema na fonte.")
    for _, r in susp.nlargest(min(10, len(susp)), "razao").iterrows():
        print(f"    {r['ticker']:<11} {r['fiscal_date']}  "
              f"{r['anterior']:>16,.0f} -> {r['valor']:>16,.0f}  ({r['razao']:>9.1f}x)")


def gravar(engine, linhas):
    vistos, unicas = set(), []
    for l in linhas:
        k = (l[0], l[1], l[2], l[3], l[4])
        if k not in vistos:
            vistos.add(k)
            unicas.append(l)
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
            """, unicas, page_size=1000)
        conn.commit()
    finally:
        conn.close()
    return len(unicas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2025)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    cd_para_tickers = carregar_tickers(engine)
    print(f"{len(cd_para_tickers)} empresas mapeadas para tickers\n")

    anos = list(range(args.de, args.ate + 1))
    print("Lendo o histórico de aprovações de capital...")
    aprov = consolidar_aprovacoes(anos)
    if aprov.empty:
        raise SystemExit("Nenhuma aprovação lida - confira os arquivos FRE em dados_cvm/.")

    print("\nMontando de-para CNPJ -> CD_CVM...")
    cnpj_para_cd = mapa_cnpj_cd_cvm(anos)
    print(f"  {len(cnpj_para_cd)} CNPJs mapeados")

    print(f"\nDerivando a posição vigente em 31/12 de cada exercício...")
    pos = posicao_por_exercicio(aprov, args.de - 1, args.ate)
    print(f"  {len(pos)} pares (empresa, exercício)")

    linhas, sem_cd, sem_ticker = montar_linhas(pos, cnpj_para_cd, cd_para_tickers)
    print(f"  {len(linhas)} linhas, {len({l[0] for l in linhas})} tickers")
    print(f"  (sem CD_CVM: {len(sem_cd)}, fora do universo: {len(sem_ticker)})")

    cob = (pd.DataFrame([(l[3].year, l[0]) for l in linhas], columns=["exercicio", "ticker"])
             .groupby("exercicio")["ticker"].nunique())
    print("\nTickers por exercício:")
    print(cob.to_string())

    checar_sanidade(linhas)

    if args.dry_run:
        print("\n(dry-run - nada gravado)")
        return
    n = gravar(engine, linhas)
    print(f"\n{n} linhas gravadas.")

    with engine.connect() as conn:
        r = pd.read_sql(text("""
            SELECT line_item, COUNT(*) AS linhas, COUNT(DISTINCT ticker) AS tickers,
                   MIN(fiscal_date) AS de, MAX(fiscal_date) AS ate
            FROM financials WHERE source = 'cvm_fre'
            GROUP BY line_item ORDER BY line_item
        """), conn)
    print(r.to_string(index=False))


if __name__ == "__main__":
    main()