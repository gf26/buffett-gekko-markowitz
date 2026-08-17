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
    nome_companhia    TEXT,
    PRIMARY KEY (cd_cvm, exercicio, classe, data_pagamento, tipo)
);
CREATE INDEX IF NOT EXISTS idx_proventos_cvm_cd ON proventos_cvm(cd_cvm);
"""


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
    chaves = ["CNPJ_Companhia", "exercicio", "classe", "_pagamento", "Dividendo_Distribuido"]
    t = t.drop_duplicates(chaves, keep="last")
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
    t = t[(t["Quantidade_Acoes_Ordinarias"].fillna(0)
           + t["Quantidade_Acoes_Preferenciais"].fillna(0)) > 0]

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
            "valor_por_acao": float(r["Montante"]) / float(n),
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


def validar_contra_yahoo(engine, prov):
    """Compara o valor calculado com a tabela `dividends` do Yahoo.

    É a checagem que decide se o método pode ser aplicado aos deslistados:
    se bate nos tickers que o Yahoo cobre, bate nos que ele não cobre."""
    with engine.connect() as conn:
        mapa = pd.read_sql(text("SELECT ticker, cd_cvm FROM ticker_cvm_map"), conn)
        yah = pd.read_sql(text("""
            SELECT ticker, EXTRACT(YEAR FROM ex_date)::int AS ano, SUM(amount) AS total_yahoo
            FROM dividends GROUP BY 1, 2
        """), conn)
    if yah.empty:
        print("\n  Sem dados do Yahoo para comparar.")
        return

    mapa["cd_cvm"] = mapa["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    mapa["classe"] = mapa["ticker"].str.replace(".SA", "", regex=False).str[4:5].map(
        {"3": "ON", "4": "PN", "5": "PN", "6": "PN"})

    p = prov.groupby(["cd_cvm", "exercicio", "classe"], as_index=False)["valor_por_acao"].sum()
    p = p.merge(mapa[["ticker", "cd_cvm", "classe"]], on=["cd_cvm", "classe"])
    comp = p.merge(yah, left_on=["ticker", "exercicio"], right_on=["ticker", "ano"])
    comp = comp[comp["total_yahoo"] > 0]
    if comp.empty:
        print("\n  Nenhum par comparável com o Yahoo.")
        return

    comp["razao"] = comp["valor_por_acao"] / comp["total_yahoo"]
    dentro = comp["razao"].between(0.7, 1.3)
    print("\n" + "=" * 78)
    print("VALIDAÇÃO CONTRA O YAHOO")
    print("=" * 78)
    print(f"{len(comp)} comparações (ticker × exercício), {comp['ticker'].nunique()} tickers")
    print(f"  razão dentro de ±30%: {dentro.mean()*100:.1f}%")
    print(f"  razão mediana: {comp['razao'].median():.3f}  (1,0 = coincidência perfeita)")
    print("\n  Nota: divergência é esperada em parte - o exercício SOCIAL do FRE não")
    print("  coincide com o ano-calendário da data-ex do Yahoo, então proventos de")
    print("  dezembro caem em anos diferentes nas duas fontes.")
    ruins = comp[~comp["razao"].between(0.5, 2.0)]
    if not ruins.empty:
        print(f"\n  {len(ruins)} casos com razão fora de 0,5-2,0 (10 maiores):")
        print(ruins.nlargest(min(10, len(ruins)), "razao")[
            ["ticker", "exercicio", "valor_por_acao", "total_yahoo", "razao"]].to_string(index=False))


def gravar(engine, prov):
    with engine.begin() as conn:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    cols = ["cd_cvm", "exercicio", "classe", "data_pagamento", "tipo",
            "montante_total", "acoes_classe", "valor_por_acao", "nome_companhia"]
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
