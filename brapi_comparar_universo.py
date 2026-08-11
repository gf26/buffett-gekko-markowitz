"""
Compara o catálogo da brapi com a tabela `tickers` do banco.

RESPONDE
--------
1. Quais ações ON negociadas hoje na B3 NÃO estão na sua base?
2. Quais tickers da sua base NÃO aparecem no catálogo da brapi?
   (candidatos a deslistados, ou tickers com erro de digitação)

NÃO GRAVA NADA. Só compara e gera CSVs para você revisar.

PRÉ-REQUISITO
-------------
Rodar antes: python brapi_catalogo.py

CRITÉRIO DE "ON"
----------------
Ticker terminando em "3", conforme você definiu. BDRs terminam em 32/33/34/35,
então esse critério já os exclui naturalmente.

Uso:
    DATABASE_URL="..." python brapi_comparar_universo.py
    DATABASE_URL="..." python brapi_comparar_universo.py --todos  (não só ON)
"""
import argparse
import os
import sys

import pandas as pd
from sqlalchemy import create_engine, text

ARQUIVO_CATALOGO = "brapi_catalogo.csv"
SAIDA_FALTANDO = "brapi_acoes_faltando_na_base.csv"
SAIDA_SO_NA_BASE = "brapi_tickers_so_na_minha_base.csv"


def normalizar(ticker):
    """Sua base usa sufixo .SA (formato Yahoo); a brapi não usa."""
    return str(ticker).strip().upper().replace(".SA", "")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--todos", action="store_true",
                    help="Considera todas as ações, não só as terminadas em 3 (ON).")
    args = p.parse_args()

    if not os.path.exists(ARQUIVO_CATALOGO):
        sys.exit(f"{ARQUIVO_CATALOGO} não encontrado - rode brapi_catalogo.py primeiro.")

    cat = pd.read_csv(ARQUIVO_CATALOGO, dtype=str)
    # só o subtipo 'stock' é confiável: as demais categorias vieram com
    # exatamente 2 itens cada, o que indica amostra do plano gratuito e não
    # a lista real (a B3 tem centenas de FIIs, por exemplo)
    acoes = cat[cat["_subtipo"] == "stock"].copy()
    acoes["ticker_norm"] = acoes["stock"].apply(normalizar)

    if not args.todos:
        acoes = acoes[acoes["ticker_norm"].str.endswith("3")]
        print(f"Filtrando por ON (termina em 3): {len(acoes)} ações")
    else:
        print(f"Considerando todas as ações: {len(acoes)}")

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        base = pd.read_sql(text("SELECT ticker, name, active FROM tickers"), conn)
    base["ticker_norm"] = base["ticker"].apply(normalizar)
    print(f"Sua base tem {len(base)} tickers\n")

    na_base = set(base["ticker_norm"])
    no_catalogo = set(acoes["ticker_norm"])

    # 1. na brapi mas não na base = lacunas
    faltando = acoes[~acoes["ticker_norm"].isin(na_base)].copy()
    cols = [c for c in ["stock", "name", "sector", "subsector", "close", "volume", "market_cap"]
            if c in faltando.columns]
    faltando = faltando[cols].sort_values("volume", ascending=False, key=lambda s: pd.to_numeric(s, errors="coerce"))
    faltando.to_csv(SAIDA_FALTANDO, index=False, encoding="utf-8-sig")

    # 2. na base mas não na brapi = possíveis deslistados
    so_na_base = base[~base["ticker_norm"].isin(no_catalogo)].copy()
    so_na_base[["ticker", "name", "active"]].to_csv(SAIDA_SO_NA_BASE, index=False, encoding="utf-8-sig")

    print("=" * 70)
    print(f"FALTANDO NA SUA BASE: {len(faltando)} ações")
    print("=" * 70)
    if not faltando.empty:
        print("(ordenadas por volume - as de cima são as mais negociadas)\n")
        print(faltando.head(30).to_string(index=False))
        if len(faltando) > 30:
            print(f"\n... e mais {len(faltando) - 30}. Lista completa em {SAIDA_FALTANDO}")

    print("\n" + "=" * 70)
    print(f"NA SUA BASE MAS NÃO NO CATÁLOGO: {len(so_na_base)} tickers")
    print("=" * 70)
    print("Podem ser: deslistados (saíram da B3) ou erro de digitação.")
    if not so_na_base.empty:
        print()
        print(so_na_base[["ticker", "name", "active"]].head(30).to_string(index=False))
        if len(so_na_base) > 30:
            print(f"\n... e mais {len(so_na_base) - 30}. Lista completa em {SAIDA_SO_NA_BASE}")

    print(f"\nArquivos gerados: {SAIDA_FALTANDO}, {SAIDA_SO_NA_BASE}")
    print("\nPara adicionar as que faltam (exemplo):")
    print("  INSERT INTO tickers (ticker, name, active)")
    print("  VALUES ('XXXX3.SA', 'Nome da Empresa', TRUE)")
    print("  ON CONFLICT (ticker) DO NOTHING;")
    print("\nDepois: python ingest_prices.py  (para puxar o histórico de preço)")


if __name__ == "__main__":
    main()
