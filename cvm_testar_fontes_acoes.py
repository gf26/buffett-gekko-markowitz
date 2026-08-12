"""
DIAGNÓSTICO: fontes alternativas para ações em circulação antes de 2020.

O PROBLEMA
----------
A CVM só publica o arquivo `composicao_capital` a partir de 2020. Sem número
de ações não há valor de mercado, e sem valor de mercado não existem P/L,
P/VPA, Earnings Yield nem FCF Yield - ou seja, todo o lado de valuation do
screener fica indisponível em 2010-2019.

O QUE ESTE SCRIPT FAZ (não grava nada)
--------------------------------------
TESTE 1: o arquivo composicao_capital existe nos ITR (trimestrais) de anos
         anteriores a 2020? Se existir, resolve sem estimativa nenhuma.

TESTE 2: mede o ERRO do método LPA. Em 2020-2025 temos as duas coisas - o
         número real de ações e o Lucro Básico por Ação (conta 3.99.01).
         Calculando ações = Lucro Líquido / LPA e comparando com o valor
         real, obtemos a distribuição de erro MEDIDA, não suposta.

         Por que há erro: o LPA é calculado sobre a média ponderada das
         ações no período, enquanto composicao_capital traz o saldo no fim
         do exercício. Empresas que emitiram ou recompraram ações durante o
         ano vão divergir - e é justamente o tamanho dessa divergência que
         precisamos conhecer antes de adotar o método.

TESTE 3: mesma medição para a reconstrução via splits, usando a tabela
         `splits` que já existe no banco desde 2000.

Uso:
    DATABASE_URL="..." python cvm_testar_fontes_acoes.py
"""
import os

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

import cvm_fonte

ANOS_TESTE_ITR = [2013, 2016, 2019]      # anos sem composicao_capital no DFP
ANOS_COM_VERDADE = [2020, 2021, 2022, 2023, 2024]  # onde temos o valor real


def secao(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ------------------------------------------------------------------
def teste_itr():
    secao("TESTE 1 - o ITR tem composicao_capital antes de 2020?")
    achou_algum = False
    for ano in ANOS_TESTE_ITR:
        zf = cvm_fonte.obter_itr(ano)
        if zf is None:
            print(f"  {ano}: arquivo ITR não disponível no cache")
            continue
        alvos = [n for n in zf.namelist() if "capital" in n.lower()]
        if alvos:
            achou_algum = True
            print(f"  {ano}: ENCONTRADO -> {alvos}")
            with zf.open(alvos[0]) as f:
                df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str, nrows=3)
            print(f"        colunas: {list(df.columns)}")
        else:
            print(f"  {ano}: não tem arquivo de composição de capital")
    print()
    if achou_algum:
        print("  -> O ITR resolve o problema sem estimativa. Melhor caminho.")
    else:
        print("  -> ITR não ajuda. Seguir para os métodos estimados (testes 2 e 3).")
    return achou_algum


# ------------------------------------------------------------------
def carregar(engine):
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, fiscal_date, line_item, value
            FROM financials
            WHERE source = 'cvm' AND period_type = 'annual'
              AND line_item IN ('Ordinary Shares Number', 'Net Income')
        """), conn)
    df["ano"] = pd.to_datetime(df["fiscal_date"]).dt.year
    return df.pivot_table(index=["ticker", "ano"], columns="line_item",
                           values="value", aggfunc="first").reset_index()


def carregar_lpa():
    """Lucro Básico por Ação (conta 3.99.01) dos arquivos já em cache."""
    linhas = []
    for ano in ANOS_COM_VERDADE:
        zf = cvm_fonte.obter_dfp(ano)
        if zf is None:
            continue
        nome = f"dfp_cia_aberta_DRE_con_{ano}.csv"
        if nome not in zf.namelist():
            continue
        with zf.open(nome) as f:
            d = pd.read_csv(f, sep=";", encoding="latin-1", decimal=".",
                             dtype={"CD_CONTA": str, "CD_CVM": str})
        d["CD_CVM"] = d["CD_CVM"].str.strip().str.lstrip("0")
        d["CD_CONTA"] = d["CD_CONTA"].str.strip()
        if "ORDEM_EXERC" in d.columns:
            d = d[d["ORDEM_EXERC"] == "ÚLTIMO"]
        d = d[d["CD_CONTA"] == "3.99.01"]
        if d.empty:
            continue
        # o LPA vem em reais por ação e NÃO sofre a escala do documento
        d = d[["CD_CVM", "DT_FIM_EXERC", "VL_CONTA"]].copy()
        d["ano"] = ano
        linhas.append(d.rename(columns={"VL_CONTA": "lpa"}))
    return pd.concat(linhas, ignore_index=True) if linhas else pd.DataFrame()


def teste_lpa(engine):
    secao("TESTE 2 - erro do método LPA (ações = Lucro Líquido / LPA)")
    dados = carregar(engine)
    if "Ordinary Shares Number" not in dados.columns:
        print("  Sem valores reais de ações no banco - rode o cvm_ingestor.py.")
        return
    lpa = carregar_lpa()
    if lpa.empty:
        print("  Não consegui ler o LPA dos arquivos.")
        return

    with engine.connect() as conn:
        mapa = pd.read_sql(text("SELECT ticker, cd_cvm FROM ticker_cvm_map"), conn)
    mapa["cd_cvm"] = mapa["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    lpa = lpa.merge(mapa, left_on="CD_CVM", right_on="cd_cvm", how="inner")

    comp = dados.merge(lpa[["ticker", "ano", "lpa"]], on=["ticker", "ano"], how="inner")
    comp = comp.dropna(subset=["Ordinary Shares Number", "Net Income", "lpa"])
    comp = comp[comp["lpa"] != 0]
    if comp.empty:
        print("  Nenhum par comparável.")
        return

    comp["acoes_estimadas"] = comp["Net Income"] / comp["lpa"]
    comp["erro_pct"] = ((comp["acoes_estimadas"] - comp["Ordinary Shares Number"]).abs()
                         / comp["Ordinary Shares Number"].abs() * 100)
    comp = comp[np.isfinite(comp["erro_pct"])]

    print(f"  {len(comp)} comparações, {comp['ticker'].nunique()} tickers\n")
    for faixa, limite in [("erro <= 1%", 1), ("erro <= 5%", 5), ("erro <= 10%", 10), ("erro <= 25%", 25)]:
        pct = (comp["erro_pct"] <= limite).mean() * 100
        print(f"    {faixa:<14} {pct:>5.1f}% dos casos")
    print(f"\n    erro mediano: {comp['erro_pct'].median():.2f}%")
    print(f"    erro médio:   {comp['erro_pct'].mean():.2f}%")

    print("\n  10 piores casos:")
    piores = comp.nlargest(10, "erro_pct")[["ticker", "ano", "Ordinary Shares Number",
                                              "acoes_estimadas", "erro_pct"]]
    for _, r in piores.iterrows():
        print(f"    {r['ticker']:<11} {int(r['ano'])}  real={r['Ordinary Shares Number']:>16,.0f}  "
              f"estimado={r['acoes_estimadas']:>16,.0f}  erro {r['erro_pct']:>8.1f}%")
    return comp


# ------------------------------------------------------------------
def teste_splits(engine):
    secao("TESTE 3 - erro da reconstrução por splits")
    with engine.connect() as conn:
        splits = pd.read_sql(text("SELECT ticker, date, ratio FROM splits"), conn)
        dados = pd.read_sql(text("""
            SELECT ticker, fiscal_date, value AS acoes
            FROM financials
            WHERE source='cvm' AND period_type='annual' AND line_item='Ordinary Shares Number'
        """), conn)
    if dados.empty:
        print("  Sem valores reais para comparar.")
        return
    dados["ano"] = pd.to_datetime(dados["fiscal_date"]).dt.year
    splits["ano"] = pd.to_datetime(splits["date"]).dt.year

    # parte do ano mais recente e desfaz os splits para trás
    base = dados.sort_values("ano").drop_duplicates("ticker", keep="last")
    erros = []
    for _, b in base.iterrows():
        tk, ano_base, acoes_base = b["ticker"], b["ano"], b["acoes"]
        hist = dados[(dados["ticker"] == tk) & (dados["ano"] < ano_base)]
        for _, h in hist.iterrows():
            sp = splits[(splits["ticker"] == tk) & (splits["ano"] > h["ano"]) & (splits["ano"] <= ano_base)]
            fator = sp["ratio"].astype(float).prod() if not sp.empty else 1.0
            estimado = acoes_base / fator if fator else np.nan
            if pd.notna(estimado) and h["acoes"]:
                erros.append({"ticker": tk, "ano": h["ano"], "real": h["acoes"],
                               "estimado": estimado,
                               "erro_pct": abs(estimado - h["acoes"]) / abs(h["acoes"]) * 100})
    if not erros:
        print("  Sem histórico suficiente para testar.")
        return
    e = pd.DataFrame(erros)
    e = e[np.isfinite(e["erro_pct"])]
    print(f"  {len(e)} comparações, {e['ticker'].nunique()} tickers\n")
    for faixa, limite in [("erro <= 1%", 1), ("erro <= 5%", 5), ("erro <= 10%", 10), ("erro <= 25%", 25)]:
        print(f"    {faixa:<14} {(e['erro_pct'] <= limite).mean()*100:>5.1f}% dos casos")
    print(f"\n    erro mediano: {e['erro_pct'].median():.2f}%")
    return e


# ------------------------------------------------------------------
def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    tem_itr = teste_itr()
    comp_lpa = teste_lpa(engine)
    comp_split = teste_splits(engine)

    secao("CONCLUSÃO")
    if tem_itr:
        print("Use o ITR: tem o número real, sem estimativa. Nada mais é necessário.")
        return
    print("O ITR não cobre os anos antigos. Entre os métodos estimados:\n")
    if comp_lpa is not None and len(comp_lpa):
        med = comp_lpa["erro_pct"].median()
        bom = (comp_lpa["erro_pct"] <= 5).mean() * 100
        print(f"  LPA:    erro mediano {med:.2f}%, {bom:.1f}% dos casos com erro <= 5%")
    if comp_split is not None and len(comp_split):
        med = comp_split["erro_pct"].median()
        bom = (comp_split["erro_pct"] <= 5).mean() * 100
        print(f"  Splits: erro mediano {med:.2f}%, {bom:.1f}% dos casos com erro <= 5%")
    print("\nCOMO DECIDIR: um erro mediano abaixo de ~3% provavelmente é tolerável -")
    print("ele afeta P/L e P/VPA na mesma proporção, e o screener usa PERCENTIS")
    print("(posição relativa), que são menos sensíveis a erro uniforme que valores")
    print("absolutos. Acima de ~10%, o ranking começa a embaralhar e o dado pode")
    print("ser pior que a ausência dele.")


if __name__ == "__main__":
    main()
