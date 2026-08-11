"""
Teste: a brapi tem histórico de preços de empresas DESLISTADAS?

POR QUE ISTO IMPORTA
--------------------
O Yahoo Finance remove tickers que saíram da bolsa, em vez de arquivá-los.
Isso significa que empresas como APER3 (Alper) - que negociaram de verdade e
depois foram deslistadas - não têm série de preço na nossa base. E são
justamente essas que corrigiriam o viés de sobrevivência do backtest.

A pergunta é se a brapi mantém esse histórico. Como ela também deriva de
fontes públicas focadas no mercado atual, é bem possível que tenha a mesma
limitação. Este script responde isso em 30 segundos, antes de investir tempo
construindo um ingestor.

COMO USAR
---------
1. Pegue seu token em https://brapi.dev/dashboard
2. Rode:
     BRAPI_TOKEN="seu_token_aqui" python teste_brapi_deslistados.py

   Ou passe direto:
     python teste_brapi_deslistados.py --token seu_token_aqui

3. Para testar outros tickers:
     python teste_brapi_deslistados.py --tickers APER3 BIDI11 HGTX3
"""
import argparse
import os
import sys

import requests

URL = "https://brapi.dev/api/quote/{ticker}"

# Casos de teste, escolhidos de propósito:
#   - PETR4: ativa e líquida. CONTROLE - se falhar, o problema é o token.
#   - APER3: deslistada, com histórico confirmado no Investing.com.
#   - HGTX3 (Hering): incorporada pelo Grupo Soma em 2021.
#   - BIDI11 (Banco Inter): virou BDR após reestruturação em 2022.
TICKERS_PADRAO = ["PETR4", "APER3", "HGTX3", "BIDI11"]


def testar(ticker, token, timeout=30):
    params = {"range": "max", "interval": "1d", "token": token}
    try:
        resp = requests.get(URL.format(ticker=ticker), params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return {"ticker": ticker, "status": "ERRO DE REDE", "detalhe": str(e)[:80]}

    if resp.status_code == 401:
        return {"ticker": ticker, "status": "TOKEN INVÁLIDO", "detalhe": "confira o token"}
    if resp.status_code == 404:
        return {"ticker": ticker, "status": "NÃO ENCONTRADO", "detalhe": "ticker não existe na brapi"}
    if resp.status_code == 429:
        return {"ticker": ticker, "status": "LIMITE ATINGIDO", "detalhe": "cota de requisições esgotada"}
    if resp.status_code != 200:
        return {"ticker": ticker, "status": f"HTTP {resp.status_code}", "detalhe": resp.text[:80]}

    try:
        dados = resp.json()
    except ValueError:
        return {"ticker": ticker, "status": "RESPOSTA INVÁLIDA", "detalhe": resp.text[:80]}

    resultados = dados.get("results") or []
    if not resultados:
        return {"ticker": ticker, "status": "SEM RESULTADO", "detalhe": str(dados)[:80]}

    r = resultados[0]
    historico = r.get("historicalDataPrice") or []
    if not historico:
        return {"ticker": ticker, "status": "SEM HISTÓRICO",
                "detalhe": "ticker existe mas não devolveu série de preços"}

    import datetime as dt
    def data_de(item):
        try:
            return dt.datetime.fromtimestamp(item["date"]).date()
        except Exception:
            return "?"

    return {
        "ticker": ticker,
        "status": "OK",
        "detalhe": f"{len(historico)} pregões, de {data_de(historico[0])} a {data_de(historico[-1])}",
    }


def main():
    p = argparse.ArgumentParser(description="Testa se a brapi tem histórico de deslistados.")
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--tickers", nargs="*", default=TICKERS_PADRAO)
    args = p.parse_args()

    if not args.token:
        sys.exit("Token não informado. Use --token SEU_TOKEN ou defina BRAPI_TOKEN.")

    print(f"Testando {len(args.tickers)} tickers na brapi...\n")
    print(f"{'ticker':<10} {'status':<18} detalhe")
    print("-" * 78)

    resultados = []
    for t in args.tickers:
        r = testar(t, args.token)
        resultados.append(r)
        print(f"{r['ticker']:<10} {r['status']:<18} {r['detalhe']}")

    print("\n" + "=" * 78)
    controle = next((r for r in resultados if r["ticker"] == "PETR4"), None)
    if controle and controle["status"] != "OK":
        print("ATENÇÃO: até o ticker de CONTROLE (PETR4) falhou.")
        print("O problema é de token/conexão, não de cobertura. Resolva isso primeiro.")
        return

    deslistados = [r for r in resultados if r["ticker"] != "PETR4"]
    com_historico = [r for r in deslistados if r["status"] == "OK"]

    if com_historico:
        print(f"VEREDITO: a brapi TEM histórico de {len(com_historico)} de {len(deslistados)} deslistados.")
        print("Vale construir o ingestor - isso reduz o viés de sobrevivência do backtest.")
    else:
        print("VEREDITO: a brapi NÃO tem histórico dos deslistados testados.")
        print("Mesma limitação do Yahoo. Não vale construir o ingestor - o viés de")
        print("sobrevivência continua como limitação documentada, e o caminho seria")
        print("buscar os dados históricos de cotação da própria B3 (formato mais")
        print("trabalhoso) ou aceitar a limitação.")
    print("=" * 78)
    print(f"\nRequisições usadas neste teste: {len(args.tickers)} (de 15.000/mês no plano grátis)")


if __name__ == "__main__":
    main()
