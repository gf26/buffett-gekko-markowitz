"""
Casamento ticker -> empresa da CVM pelo ISIN.

POR QUE ISIN E NÃO NOME
-----------------------
O universo tem 350 tickers; o COTAHIST tem 925 com preço. Expandir exige ligar
os novos a um CD_CVM da CVM.

A tentativa anterior usou similaridade de NOME e produziu 22 mapeamentos
errados: a Bombril recebeu os números do Banco do Brasil ("BOBRIL" x "BCO
BRASIL" dá 0,71 de similaridade), a Sansuy os da Cosan, a Bardella os da
Nordon. Um mapeamento errado é pior que um ausente, porque entra no ranking
parecendo legítimo.

O ISIN é identificador único e padronizado do papel (ISO 6166). Ele aparece:
  - no COTAHIST, campo CODISI (já ingerido em prices_cotahist)
  - no FCA da CVM, arquivo fca_cia_aberta_valor_mobiliario_AAAA.csv

Casar por ele é determinístico - ou é o mesmo papel, ou não é.

⚠️ ÍNDICES E ETFs
O filtro de formato aceita qualquer coisa com 4 letras + 3/4/5/6/11, o que
deixa passar IBOV11 (o índice Bovespa, com R$ 3,8 bi de volume diário) e
possivelmente ETFs. Estes são identificados e separados: um índice não tem
CD_CVM porque não é empresa.

Uso:
    DATABASE_URL="..." python isin_mapeamento.py
    DATABASE_URL="..." python isin_mapeamento.py --gerar-sql
"""
import argparse
import os
import re
import zipfile

import pandas as pd
from sqlalchemy import create_engine, text

PASTA = "dados_cvm"
CSV = "isin_candidatos.csv"

# ISIN brasileiro de AÇÃO: BR + 4 letras + ACN (ação nominativa) + tipo.
# BDR usa outro padrão; índices e ETFs também.
RE_ISIN_ACAO = re.compile(r"^BR[A-Z0-9]{4}(?:ACN|CDA)", re.IGNORECASE)

# nomes de pregão que denunciam índice ou fundo, não empresa
RE_NAO_EMPRESA = re.compile(
    r"\b(?:IBOVESPA|INDICE|[ÍI]NDICE|ETF|FDO|FUNDO|FII|IBRX|SMLL|IDIV|ICON|IMOB)\b",
    re.IGNORECASE)


def ler_fca_isin(anos):
    """ISIN -> CNPJ, dos arquivos FCA."""
    frames = []
    for ano in anos:
        p = os.path.join(PASTA, f"fca_cia_aberta_{ano}.zip")
        if not os.path.exists(p):
            continue
        zf = zipfile.ZipFile(p)
        nome = f"fca_cia_aberta_valor_mobiliario_{ano}.csv"
        if nome not in zf.namelist():
            continue
        with zf.open(nome) as f:
            d = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
        col_isin = next((c for c in d.columns if "ISIN" in c.upper()), None)
        if not col_isin:
            continue
        d = d[[c for c in ("CNPJ_Companhia", "Nome_Empresarial", "Codigo_Negociacao")
               if c in d.columns] + [col_isin]].copy()
        d = d.rename(columns={col_isin: "isin"})
        d["_ano"] = ano
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    t = pd.concat(frames, ignore_index=True)
    t["isin"] = t["isin"].astype(str).str.strip().str.upper()
    t = t[t["isin"].str.len() >= 10]
    return t.sort_values("_ano").drop_duplicates("isin", keep="last")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--min-pregoes", type=int, default=250)
    p.add_argument("--min-volume", type=float, default=50_000)
    p.add_argument("--gerar-sql", action="store_true")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        atuais = {r[0].replace(".SA", "") for r in
                  conn.execute(text("SELECT ticker FROM tickers")).fetchall()}
        cot = pd.read_sql(text("""
            SELECT ticker, COUNT(*) AS pregoes, AVG(volume) AS volume_medio,
                   MAX(date) AS ultimo, MAX(nome_pregao) AS nome, MAX(isin) AS isin
            FROM prices_cotahist GROUP BY ticker
        """), conn)
        mapa = pd.read_sql(text("SELECT ticker, cd_cvm FROM ticker_cvm_map"), conn)

    c = cot[~cot["ticker"].isin(atuais)].copy()
    c = c[c["ticker"].str.match(r"^[A-Z]{4}(?:3|4|5|6|11)$")]
    c = c[(c["pregoes"] >= args.min_pregoes) &
          (c["volume_medio"].fillna(0) >= args.min_volume)]
    print(f"{len(c)} candidatos fora do universo, com liquidez mínima\n")

    # separa o que não é empresa
    c["isin"] = c["isin"].astype(str).str.strip().str.upper()
    nao_emp = (c["nome"].astype(str).str.contains(RE_NAO_EMPRESA, na=False)
               | ~c["isin"].str.match(RE_ISIN_ACAO, na=False))
    fora = c[nao_emp]
    c = c[~nao_emp]
    if not fora.empty:
        print(f"{len(fora)} descartados por não serem ações de empresa "
              f"(índices, ETFs, ISIN de outro tipo):")
        print(fora[["ticker", "nome", "isin"]].head(12).to_string(index=False))
        print()

    print("Lendo ISIN dos arquivos FCA...")
    fca = ler_fca_isin(range(2010, 2027))
    if fca.empty:
        raise SystemExit("Nenhum FCA lido. Baixe fca_cia_aberta_AAAA.zip para dados_cvm/.")
    print(f"  {len(fca)} ISINs no FCA\n")

    # CNPJ -> CD_CVM pelo cadastro
    cad_p = os.path.join(PASTA, "cad_cia_aberta.csv")
    cnpj_cd = {}
    if os.path.exists(cad_p):
        cad = pd.read_csv(cad_p, sep=";", encoding="latin-1", dtype=str)
        c_cnpj = next((x for x in cad.columns if "CNPJ" in x.upper()), None)
        c_cd = next((x for x in cad.columns if x.upper() == "CD_CVM"), None)
        c_sit = next((x for x in cad.columns if x.upper() == "SIT"), None)
        if c_cnpj and c_cd:
            if c_sit:   # registro ATIVO vence o CANCELADO
                cad = cad.copy()
                cad["_a"] = cad[c_sit].astype(str).str.strip().str.upper().eq("ATIVO")
                cad = cad.sort_values("_a")
            cnpj_cd = dict(zip(cad[c_cnpj].astype(str).str.strip(),
                                cad[c_cd].astype(str).str.strip().str.lstrip("0")))
    print(f"{len(cnpj_cd)} CNPJs no cadastro\n")

    r = c.merge(fca[["isin", "CNPJ_Companhia", "Nome_Empresarial"]], on="isin", how="left")
    r["cd_cvm"] = r["CNPJ_Companhia"].map(cnpj_cd)

    ok = r[r["cd_cvm"].notna()].copy()
    sem = r[r["cd_cvm"].isna()].copy()

    ja = set(mapa["ticker"].str.replace(".SA", "", regex=False))
    ok["ja_mapeado"] = ok["ticker"].isin(ja)

    print("=" * 78)
    print(f"CASADOS POR ISIN: {len(ok)}")
    print("=" * 78)
    if not ok.empty:
        print(ok.sort_values("volume_medio", ascending=False)
              [["ticker", "nome", "Nome_Empresarial", "cd_cvm", "pregoes",
                "volume_medio", "ja_mapeado"]].head(30).to_string(index=False))
        ok.to_csv(CSV, index=False, encoding="utf-8-sig")
        print(f"\nCSV completo: {CSV}")

    print("\n" + "=" * 78)
    print(f"SEM CASAMENTO: {len(sem)}")
    print("=" * 78)
    print("O ISIN não está no FCA - empresa antiga, ou FCA daquele ano ausente.")
    if not sem.empty:
        print(sem.sort_values("volume_medio", ascending=False)
              [["ticker", "nome", "isin", "pregoes", "volume_medio"]]
              .head(15).to_string(index=False))

    if args.gerar_sql and not ok.empty:
        novos = ok[~ok["ja_mapeado"]]
        print("\n" + "=" * 78)
        print(f"SQL para {len(novos)} mapeamentos novos")
        print("=" * 78)
        print("-- CONFIRA a coluna Nome_Empresarial antes de rodar: o casamento é")
        print("-- por ISIN (determinístico), mas vale a conferência visual.")
        print("INSERT INTO ticker_cvm_map (ticker, cd_cvm, nome_cvm, confianca, vigente)")
        print("VALUES")
        linhas = [f"  ('{t}.SA', '{cd}', '{str(n).replace(chr(39), chr(39)*2)[:80]}', "
                  f"'isin', TRUE)"
                  for t, cd, n in zip(novos["ticker"], novos["cd_cvm"],
                                       novos["Nome_Empresarial"])]
        print(",\n".join(linhas))
        print("ON CONFLICT (ticker, cd_cvm) DO NOTHING;")


if __name__ == "__main__":
    main()
