"""
Candidatos para expandir o universo - com revisão, não em bloco.

POR QUE NÃO ADICIONAR TUDO
--------------------------
A brapi tem 476 tickers com proventos e o COTAHIST tem 925 com preço. Mas nem
todo ticker é candidato ao screener: há deslistados sem relevância, papéis que
quase não negociam e casos que exigem mapeamento manual.

E há precedente: a última expansão automática produziu 22 mapeamentos errados
por similaridade de nome - a Bombril recebeu os números do Banco do Brasil,
a Sansuy os da Cosan. Um mapeamento errado é pior que um ausente, porque entra
no ranking parecendo legítimo.

Este script CLASSIFICA os candidatos por qualidade de evidência, para que a
adição seja informada. Ele não altera nada sozinho.

OS TRÊS GRUPOS
--------------
A. PRONTOS - já têm CD_CVM identificável pelo prefixo de outra classe da mesma
   empresa (ITUB3 existe, então ITUB4 herda o mesmo CD_CVM). Baixo risco.

B. COM ISIN - o COTAHIST traz o ISIN, e o FCA da CVM também. Casar por ISIN é
   seguro, diferente de casar por nome.

C. SÓ POR NOME - o grupo perigoso. Exige confirmação individual.

Uso:
    DATABASE_URL="..." python universo_candidatos.py
    DATABASE_URL="..." python universo_candidatos.py --min-pregoes 500 --min-volume 100000
"""
import argparse
import os
import re

import pandas as pd
from sqlalchemy import create_engine, text

RE_TICKER = re.compile(r"^[A-Z]{4}(?:3|4|5|6|11)$")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--min-pregoes", type=int, default=250,
                    help="Mínimo de pregões no histórico (padrão: ~1 ano)")
    p.add_argument("--min-volume", type=float, default=50_000,
                    help="Volume financeiro médio diário mínimo, em R$")
    p.add_argument("--csv", default="universo_candidatos.csv")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        atuais = {r[0] for r in conn.execute(text("SELECT ticker FROM tickers")).fetchall()}
        mapeados = {r[0] for r in conn.execute(text(
            "SELECT ticker FROM ticker_cvm_map")).fetchall()}
        cot = pd.read_sql(text("""
            SELECT ticker,
                   COUNT(*) AS pregoes,
                   AVG(volume) AS volume_medio,
                   MAX(date) AS ultimo_pregao,
                   MAX(nome_pregao) AS nome,
                   MAX(isin) AS isin
            FROM prices_cotahist
            GROUP BY ticker
        """), conn)
        com_prov = {r[0] for r in conn.execute(text(
            "SELECT DISTINCT ticker FROM proventos_brapi")).fetchall()}
        mapa_cd = pd.read_sql(text("SELECT ticker, cd_cvm, nome_cvm FROM ticker_cvm_map"), conn)

    atuais_sem_sufixo = {t.replace(".SA", "") for t in atuais}
    mapa_cd["base"] = mapa_cd["ticker"].str.replace(".SA", "", regex=False)
    prefixo_para_cd = dict(zip(mapa_cd["base"].str[:4], mapa_cd["cd_cvm"]))
    nome_por_cd = dict(zip(mapa_cd["cd_cvm"], mapa_cd["nome_cvm"]))

    c = cot[~cot["ticker"].isin(atuais_sem_sufixo)].copy()
    c = c[c["ticker"].str.match(RE_TICKER)]
    print(f"{len(cot)} tickers no COTAHIST")
    print(f"{len(c)} fora do universo e com formato de ação/unit\n")

    antes = len(c)
    c = c[(c["pregoes"] >= args.min_pregoes) &
          (c["volume_medio"].fillna(0) >= args.min_volume)]
    print(f"Filtros: >= {args.min_pregoes} pregões e >= R$ {args.min_volume:,.0f}/dia")
    print(f"  {antes} -> {len(c)} candidatos\n")

    c["prefixo"] = c["ticker"].str[:4]
    c["cd_cvm_prefixo"] = c["prefixo"].map(prefixo_para_cd)
    c["empresa_cvm"] = c["cd_cvm_prefixo"].map(nome_por_cd)
    c["tem_proventos"] = c["ticker"].isin(com_prov)
    c["ainda_negocia"] = pd.to_datetime(c["ultimo_pregao"]) >= pd.Timestamp("2025-01-01")

    a = c[c["cd_cvm_prefixo"].notna()].copy()
    b = c[c["cd_cvm_prefixo"].isna() & c["isin"].notna() & (c["isin"] != "")].copy()
    d = c[c["cd_cvm_prefixo"].isna() & (c["isin"].isna() | (c["isin"] == ""))].copy()

    print("=" * 78)
    print(f"GRUPO A - {len(a)} com CD_CVM herdado do prefixo (baixo risco)")
    print("=" * 78)
    print("Outra classe da mesma empresa já está mapeada - ITUB4 herda o CD_CVM")
    print("de ITUB3. É o mesmo emissor, então o risco de erro é baixo.\n")
    if not a.empty:
        cols = ["ticker", "nome", "empresa_cvm", "pregoes", "volume_medio",
                "ainda_negocia", "tem_proventos"]
        print(a.sort_values("volume_medio", ascending=False)[cols]
              .head(30).to_string(index=False))

    print("\n" + "=" * 78)
    print(f"GRUPO B - {len(b)} sem prefixo conhecido, mas COM ISIN")
    print("=" * 78)
    print("O ISIN é identificador único do papel e aparece tanto no COTAHIST")
    print("quanto no FCA da CVM. Casar por ele é seguro - diferente de casar")
    print("por nome, que já produziu 22 erros neste projeto.\n")
    if not b.empty:
        print(b.sort_values("volume_medio", ascending=False)
              [["ticker", "nome", "isin", "pregoes", "volume_medio", "ainda_negocia"]]
              .head(30).to_string(index=False))

    print("\n" + "=" * 78)
    print(f"GRUPO C - {len(d)} sem prefixo e sem ISIN")
    print("=" * 78)
    print("Só restaria casar por nome. NÃO fazer automaticamente.\n")
    if not d.empty:
        print(d.sort_values("volume_medio", ascending=False)
              [["ticker", "nome", "pregoes", "volume_medio", "ainda_negocia"]]
              .head(20).to_string(index=False))

    c["grupo"] = ["A" if pd.notna(x) else ("B" if isin else "C")
                  for x, isin in zip(c["cd_cvm_prefixo"],
                                      c["isin"].fillna("").astype(bool))]
    c.sort_values(["grupo", "volume_medio"], ascending=[True, False]).to_csv(
        args.csv, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 78)
    print(f"CSV: {args.csv}")
    print("=" * 78)
    print(f"Grupo A ({len(a)}): revise a coluna `empresa_cvm` - se o nome bater")
    print("  com o do papel, o mapeamento é seguro.")
    print(f"Grupo B ({len(b)}): precisa de um passo a mais, casando o ISIN com o")
    print("  FCA da CVM. Vale escrever se o grupo for grande.")
    print(f"Grupo C ({len(d)}): confirmar um a um, ou deixar de fora.")
    print("\nDepois de revisar, o INSERT em ticker_cvm_map usa")
    print("confianca='inserido_manualmente'.")


if __name__ == "__main__":
    main()
