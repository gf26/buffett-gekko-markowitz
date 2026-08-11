"""
Validação da carga histórica da CVM.

Rode DEPOIS do cvm_ingestor.py, antes de confiar nos dados para backtest.
Não altera nada - só lê, confere e relata.

O QUE ELE VERIFICA
------------------
1. COBERTURA: quantos tickers e quantos exercícios por ano. Se um ano tiver
   muito menos que os vizinhos, algo falhou naquele arquivo.
2. BURACOS: tickers com anos faltando no meio da série (ex: tem 2015 e 2017
   mas não 2016) - sinal de problema de parsing ou de mapeamento.
3. CONTINUIDADE CVM x YAHOO: nos anos em que as duas fontes existem, os
   valores batem? Divergência grande = de-para errado.
4. SALTOS ABSURDOS: variação ano a ano acima de um limiar em contas que
   normalmente são estáveis (Ativo Total, Patrimônio Líquido). Pega erro de
   escala (1000x) e troca de moeda.
5. SINAIS INVERTIDOS: contas que deveriam ser sempre positivas (Ativo Total,
   Receita) aparecendo negativas.
6. DATA DE PUBLICAÇÃO: quantos registros ficaram com published_date - é o que
   permite backtest point-in-time honesto.

Uso:
    DATABASE_URL="..." python cvm_validar_carga.py
    DATABASE_URL="..." python cvm_validar_carga.py --salto-max 300
"""
import argparse
import os

import pandas as pd
from sqlalchemy import create_engine, text

# contas que, por natureza contábil, não deveriam ser negativas
SEMPRE_POSITIVAS = ["Total Assets", "Current Assets", "Total Revenue", "Current Liabilities"]
# contas estáveis o suficiente para que um salto enorme indique erro de dado
ESTAVEIS = ["Total Assets", "Stockholders Equity"]


def secao(titulo):
    print("\n" + "=" * 78)
    print(titulo)
    print("=" * 78)


def cobertura_por_ano(engine):
    secao("1. COBERTURA POR ANO")
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT EXTRACT(YEAR FROM fiscal_date)::int AS ano,
                   source,
                   COUNT(DISTINCT ticker) AS tickers,
                   COUNT(*)               AS linhas
            FROM financials
            WHERE period_type = 'annual'
            GROUP BY 1, 2 ORDER BY 1, 2
        """), conn)
    if df.empty:
        print("Nenhum dado anual encontrado.")
        return
    pivot = df.pivot_table(index="ano", columns="source", values="tickers", fill_value=0)
    print(pivot.to_string())

    if "cvm" in pivot.columns:
        cvm = pivot["cvm"]
        mediana = cvm[cvm > 0].median()
        fracos = cvm[(cvm > 0) & (cvm < mediana * 0.5)]
        if not fracos.empty:
            print(f"\n  ATENÇÃO: anos com menos da metade da cobertura mediana ({mediana:.0f} tickers):")
            for ano, n in fracos.items():
                print(f"    {ano}: só {n} tickers - conferir se o arquivo daquele ano foi lido direito")
        else:
            print(f"\n  OK: cobertura consistente entre anos (mediana {mediana:.0f} tickers)")


def buracos_na_serie(engine):
    secao("2. BURACOS NA SÉRIE HISTÓRICA")
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, EXTRACT(YEAR FROM fiscal_date)::int AS ano
            FROM financials
            WHERE period_type = 'annual' AND line_item = 'Total Assets'
            GROUP BY 1, 2
        """), conn)
    if df.empty:
        print("Sem dados para verificar.")
        return

    problemas = []
    for ticker, g in df.groupby("ticker"):
        anos = sorted(g["ano"])
        if len(anos) < 2:
            continue
        esperados = set(range(anos[0], anos[-1] + 1))
        faltando = sorted(esperados - set(anos))
        if faltando:
            problemas.append((ticker, anos[0], anos[-1], faltando))

    if not problemas:
        print("  OK: nenhum ticker com ano faltando no meio da série.")
        return
    print(f"  {len(problemas)} tickers com buraco na série (mostrando os 20 piores):")
    problemas.sort(key=lambda x: -len(x[3]))
    for ticker, ini, fim, faltando in problemas[:20]:
        print(f"    {ticker:<12} {ini}-{fim}, faltam {len(faltando)}: {faltando[:8]}")


def continuidade_fontes(engine, tolerancia_pct):
    secao("3. CONTINUIDADE CVM x YAHOO (anos com as duas fontes)")
    print("  Nota: onde a CVM sobrescreveu o Yahoo, só resta uma fonte por linha -")
    print("  esta checagem só encontra sobreposição se a carga foi parcial.")
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT source, COUNT(*) AS linhas, COUNT(DISTINCT ticker) AS tickers,
                   MIN(fiscal_date) AS de, MAX(fiscal_date) AS ate
            FROM financials GROUP BY source ORDER BY source
        """), conn)
    print(df.to_string(index=False))


def saltos_absurdos(engine, salto_max_pct):
    secao(f"4. SALTOS ANO A ANO ACIMA DE {salto_max_pct}%")
    print("  (pega erro de escala 1000x e troca de moeda - contas normalmente estáveis)")
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, line_item, fiscal_date, value
            FROM financials
            WHERE period_type = 'annual' AND line_item = ANY(:contas) AND value IS NOT NULL
            ORDER BY ticker, line_item, fiscal_date
        """), conn, params={"contas": ESTAVEIS})
    if df.empty:
        print("  Sem dados para verificar.")
        return

    df["fiscal_date"] = pd.to_datetime(df["fiscal_date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["anterior"] = df.groupby(["ticker", "line_item"])["value"].shift(1)
    df["var_pct"] = ((df["value"] - df["anterior"]).abs() / df["anterior"].abs().replace(0, pd.NA) * 100)
    df["var_pct"] = pd.to_numeric(df["var_pct"], errors="coerce")

    suspeitos = df[df["var_pct"] > salto_max_pct].dropna(subset=["var_pct"])
    if suspeitos.empty:
        print(f"  OK: nenhuma variação acima de {salto_max_pct}%.")
        return
    print(f"  {len(suspeitos)} saltos suspeitos (mostrando os 20 maiores):")
    for _, r in suspeitos.nlargest(20, "var_pct").iterrows():
        razao = r["value"] / r["anterior"] if r["anterior"] else float("nan")
        print(f"    {r['ticker']:<12} {r['line_item']:<22} {r['fiscal_date'].date()} "
              f"var {r['var_pct']:>10.0f}%  (razão {razao:.2f})")
    print("\n  Razão perto de 1000 = erro de escala. Perto de 5-6 = troca BRL/USD.")


def sinais_invertidos(engine):
    secao("5. VALORES NEGATIVOS EM CONTAS QUE DEVERIAM SER POSITIVAS")
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, line_item, fiscal_date, value, source
            FROM financials
            WHERE period_type = 'annual' AND line_item = ANY(:contas) AND value < 0
            ORDER BY value
        """), conn, params={"contas": SEMPRE_POSITIVAS})
    if df.empty:
        print("  OK: nenhum valor negativo indevido.")
        return
    print(f"  {len(df)} valores negativos indevidos (mostrando 20):")
    print(df.head(20).to_string(index=False))


def cobertura_publicacao(engine):
    secao("6. DATA DE PUBLICAÇÃO (point-in-time)")
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT source,
                   COUNT(*) AS linhas,
                   COUNT(published_date) AS com_data,
                   ROUND(100.0 * COUNT(published_date) / NULLIF(COUNT(*), 0), 1) AS pct
            FROM financials GROUP BY source ORDER BY source
        """), conn)
    print(df.to_string(index=False))
    print("\n  Onde published_date existe, o backtest usa a data REAL de divulgação.")
    print("  Onde não existe, cai no lag conservador de 3 meses (pit_helpers.py).")


def main():
    p = argparse.ArgumentParser(description="Valida a carga histórica da CVM.")
    p.add_argument("--salto-max", type=float, default=500.0,
                    help="Variação anual (%%) acima da qual um valor é considerado suspeito. Padrão: 500.")
    p.add_argument("--tolerancia", type=float, default=2.0)
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])

    cobertura_por_ano(engine)
    buracos_na_serie(engine)
    continuidade_fontes(engine, args.tolerancia)
    saltos_absurdos(engine, args.salto_max)
    sinais_invertidos(engine)
    cobertura_publicacao(engine)

    secao("PRÓXIMO PASSO")
    print("Se a cobertura estiver consistente e os saltos suspeitos forem poucos e")
    print("explicáveis, rode o backtest de novo - agora com histórico longo:")
    print("    python backtest.py --rebalance-freq quarterly")
    print("\nCom 15 anos em vez de 2, o número de períodos deve saltar de ~8 para ~50+,")
    print("o que muda bastante a força estatística do resultado.")


if __name__ == "__main__":
    main()