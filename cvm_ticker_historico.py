"""
Tabela `ticker_historico` - qual ticker cada empresa negociou, e quando.

PARA QUE SERVE
--------------
Atacar o VIÉS DE SOBREVIVÊNCIA. Hoje o universo só contém empresas que existem
HOJE - as que quebraram ou saíram da bolsa não estão, o que torna todo
backtest otimista de forma não mensurável.

O FCA (`fca_cia_aberta_valor_mobiliario_AAAA.csv`) lista, por empresa e ano,
os valores mobiliários emitidos - com código de negociação, mercado, segmento
de listagem e datas de início/fim de negociação. Cruzando os 16 anos de
arquivos, monta-se o mapa de quem negociou o quê e quando, incluindo empresas
que já saíram.

Com essa tabela mais as cotações históricas da B3, dá para reconstruir o
universo investível de cada data do passado - que é o que falta para o
backtest deixar de ser otimista.

⚠️ LIMITAÇÃO CONHECIDA: na amostra do FCA 2013, a coluna Codigo_Negociacao
veio VAZIA para várias empresas (inclusive Banco do Brasil). Este script
reporta a taxa de preenchimento por ano antes de gravar - se os anos antigos
vierem vazios, o mapa histórico fica limitado aos anos recentes e o problema
do viés de sobrevivência só é parcialmente resolvido. Rode com --dry-run
primeiro para ver.

Uso:
    DATABASE_URL="..." python cvm_ticker_historico.py --dry-run
    DATABASE_URL="..." python cvm_ticker_historico.py
"""
import argparse
import os
import re
import warnings
import zipfile

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

warnings.filterwarnings("ignore", category=pd.errors.DtypeWarning)

PASTA_CACHE = "dados_cvm"
CSV_SAIDA = "ticker_historico.csv"

# só interessam ações e units - o arquivo também lista debêntures, bônus etc.
RE_ACAO = re.compile(r"a[cç][õo]es|unit", re.IGNORECASE)


def ler_fca(ano):
    caminho = os.path.join(PASTA_CACHE, f"fca_cia_aberta_{ano}.zip")
    if not os.path.exists(caminho):
        return pd.DataFrame()
    zf = zipfile.ZipFile(caminho)
    nome = f"fca_cia_aberta_valor_mobiliario_{ano}.csv"
    if nome not in zf.namelist():
        return pd.DataFrame()
    with zf.open(nome) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
    df["_ano"] = ano
    return df


def mapa_cnpj_cd_cvm():
    """CNPJ -> CD_CVM. Usa o CADASTRO, não os arquivos DFP.

    O cadastro (`cad_cia_aberta.csv`) traz TODAS as empresas já registradas na
    CVM, inclusive as canceladas - que são justamente as que interessam aqui.
    Montar o de-para a partir dos DFP deixaria de fora quem parou de entregar
    demonstrativo, ou seja, exatamente as deslistadas."""
    caminho = os.path.join(PASTA_CACHE, "cad_cia_aberta.csv")
    if not os.path.exists(caminho):
        print(f"  aviso: {caminho} não encontrado - o de-para CNPJ->CD_CVM ficará vazio")
        return {}
    df = pd.read_csv(caminho, sep=";", encoding="latin-1", dtype=str)
    col_cnpj = next((c for c in df.columns if "CNPJ" in c.upper()), None)
    col_cd = next((c for c in df.columns if c.upper() in ("CD_CVM", "CODIGO_CVM")), None)
    if not col_cnpj or not col_cd:
        print(f"  aviso: colunas de CNPJ/CD_CVM não encontradas no cadastro: {list(df.columns)[:10]}")
        return {}
    df[col_cd] = df[col_cd].astype(str).str.strip().str.lstrip("0")
    return dict(zip(df[col_cnpj].astype(str).str.strip(), df[col_cd]))


def normalizar_ticker(t):
    if not isinstance(t, str):
        return None
    t = t.strip().upper()
    # o campo às vezes traz vários códigos separados por vírgula ou barra
    t = re.split(r"[,;/\s]+", t)[0]
    return t or None


def montar(anos):
    frames = []
    cobertura = []
    for ano in anos:
        df = ler_fca(ano)
        if df.empty:
            cobertura.append({"ano": ano, "linhas": 0, "com_ticker": 0, "pct": 0.0})
            continue
        col_vm = next((c for c in df.columns if "Valor_Mobiliario" in c), None)
        col_cod = next((c for c in df.columns if "Codigo_Negociacao" in c), None)
        if col_vm is None or col_cod is None:
            print(f"  {ano}: colunas esperadas ausentes - {list(df.columns)[:8]}")
            continue
        acoes = df[df[col_vm].astype(str).str.contains(RE_ACAO, na=False)].copy()
        acoes["ticker"] = acoes[col_cod].apply(normalizar_ticker)
        n_ok = acoes["ticker"].notna().sum()
        cobertura.append({"ano": ano, "linhas": len(acoes), "com_ticker": int(n_ok),
                           "pct": round(100 * n_ok / len(acoes), 1) if len(acoes) else 0.0})
        frames.append(acoes)

    cob = pd.DataFrame(cobertura)
    print("\nPreenchimento de Codigo_Negociacao por ano:")
    print(cob.to_string(index=False))
    vazios = cob[(cob["linhas"] > 0) & (cob["pct"] < 50)]
    if not vazios.empty:
        print(f"\n  ATENÇÃO: {len(vazios)} ano(s) com menos de 50% dos códigos preenchidos.")
        print("  Nesses anos o mapa histórico fica incompleto - o campo vem vazio na fonte.")

    if not frames:
        return pd.DataFrame(), cob
    return pd.concat(frames, ignore_index=True), cob


def consolidar(bruto, cnpj_para_cd):
    """Uma linha por (empresa, ticker), com o intervalo em que apareceu."""
    col = lambda nome: next((c for c in bruto.columns if nome in c), None)
    c_cnpj = col("CNPJ_Companhia")
    c_nome = col("Nome_Empresarial") or col("Nome_Companhia")
    c_vm = col("Valor_Mobiliario")
    c_merc = col("Mercado")
    c_seg = col("Segmento")
    c_ini = col("Data_Inicio_Negociacao")
    c_fim = col("Data_Fim_Negociacao")

    d = bruto[bruto["ticker"].notna()].copy()
    if d.empty:
        return pd.DataFrame()

    d["cnpj"] = d[c_cnpj].astype(str).str.strip()
    d["cd_cvm"] = d["cnpj"].map(cnpj_para_cd)

    agg = d.groupby(["cnpj", "ticker"], as_index=False).agg(
        cd_cvm=("cd_cvm", "first"),
        nome=(c_nome, "last") if c_nome else ("ticker", "first"),
        tipo_valor_mobiliario=(c_vm, "last") if c_vm else ("ticker", "first"),
        mercado=(c_merc, "last") if c_merc else ("ticker", "first"),
        segmento=(c_seg, "last") if c_seg else ("ticker", "first"),
        data_inicio_negociacao=(c_ini, "min") if c_ini else ("ticker", "first"),
        data_fim_negociacao=(c_fim, "max") if c_fim else ("ticker", "first"),
        primeiro_ano_fca=("_ano", "min"),
        ultimo_ano_fca=("_ano", "max"),
    )
    return agg


def gravar(engine, df):
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticker_historico (
                    cnpj                    TEXT NOT NULL,
                    ticker                  TEXT NOT NULL,
                    cd_cvm                  TEXT,
                    nome                    TEXT,
                    tipo_valor_mobiliario   TEXT,
                    mercado                 TEXT,
                    segmento                TEXT,
                    data_inicio_negociacao  DATE,
                    data_fim_negociacao     DATE,
                    primeiro_ano_fca        INT,
                    ultimo_ano_fca          INT,
                    atualizado_em           TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (cnpj, ticker)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ticker_historico_ticker ON ticker_historico(ticker)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ticker_historico_cd ON ticker_historico(cd_cvm)")
            cur.execute("DELETE FROM ticker_historico")

            def data(v):
                try:
                    d = pd.to_datetime(v, errors="coerce")
                    return None if pd.isna(d) else d.date()
                except Exception:
                    return None

            linhas = [(r["cnpj"], r["ticker"], r.get("cd_cvm"), r.get("nome"),
                        r.get("tipo_valor_mobiliario"), r.get("mercado"), r.get("segmento"),
                        data(r.get("data_inicio_negociacao")), data(r.get("data_fim_negociacao")),
                        int(r["primeiro_ano_fca"]), int(r["ultimo_ano_fca"]))
                       for _, r in df.iterrows()]
            execute_values(cur, """
                INSERT INTO ticker_historico
                    (cnpj, ticker, cd_cvm, nome, tipo_valor_mobiliario, mercado, segmento,
                     data_inicio_negociacao, data_fim_negociacao, primeiro_ano_fca, ultimo_ano_fca)
                VALUES %s
                ON CONFLICT (cnpj, ticker) DO UPDATE SET
                    cd_cvm = EXCLUDED.cd_cvm, nome = EXCLUDED.nome,
                    data_fim_negociacao = EXCLUDED.data_fim_negociacao,
                    ultimo_ano_fca = EXCLUDED.ultimo_ano_fca, atualizado_em = now()
            """, linhas, page_size=1000)
        conn.commit()
    finally:
        conn.close()
    return len(df)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2025)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    anos = list(range(args.de, args.ate + 1))
    print(f"Lendo FCA de {args.de} a {args.ate}...")
    bruto, cob = montar(anos)
    if bruto.empty:
        raise SystemExit("\nNenhum dado utilizável. Confira se os arquivos FCA estão em dados_cvm/.")

    print("\nMontando de-para CNPJ -> CD_CVM a partir do cadastro...")
    cnpj_para_cd = mapa_cnpj_cd_cvm()
    print(f"  {len(cnpj_para_cd)} CNPJs no cadastro")

    df = consolidar(bruto, cnpj_para_cd)
    df.to_csv(CSV_SAIDA, index=False, encoding="utf-8-sig")

    print(f"\n{len(df)} pares (empresa, ticker) distintos")
    print(f"  {df['cd_cvm'].notna().sum()} com CD_CVM")
    if "tipo_valor_mobiliario" in df.columns:
        print("\nPor tipo de valor mobiliário:")
        print(df["tipo_valor_mobiliario"].value_counts().head(8).to_string())

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        atuais = {r[0].replace(".SA", "") for r in conn.execute(text("SELECT ticker FROM tickers")).fetchall()}
    hist = set(df["ticker"])
    novos = hist - atuais
    print(f"\n{len(atuais)} tickers no seu universo atual")
    print(f"{len(hist)} tickers no histórico do FCA")
    print(f"{len(novos)} tickers que EXISTIRAM mas não estão no seu universo")
    if novos:
        amostra = df[df["ticker"].isin(sorted(novos)[:15])]
        print("\n  amostra (candidatos a resolver o viés de sobrevivência):")
        cols = [c for c in ["ticker", "nome", "data_fim_negociacao", "ultimo_ano_fca"] if c in amostra.columns]
        print(amostra[cols].head(15).to_string(index=False))

    print(f"\nCSV gerado: {CSV_SAIDA}")
    if args.dry_run:
        print("(dry-run - nada gravado no banco)")
        return
    n = gravar(engine, df)
    print(f"{n} linhas gravadas em ticker_historico.")
    print("\nPróximo passo: cruzar com as cotações históricas da B3 para reconstruir")
    print("o universo investível de cada data - o que remove o viés de sobrevivência.")


if __name__ == "__main__":
    main()
