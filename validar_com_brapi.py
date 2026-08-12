"""
Validação cruzada: nossos dados (CVM/Yahoo) x brapi.

O QUE ISTO TESTA
----------------
Se o de-para da CVM foi implementado corretamente. A brapi deriva da CVM, então
concordância alta indica que o PARSING está certo; divergência aponta conta
trocada, escala errada ou plano de contas mal aplicado.

O QUE ISTO **NÃO** TESTA
------------------------
Não é uma terceira fonte independente. Se brapi e CVM concordarem e o Yahoo
divergir, isso sugere que o Yahoo está errado - mas as duas primeiras vêm da
mesma origem. O valor real aqui é pegar erro de implementação nosso.

AMOSTRA (escolhida por RISCO, não por representatividade)
----------------------------------------------------------
Prioriza os casos onde já sabemos ou suspeitamos de problema:
  - Bancos (plano de contas diferente, recém-implementado)
  - Empresa "Financial Services" que usa plano PADRÃO (armadilha da detecção)
  - Reportadoras em moeda estrangeira (Yahoo em USD rotulado como BRL)
  - Saltos suspeitos detectados na validação da carga
  - Um representante de cada setor, como controle

CUSTO: 1 requisição por ticker (~20 de 15.000/mês).

Uso:
    BRAPI_TOKEN="..." DATABASE_URL="..." python validar_com_brapi.py
    BRAPI_TOKEN="..." DATABASE_URL="..." python validar_com_brapi.py --tickers ITUB3 BBDC3
"""
import argparse
import os
import sys
import time

import pandas as pd
import requests
from sqlalchemy import create_engine, text

URL = "https://brapi.dev/api/quote/{ticker}"

# --- amostra: por que cada um está aqui ---
AMOSTRA = {
    # Bancos: plano de contas próprio, implementado esta semana. Maior risco.
    "ITUB3": "banco - salto de 11,3x na fronteira CVM/Yahoo",
    "BBDC3": "banco - único já validado manualmente (PL 168,4 bi confere)",
    "BPAC3": "banco - salto de 8,4x",
    "PINE4": "banco pequeno - salto de 23,5x",
    # Armadilha da detecção: Yahoo diz 'Financial Services' mas usa plano padrão
    "B3SA3": "bolsa - setor financeiro no Yahoo, plano de contas PADRÃO",
    # Moeda: Yahoo reporta em USD rotulado como BRL
    "VALE3": "reporta em USD no Yahoo (razão 6,19)",
    "EMBJ3": "reporta em USD no Yahoo (salto de 6,79x)",
    # Saltos extremos ainda inexplicados
    "AZTE3": "salto de 192.262x - inexplicado",
    "FICT3": "salto de 81.814x - inexplicado",
    "PDTC3": "salto de 907x - possível erro de escala",
    "AMAR3": "salto de 609x",
    # PL negativo (razão negativa) - conferir se é real
    "RCSL3": "PL virou negativo - confirmar se é evento real",
    # Controle: um por setor, sem problema conhecido
    "PETR3": "controle - energia/petróleo",
    "WEGE3": "controle - bens industriais",
    "ABEV3": "controle - consumo",
    "SUZB3": "controle - papel e celulose",
    "RADL3": "controle - saúde/varejo",
    "TOTS3": "controle - tecnologia",
    "EGIE3": "controle - utilities",
    "MULT3": "controle - imobiliário",
}

# de-para: campo da brapi -> line_item nosso
CAMPOS = {
    "totalAssets": "Total Assets",
    "totalStockholderEquity": "Stockholders Equity",
    "totalCurrentAssets": "Current Assets",
    "totalCurrentLiabilities": "Current Liabilities",
}


def buscar_brapi(ticker, token, timeout=30):
    params = {"modules": "balanceSheetHistory,incomeStatementHistory", "token": token}
    try:
        r = requests.get(URL.format(ticker=ticker), params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return None, f"erro de rede: {str(e)[:60]}"
    if r.status_code != 200:
        try:
            msg = r.json().get("message", r.text[:80])
        except ValueError:
            msg = r.text[:80]
        return None, f"HTTP {r.status_code}: {msg}"
    try:
        res = (r.json().get("results") or [])
    except ValueError:
        return None, "resposta não é JSON"
    if not res:
        return None, "sem resultados"
    return res[0], None


def extrair_balanco(dados):
    """Devolve {ano: {campo: valor}} do histórico de balanço da brapi."""
    hist = (dados.get("balanceSheetHistory") or {})
    itens = hist.get("balanceSheetStatements") if isinstance(hist, dict) else hist
    if not itens:
        return {}
    out = {}
    for it in itens:
        data = str(it.get("endDate") or "")[:10]
        if not data:
            continue
        ano = data[:4]
        out[ano] = {k: it.get(k) for k in CAMPOS if it.get(k) is not None}
    return out


def nossos_dados(engine, tickers):
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, fiscal_date, line_item, value, source
            FROM financials
            WHERE period_type = 'annual'
              AND line_item = ANY(:itens)
              AND REPLACE(ticker, '.SA', '') = ANY(:tks)
        """), conn, params={"itens": list(CAMPOS.values()), "tks": tickers})
    if df.empty:
        return df
    df["ano"] = pd.to_datetime(df["fiscal_date"]).dt.year.astype(str)
    return df


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--tickers", nargs="*", default=list(AMOSTRA))
    p.add_argument("--tolerancia", type=float, default=2.0, help="Diferença %% aceitável.")
    p.add_argument("--pausa", type=float, default=0.5, help="Segundos entre requisições.")
    args = p.parse_args()

    if not args.token:
        sys.exit("Token não informado. Use --token ou defina BRAPI_TOKEN.")

    engine = create_engine(os.environ["DATABASE_URL"])
    nossos = nossos_dados(engine, args.tickers)
    if nossos.empty:
        sys.exit("Nenhum dado nosso encontrado para esses tickers.")

    print(f"Consultando {len(args.tickers)} tickers na brapi "
          f"({len(args.tickers)} requisições de 15.000/mês)\n")

    linhas, falhas = [], []
    for tk in args.tickers:
        dados, erro = buscar_brapi(tk, args.token)
        if erro:
            falhas.append((tk, erro))
            print(f"  {tk:<8} FALHOU: {erro}")
            time.sleep(args.pausa)
            continue

        balanco = extrair_balanco(dados)
        if not balanco:
            falhas.append((tk, "sem histórico de balanço na resposta"))
            print(f"  {tk:<8} sem histórico de balanço")
            time.sleep(args.pausa)
            continue

        sub = nossos[nossos["ticker"].str.replace(".SA", "", regex=False) == tk]
        comparados = 0
        for ano, campos in balanco.items():
            for campo_brapi, valor_brapi in campos.items():
                nosso_item = CAMPOS[campo_brapi]
                m = sub[(sub["ano"] == ano) & (sub["line_item"] == nosso_item)]
                if m.empty:
                    continue
                nosso_valor = float(m.iloc[0]["value"])
                fonte = m.iloc[0]["source"]
                try:
                    vb = float(valor_brapi)
                except (TypeError, ValueError):
                    continue
                if vb == 0:
                    continue
                dif = abs(nosso_valor - vb) / abs(vb) * 100
                linhas.append({
                    "ticker": tk, "ano": ano, "campo": nosso_item,
                    "nosso": nosso_valor, "brapi": vb,
                    "dif_pct": dif, "razao": nosso_valor / vb,
                    "fonte": fonte, "bate": dif <= args.tolerancia,
                })
                comparados += 1
        print(f"  {tk:<8} {comparados:>3} comparações  ({AMOSTRA.get(tk, '')[:45]})")
        time.sleep(args.pausa)

    if not linhas:
        sys.exit("\nNenhuma comparação possível - confira se a brapi devolveu balanço.")

    df = pd.DataFrame(linhas)

    print("\n" + "=" * 78)
    print(f"RESULTADO - {len(df)} comparações, tolerância {args.tolerancia}%")
    print("=" * 78)
    print(f"Concordância geral: {df['bate'].mean() * 100:.1f}%\n")

    print("Por fonte do NOSSO dado:")
    print(df.groupby("fonte").agg(
        comparacoes=("bate", "size"),
        concordancia_pct=("bate", lambda s: round(s.mean() * 100, 1)),
        dif_mediana=("dif_pct", lambda s: round(s.median(), 2)),
    ).to_string())

    print("\nPor ticker (só os que têm divergência):")
    por_tk = df.groupby("ticker").agg(
        comparacoes=("bate", "size"),
        concordancia_pct=("bate", lambda s: round(s.mean() * 100, 1)),
        razao_mediana=("razao", lambda s: round(s.median(), 3)),
    ).sort_values("concordancia_pct")
    ruins = por_tk[por_tk["concordancia_pct"] < 100]
    if ruins.empty:
        print("  nenhuma - todos os tickers bateram integralmente")
    else:
        print(ruins.to_string())
        print("\n  Razão ~6 = moeda (BRL/USD). ~1000 = escala. Outros valores =")
        print("  provável conta trocada no de-para.")

    div = df[~df["bate"]]
    if not div.empty:
        print(f"\n15 maiores divergências:")
        print(div.nlargest(15, "dif_pct")[
            ["ticker", "ano", "campo", "nosso", "brapi", "razao", "fonte"]].to_string(index=False))

    if falhas:
        print(f"\n{len(falhas)} tickers não puderam ser consultados:")
        for tk, e in falhas:
            print(f"  {tk}: {e}")

    print("\n" + "=" * 78)
    cvm = df[df["fonte"] == "cvm"]
    if not cvm.empty:
        taxa = cvm["bate"].mean() * 100
        print(f"VEREDITO (dados da CVM): {taxa:.1f}% de concordância com a brapi")
        if taxa >= 95:
            print("Parsing da CVM validado - pode seguir para a carga completa.")
        elif taxa >= 85:
            print("Boa, mas investigue as divergências acima antes da carga completa.")
        else:
            print("NÃO faça a carga completa ainda - há erro sistemático no de-para.")
    else:
        print("Nenhum dado da CVM na amostra - rode o cvm_ingestor.py primeiro.")
    print("=" * 78)


if __name__ == "__main__":
    main()
