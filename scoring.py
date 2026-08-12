"""
Motor de scoring point-in-time, em memória.

POR QUE ESTE MÓDULO EXISTE
--------------------------
Os scripts `compute_*.py` calculam os índices para HOJE e gravam no banco -
uma linha por ticker, sobrescrita a cada execução. Isso serve para o screener,
mas é inútil para backtest: não existe histórico de rankings, e reconsultar o
banco ticker a ticker em cada data de rebalanceamento seria proibitivamente
lento (12 datas x 272 tickers x várias queries).

Aqui a abordagem é outra: carrega TODO o histórico de fundamentos e preços uma
vez só, em memória, e reconstrói o ranking para qualquer data passada fatiando
esse bloco - respeitando o que estava disponível naquela data (point-in-time).

As fórmulas são as mesmas dos scripts de produção. A duplicação é consciente:
os scripts gravam no banco e operam por ticker; estas funções são puras e
vetorizadas. Se uma fórmula mudar lá, precisa mudar aqui - há um teste de
consistência sugerido no final do backtest.py para pegar divergências.
"""
import numpy as np
import pandas as pd
from sqlalchemy import text

from pit_helpers import available_from

TRADING_DAYS_PER_YEAR = 252
FINANCEIRO_UTILITY_SECTORS = {"Financial Services", "Utilities"}
EBIT_FIELDS = ("EBIT", "Operating Income", "Total Operating Income As Reported", "Pretax Income")
VALUATION_COLS_GERAL = ["earnings_yield_pct", "fcf_yield_pct"]
QUALITY_COLS_GERAL = ["return_on_capital_pct", "gross_profitability_pct"]


# ============================================================
# Carga (uma vez só)
# ============================================================

def load_all_financials(engine, period_type="annual"):
    """Todo o histórico de demonstrativos, em formato tidy, com a data a
    partir da qual assumimos que cada registro era público."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, statement, period_type, fiscal_date, line_item, value, first_seen_at
            FROM financials WHERE period_type = :pt
        """), conn, params={"pt": period_type})
    if df.empty:
        return df
    df["fiscal_date"] = pd.to_datetime(df["fiscal_date"])
    df["available_from"] = df.apply(
        lambda r: available_from(r["fiscal_date"], r["period_type"]), axis=1
    )
    seen = pd.to_datetime(df["first_seen_at"], errors="coerce", utc=True)
    df["first_seen_at"] = seen.dt.tz_localize(None) if seen.notna().any() else pd.NaT
    return df


def load_all_prices(engine):
    """Preços diários ajustados, em formato largo (datas x tickers)."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, date, adj_close, close, volume FROM prices_daily
            WHERE adj_close IS NOT NULL ORDER BY date
        """), conn)
    df["date"] = pd.to_datetime(df["date"])
    adj = df.pivot(index="date", columns="ticker", values="adj_close").sort_index()
    raw = df.pivot(index="date", columns="ticker", values="close").sort_index()
    vol = df.pivot(index="date", columns="ticker", values="volume").sort_index()
    return adj, raw, vol


def load_sectors(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker, info->>'sector' FROM company_info")).fetchall()
    return {r[0]: r[1] for r in rows}


def load_shares_outstanding(engine):
    """Ações em circulação por ticker/exercício, do balanço - usado para
    reconstruir market cap histórico (o 'marketCap' do company_info é só o
    valor de HOJE, inútil para datas passadas)."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, fiscal_date, line_item, value FROM financials
            WHERE statement = 'balance_sheet' AND period_type = 'annual'
              AND line_item IN ('Ordinary Shares Number', 'Share Issued')
        """), conn)
    if df.empty:
        return df
    df["fiscal_date"] = pd.to_datetime(df["fiscal_date"])
    # prefere 'Ordinary Shares Number' quando ambos existem
    df["prio"] = (df["line_item"] == "Ordinary Shares Number").astype(int)
    df = df.sort_values("prio", ascending=False).drop_duplicates(["ticker", "fiscal_date"])
    return df[["ticker", "fiscal_date", "value"]].rename(columns={"value": "shares"})


# ============================================================
# Fatiamento point-in-time
# ============================================================

def snapshot_financials(all_fin, as_of, use_first_seen=False):
    """Para cada (ticker, statement, line_item), o valor do exercício mais
    recente que já era público em `as_of`. Devolve também o penúltimo
    exercício (LFY-1), necessário para Piotroski e taxas de crescimento.

    use_first_seen=False por padrão: a coluna `first_seen_at` foi criada com
    DEFAULT now() - toda linha JÁ EXISTENTE na tabela ganhou first_seen_at =
    data da migração, não a data real de publicação. Usar isso para
    reconstruir datas anteriores à migração filtra TUDO fora (first_seen_at
    sempre "recente" > qualquer as_of do passado), zerando o backtest
    silenciosamente. first_seen_at só passa a ser um proxy válido para dados
    coletados DAQUI PRA FRENTE - vale religar (use_first_seen=True) depois
    que o sistema tiver rodado por tempo suficiente para first_seen_at
    refletir chegada de dado de verdade, não a data da migração."""
    as_of = pd.Timestamp(as_of)
    mask = all_fin["available_from"] <= as_of
    if use_first_seen:
        mask &= all_fin["first_seen_at"].isna() | (all_fin["first_seen_at"] <= as_of)
    avail = all_fin[mask]
    if avail.empty:
        return pd.DataFrame(), pd.DataFrame()

    ranked = avail.sort_values("fiscal_date", ascending=False)
    key = ["ticker", "statement", "line_item"]
    lfy = ranked.drop_duplicates(key, keep="first")
    lfy1 = ranked[~ranked.index.isin(lfy.index)].drop_duplicates(key, keep="first")
    return lfy, lfy1


def _pivot_statement(snapshot, statement):
    sub = snapshot[snapshot["statement"] == statement]
    if sub.empty:
        return pd.DataFrame()
    return sub.pivot_table(index="ticker", columns="line_item", values="value", aggfunc="first")


def g(df, ticker, *names, default=None):
    """Primeiro valor presente entre os nomes de linha dados."""
    if df.empty or ticker not in df.index:
        return default
    row = df.loc[ticker]
    for n in names:
        if n in row.index and pd.notna(row[n]):
            return float(row[n])
    return default


def safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


# ============================================================
# Métricas (mesmas fórmulas dos scripts de produção)
# ============================================================

def compute_piotroski_row(bs, bs1, inc, inc1, cf, ticker):
    ni = g(inc, ticker, "Net Income")
    ni1 = g(inc1, ticker, "Net Income")
    ta = g(bs, ticker, "Total Assets")
    ta1 = g(bs1, ticker, "Total Assets")
    cfo = g(cf, ticker, "Operating Cash Flow")
    ltd = g(bs, ticker, "Long Term Debt", default=0)
    ltd1 = g(bs1, ticker, "Long Term Debt", default=0)
    ca, ca1 = g(bs, ticker, "Current Assets"), g(bs1, ticker, "Current Assets")
    cl, cl1 = g(bs, ticker, "Current Liabilities"), g(bs1, ticker, "Current Liabilities")
    sh = g(bs, ticker, "Ordinary Shares Number", "Share Issued")
    sh1 = g(bs1, ticker, "Ordinary Shares Number", "Share Issued")
    sales, sales1 = g(inc, ticker, "Total Revenue"), g(inc1, ticker, "Total Revenue")
    gm = safe_div(g(inc, ticker, "Gross Profit"), sales)
    gm1 = safe_div(g(inc1, ticker, "Gross Profit"), sales1)

    def cmp(a, b, op):
        return None if (a is None or b is None) else op(a, b)

    checks = [
        cmp(ni, 0, lambda a, b: a > b),
        cmp(cfo, 0, lambda a, b: a > b),
        cmp(safe_div(ni, ta), safe_div(ni1, ta1), lambda a, b: a > b),
        cmp(cfo, ni, lambda a, b: a > b),
        cmp(safe_div(ltd, ta), safe_div(ltd1, ta1), lambda a, b: a < b),
        cmp(safe_div(ca, cl), safe_div(ca1, cl1), lambda a, b: a > b),
        cmp(sh, sh1, lambda a, b: a <= b),
        cmp(gm, gm1, lambda a, b: a > b),
        cmp(safe_div(sales, ta), safe_div(sales1, ta1), lambda a, b: a > b),
    ]
    known = [c for c in checks if c is not None]
    return sum(1 for c in known if c) if known else None


def _fatores_unit(precos, tickers):
    """Quantas ações cada unit representa, inferido do PREÇO.

    Uma unit é um pacote (ex: SANB11 = 1 ON + 1 PN; BPAC11 = 1 ON + 2 PN).
    O preço negociado é do pacote inteiro, mas o balanço conta ações
    individuais - então `preço x ações` infla o valor de mercado da empresa
    pelo tamanho do pacote.

    Em vez de manter uma tabela fixa de composições (que envelhece a cada
    reestruturação), inferimos o fator da razão entre o preço da unit e o
    preço da ON da mesma empresa: se a unit vale 3x a ON, ela contém ~3
    ações. É aproximado - PN e ON têm preços um pouco diferentes -, mas o
    erro é de poucos por cento contra os 200-300% de não corrigir.

    Onde não existir a ON correspondente para comparar, o fator fica 1 e o
    valor de mercado daquela unit segue superestimado - situação sinalizada
    no retorno para quem quiser tratar."""
    fatores = {}
    for tk in tickers:
        base = str(tk).replace(".SA", "")
        if not base.endswith("11"):
            continue
        prefixo = base[:4]
        # candidatas: ON (3) da mesma empresa, com preço disponível
        on = f"{prefixo}3.SA"
        p_unit, p_on = precos.get(tk), precos.get(on)
        if p_unit is None or p_on is None or pd.isna(p_unit) or pd.isna(p_on) or p_on <= 0:
            continue
        razao = float(p_unit) / float(p_on)
        # units costumam conter de 2 a 6 ações; fora disso é provável que
        # sejam empresas diferentes com prefixo parecido, não uma unit
        if 1.5 <= razao <= 8:
            fatores[tk] = round(razao)
    return fatores


def build_metrics_snapshot(all_fin, as_of, prices_adj, shares_df, sectors):
    """Reconstrói, para `as_of`, a tabela de métricas por ticker que
    alimenta o ranking (equivalente ao `fundamental_ratios` daquela data)."""
    lfy, lfy1 = snapshot_financials(all_fin, as_of)
    if lfy.empty:
        return pd.DataFrame()

    bs, bs1 = _pivot_statement(lfy, "balance_sheet"), _pivot_statement(lfy1, "balance_sheet")
    inc, inc1 = _pivot_statement(lfy, "income_statement"), _pivot_statement(lfy1, "income_statement")
    cf = _pivot_statement(lfy, "cashflow")

    # preço mais recente disponível em as_of
    px = prices_adj.loc[prices_adj.index <= pd.Timestamp(as_of)]
    if px.empty:
        return pd.DataFrame()
    last_px = px.iloc[-1]

    # market cap histórico = preço na data x ações do último balanço disponível
    sh_avail = shares_df[shares_df["fiscal_date"] <= pd.Timestamp(as_of)] if not shares_df.empty else shares_df
    shares_map = {}
    if not sh_avail.empty:
        latest = sh_avail.sort_values("fiscal_date").drop_duplicates("ticker", keep="last")
        shares_map = dict(zip(latest["ticker"], latest["shares"]))

    fator_unit = _fatores_unit(last_px, shares_map.keys())

    rows = []
    for ticker in bs.index:
        price = last_px.get(ticker)
        if price is None or pd.isna(price):
            continue
        shares = shares_map.get(ticker)
        # UNITS: o preço é de um pacote (ex: 1 ON + 2 PN), mas a contagem de
        # ações do balanço é em ações INDIVIDUAIS. Multiplicar direto infla o
        # valor de mercado pelo tamanho do pacote. Dividimos o preço pelo
        # fator de composição para trazê-lo à base "por ação".
        preco_por_acao = float(price) / fator_unit.get(ticker, 1.0)
        market_cap = preco_por_acao * float(shares) if shares else None

        book = g(bs, ticker, "Stockholders Equity", "Common Stock Equity")
        cash = g(bs, ticker, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments", default=0)
        total_debt = g(bs, ticker, "Total Debt", default=0)
        ca, cl = g(bs, ticker, "Current Assets"), g(bs, ticker, "Current Liabilities")
        net_ppe = g(bs, ticker, "Net PPE")
        ta = g(bs, ticker, "Total Assets")
        ebit = g(inc, ticker, *EBIT_FIELDS)
        gross_profit = g(inc, ticker, "Gross Profit")
        net_income = g(inc, ticker, "Net Income")
        ocf = g(cf, ticker, "Operating Cash Flow")
        capex = g(cf, ticker, "Capital Expenditure", "Capital Expenditure Reported", default=0)

        ev = (market_cap + total_debt - cash) if market_cap is not None else None
        capital_employed = ((ca - cl) + net_ppe) if None not in (ca, cl, net_ppe) else None

        rows.append({
            "ticker": ticker,
            "sector": sectors.get(ticker),
            "price": float(price),
            "market_cap": market_cap,
            "price_to_book": safe_div(market_cap, book),
            "roe_pct": (safe_div(net_income, book) * 100) if safe_div(net_income, book) is not None else None,
            "earnings_yield_pct": (safe_div(ebit, ev) * 100) if safe_div(ebit, ev) is not None else None,
            "fcf_yield_pct": (safe_div(ocf + capex, ev) * 100) if (ocf is not None and safe_div(ocf + capex, ev) is not None) else None,
            "return_on_capital_pct": (safe_div(ebit, capital_employed) * 100) if safe_div(ebit, capital_employed) is not None else None,
            "gross_profitability_pct": (safe_div(gross_profit, ta) * 100) if safe_div(gross_profit, ta) is not None else None,
            "piotroski_f_score": compute_piotroski_row(bs, bs1, inc, inc1, cf, ticker),
        })

    return pd.DataFrame(rows).set_index("ticker")


# ============================================================
# Ranking (mesma lógica do compute_composite_score.py)
# ============================================================

def compute_composite(metrics):
    """Adiciona peer_group e composite_percentile ao snapshot de métricas."""
    if metrics.empty:
        return metrics
    df = metrics.copy()
    df["peer_group"] = df["sector"].apply(
        lambda s: "financeiro_utility" if s in FINANCEIRO_UTILITY_SECTORS else "geral"
    )
    df["composite_percentile"] = np.nan

    geral = df[df["peer_group"] == "geral"]
    if not geral.empty:
        val = geral[VALUATION_COLS_GERAL].rank(pct=True).mean(axis=1, skipna=True)
        qual = geral[QUALITY_COLS_GERAL].rank(pct=True).mean(axis=1, skipna=True)
        combined = pd.concat([val, qual], axis=1).mean(axis=1, skipna=True)
        valid = geral[VALUATION_COLS_GERAL].notna().any(axis=1) & geral[QUALITY_COLS_GERAL].notna().any(axis=1)
        df.loc[geral.index[valid], "composite_percentile"] = (combined[valid] * 100).round(1)

    fin = df[df["peer_group"] == "financeiro_utility"].dropna(subset=["roe_pct", "price_to_book"])
    if not fin.empty:
        roe_r = fin["roe_pct"].rank(pct=True)
        pb_r = fin["price_to_book"].rank(pct=True, ascending=False)
        df.loc[fin.index, "composite_percentile"] = ((roe_r + pb_r) / 2 * 100).round(1)

    return df


def _dedup_por_empresa(df, adtv_map=None):
    """Um ticker por empresa, o de maior liquidez.

    POR QUE: depois de incluir as classes PN e units, várias empresas têm
    dois tickers no universo (ITUB3+ITUB4, PETR3+PETR4, BBDC3+BBDC4). Para o
    otimizador eles são ativos DISTINTOS, e ele pode alocar em ambos achando
    que diversifica - quando é a mesma empresa, com correlação perto de 1. A
    diversificação vira ilusão e o risco real da carteira fica maior que o
    calculado.

    A empresa é identificada pelo prefixo de 4 letras do código B3, que é
    como a B3 organiza as classes de um mesmo emissor.

    Critério de desempate: liquidez. É o que determina se a posição é
    executável de verdade - de nada adianta preferir a ON se ela não
    negocia."""
    if df.empty:
        return df
    d = df.copy()
    d["_empresa"] = [str(t).replace(".SA", "")[:4] for t in d.index]
    d["_liquidez"] = [float((adtv_map or {}).get(t) or 0) for t in d.index]
    # em empate de liquidez, fica o de melhor pontuação
    d = d.sort_values(["_liquidez", "composite_percentile"], ascending=[False, False])
    d = d[~d["_empresa"].duplicated(keep="first")]
    return d.drop(columns=["_empresa", "_liquidez"])


def select_portfolio(scored, n_assets=10, piotroski_min=7, by_sector=False,
                     n_per_sector=2, allowed_sectors=None, min_adtv=None, adtv_map=None):
    """
    Seleciona os tickers da carteira a partir do snapshot já pontuado.

    by_sector=False: os n_assets melhores do ranking geral.
    by_sector=True : os n_per_sector melhores de CADA setor (opcionalmente
        restrito a allowed_sectors). Tende a produzir carteiras mais
        descorrelacionadas, evitando que um setor inteiro barato domine a
        seleção.
    """
    df = scored.dropna(subset=["composite_percentile"]).copy()

    if piotroski_min is not None:
        df = df[df["piotroski_f_score"].notna() & (df["piotroski_f_score"] >= piotroski_min)]

    if min_adtv and adtv_map:
        df = df[df.index.map(lambda t: (adtv_map.get(t) or 0) >= min_adtv)]

    if df.empty:
        return []

    df = _dedup_por_empresa(df, adtv_map)

    if not by_sector:
        return df.nlargest(n_assets, "composite_percentile").index.tolist()

    if allowed_sectors:
        df = df[df["sector"].isin(allowed_sectors)]
    picks = (df.dropna(subset=["sector"])
               .sort_values("composite_percentile", ascending=False)
               .groupby("sector")
               .head(n_per_sector))
    return picks.index.tolist()