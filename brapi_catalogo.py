"""
Catálogo de tickers da B3 via brapi.

O QUE FAZ
---------
Busca a lista de ativos negociados na B3, separada por subtipo, e salva em
arquivos locais. NÃO grava nada no banco - é um catálogo de referência.

POR QUE NÃO GRAVA NO BANCO
--------------------------
FIIs, ETFs, FI-Infra etc. não têm demonstrativo financeiro de empresa. Se
entrassem na tabela `tickers`, o ingest_prices.py tentaria baixar preço deles
todo dia e os scripts de cálculo tentariam computar Piotroski, Magic Formula
e afins - que não fazem sentido para esses ativos. Seria desperdício e ruído.

O uso pretendido é:
  - AGORA: comparar o subtipo "stock" com sua tabela `tickers` para achar
    ações que faltam (ver cvm_comparar_universo_brapi.py).
  - FUTURO: se um dia você quiser cobrir FIIs ou ETFs, o catálogo já está
    salvo e não custa requisição nova.

CUSTO
-----
Uma requisição por subtipo. Com 8 subtipos, são 8 requisições de 15.000/mês.

Uso:
    BRAPI_TOKEN="nxdosUF2HxKTHChnCUHhwc" python brapi_catalogo.py
    python brapi_catalogo.py --token nxdosUF2HxKTHChnCUHhwc --subtipos stock unit
"""
import argparse
import json
import os
import sys

import pandas as pd
import requests

URL = "https://brapi.dev/api/quote/list"

# 'bdr' fica de fora de propósito: são ações estrangeiras negociadas aqui,
# fora do escopo de um screener da B3.
SUBTIPOS_PADRAO = ["stock", "unit", "fii", "etf", "fi-infra", "fi-agro", "fip", "fidc"]

ARQUIVO_JSON = "brapi_catalogo.json"
ARQUIVO_CSV = "brapi_catalogo.csv"


def buscar(subtipo, token, timeout=60):
    params = {"type": "stock", "subType": subtipo, "token": token}
    try:
        resp = requests.get(URL, params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        print(f"  {subtipo:<10} ERRO DE REDE: {e}")
        return []

    if resp.status_code != 200:
        # mostra a mensagem da API - costuma explicar o motivo (limite de
        # plano, parâmetro inválido, etc.)
        try:
            msg = resp.json().get("message", resp.text[:100])
        except ValueError:
            msg = resp.text[:100]
        print(f"  {subtipo:<10} HTTP {resp.status_code}: {msg}")
        return []

    try:
        dados = resp.json()
    except ValueError:
        print(f"  {subtipo:<10} resposta não é JSON válido")
        return []

    itens = dados.get("stocks") or dados.get("indexes") or []
    for i in itens:
        i["_subtipo"] = subtipo

    # avisa se houver paginação (só pegaríamos a primeira página)
    paginas = dados.get("totalPages")
    if paginas and paginas > 1:
        print(f"  {subtipo:<10} {len(itens):>4} itens  (ATENÇÃO: {paginas} páginas - pegamos só a 1ª)")
    else:
        print(f"  {subtipo:<10} {len(itens):>4} itens")

    return itens


def main():
    p = argparse.ArgumentParser(description="Cataloga tickers da B3 via brapi.")
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--subtipos", nargs="*", default=SUBTIPOS_PADRAO)
    args = p.parse_args()

    if not args.token:
        sys.exit("Token não informado. Use --token SEU_TOKEN ou defina BRAPI_TOKEN.")

    print(f"Buscando {len(args.subtipos)} subtipos ({len(args.subtipos)} requisições de 15.000/mês)\n")

    todos = []
    for st in args.subtipos:
        todos.extend(buscar(st, args.token))

    if not todos:
        sys.exit("\nNada retornado. Confira o token e as mensagens de erro acima.")

    with open(ARQUIVO_JSON, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)

    df = pd.DataFrame(todos)
    df.to_csv(ARQUIVO_CSV, index=False, encoding="utf-8-sig")

    print(f"\n{'=' * 60}")
    print(f"Total: {len(todos)} ativos")
    print(f"{'=' * 60}")
    print(df["_subtipo"].value_counts().to_string())

    print(f"\nCampos disponíveis: {', '.join(df.columns)}")
    print(f"\nSalvo em: {ARQUIVO_JSON} e {ARQUIVO_CSV}")

    acoes = df[df["_subtipo"] == "stock"]
    if not acoes.empty and "stock" in df["_subtipo"].values:
        col_ticker = "stock" if "stock" in df.columns else df.columns[0]
        on = acoes[acoes[col_ticker].astype(str).str.endswith("3")]
        print(f"\nDas {len(acoes)} ações, {len(on)} terminam em '3' (ON, seu critério).")
        print("Próximo passo: comparar com sua tabela `tickers` para achar as que faltam.")


if __name__ == "__main__":
    main()
