"""
Camada única de acesso a preços.

POR QUE ESTA CAMADA EXISTE
--------------------------
Hoje `scoring.py`, `backtest.py` e `portfolio_optimizer.py` leem preços
diretamente do Postgres, cada um com seu próprio SQL. Isso significa que
trocar a fonte de preços exige mexer em três lugares, com risco de deixar um
para trás.

E a troca está no plano: o COTAHIST da B3 vai substituir o Yahoo como fonte
do histórico, porque o Yahoo remove tickers deslistados (causa direta do viés
de sobrevivência) e tem ajuste de proventos que às vezes quebra - a AZEV3
aparece cotada a R$ 0,0001 por 740 pregões, o que levou o 1/N do backtest a
reportar 10.292.549% de retorno.

Com esta camada, essa migração muda UMA função em vez de três scripts.

O QUE ELA NÃO FAZ
-----------------
Não muda comportamento nenhum agora. Lê do mesmo lugar, devolve o mesmo
formato. É deliberadamente uma refatoração sem efeito visível - o momento de
mudar comportamento é depois, quando o COTAHIST entrar, e aí qualquer
diferença no resultado será atribuível à troca de fonte, não a esta mudança.

FILTRO DE SANIDADE
------------------
`carregar_precos` aceita `filtrar_implausiveis=True` para descartar retornos
diários fora de uma faixa razoável. Serve para os erros de ajuste da fonte
(AZEV3: R$ 0,0001 -> R$ 302,82 num pregão, fator de 3 milhões). O padrão é
False para não alterar silenciosamente o que os scripts já fazem.
"""
import pandas as pd
from sqlalchemy import text

# retorno DIÁRIO fora desta faixa é quase sempre erro de ajuste de proventos,
# não evento de mercado. Uma ação pode dobrar num dia; multiplicar por 3
# milhões, não.
RETORNO_DIARIO_MAX = 2.0    # +200%
RETORNO_DIARIO_MIN = -0.75  # -75%


def carregar_precos(engine, tickers=None, desde=None, ate=None,
                     coluna="adj_close", filtrar_implausiveis=False):
    """Série de preços em formato largo: uma coluna por ticker, índice de datas.

    tickers: lista para restringir; None traz todos.
    coluna:  'adj_close' (padrão, ajustado por proventos) ou 'close'.
    """
    cond = ["date IS NOT NULL", f"{coluna} IS NOT NULL"]
    params = {}
    if tickers:
        cond.append("ticker = ANY(:tickers)")
        params["tickers"] = list(tickers)
    if desde:
        cond.append("date >= :desde")
        params["desde"] = desde
    if ate:
        cond.append("date <= :ate")
        params["ate"] = ate

    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT ticker, date, {coluna} AS preco
            FROM prices_daily
            WHERE {' AND '.join(cond)}
            ORDER BY date
        """), conn, params=params)

    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])
    largo = df.pivot_table(index="date", columns="ticker", values="preco", aggfunc="last")

    if filtrar_implausiveis:
        largo = _remover_saltos(largo)
    return largo


def _remover_saltos(px):
    """Marca como ausente o preço que produz retorno diário implausível.

    Descartar em vez de corrigir é deliberado: não sabemos qual das duas
    pontas está errada, então o dado é DESCONHECIDO, não zero. Um preço
    ausente propaga como ausente; um preço "corrigido" por chute propaga como
    se fosse informação."""
    if px.empty:
        return px
    ret = px.pct_change()
    ruim = (ret > RETORNO_DIARIO_MAX) | (ret < RETORNO_DIARIO_MIN)
    n = int(ruim.to_numpy().sum())
    if n:
        px = px.mask(ruim)
        print(f"  ({n} preços descartados por retorno diário implausível - "
              f"provável erro de ajuste na fonte)")
    return px


def carregar_liquidez(engine):
    """Volume financeiro médio diário por ticker (ADTV), de market_metrics.

    ⚠️ LIMITAÇÃO: é a liquidez de HOJE. Usada em datas passadas, assume que
    quem é líquido agora era líquido antes - o que é falso para empresas que
    abriram capital recentemente. O `backtest.py` compensa exigindo preço nas
    duas pontas da janela, mas a medida correta seria ADTV calculado na data,
    que exigiria uma tabela histórica de liquidez."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT ticker, avg_daily_value_brl FROM market_metrics"
        )).fetchall()
    return {t: (float(v) if v is not None else 0.0) for t, v in rows}


def carregar_proventos(engine, tickers=None):
    """Dividendos e JCP por ticker/data.

    Ainda não usado, mas será necessário na migração para o COTAHIST: aquela
    fonte traz preços SEM ajuste, e o ajuste terá que ser feito aqui, com
    estes proventos mais a tabela `splits`."""
    cond, params = ["1=1"], {}
    if tickers:
        cond.append("ticker = ANY(:tickers)")
        params["tickers"] = list(tickers)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT ticker, date, amount FROM dividends
            WHERE {' AND '.join(cond)} ORDER BY date
        """), conn, params=params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def carregar_splits(engine, tickers=None):
    """Desdobramentos e grupamentos por ticker/data. Ver carregar_proventos."""
    cond, params = ["1=1"], {}
    if tickers:
        cond.append("ticker = ANY(:tickers)")
        params["tickers"] = list(tickers)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT ticker, date, ratio FROM splits
            WHERE {' AND '.join(cond)} ORDER BY date
        """), conn, params=params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df
