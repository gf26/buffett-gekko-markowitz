"""
Proventos por ação, a partir do FRE.

O PROBLEMA QUE RESOLVE
----------------------
As tabelas `dividends` e `splits` vieram do Yahoo e cobrem 308 e 244 tickers.
O COTAHIST tem 1.080 - ou seja, ~772 tickers (justamente os deslistados, que
são o alvo da correção do viés de sobrevivência) ficariam sem ajuste de
proventos.

COMO FUNCIONA
-------------
O FRE traz duas informações no MESMO documento, da mesma empresa e do mesmo
exercício:

  fre_cia_aberta_distribuicao_dividendos_classe_acao_AAAA.csv
      Montante distribuído POR ESPÉCIE de ação (ON, PN), com data de pagamento
      e o tipo (Dividendo Obrigatório, JCP, etc).

  fre_cia_aberta_capital_social_AAAA.csv
      Quantidade de ações POR CLASSE.

Dividindo um pelo outro chega-se ao valor por ação, sem misturar fontes e sem
estimativa: são dois campos declarados pela própria empresa.

LIMITAÇÕES CONHECIDAS
---------------------
1. A base acionária pode mudar entre a data do provento e o fechamento do
   exercício. O efeito é pequeno (emissões grandes no meio do ano são raras),
   mas existe. Quando ocorre, costuma aparecer como evento de subscrição no
   COTAHIST (marcador ES), o que permite detectar.

2. O FRE traz DATA DE PAGAMENTO, não data-ex. São diferentes - o pagamento
   vem semanas depois. Para ajustar preço é a data-ex que importa, e ela vem
   do COTAHIST (marcadores ED e EJ). O casamento entre as duas é feito num
   módulo posterior.

3. Uma empresa pode declarar valores por espécie sem que a espécie tenha
   ações registradas naquele exercício - nesses casos o cálculo é descartado.

VALIDAÇÃO
---------
O script compara o valor calculado com a tabela `dividends` do Yahoo nos
tickers em que ela existe. Se bater nos 308 conhecidos, o método está
validado para aplicar aos 772 deslistados.

Uso:
    DATABASE_URL="..." python cvm_proventos.py --de 2010 --ate 2026 --dry-run
    DATABASE_URL="..." python cvm_proventos.py --de 2010 --ate 2026
"""
import argparse
import os
import warnings
import zipfile

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

warnings.filterwarnings("ignore", category=pd.errors.DtypeWarning)

PASTA = "dados_cvm"

# ver comentário em carregar_acoes_por_classe
PISO_ACOES = 100_000

# Espécie no FRE -> sufixo do ticker na B3
ESPECIE_PARA_CLASSE = {
    "ORDINÁRIA": "ON",
    "ORDINARIA": "ON",
    "PREFERENCIAL": "PN",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS proventos_cvm (
    cd_cvm            TEXT NOT NULL,
    exercicio         INT  NOT NULL,
    classe            TEXT NOT NULL,
    data_pagamento    DATE,
    tipo              TEXT,
    montante_total    NUMERIC,
    acoes_classe      NUMERIC,
    valor_por_acao    NUMERIC,
    bruto_por_acao    NUMERIC,
    nome_companhia    TEXT,
    PRIMARY KEY (cd_cvm, exercicio, classe, data_pagamento, tipo)
);
CREATE INDEX IF NOT EXISTS idx_proventos_cvm_cd ON proventos_cvm(cd_cvm);
"""


# JCP sofre retenção de 15% de IR na fonte. A empresa declara o montante
# BRUTO no FRE; o acionista recebe o líquido, que é o que aparece nas fontes de
# mercado e o que efetivamente afeta o preço na data-ex.
#
# Confirmado por medição: comparando distribuição a distribuição com o Yahoo,
# a razão mediana ficou em 1,008 para "Dividendo Obrigatório" e 1,172 para
# "Juros Sobre Capital Próprio". E 1/0,85 = 1,176 - praticamente o valor
# observado. Não é estimativa, é a alíquota.
IR_JCP = 0.15


def _fator_liquido(tipo):
    t = str(tipo or "").upper()
    if "JUROS" in t and "CAPITAL" in t:
        return 1.0 - IR_JCP
    return 1.0


def ler_zip_csv(ano, nome_base):
    p = os.path.join(PASTA, f"fre_cia_aberta_{ano}.zip")
    if not os.path.exists(p):
        return pd.DataFrame()
    zf = zipfile.ZipFile(p)
    nome = f"fre_cia_aberta_{nome_base}_{ano}.csv"
    if nome not in zf.namelist():
        return pd.DataFrame()
    with zf.open(nome) as f:
        return pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)


def carregar_dividendos(anos):
    """Montante distribuído por espécie, de todos os arquivos FRE."""
    frames = []
    for ano in anos:
        d = ler_zip_csv(ano, "distribuicao_dividendos_classe_acao")
        if d.empty:
            continue
        d["_ano_arquivo"] = ano
        frames.append(d)
        print(f"  arquivo {ano}: {len(d):>6} linhas de distribuição")
    if not frames:
        return pd.DataFrame()

    t = pd.concat(frames, ignore_index=True)
    t["Montante"] = pd.to_numeric(t["Montante"], errors="coerce")
    t["_pagamento"] = pd.to_datetime(t.get("Data_Pagamento_Dividendo"), errors="coerce")
    t["_fim_exercicio"] = pd.to_datetime(t.get("Data_Fim_Exercicio_Social"), errors="coerce")
    t["exercicio"] = t["_fim_exercicio"].dt.year
    t["classe"] = (t["Especie_Acao"].astype(str).str.strip().str.upper()
                     .map(ESPECIE_PARA_CLASSE))

    antes = len(t)
    t = t.dropna(subset=["Montante", "exercicio", "classe"])
    t = t[t["Montante"] > 0]
    print(f"\n  {antes - len(t)} linhas descartadas (montante zero, espécie "
          f"não reconhecida ou exercício ausente)")

    # a mesma distribuição aparece em vários arquivos FRE; fica a versão mais
    # recente de cada (empresa, exercício, classe, pagamento, tipo)
    if "Versao" in t.columns:
        t["Versao"] = pd.to_numeric(t["Versao"], errors="coerce")
        t = t.sort_values(["_ano_arquivo", "Versao"])
    # DEDUPLICAÇÃO PELO MONTANTE, não pela data de pagamento.
    #
    # A mesma distribuição aparece em vários arquivos FRE, e nem todos trazem
    # a data de pagamento. Caso real da LPSB3, exercício 2011: duas
    # distribuições (dividendo de R$ 20.812.741 e JCP de R$ 15.369.215)
    # aparecem em 6 linhas - arquivo 2012 sem data, arquivos 2013 e 2014 com
    # data. Como NaN nunca é igual a NaN, as linhas sem data sobreviviam à
    # deduplicação e o montante era contado em dobro.
    #
    # O montante é o identificador estável: duas distribuições diferentes da
    # mesma empresa, no mesmo exercício e na mesma classe raramente têm valor
    # idêntico até os centavos.
    # arredonda o montante para o milhar antes de comparar: a mesma
    # distribuição aparece com centavos diferentes entre arquivos (a Sabesp
    # tem 823.492.690,00 e 823.492.680,00 para a mesma distribuição de JCP -
    # 10 centavos de diferença faziam as duas sobreviverem à deduplicação).
    t["_montante_chave"] = (t["Montante"] / 1000).round(0)
    chaves = ["CNPJ_Companhia", "exercicio", "classe", "Dividendo_Distribuido", "_montante_chave"]
    antes_dedup = len(t)
    # ordena com as linhas COM data por último, para que a versão preservada
    # seja a que tem a informação mais completa
    t["_tem_data"] = t["_pagamento"].notna()
    t = t.sort_values(["_tem_data", "_ano_arquivo", "Versao"] if "Versao" in t.columns
                       else ["_tem_data", "_ano_arquivo"])
    t = t.drop_duplicates(chaves, keep="last")
    print(f"  {antes_dedup - len(t)} linhas duplicadas removidas "
          f"(mesma distribuição repetida em vários arquivos FRE)")
    print(f"  {len(t)} distribuições distintas, {t['CNPJ_Companhia'].nunique()} empresas")
    return t


def carregar_acoes_por_classe(anos):
    """Quantidade de ações por classe e exercício, do capital_social.

    Mesma lógica point-in-time do cvm_ingestor_fre: vale a aprovação de
    capital vigente no fim do exercício."""
    frames = []
    for ano in anos:
        d = ler_zip_csv(ano, "capital_social")
        if d.empty:
            continue
        d = d[d["Tipo_Capital"].isin(
            ["Capital Integralizado", "Capital Subscrito", "Capital Emitido"])]
        d["_ano_arquivo"] = ano
        frames.append(d)
    if not frames:
        return pd.DataFrame()

    t = pd.concat(frames, ignore_index=True)
    for c in ["Quantidade_Acoes_Ordinarias", "Quantidade_Acoes_Preferenciais"]:
        t[c] = pd.to_numeric(t.get(c), errors="coerce")
    t["_aprovacao"] = pd.to_datetime(t.get("Data_Autorizacao_Aprovacao"), errors="coerce")
    t = t.dropna(subset=["_aprovacao"])
    total_acoes = (t["Quantidade_Acoes_Ordinarias"].fillna(0)
                   + t["Quantidade_Acoes_Preferenciais"].fillna(0))

    # PISO DE SANIDADE - o mesmo do cvm_ingestor_fre.py.
    #
    # O arquivo traz o histórico de aprovações de capital, incluindo as de
    # quando a empresa ainda era fechada. A LPSB3 tem uma aprovação com 64.000
    # ações; dividir R$ 36,2 milhões de dividendo por ela dá R$ 565 por ação -
    # foi o que produziu razão de 2.485x contra o Yahoo na validação.
    #
    # Empresa listada não tem menos de 100 mil ações. O piso é conservador:
    # descarta a aprovação implausível sem eliminar empresas pequenas
    # legítimas, cujas contagens estão na casa dos milhões.
    antes = len(t)
    t = t[total_acoes >= PISO_ACOES]
    if antes - len(t):
        print(f"  {antes - len(t)} aprovações descartadas por total abaixo de "
              f"{PISO_ACOES:,} ações (implausível para empresa listada)")

    pref = {"Capital Integralizado": 2, "Capital Subscrito": 1, "Capital Emitido": 0}
    t["_pref"] = t["Tipo_Capital"].map(pref)
    t = t.sort_values(["_pref", "_ano_arquivo"]).drop_duplicates(
        ["CNPJ_Companhia", "_aprovacao"], keep="last")
    return t.sort_values(["CNPJ_Companhia", "_aprovacao"])


def acoes_no_exercicio(aprovacoes, cnpj, exercicio):
    """Ações por classe vigentes no fim do exercício."""
    g = aprovacoes[aprovacoes["CNPJ_Companhia"] == cnpj]
    if g.empty:
        return None, None
    corte = pd.Timestamp(year=int(exercicio), month=12, day=31)
    vig = g[g["_aprovacao"] <= corte]
    if vig.empty:
        return None, None
    r = vig.iloc[-1]
    return r["Quantidade_Acoes_Ordinarias"], r["Quantidade_Acoes_Preferenciais"]


def calcular(div, aprovacoes, cnpj_para_cd):
    linhas, sem_acoes, sem_cd = [], 0, set()
    cache = {}
    for _, r in div.iterrows():
        cnpj = str(r["CNPJ_Companhia"]).strip()
        cd = cnpj_para_cd.get(cnpj)
        if not cd:
            sem_cd.add(cnpj)
            continue
        chave = (cnpj, int(r["exercicio"]))
        if chave not in cache:
            cache[chave] = acoes_no_exercicio(aprovacoes, cnpj, r["exercicio"])
        n_on, n_pn = cache[chave]
        n = n_on if r["classe"] == "ON" else n_pn
        if n is None or pd.isna(n) or n <= 0:
            sem_acoes += 1
            continue
        linhas.append({
            "cd_cvm": cd,
            "exercicio": int(r["exercicio"]),
            "classe": r["classe"],
            "data_pagamento": (r["_pagamento"].date()
                                if pd.notna(r["_pagamento"]) else None),
            "tipo": str(r.get("Dividendo_Distribuido") or "")[:60] or None,
            "montante_total": float(r["Montante"]),
            "acoes_classe": float(n),
            "valor_por_acao": float(r["Montante"]) / float(n) * _fator_liquido(r.get("Dividendo_Distribuido")),
            "bruto_por_acao": float(r["Montante"]) / float(n),
            "nome_companhia": str(r.get("Nome_Companhia") or "")[:120] or None,
        })
    if sem_acoes:
        print(f"  {sem_acoes} distribuições sem contagem de ações da classe - descartadas")
    if sem_cd:
        print(f"  {len(sem_cd)} CNPJs sem CD_CVM")
    return pd.DataFrame(linhas)


def mapa_cnpj_cd_cvm():
    mapa = {}
    cad = os.path.join(PASTA, "cad_cia_aberta.csv")
    if os.path.exists(cad):
        d = pd.read_csv(cad, sep=";", encoding="latin-1", dtype=str)
        c_cnpj = next((c for c in d.columns if "CNPJ" in c.upper()), None)
        c_cd = next((c for c in d.columns if c.upper() == "CD_CVM"), None)
        c_sit = next((c for c in d.columns if c.upper() == "SIT"), None)
        if c_cnpj and c_cd:
            if c_sit:   # registro ATIVO vence o CANCELADO (ver cvm_ingestor_fre)
                d = d.copy()
                d["_a"] = d[c_sit].astype(str).str.strip().str.upper().eq("ATIVO")
                d = d.sort_values("_a")
            for cnpj, cd in zip(d[c_cnpj].astype(str).str.strip(),
                                 d[c_cd].astype(str).str.strip().str.lstrip("0")):
                mapa[cnpj] = cd
    return mapa


def validar_contra_yahoo(engine, prov, tolerancia_dias=120):
    """Compara CADA distribuição com o provento do Yahoo mais próximo em data.

    POR QUE NÃO SOMAR POR EXERCÍCIO: a versão anterior somava todos os
    proventos de um exercício social e comparava com a soma do ano-calendário
    do Yahoo. Isso misturava três coisas num número só - duplicação residual,
    desalinhamento temporal (o provento do exercício 2011 da LPSB3 foi pago em
    junho/2012) e erro de contagem de ações. O resultado era ilegível: razão
    mediana de 1,3 sem que se soubesse quanto vinha de cada causa.

    Comparando distribuição a distribuição, cada divergência tem uma causa
    identificável. Casos sem par no Yahoo dentro da tolerância são reportados
    à parte, em vez de contaminarem a estatística."""
    with engine.connect() as conn:
        mapa = pd.read_sql(text("SELECT ticker, cd_cvm FROM ticker_cvm_map"), conn)
        yah = pd.read_sql(text(
            "SELECT ticker, ex_date, amount FROM dividends WHERE amount > 0"), conn)
    if yah.empty:
        print("\n  Sem dados do Yahoo para comparar.")
        return

    mapa["cd_cvm"] = mapa["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    mapa["classe"] = mapa["ticker"].str.replace(".SA", "", regex=False).str[4:5].map(
        {"3": "ON", "4": "PN", "5": "PN", "6": "PN"})
    yah["ex_date"] = pd.to_datetime(yah["ex_date"])

    p = prov[prov["data_pagamento"].notna()].copy()
    p["data_pagamento"] = pd.to_datetime(p["data_pagamento"])
    p = p.merge(mapa[["ticker", "cd_cvm", "classe"]], on=["cd_cvm", "classe"])
    if p.empty:
        print("\n  Nenhuma distribuição com data e ticker mapeado.")
        return

    # para cada distribuição, o provento do Yahoo mais próximo no mesmo ticker
    pares, sem_par = [], 0
    for tk, g in p.groupby("ticker"):
        y = yah[yah["ticker"] == tk]
        if y.empty:
            continue
        for _, r in g.iterrows():
            dif = (y["ex_date"] - r["data_pagamento"]).abs()
            k = dif.idxmin()
            if dif.loc[k] > pd.Timedelta(days=tolerancia_dias):
                sem_par += 1
                continue
            pares.append({
                "ticker": tk, "exercicio": r["exercicio"], "tipo": r["tipo"],
                "pagamento": r["data_pagamento"].date(),
                "ex_date_yahoo": y.loc[k, "ex_date"].date(),
                "dias": int(dif.loc[k].days),
                "calculado": r["valor_por_acao"],
                "yahoo": float(y.loc[k, "amount"]),
                "acoes": r["acoes_classe"],
            })
    if not pares:
        print("\n  Nenhum par encontrado dentro da tolerância.")
        return

    c = pd.DataFrame(pares)
    c["razao"] = c["calculado"] / c["yahoo"]

    # SEPARAR ERRO NOSSO DE ERRO DA FONTE.
    #
    # O Yahoo registra 0,0100 para proventos do Itaú em 2018 - um centavo por
    # ação, quando o Itaú pagava mais de R$ 1,50. Com 4,96 bilhões de ações
    # (contagem que sabemos estar correta), o valor calculado de R$ 1,59 é o
    # plausível e o do Yahoo é que está truncado.
    #
    # Contar esses casos como "divergência" mediria a qualidade do Yahoo, não
    # a do nosso método. Ficam separados: valor do Yahoo suspeito quando é
    # baixo demais E a empresa é grande o bastante para que aquilo seja
    # implausível.
    c["yahoo_suspeito"] = (c["yahoo"] <= 0.02) & (c["acoes"] >= 100e6)
    n_susp = int(c["yahoo_suspeito"].sum())
    conf = c[~c["yahoo_suspeito"]]

    print("\n" + "=" * 78)
    print("VALIDAÇÃO CONTRA O YAHOO - por distribuição individual")
    print("=" * 78)
    print(f"{len(c)} distribuições pareadas ({c['ticker'].nunique()} tickers), "
          f"{sem_par} sem par em ±{tolerancia_dias} dias")
    if n_susp:
        print(f"  {n_susp} excluídas: valor do Yahoo <= R$ 0,02 em empresa com mais de")
        print(f"     100 mi de ações - implausível, provável truncamento na fonte")
    print(f"\n  Sobre as {len(conf)} confiáveis:")
    for faixa, lo, hi in [("±10%", 0.9, 1.1), ("±30%", 0.7, 1.3), ("±100%", 0.5, 2.0)]:
        print(f"    dentro de {faixa:<7} {conf['razao'].between(lo, hi).mean()*100:>5.1f}%")
    print(f"    razão mediana: {conf['razao'].median():.3f}")
    print(f"    defasagem mediana entre pagamento e data-ex: {conf['dias'].median():.0f} dias")

    print("\n  Por tipo de provento:")
    print(conf.groupby("tipo").agg(
        n=("razao", "size"),
        razao_mediana=("razao", lambda s: round(s.median(), 3)),
        dentro_30pct=("razao", lambda s: f"{s.between(0.7,1.3).mean()*100:.0f}%"),
    ).sort_values("n", ascending=False).head(6).to_string())

    ruins = conf[~conf["razao"].between(0.5, 2.0)]
    if not ruins.empty:
        print(f"\n  {len(ruins)} fora de 0,5-2,0. Os 10 maiores "
              f"(a coluna 'acoes' revela se a causa é contagem):")
        print(ruins.nlargest(min(10, len(ruins)), "razao")[
            ["ticker", "exercicio", "calculado", "yahoo", "razao", "acoes"]].to_string(index=False))


def gravar(engine, prov):
    with engine.begin() as conn:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    cols = ["cd_cvm", "exercicio", "classe", "data_pagamento", "tipo",
            "montante_total", "acoes_classe", "valor_por_acao", "bruto_por_acao",
            "nome_companhia"]
    d = prov[cols].where(pd.notna(prov[cols]), None)
    linhas = list(d.itertuples(index=False, name=None))
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM proventos_cvm")
            execute_values(cur, f"""
                INSERT INTO proventos_cvm ({','.join(cols)}) VALUES %s
                ON CONFLICT (cd_cvm, exercicio, classe, data_pagamento, tipo)
                DO UPDATE SET valor_por_acao = EXCLUDED.valor_por_acao
            """, linhas, page_size=1000)
        conn.commit()
    finally:
        conn.close()
    return len(linhas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2026)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    anos = list(range(args.de, args.ate + 1))
    print("Lendo distribuições de dividendos do FRE...")
    div = carregar_dividendos(anos)
    if div.empty:
        raise SystemExit("Nenhuma distribuição lida.")

    print("\nLendo o capital social (ações por classe)...")
    aprov = carregar_acoes_por_classe(anos)
    print(f"  {len(aprov)} aprovações, {aprov['CNPJ_Companhia'].nunique()} empresas")

    print("\nMontando de-para CNPJ -> CD_CVM...")
    cnpj_para_cd = mapa_cnpj_cd_cvm()
    print(f"  {len(cnpj_para_cd)} CNPJs")

    print("\nCalculando valor por ação...")
    prov = calcular(div, aprov, cnpj_para_cd)
    if prov.empty:
        raise SystemExit("Nenhum provento calculado.")
    print(f"  {len(prov)} proventos, {prov['cd_cvm'].nunique()} empresas")

    print("\nPor tipo:")
    print(prov["tipo"].value_counts().head(8).to_string())
    print("\nPor exercício:")
    print(prov.groupby("exercicio").size().to_string())
    print("\nValor por ação - distribuição:")
    print(prov["valor_por_acao"].describe(percentiles=[.05, .5, .95]).to_string())

    engine = create_engine(os.environ["DATABASE_URL"])
    validar_contra_yahoo(engine, prov)

    if args.dry_run:
        print("\n(dry-run - nada gravado)")
        return
    n = gravar(engine, prov)
    print(f"\n{n} proventos gravados em proventos_cvm.")


if __name__ == "__main__":
    main()