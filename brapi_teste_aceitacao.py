"""
Teste de aceitação da brapi Pro - roda no primeiro dia, decide sobre o reembolso.

O plano Pro custa R$ 116,66/mês (anual) e tem 7 dias de reembolso total. Este
script responde, com dados reais, se ele entrega o que o projeto precisa.

QUATRO TESTES, em ordem de importância
--------------------------------------
1. DESLISTADAS - o ponto que mais importa. A página de preços não menciona
   empresas que saíram da bolsa, e é justamente o que causa o viés de
   sobrevivência: o Yahoo remove esses tickers, e por isso o backtest mede uma
   carteira de sobreviventes. Se a brapi tiver, resolve o problema estrutural
   do projeto. Se não tiver, o COTAHIST continua sendo o único caminho.

2. PROFUNDIDADE REAL - a página diz "completos (10+ anos)", o que é vago. O
   projeto precisa de 2010. Se forem 10 anos contados de hoje, cobre só 2016+.

3. CASOS DIVERGENTES - hoje não há como saber quem está certo quando nós
   calculamos R$ 0,19 e o Yahoo diz R$ 0,01. Uma terceira fonte arbitra.
   Suspeita atual: o Yahoo trunca em dois decimais, porque BBDC3, BBDC4,
   ITSA3, ITSA4, POMO3, LREN3 e EUCA3 aparecem TODOS com exatamente 0,0100 -
   valor idêntico em empresas diferentes não é coincidência.

4. SPLITS - a tabela `splits` atual tem 244 tickers de 1.080 no COTAHIST. O
   ajuste proporcional de preço depende dela, e é o ajuste que mais importa
   (um desdobramento move o preço 50-99%; um dividendo, 1-3%).

Uso:
    BRAPI_TOKEN="..." python brapi_teste_aceitacao.py
    BRAPI_TOKEN="..." python brapi_teste_aceitacao.py --so-teste 1
"""
import argparse
import os
import sys
import time

import pandas as pd
import requests

URL = "https://brapi.dev/api/quote/{ticker}"

# Deslistadas confirmadas pelo COTAHIST, com o ano em que pararam de negociar.
# Todas têm histórico real - se a brapi não as tiver, é limitação dela.
DESLISTADAS = {
    "APER3": "Alper Seguros (saiu 2022)",
    "HGTX3": "Cia Hering (incorporada pelo Grupo Soma, 2021)",
    "CCRO3": "CCR (virou Motiva, 2025)",
    "CIEL3": "Cielo (fechou capital, 2024)",
    "ENBR3": "EDP Brasil (fechou capital, 2023)",
    "BIDI11": "Banco Inter (virou BDR, 2022)",
    "VALE5": "Vale PN (unificada em VALE3, 2017)",
    "SULA11": "SulAmérica (incorporada pela Rede D'Or, 2022)",
}

# Casos em que nós e o Yahoo divergimos por ~19x. O valor do Yahoo é sempre
# exatamente 0,0100, o que sugere truncamento.
DIVERGENTES = [
    ("BBDC3", 2011, 0.198739, 0.0100),
    ("BBDC4", 2015, 0.188901, 0.0100),
    ("ITSA4", 2016, 0.193800, 0.0100),
    ("LREN3", 2013, 0.396222, 0.0200),
    ("EGIE3", 2021, 0.967632, 0.0500),
]

LIQUIDOS = ["PETR4", "VALE3", "ITUB4", "BBDC4", "WEGE3"]


def buscar(ticker, token, **params):
    # `dividends=true` é OBRIGATÓRIO para a resposta incluir dividendsData.
    # Sem ele, PETR4 e VALE3 voltavam com zero dividendos - o que parecia
    # limitação da brapi e era parâmetro faltando.
    p = {"token": token, "dividends": "true", **params}
    try:
        r = requests.get(URL.format(ticker=ticker), params=p, timeout=40)
    except requests.exceptions.RequestException as e:
        return None, f"rede: {type(e).__name__}"
    if r.status_code != 200:
        try:
            msg = r.json().get("message", r.text[:90])
        except ValueError:
            msg = r.text[:90]
        return None, f"HTTP {r.status_code}: {msg}"
    try:
        res = r.json().get("results") or []
    except ValueError:
        return None, "resposta não é JSON"
    return (res[0] if res else None), None


def secao(n, titulo):
    print("\n" + "=" * 78)
    print(f"TESTE {n} - {titulo}")
    print("=" * 78)


# ------------------------------------------------------------------
def teste_deslistadas(token):
    secao(1, "DESLISTADAS (o mais importante)")
    print("Se a brapi tiver histórico destes, o viés de sobrevivência é")
    print("atacável por ela. Se não, o COTAHIST continua sendo o único caminho.\n")
    ok = 0
    for tk, desc in DESLISTADAS.items():
        d, err = buscar(tk, token, range="max", interval="1d")
        if err:
            print(f"  {tk:<8} FALHOU   {err[:50]}")
        else:
            h = (d or {}).get("historicalDataPrice") or []
            if h:
                ok += 1
                import datetime as dt
                d0 = dt.datetime.fromtimestamp(h[0]["date"]).date()
                d1 = dt.datetime.fromtimestamp(h[-1]["date"]).date()
                print(f"  {tk:<8} OK       {len(h):>5} pregões, {d0} a {d1}")
            else:
                print(f"  {tk:<8} VAZIO    ticker existe, sem série")
        print(f"           {desc}")
        time.sleep(0.4)
    print(f"\n  VEREDITO: {ok} de {len(DESLISTADAS)} com histórico.")
    if ok == len(DESLISTADAS):
        print("  Cobertura completa - resolve o viés de sobrevivência sozinha.")
    elif ok >= 3:
        print("  COBERTURA PARCIAL. A brapi MANTÉM deslistados (VALE5, com série")
        print("  de 2000 a 2019, prova isso), mas não todos. Não substitui o")
        print("  COTAHIST, que tem os 1.080 tickers - complementa como árbitro")
        print("  e como fonte de proventos com data-ex.")
    else:
        print("  Cobertura muito baixa - o COTAHIST continua sendo o caminho.")
    return ok


def teste_profundidade(token):
    secao(2, "PROFUNDIDADE REAL DO HISTÓRICO")
    print('A página diz "completos (10+ anos)". O projeto precisa de 2010.\n')
    for tk in LIQUIDOS[:3]:
        d, err = buscar(tk, token, range="max", interval="1d",
                         modules="balanceSheetHistory")
        if err:
            print(f"  {tk:<8} {err[:60]}")
            continue
        h = (d or {}).get("historicalDataPrice") or []
        div = (d or {}).get("dividendsData", {}) or {}
        cash = div.get("cashDividends") or []
        bal = ((d or {}).get("balanceSheetHistory") or {})
        bal = bal.get("balanceSheetStatements", bal) if isinstance(bal, dict) else bal
        import datetime as dt
        p0 = dt.datetime.fromtimestamp(h[0]["date"]).date() if h else "-"
        dv = sorted(str(c.get("lastDatePrior") or c.get("paymentDate") or "")[:10] for c in cash)
        bl = sorted(str(b.get("endDate") or "")[:10] for b in (bal or []))
        print(f"  {tk:<8} preços desde {p0}")
        print(f"           dividendos: {len(cash)} registros, mais antigo {dv[0] if dv else '-'}")
        print(f"           balanços:   {len(bal or [])} registros, mais antigo {bl[0] if bl else '-'}")
        time.sleep(0.4)
    print("\n  Confira se alcança 2010. Se parar em 2016, cobre menos que o COTAHIST.")


def teste_divergentes(token):
    secao(3, "ARBITRAGEM DOS CASOS DIVERGENTES")
    print("Nós calculamos ~R$ 0,19; o Yahoo diz R$ 0,01. Quem está certo?")
    print("O Yahoo mostra 0,0100 EXATO em empresas diferentes - suspeito de")
    print("truncamento em dois decimais.\n")
    print(f"  {'ticker':<8} {'ano':>5} {'nosso':>10} {'yahoo':>8} {'brapi':>12}  veredito")
    for tk, ano, nosso, yahoo in DIVERGENTES:
        d, err = buscar(tk, token, range="max", modules="")
        if err:
            print(f"  {tk:<8} {ano:>5} {nosso:>10.4f} {yahoo:>8.4f}  {err[:30]}")
            time.sleep(0.4)
            continue
        cash = ((d or {}).get("dividendsData", {}) or {}).get("cashDividends") or []
        # lastDatePrior é a DATA-EX (última data com direito ao provento) -
        # é ela que afeta o preço e que deve ser usada para ajuste. O
        # paymentDate vem semanas depois.
        do_ano = [c for c in cash
                  if str(c.get("lastDatePrior") or c.get("paymentDate") or "").startswith(str(ano))]
        total = sum(float(c.get("rate") or 0) for c in do_ano)
        if not do_ano:
            v = "sem dados"
        else:
            perto_nosso = abs(total - nosso) < abs(total - yahoo)
            v = f"{total:>12.4f}  {'CONFIRMA NÓS' if perto_nosso else 'confirma Yahoo'}"
        print(f"  {tk:<8} {ano:>5} {nosso:>10.4f} {yahoo:>8.4f}  {v}")
        time.sleep(0.4)
    print("\n  Se a brapi confirmar nossos valores, o método do FRE está validado")
    print("  e os ~2.300 casos 'divergentes' são limitação do Yahoo, não erro nosso.")


def teste_splits(token):
    secao(4, "COBERTURA DE SPLITS")
    print("A tabela `splits` atual tem 244 tickers de 1.080 no COTAHIST.")
    print("O ajuste proporcional é o que mais importa: um desdobramento move o")
    print("preço 50-99%; um dividendo, 1-3%.\n")
    alvos = ["PETR4", "WEGE3", "MGLU3", "ITUB4", "APER3", "HGTX3"]
    for tk in alvos:
        d, err = buscar(tk, token, range="max")
        if err:
            print(f"  {tk:<8} {err[:55]}")
            time.sleep(0.4)
            continue
        dd = (d or {}).get("dividendsData", {}) or {}
        sp = dd.get("stockDividends") or []
        if sp:
            datas = sorted(str(s.get("lastDatePrior") or s.get("approvedOn")
                                or s.get("date") or "")[:10] for s in sp)
            print(f"  {tk:<8} {len(sp):>3} eventos, de {datas[0]} a {datas[-1]}")
        else:
            print(f"  {tk:<8} nenhum evento retornado")
        time.sleep(0.4)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--so-teste", type=int, choices=[1, 2, 3, 4])
    args = p.parse_args()
    if not args.token:
        sys.exit("Token não informado. Use --token ou defina BRAPI_TOKEN.")

    print("TESTE DE ACEITAÇÃO DA BRAPI PRO")
    print("Objetivo: decidir sobre o reembolso de 7 dias com base em dados.")
    print(f"Custo estimado: ~25 requisições de 500.000/mês.")

    testes = {1: teste_deslistadas, 2: teste_profundidade,
              3: teste_divergentes, 4: teste_splits}
    for n, f in testes.items():
        if args.so_teste and n != args.so_teste:
            continue
        try:
            f(args.token)
        except Exception as e:
            print(f"\n  ERRO no teste {n}: {type(e).__name__}: {e}")

    print("\n" + "=" * 78)
    print("COMO DECIDIR")
    print("=" * 78)
    print("O teste 1 é o que pesa. Se a brapi cobrir deslistadas, ela resolve o")
    print("problema estrutural do projeto e vale o custo por si só.")
    print("Se não cobrir, ela ainda serve como ÁRBITRO (teste 3) e como")
    print("substituta do Yahoo no incremental - mas aí a decisão é sobre")
    print("conveniência, não sobre capacidade, e o COTAHIST continua necessário.")


if __name__ == "__main__":
    main()