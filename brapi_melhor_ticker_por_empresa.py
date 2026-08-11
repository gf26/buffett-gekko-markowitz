"""
Escolhe o melhor ticker por EMPRESA, combinando governança e liquidez.

O PROBLEMA
----------
Filtrar só por ON (termina em 3) tem dois efeitos diferentes, e só um deles é
intencional:

  (a) INTENCIONAL: quando a empresa tem ON e PN, você prefere a ON.
  (b) NÃO INTENCIONAL: empresas que só emitiram PN ou units ficam
      COMPLETAMENTE invisíveis - não entram no ranking, nem no cálculo de
      percentil dos peer groups, nem no backtest.

Este script resolve (b) sem abandonar (a).

CRITÉRIO
--------
Para cada empresa (agrupada por nome):
  1. Se NÃO existe ON  -> pega o ticker mais líquido que existir (PN/unit).
  2. Se existe ON       -> fica com a ON, EXCETO se outra classe for
                           significativamente mais líquida (ver --fator).

Sobre o fator de liquidez: no Brasil é comum a PN concentrar quase toda a
negociação mesmo com ON disponível (BBDC3/BBDC4, ITUB3/ITUB4). Um fator baixo
(1.5) troca em muitos casos de empate técnico; 3 a 5 captura melhor a ideia de
"a diferença é grande o suficiente para abrir mão da governança".

NÃO GRAVA NADA - gera CSVs para você revisar.

Uso:
    DATABASE_URL="..." python brapi_melhor_ticker_por_empresa.py
    DATABASE_URL="..." python brapi_melhor_ticker_por_empresa.py --fator 5
"""
import argparse
import os
import sys

import pandas as pd
from sqlalchemy import create_engine, text

ARQUIVO_CATALOGO = "brapi_catalogo.csv"
SAIDA_SEM_ON = "empresas_sem_on_para_adicionar.csv"
SAIDA_TROCA_LIQUIDEZ = "empresas_troca_por_liquidez.csv"


def normalizar_ticker(t):
    return str(t).strip().upper().replace(".SA", "")


def classe(ticker):
    """ON=3, PN=4, PNA=5, PNB=6, unit=11."""
    t = normalizar_ticker(ticker)
    if t.endswith("11"):
        return "unit"
    if t.endswith("3"):
        return "ON"
    if t.endswith("4"):
        return "PN"
    if t.endswith(("5", "6", "7", "8")):
        return "PN (classe especial)"
    return "outro"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fator", type=float, default=3.0,
                    help="Quantas vezes mais líquida outra classe precisa ser para "
                         "substituir a ON. Padrão: 3. Use 1.5 para trocar mais fácil.")
    args = p.parse_args()

    if not os.path.exists(ARQUIVO_CATALOGO):
        sys.exit(f"{ARQUIVO_CATALOGO} não encontrado - rode brapi_catalogo.py primeiro.")

    cat = pd.read_csv(ARQUIVO_CATALOGO, dtype=str)
    cat = cat[cat["_subtipo"].isin(["stock", "unit"])].copy()
    cat["ticker_norm"] = cat["stock"].apply(normalizar_ticker)
    cat["classe"] = cat["stock"].apply(classe)
    cat["volume_num"] = pd.to_numeric(cat["volume"], errors="coerce").fillna(0)

    # agrupa por empresa. O nome é o melhor identificador disponível: PETR3 e
    # PETR4 compartilham o mesmo `name` no catálogo.
    cat["empresa"] = cat["name"].astype(str).str.strip().str.upper()
    print(f"{len(cat)} tickers, {cat['empresa'].nunique()} empresas distintas\n")

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        base = pd.read_sql(text("SELECT ticker FROM tickers"), conn)
    na_base = set(base["ticker"].apply(normalizar_ticker))

    sem_on, troca = [], []

    for empresa, g in cat.groupby("empresa"):
        ons = g[g["classe"] == "ON"]
        outros = g[g["classe"] != "ON"]

        if ons.empty:
            # caso 1: empresa sem nenhuma ON - pega a mais líquida que houver
            if outros.empty:
                continue
            melhor = outros.loc[outros["volume_num"].idxmax()]
            if melhor["ticker_norm"] in na_base:
                continue
            sem_on.append({
                "empresa": empresa,
                "ticker_sugerido": melhor["stock"],
                "classe": melhor["classe"],
                "volume": melhor["volume_num"],
                "setor": melhor.get("sector"),
                "close": melhor.get("close"),
                "outras_classes": ", ".join(sorted(set(outros["stock"]) - {melhor["stock"]})) or "-",
            })
            continue

        # caso 2: tem ON - só troca se outra classe for MUITO mais líquida
        on = ons.loc[ons["volume_num"].idxmax()]
        if outros.empty:
            continue
        alt = outros.loc[outros["volume_num"].idxmax()]
        vol_on = on["volume_num"]
        if vol_on > 0 and alt["volume_num"] >= vol_on * args.fator:
            troca.append({
                "empresa": empresa,
                "ticker_on": on["stock"],
                "volume_on": vol_on,
                "ticker_alternativo": alt["stock"],
                "classe_alternativa": alt["classe"],
                "volume_alternativo": alt["volume_num"],
                "quantas_vezes_mais_liquida": round(alt["volume_num"] / vol_on, 1),
                "on_ja_na_base": on["ticker_norm"] in na_base,
                "alt_ja_na_base": alt["ticker_norm"] in na_base,
            })

    df_sem_on = pd.DataFrame(sem_on).sort_values("volume", ascending=False) if sem_on else pd.DataFrame()
    df_troca = pd.DataFrame(troca).sort_values("quantas_vezes_mais_liquida", ascending=False) if troca else pd.DataFrame()

    if not df_sem_on.empty:
        df_sem_on.to_csv(SAIDA_SEM_ON, index=False, encoding="utf-8-sig")
    if not df_troca.empty:
        df_troca.to_csv(SAIDA_TROCA_LIQUIDEZ, index=False, encoding="utf-8-sig")

    print("=" * 78)
    print(f"1. EMPRESAS SEM NENHUMA ON, ausentes da sua base: {len(df_sem_on)}")
    print("   (o efeito não intencional do filtro - estas você nunca viu)")
    print("=" * 78)
    if not df_sem_on.empty:
        print(df_sem_on.head(30).to_string(index=False))
        if len(df_sem_on) > 30:
            print(f"\n... e mais {len(df_sem_on) - 30}. Completo em {SAIDA_SEM_ON}")
    else:
        print("   Nenhuma - sua base já cobre todas.")

    print("\n" + "=" * 78)
    print(f"2. EMPRESAS ONDE OUTRA CLASSE É >= {args.fator}x MAIS LÍQUIDA: {len(df_troca)}")
    print("   (decisão sua: trocar prioriza execução; manter prioriza governança)")
    print("=" * 78)
    if not df_troca.empty:
        print(df_troca.head(30).to_string(index=False))
        if len(df_troca) > 30:
            print(f"\n... e mais {len(df_troca) - 30}. Completo em {SAIDA_TROCA_LIQUIDEZ}")
    else:
        print(f"   Nenhuma com fator {args.fator}. Tente --fator 1.5 para ver mais casos.")

    print("\nPara adicionar (exemplo):")
    print("  INSERT INTO tickers (ticker, name, active)")
    print("  VALUES ('XXXX4.SA', 'Nome da Empresa', TRUE)")
    print("  ON CONFLICT (ticker) DO NOTHING;")
    print("\nDepois: python ingest_prices.py")


if __name__ == "__main__":
    main()
