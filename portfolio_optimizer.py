"""
Etapa 3: Otimizador de Portfólio (Monte Carlo vetorizado).

Reconstrói a lógica do Buffet_Gekko_v8_Portfolio.ipynb original (pesos
aleatórios, fronteira eficiente), mas sem o loop Python de 1 milhão de
iterações - aqui é tudo álgebra matricial em NumPy, o que permite rodar
dezenas de milhares de simulações em menos de 1 segundo. Isso importa porque
a ideia é que o futuro app chame isso ao vivo, toda vez que o usuário mudar
os tickers ou os parâmetros (não é um job agendado como os scripts de coleta
e cálculo de índices).

Este arquivo é uma BIBLIOTECA (funções reutilizáveis), não um job agendado -
não precisa de workflow no GitHub Actions. O bloco no final (`if __name__ ==
"__main__"`) é só para testar manualmente pelo terminal.

Convenção de cálculo (Markowitz clássico):
- Retorno esperado anualizado = média diária dos LOG-retornos * 252
- Volatilidade anualizada = raiz(variância diária * 252), variância via
  matriz de covariância (W' Σ W)
- Sharpe = (retorno anualizado - taxa livre de risco) / volatilidade
- Sortino: usa semi-desvio (só a parte negativa do retorno de cada
  simulação, elevada ao quadrado, média sobre TODOS os dias - não só os
  negativos). É uma convenção um pouco diferente da usada em
  compute_market_metrics.py (que soma só os dias negativos e divide pela
  contagem deles) - aqui usei a versão vetorizável para rodar em massa; as
  duas são convenções legítimas e amplamente usadas, só não são idênticas
  numericamente.

Usage (como biblioteca, dentro de outro script ou de um notebook):
    from portfolio_optimizer import load_returns, run_simulation, find_optimal_portfolios
    returns = load_returns(engine, ["PETR4.SA", "VALE3.SA", ...])
    results = run_simulation(returns, n_portfolios=30000)
    best = find_optimal_portfolios(results, list(returns.columns))

Usage (teste manual pelo terminal):
    DATABASE_URL="postgresql://..." python portfolio_optimizer.py PETR4.SA VALE3.SA ITUB4.SA WEGE3.SA
"""
import os

import numpy as np
import pandas as pd
import requests
from sqlalchemy import create_engine, text

TRADING_DAYS_PER_YEAR = 252
DEFAULT_N_PORTFOLIOS = 30_000
DEFAULT_LOOKBACK_DAYS = 756  # ~3 anos de pregão, mesma janela do compute_market_metrics.py
MIN_TICKERS = 2
MAX_TICKERS = 30  # sanity limit - isso não é pensado para centenas de ativos de uma vez
BCB_SELIC_META_SERIES = 432  # Meta Selic definida pelo Copom, % a.a. - api.bcb.gov.br (SGS)
IBOVESPA_TICKER = "^BVSP"


def fetch_selic_rate_anual(default=0.0):
    """Busca a Selic meta atual (% ao ano) via API pública do Banco Central
    (SGS, série 432) e devolve como decimal (ex: 0.15 para 15% a.a.). Se a
    chamada falhar (sem internet, API fora do ar), devolve `default` e avisa
    - não trava a simulação por causa disso."""
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{BCB_SELIC_META_SERIES}/dados/ultimos/1?formato=json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        valor_pct = float(data[0]["valor"])
        return valor_pct / 100
    except Exception as e:
        print(f"Aviso: não consegui buscar a Selic no Banco Central ({e}). Usando {default*100:.1f}% a.a. como taxa livre de risco.")
        return default


def load_returns(engine, tickers, lookback_days=DEFAULT_LOOKBACK_DAYS):
    """Retorna um DataFrame (datas x tickers) de log-retornos diários, usando
    só os tickers que têm dado suficiente na janela pedida. Levanta um erro
    claro se algum ticker pedido não tiver dado nenhum."""
    if not (MIN_TICKERS <= len(tickers) <= MAX_TICKERS):
        raise ValueError(f"Escolha entre {MIN_TICKERS} e {MAX_TICKERS} tickers (recebi {len(tickers)}).")

    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ticker, date, adj_close FROM prices_daily
            WHERE ticker = ANY(:tickers) AND adj_close IS NOT NULL
            ORDER BY date
        """), conn, params={"tickers": tickers})

    missing = set(tickers) - set(df["ticker"].unique())
    if missing:
        raise ValueError(f"Sem dado de preço para: {', '.join(sorted(missing))}")

    wide = df.pivot(index="date", columns="ticker", values="adj_close").sort_index()
    wide = wide.tail(lookback_days)

    # descarta tickers com dado insuficiente na janela (em vez de derrubar a
    # simulação inteira por causa de 1 ticker problemático)
    min_obs = int(lookback_days * 0.9)
    usable = wide.columns[wide.notna().sum() >= min_obs]
    dropped = set(tickers) - set(usable)
    if dropped:
        print(f"Aviso: removendo por falta de histórico suficiente no período: {', '.join(sorted(dropped))}")
    wide = wide[usable].dropna()

    if wide.shape[1] < MIN_TICKERS:
        raise ValueError("Tickers insuficientes com dado suficiente para montar a simulação.")

    returns = np.log(wide / wide.shift(1)).dropna()
    return returns


def _validate_weight_bounds(n_assets, min_weight, max_weight):
    if not (0 <= min_weight <= max_weight <= 1):
        raise ValueError("É preciso que 0 <= min_weight <= max_weight <= 1.")
    if min_weight * n_assets > 1.0 + 1e-9:
        raise ValueError(
            f"min_weight={min_weight:.1%} × {n_assets} ativos > 100% - impossível "
            f"garantir esse mínimo para todos os ativos ao mesmo tempo. Reduza o "
            f"mínimo ou inclua mais ativos."
        )
    if max_weight * n_assets < 1.0 - 1e-9:
        raise ValueError(
            f"max_weight={max_weight:.1%} × {n_assets} ativos < 100% - impossível "
            f"chegar a 100% respeitando esse teto. Aumente o máximo ou inclua mais ativos."
        )


def _sample_constrained_weights(n_assets, n_portfolios, min_weight, max_weight, rng, max_attempts_multiplier=200):
    """Gera pesos aleatórios somando 1, respeitando min_weight <= peso <= max_weight
    por ativo, via amostragem por rejeição (mantém a amostra estatisticamente
    correta - uniforme sobre a região viável - em vez de uma fórmula customizada
    que poderia enviesar os resultados sem se perceber)."""
    if min_weight <= 0.0 and max_weight >= 1.0:
        return rng.dirichlet(np.ones(n_assets), size=n_portfolios)

    _validate_weight_bounds(n_assets, min_weight, max_weight)

    accepted_batches = []
    n_accepted = 0
    total_generated = 0
    batch_size = max(n_portfolios, 5000)
    max_total = n_portfolios * max_attempts_multiplier

    while n_accepted < n_portfolios and total_generated < max_total:
        batch = rng.dirichlet(np.ones(n_assets), size=batch_size)
        mask = np.all((batch >= min_weight) & (batch <= max_weight), axis=1)
        valid = batch[mask]
        accepted_batches.append(valid)
        n_accepted += len(valid)
        total_generated += batch_size

    W = np.vstack(accepted_batches) if accepted_batches else np.empty((0, n_assets))
    acceptance_rate = len(W) / total_generated if total_generated else 0
    print(f"  restrição de peso: {acceptance_rate:.1%} dos portfólios gerados respeitavam os limites "
          f"({len(W):,} válidos de {total_generated:,} tentados)")

    if len(W) < n_portfolios:
        print(f"  aviso: só consegui {len(W):,} portfólios válidos (pedi {n_portfolios:,}) - limites "
              f"muito apertados para {n_assets} ativos. Considere afrouxar min/max_weight.")
        return W
    return W[:n_portfolios]


def run_simulation(returns, n_portfolios=DEFAULT_N_PORTFOLIOS, risk_free_rate_annual=0.0,
                    min_weight=0.0, max_weight=1.0, seed=None):
    """Roda a simulação de Monte Carlo vetorizada. Retorna um DataFrame com
    uma linha por portfólio simulado: retorno/vol/Sharpe/Sortino anualizados
    + o peso de cada ticker.

    min_weight/max_weight (0 a 1, ex: 0.02 e 0.35 para 2%-35%) restringem a
    alocação por ativo - nenhum portfólio simulado terá um ativo fora dessa
    faixa. Isso pode "esconder" a carteira numericamente ótima sem restrição,
    de propósito: o objetivo é evitar posições irrelevantes ou concentração
    excessiva num único ativo, não necessariamente maximizar Sharpe a
    qualquer custo."""
    rng = np.random.default_rng(seed)
    tickers = list(returns.columns)
    n_assets = len(tickers)

    mean_daily = returns.mean().to_numpy()
    cov_daily = returns.cov().to_numpy()

    W = _sample_constrained_weights(n_assets, n_portfolios, min_weight, max_weight, rng)
    if len(W) == 0:
        raise ValueError("Nenhum portfólio válido gerado dentro dos limites de peso - afrouxe as restrições.")

    ann_return = (W @ mean_daily) * TRADING_DAYS_PER_YEAR

    port_var_daily = np.sum((W @ cov_daily) * W, axis=1)
    ann_vol = np.sqrt(port_var_daily * TRADING_DAYS_PER_YEAR)

    sharpe = (ann_return - risk_free_rate_annual) / ann_vol

    port_returns_series = returns.to_numpy() @ W.T  # (n_dias, n_portfolios)
    downside_sq_mean = np.minimum(port_returns_series, 0) ** 2
    downside_dev = np.sqrt(downside_sq_mean.mean(axis=0) * TRADING_DAYS_PER_YEAR)
    with np.errstate(invalid="ignore", divide="ignore"):
        sortino = np.where(downside_dev > 0, (ann_return - risk_free_rate_annual) / downside_dev, np.nan)

    results = pd.DataFrame({
        "ann_return_pct": ann_return * 100,
        "ann_vol_pct": ann_vol * 100,
        "sharpe": sharpe,
        "sortino": sortino,
    })
    weights = pd.DataFrame(W * 100, columns=[f"peso_{t}" for t in tickers])
    return pd.concat([results, weights], axis=1)


def discretize_allocation(weights_pct, prices, capital, fractional_market=True):
    """
    Converte pesos-alvo (%) numa alocação real, em quantidade de ações -
    porque não dá pra comprar uma fração arbitrária de ação, então o peso
    exato quase nunca é atingível.

    weights_pct: dict {ticker: peso percentual, ex: 12.34}
    prices: dict {ticker: preço atual}
    capital: valor total em R$ a alocar
    fractional_market: se True (padrão), permite comprar de 1 em 1 ação -
        o "mercado fracionário" da B3 (tickers com sufixo F, ex: PETR4F).
        Se False, só permite lotes fechados de 100 ações (lote padrão) -
        nesse modo, posições pequenas em ações caras podem ficar com
        quantidade 0 (não dá pra comprar 1 lote inteiro dentro do orçamento
        daquele ativo).

    Arredonda sempre PARA BAIXO (nunca ultrapassa o capital disponível) - uma
    corretora não deixaria você comprar além do que você tem em conta, então
    "sobra de caixa" negativa não faria sentido aqui.

    Retorna (alocacao, caixa_sobrando):
        alocacao: dict {ticker: {peso_alvo_pct, quantidade, valor_alocado, peso_realizado_pct}}
        caixa_sobrando: R$ não alocado (sempre >= 0), porque quantidades inteiras
            raramente usam 100% do capital exatamente
    """
    step = 1 if fractional_market else 100
    alocacao = {}
    total_alocado = 0.0

    for ticker, w in weights_pct.items():
        if ticker not in prices or prices[ticker] is None or prices[ticker] <= 0:
            raise ValueError(f"Preço ausente ou inválido para {ticker}.")
        valor_alvo = capital * w / 100
        preco = prices[ticker]
        qtd_bruta = valor_alvo / preco
        qtd_passos = int(qtd_bruta // step)  # sempre para baixo
        quantidade = qtd_passos * step
        valor_alocado = quantidade * preco
        alocacao[ticker] = {
            "peso_alvo_pct": round(w, 2),
            "quantidade": quantidade,
            "valor_alocado": round(valor_alocado, 2),
        }
        total_alocado += valor_alocado

    caixa_sobrando = round(capital - total_alocado, 2)
    for ticker in alocacao:
        alocacao[ticker]["peso_realizado_pct"] = round(alocacao[ticker]["valor_alocado"] / capital * 100, 2)

    return alocacao, caixa_sobrando


def load_single_ticker_returns(engine, ticker, lookback_days=DEFAULT_LOOKBACK_DAYS):
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT date, adj_close FROM prices_daily
            WHERE ticker = :t AND adj_close IS NOT NULL
            ORDER BY date
        """), conn, params={"t": ticker})
    if df.empty:
        return None
    prices = df.set_index("date")["adj_close"].tail(lookback_days)
    return np.log(prices / prices.shift(1)).dropna()


def compute_benchmark_ann_return_pct(engine, lookback_days=DEFAULT_LOOKBACK_DAYS, benchmark_ticker=IBOVESPA_TICKER):
    """Retorno anualizado do Ibovespa na mesma janela e com a mesma convenção
    (média do log-retorno diário * 252) usada para os portfólios simulados -
    para comparação direta, "maçã com maçã"."""
    r = load_single_ticker_returns(engine, benchmark_ticker, lookback_days)
    if r is None or r.empty:
        return None
    return float(r.mean() * TRADING_DAYS_PER_YEAR * 100)


def find_optimal_portfolios(results, tickers):
    """Extrai os 4 portfólios de destaque: maior retorno, menor volatilidade,
    maior Sharpe, maior Sortino."""
    picks = {
        "maior_retorno": results["ann_return_pct"].idxmax(),
        "menor_volatilidade": results["ann_vol_pct"].idxmin(),
        "maior_sharpe": results["sharpe"].idxmax(),
        "maior_sortino": results["sortino"].idxmax() if results["sortino"].notna().any() else None,
    }
    out = {}
    for name, i in picks.items():
        if i is None:
            out[name] = None
            continue
        row = results.loc[i]
        out[name] = {
            "index": i,
            "ann_return_pct": round(float(row["ann_return_pct"]), 2),
            "ann_vol_pct": round(float(row["ann_vol_pct"]), 2),
            "sharpe": round(float(row["sharpe"]), 3),
            "sortino": round(float(row["sortino"]), 3) if pd.notna(row["sortino"]) else None,
            "weights_pct": {t: round(float(row[f"peso_{t}"]), 2) for t in tickers},
        }
    return out


def compute_pareto_frontier(results):
    """Retorna só os portfólios NÃO-DOMINADOS: nenhum outro portfólio
    simulado tem retorno >= e volatilidade <= ao mesmo tempo. Isso é a
    "curva ótima" de verdade - o resto da nuvem é sempre pior que algum
    ponto da fronteira em pelo menos uma dimensão."""
    ordered = results.sort_values("ann_vol_pct")
    returns = ordered["ann_return_pct"].to_numpy()
    keep = np.empty(len(ordered), dtype=bool)
    best_so_far = -np.inf
    for i in range(len(ordered)):
        if returns[i] > best_so_far:
            keep[i] = True
            best_so_far = returns[i]
        else:
            keep[i] = False
    return ordered[keep]


def select_intermediate_portfolios(frontier, exclude_indices, tickers, n=10):
    """Escolhe até `n` portfólios da fronteira eficiente, distribuídos
    uniformemente por volatilidade, excluindo os que já são campeões (pra
    não repetir o que já vai aparecer destacado)."""
    candidates = frontier.drop(index=[i for i in exclude_indices if i in frontier.index], errors="ignore")
    if candidates.empty:
        return []
    n = min(n, len(candidates))
    positions = np.linspace(0, len(candidates) - 1, n).round().astype(int)
    positions = sorted(set(positions))  # remove duplicatas se n > pontos distintos
    picked = candidates.iloc[positions]

    out = []
    for i, row in picked.iterrows():
        out.append({
            "index": i,
            "ann_return_pct": round(float(row["ann_return_pct"]), 2),
            "ann_vol_pct": round(float(row["ann_vol_pct"]), 2),
            "sharpe": round(float(row["sharpe"]), 3),
            "sortino": round(float(row["sortino"]), 3) if pd.notna(row["sortino"]) else None,
            "weights_pct": {t: round(float(row[f"peso_{t}"]), 2) for t in tickers},
        })
    return out


def plot_efficient_frontier(results, best, risk_free_rate_annual, intermediate=None, output_path="efficient_frontier.png"):
    """Gráfico da fronteira eficiente, com foco visual só no que interessa:
    - nuvem de fundo (dominados ou abaixo da taxa livre de risco): apagada,
      só para dar contexto de onde vieram as simulações.
    - fronteira eficiente (não-dominados, acima da taxa livre de risco):
      linha contínua destacada - a "curva ótima" de verdade.
    - até 10 pontos intermediários da fronteira: marcadores numerados, para
      escolher um meio-termo entre os campeões.
    - os 4 campeões (maior retorno, menor vol, maior Sharpe, maior Sortino):
      estrelas grandes, com rótulo. Quando dois campeões coincidem no MESMO
      portfólio simulado, viram uma estrela só com os dois nomes juntos, em
      vez de dois marcadores sobrepostos e ilegíveis.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.size": 11,
        "axes.edgecolor": "#444444",
        "axes.labelcolor": "#222222",
        "text.color": "#222222",
        "axes.titleweight": "bold",
    })

    rf_pct = risk_free_rate_annual * 100
    focus_mask = results["ann_return_pct"] > rf_pct
    frontier = compute_pareto_frontier(results)
    frontier_focus = frontier[frontier["ann_return_pct"] > rf_pct]

    fig, ax = plt.subplots(figsize=(11, 7.5))
    ax.set_facecolor("#fbfbfb")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.grid(True, color="#e3e3e3", linewidth=0.8, zorder=0)

    # nuvem de fundo, apagada: tudo que não está em foco (dominado, ou abaixo da taxa livre de risco)
    background = results[~results.index.isin(frontier_focus.index)]
    ax.scatter(background["ann_vol_pct"], background["ann_return_pct"],
               s=5, alpha=0.12, color="#9aa5b1", linewidths=0, zorder=1, label=None)

    # linha de referência: taxa livre de risco
    ax.axhline(rf_pct, color="#c2c2c2", linestyle="--", linewidth=1, zorder=1)
    ax.text(results["ann_vol_pct"].max(), rf_pct, f"  Selic ({rf_pct:.2f}%)",
            color="#888888", fontsize=9, va="center")

    # fronteira eficiente em foco: linha + pontos
    ax.plot(frontier_focus["ann_vol_pct"], frontier_focus["ann_return_pct"],
            color="#2f6fb3", linewidth=2, zorder=2, label="Fronteira eficiente")
    ax.scatter(frontier_focus["ann_vol_pct"], frontier_focus["ann_return_pct"],
               s=10, color="#2f6fb3", alpha=0.6, zorder=2)

    # pontos intermediários: numerados
    if intermediate:
        for n, p in enumerate(intermediate, start=1):
            ax.scatter(p["ann_vol_pct"], p["ann_return_pct"], marker="o", s=90,
                       facecolor="white", edgecolor="#2f6fb3", linewidth=1.6, zorder=4)
            ax.annotate(str(n), (p["ann_vol_pct"], p["ann_return_pct"]),
                        ha="center", va="center", fontsize=8, fontweight="bold", color="#2f6fb3", zorder=5)

    # campeões, agrupando quem coincide no mesmo portfólio simulado
    champion_style = {
        "maior_retorno": ("Maior retorno", "#d62728"),
        "menor_volatilidade": ("Menor volatilidade", "#1f9e5c"),
        "maior_sharpe": ("Maior Sharpe", "#e0a400"),
        "maior_sortino": ("Maior Sortino", "#9b4fd1"),
    }
    grouped = {}
    for key, info in best.items():
        if info is None:
            continue
        grouped.setdefault(info["index"], []).append(key)

    text_offsets = [(12, 12), (12, -18), (-70, 12), (-70, -18)]
    for offset_i, (idx, keys) in enumerate(grouped.items()):
        info = best[keys[0]]
        labels = [champion_style[k][0] for k in keys]
        color = champion_style[keys[0]][1] if len(keys) == 1 else "#c2185b"
        combined_label = " & ".join(labels)
        ax.scatter(info["ann_vol_pct"], info["ann_return_pct"], marker="*", s=550,
                   color=color, edgecolor="black", linewidth=1, zorder=6)
        dx, dy = text_offsets[offset_i % len(text_offsets)]
        ax.annotate(combined_label, (info["ann_vol_pct"], info["ann_return_pct"]),
                    xytext=(dx, dy), textcoords="offset points", fontsize=9, fontweight="bold",
                    color=color, arrowprops=dict(arrowstyle="-", color=color, linewidth=0.8))

    ax.set_xlabel("Volatilidade anualizada (%)")
    ax.set_ylabel("Retorno anualizado (%)")
    ax.set_title("Fronteira Eficiente", loc="left", fontsize=14)
    ax.legend(loc="lower right", frameon=False)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Otimizador de portfólio (Monte Carlo vetorizado). Exemplo:\n"
                     "  python portfolio_optimizer.py PETR4.SA VALE3.SA ITUB4.SA WEGE3.SA --min-weight 2 --max-weight 35",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tickers", nargs="+", help="Tickers a incluir na simulação (ex: PETR4.SA VALE3.SA ...)")
    parser.add_argument("--min-weight", type=float, default=0.0,
                         help="Peso MÍNIMO por ativo, em %% (ex: 2 para 2%%). Padrão: 0 (sem mínimo).")
    parser.add_argument("--max-weight", type=float, default=100.0,
                         help="Peso MÁXIMO por ativo, em %% (ex: 35 para 35%%). Padrão: 100 (sem máximo).")
    parser.add_argument("--n-portfolios", type=int, default=DEFAULT_N_PORTFOLIOS,
                         help=f"Quantas carteiras aleatórias simular. Padrão: {DEFAULT_N_PORTFOLIOS:,}.")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                         help=f"Quantos dias de pregão usar no histórico. Padrão: {DEFAULT_LOOKBACK_DAYS} (~3 anos).")
    parser.add_argument("--risk-free-rate", type=float, default=None,
                         help="Taxa livre de risco anual, em %% (ex: 14.25). Se omitido, busca a Selic atual no Banco Central automaticamente.")
    parser.add_argument("--capital", type=float, default=None,
                         help="Se informado (em R$), mostra a alocação em quantidade de ações pra cada portfólio de destaque, "
                              "usando o preço mais recente de cada ticker.")
    parser.add_argument("--no-fractional-market", action="store_true",
                         help="Usar só lotes padrão de 100 ações (em vez do mercado fracionário, que é o padrão) no cálculo de --capital.")
    return parser.parse_args()


def load_latest_prices(engine, tickers):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT ON (ticker) ticker, adj_close
            FROM prices_daily
            WHERE ticker = ANY(:tickers) AND adj_close IS NOT NULL
            ORDER BY ticker, date DESC
        """), {"tickers": tickers}).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def main():
    args = parse_args()
    engine = create_engine(os.environ["DATABASE_URL"])

    if args.risk_free_rate is not None:
        selic = args.risk_free_rate / 100
        print(f"Taxa livre de risco informada manualmente: {selic*100:.2f}% a.a.")
    else:
        selic = fetch_selic_rate_anual()
        print(f"Selic meta atual (Banco Central): {selic*100:.2f}% a.a. - usando como taxa livre de risco.")

    ibov_ann_return_pct = compute_benchmark_ann_return_pct(engine, lookback_days=args.lookback_days)
    if ibov_ann_return_pct is not None:
        print(f"Ibovespa no mesmo período: {ibov_ann_return_pct:.2f}% a.a. (retorno anualizado, mesma janela/convenção dos portfólios abaixo).")

    min_weight = args.min_weight / 100
    max_weight = args.max_weight / 100
    if min_weight > 0 or max_weight < 1:
        print(f"Restrição de peso por ativo: {args.min_weight:.1f}% a {args.max_weight:.1f}%")

    print(f"\nCarregando histórico de preços para {len(args.tickers)} tickers...")
    returns = load_returns(engine, args.tickers, lookback_days=args.lookback_days)
    print(f"  {returns.shape[0]} dias de pregão, {returns.shape[1]} tickers utilizáveis.")

    print(f"Rodando {args.n_portfolios:,} simulações...")
    results = run_simulation(returns, n_portfolios=args.n_portfolios, risk_free_rate_annual=selic,
                              min_weight=min_weight, max_weight=max_weight)

    tickers_usados = list(returns.columns)
    best = find_optimal_portfolios(results, tickers_usados)
    champion_indices = [info["index"] for info in best.values() if info is not None]

    frontier = compute_pareto_frontier(results)
    intermediate = select_intermediate_portfolios(frontier, champion_indices, tickers_usados, n=10)

    prices = load_latest_prices(engine, tickers_usados) if args.capital else None

    print("\n" + "=" * 60)
    print("PORTFÓLIOS DE DESTAQUE")
    print("=" * 60)
    for name, info in best.items():
        print(f"\n=== {name} ===")
        if info is None:
            print("  não foi possível calcular (dado insuficiente)")
            continue
        print(f"  retorno anualizado: {info['ann_return_pct']}%")
        print(f"  volatilidade anualizada: {info['ann_vol_pct']}%")
        print(f"  Sharpe: {info['sharpe']}")
        print(f"  Sortino: {info['sortino']}")
        print(f"  vs. Selic ({selic*100:.2f}%): {info['ann_return_pct'] - selic*100:+.2f} p.p.")
        if ibov_ann_return_pct is not None:
            print(f"  vs. Ibovespa ({ibov_ann_return_pct:.2f}%): {info['ann_return_pct'] - ibov_ann_return_pct:+.2f} p.p.")
        weights_sorted = sorted(info["weights_pct"].items(), key=lambda x: -x[1])
        for ticker, w in weights_sorted:
            if w >= 0.5:
                print(f"    {ticker}: {w}%")

        if args.capital:
            alocacao, sobra = discretize_allocation(
                info["weights_pct"], prices, args.capital,
                fractional_market=not args.no_fractional_market,
            )
            print(f"  --- alocação para R${args.capital:,.2f} ---")
            for ticker, a in sorted(alocacao.items(), key=lambda x: -x[1]["valor_alocado"]):
                if a["quantidade"] > 0:
                    print(f"    {ticker}: {a['quantidade']} ações = R${a['valor_alocado']:,.2f} ({a['peso_realizado_pct']}%, alvo {a['peso_alvo_pct']}%)")
            print(f"    caixa não alocado: R${sobra:,.2f}")

    if intermediate:
        print("\n" + "=" * 60)
        print(f"PONTOS INTERMEDIÁRIOS NA FRONTEIRA ({len(intermediate)}) - trade-offs entre os campeões")
        print("=" * 60)
        for n, p in enumerate(intermediate, start=1):
            print(f"\n--- #{n} --- retorno: {p['ann_return_pct']}% | vol: {p['ann_vol_pct']}% | Sharpe: {p['sharpe']}")
            weights_sorted = sorted(p["weights_pct"].items(), key=lambda x: -x[1])
            top_holdings = ", ".join(f"{t} {w}%" for t, w in weights_sorted if w >= 1)
            print(f"    {top_holdings}")

    chart_path = plot_efficient_frontier(results, best, selic, intermediate=intermediate)
    print(f"\nGráfico salvo em: {chart_path}")


if __name__ == "__main__":
    main()