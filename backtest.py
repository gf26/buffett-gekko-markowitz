"""
Motor de backtest walk-forward.

O QUE ELE FAZ
-------------
Em cada data de rebalanceamento (trimestral, por padrão):
  1. Reconstrói o universo e os fundamentos DISPONÍVEIS naquela data
     (point-in-time - sem look-ahead).
  2. Calcula o ranking e seleciona a carteira.
  3. Define os pesos (Markowitz max-Sharpe ou peso igual).
  4. Aplica custo de transação sobre o turnover.
  5. "Segura" a carteira até o próximo rebalanceamento e mede o retorno REAL
     do período seguinte - dados que a otimização nunca viu.
E compara o resultado contra 1/N do universo, Ibovespa e mínima variância.

LIMITAÇÕES CONHECIDAS (leia antes de interpretar qualquer resultado)
--------------------------------------------------------------------
1. VIÉS DE SOBREVIVÊNCIA. O universo é composto por empresas que existem HOJE.
   Empresas que faliram ou saíram da bolsa antes do início da coleta não estão
   na base - e eram justamente os casos em que a estratégia teria perdido
   dinheiro. Isso torna QUALQUER resultado aqui otimista, de forma não
   mensurável. É a limitação mais grave, e não tem correção via código: viria
   de uma fonte de dados com universo histórico completo.

2. AMOSTRA PEQUENA. O Yahoo fornece ~3-4 anos de fundamentos anuais. Com
   rebalanceamento trimestral, sobram ~8-12 observações. Isso é pouco para
   qualquer conclusão estatística: um Sharpe alto aqui pode ser sorte.

3. SEM IMPOSTO DE RENDA. A regra brasileira (isenção mensal, alíquotas
   distintas, compensação de prejuízo) é complexa o suficiente para merecer
   tratamento próprio. O custo de 0,15%/operação cobre corretagem,
   emolumentos e uma estimativa de slippage - não IR.

4. PREÇO DE EXECUÇÃO. Assume execução no fechamento da data de
   rebalanceamento, sem impacto de mercado além do slippage embutido no custo.

Em resumo: este backtest serve para pegar erros grosseiros e dar intuição
comparativa. Não serve como prova de que a estratégia funciona.

Uso:
    DATABASE_URL="postgresql://..." python backtest.py
    DATABASE_URL="..." python backtest.py --by-sector --n-per-sector 2 --piotroski-min 6
"""
import argparse
import os

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

import scoring
import portfolio_optimizer as po
from portfolio_optimizer import _annualized_mu_sigma, _shrink_mu

TRADING_DAYS_PER_YEAR = 252
DEFAULT_COST_PCT = 0.15          # por operação, sobre o valor negociado
DEFAULT_N_ASSETS = 10
DEFAULT_PIOTROSKI_MIN = 7
DEFAULT_MIN_HISTORY_DAYS = 250   # histórico mínimo ATÉ a data de rebalanceamento
DEFAULT_MIN_ADTV = 50_000        # piso de liquidez, R$/dia


REBALANCE_FREQUENCIES = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}  # passo em meses
PERIODS_PER_YEAR = {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}


def rebalance_dates(start, end, frequency="quarterly"):
    """Datas de rebalanceamento no intervalo, na frequência pedida
    ('monthly', 'quarterly', 'semiannual' ou 'annual'). Construção manual via
    DateOffset (em vez de aliases de frequência do pandas, como 'QS'/'AS') de
    propósito: esses aliases mudaram entre versões do pandas (ex: 'AS' foi
    descontinuado em favor de 'YS'), e isso quebraria dependendo de qual
    pandas estiver instalado no ambiente."""
    if frequency not in REBALANCE_FREQUENCIES:
        raise ValueError(f"frequency deve ser um de {list(REBALANCE_FREQUENCIES)}, recebi '{frequency}'")
    step_months = REBALANCE_FREQUENCIES[frequency]
    end = pd.Timestamp(end)
    current = pd.Timestamp(start).replace(day=1)
    dates = []
    while current <= end:
        dates.append(current)
        current = current + pd.DateOffset(months=step_months)
    return pd.DatetimeIndex(dates)


def nearest_trading_day(prices_index, target):
    """Primeiro pregão em ou após `target` (o rebalanceamento acontece no
    pregão seguinte se a data cair em feriado/fim de semana)."""
    later = prices_index[prices_index >= target]
    return later[0] if len(later) else None


def optimize_weights(returns_window, tickers, method, min_weight, max_weight,
                      risk_free_rate, mu_shrinkage, l2_gamma, fallback="minvar"):
    """Pesos para a carteira selecionada. Usa SÓ dados da janela anterior ao
    rebalanceamento - nunca do futuro.

    O parâmetro `fallback` define o que fazer quando a otimização falha:

      equal  - peso igual entre os selecionados.
      minvar - PADRÃO. Mínima variância: usa SÓ a matriz de covariância, ignorando o
               retorno esperado. É a resposta padrão da literatura para a
               falha "nenhum ativo com retorno acima da taxa livre de risco",
               porque ataca a causa (estimativa de mu ruidosa) em vez do
               sintoma, e mantém a carteira investida.
      cash   - 100% no ativo livre de risco, rendendo a Selic do período.
               Logicamente coerente com o que a otimização está dizendo, mas
               veja a ressalva abaixo.

    RESSALVA SOBRE 'cash': o "retorno esperado" aqui é a média dos últimos 756
    pregões. Concluir "nada vai bater a Selic" a partir disso é previsão
    fraca - e repare QUANDO isso acontece: os casos observados são de
    2015-2016, logo após uma queda forte. Sair para caixa ali significaria
    perder a recuperação de 2016-2017. O critério tende a mandar para caixa
    DEPOIS das quedas, que é quando os retornos futuros costumam ser maiores.
    Além disso, muda a pergunta que o backtest responde: deixa de ser "o
    screener seleciona bem?" e passa a incluir market timing, sem separar os
    dois efeitos.
    """
    available = [t for t in tickers if t in returns_window.columns]
    if len(available) < 2:
        return None
    sub = returns_window[available].dropna()
    if len(sub) < 60:
        return None

    if method == "equal":
        w = 1.0 / len(available)
        return {t: w for t in available}

    from pypfopt import EfficientFrontier, objective_functions
    from pypfopt.exceptions import OptimizationError

    mu, S = _annualized_mu_sigma(sub, covariance_method="ledoit_wolf")
    if mu_shrinkage > 0:
        mu = _shrink_mu(mu, mu_shrinkage)
    try:
        ef = EfficientFrontier(mu, S, weight_bounds=(min_weight, max_weight))
        if l2_gamma > 0:
            ef.add_objective(objective_functions.L2_reg, gamma=l2_gamma)
        ef.max_sharpe(risk_free_rate=risk_free_rate)
        return {t: w for t, w in ef.clean_weights().items() if w > 0}
    except (OptimizationError, ValueError) as e:
        return _aplicar_fallback(fallback, available, mu, S, min_weight, max_weight,
                                  l2_gamma, str(e))


CAIXA = "__CAIXA__"


def _aplicar_fallback(fallback, available, mu, S, min_weight, max_weight, l2_gamma, erro):
    from pypfopt import EfficientFrontier, objective_functions
    from pypfopt.exceptions import OptimizationError

    if fallback == "minvar":
        try:
            ef = EfficientFrontier(mu, S, weight_bounds=(min_weight, max_weight))
            if l2_gamma > 0:
                ef.add_objective(objective_functions.L2_reg, gamma=l2_gamma)
            ef.min_volatility()
            print(f"      otimização falhou ({erro[:60]}) - usando mínima variância")
            return {t: w for t, w in ef.clean_weights().items() if w > 0}
        except (OptimizationError, ValueError):
            print(f"      otimização e mínima variância falharam - usando peso igual")

    elif fallback == "cash":
        print(f"      otimização falhou ({erro[:60]}) - indo 100% para caixa (Selic)")
        return {CAIXA: 1.0}

    w = 1.0 / len(available)
    if fallback != "minvar":
        print(f"      otimização falhou ({erro[:60]}) - usando peso igual")
    return {t: w for t in available}


def turnover_cost(prev_weights, new_weights, cost_pct):
    """Custo = (soma das mudanças absolutas de peso) x custo por operação.
    Uma posição que entra do zero e outra que sai contam ambas."""
    tickers = set(prev_weights) | set(new_weights)
    turnover = sum(abs(new_weights.get(t, 0.0) - prev_weights.get(t, 0.0)) for t in tickers)
    return turnover * cost_pct / 100


def period_return(prices_adj, weights, start, end, taxa_caixa=None):
    """Retorno simples da carteira entre dois pregões, com pesos fixos.

    A posição em caixa (ticker CAIXA, criada pelo fallback 'cash') rende a
    Selic anual informada, proporcional ao número de dias corridos."""
    if not weights:
        return 0.0, {}
    window = prices_adj.loc[start:end]
    if len(window) < 2:
        return 0.0, weights

    if CAIXA in weights:
        dias = max((pd.Timestamp(end) - pd.Timestamp(start)).days, 1)
        r = (1 + (taxa_caixa or 0.0)) ** (dias / 365) - 1
        peso_caixa = weights[CAIXA]
        if peso_caixa >= 0.999:
            return r, dict(weights)

    total, drifted = 0.0, {}
    for t, w in weights.items():
        if t not in window.columns:
            continue
        series = window[t].dropna()
        if len(series) < 2:
            continue
        r = float(series.iloc[-1] / series.iloc[0] - 1)
        total += w * r
        drifted[t] = w * (1 + r)

    # normaliza os pesos que "andaram" com o preço - é a carteira real na
    # próxima data, antes de rebalancear
    s = sum(drifted.values())
    drifted = {t: v / s for t, v in drifted.items()} if s > 0 else weights
    return total, drifted


def run_backtest(engine, args):
    print("Carregando histórico completo em memória...")
    all_fin = scoring.load_all_financials(engine)
    prices_adj, _, _ = scoring.load_all_prices(engine)
    sectors = scoring.load_sectors(engine)
    shares = scoring.load_shares_outstanding(engine)

    with engine.connect() as conn:
        adtv_rows = conn.execute(text(
            "SELECT ticker, avg_daily_value_brl FROM market_metrics"
        )).fetchall()
    adtv_map = {r[0]: (float(r[1]) if r[1] is not None else None) for r in adtv_rows}

    if all_fin.empty or prices_adj.empty:
        raise SystemExit("Sem dados suficientes no banco. Rode os scripts de ingestão primeiro.")

    first_available = all_fin["available_from"].min()
    # DateOffset em vez de Timedelta(days=365): o NumPy novo avisa sobre
    # conversão implícita de inteiro para timedelta genérico
    start = max(pd.Timestamp(first_available),
                pd.Timestamp(prices_adj.index.min()) + pd.DateOffset(years=1))
    end = prices_adj.index.max()
    dates = [d for d in rebalance_dates(start, end, frequency=args.rebalance_freq)]
    dates = [nearest_trading_day(prices_adj.index, d) for d in dates]
    dates = [d for d in dates if d is not None]

    if len(dates) < 3:
        raise SystemExit(f"Só {len(dates)} datas de rebalanceamento possíveis - histórico curto demais.")

    print(f"Período: {dates[0].date()} a {end.date()}  ({len(dates)} rebalanceamentos trimestrais)")
    print(f"Custo por operação: {args.cost_pct}%  |  Piso de liquidez: R$ {args.min_adtv:,.0f}/dia")

    strategies = {"markowitz": {}, "equal": {}}
    history = {k: [] for k in strategies}
    prev_weights = {k: {} for k in strategies}
    equity = {k: 1.0 for k in strategies}

    # série histórica da Selic, buscada uma vez e cacheada em disco
    selic_hist = po.fetch_selic_historica(dates[0], dates[-1])
    if selic_hist.empty:
        print(f"  (usando taxa livre de risco fixa de {args.risk_free_rate}% a.a. - "
              f"série histórica indisponível)")

    for i, rebal_date in enumerate(dates[:-1]):
        next_date = dates[i + 1]
        window = prices_adj.loc[prices_adj.index <= rebal_date]
        with np.errstate(divide="ignore", invalid="ignore"):
            returns_window = np.log(window / window.shift(1)).dropna(how="all").tail(args.lookback_days)

        # só tickers com histórico suficiente ATÉ esta data (não até hoje)
        enough = returns_window.columns[returns_window.notna().sum() >= args.min_history_days]

        metrics = scoring.build_metrics_snapshot(all_fin, rebal_date, prices_adj, shares, sectors)
        if metrics.empty:
            print(f"  {rebal_date.date()}: nenhum fundamento disponível nesta data - pulando")
            continue
        scored = scoring.compute_composite(metrics)
        scored = scored[scored.index.isin(enough)]

        excluded_short = len(metrics) - len(scored)
        selected = scoring.select_portfolio(
            scored, n_assets=args.n_assets, piotroski_min=args.piotroski_min,
            by_sector=args.by_sector, n_per_sector=args.n_per_sector,
            allowed_sectors=args.sectors, min_adtv=args.min_adtv, adtv_map=adtv_map,
        )
        if len(selected) < 2:
            print(f"  {rebal_date.date()}: só {len(selected)} tickers passaram nos filtros - pulando")
            continue

        print(f"  {rebal_date.date()}: {len(selected)} ativos selecionados "
              f"({excluded_short} excluídos por histórico curto)")

        # Selic VIGENTE nesta data, não uma taxa fixa para os 14 anos. Usar
        # 14,25% em 2020 (quando a Selic era 2%) fazia a otimização de máximo
        # Sharpe exigir retorno esperado acima de 14,25% - quase nada
        # qualificava, e o rebalanceamento caía para peso igual sem que a
        # curva "Markowitz" deixasse isso explícito.
        rf = po.selic_na_data(selic_hist, rebal_date, args.risk_free_rate / 100)
        for name in strategies:
            w = optimize_weights(returns_window, selected, name, args.min_weight / 100,
                                  args.max_weight / 100, rf,
                                  args.mu_shrinkage, args.l2_gamma,
                                  fallback=args.fallback)
            if not w:
                continue
            cost = turnover_cost(prev_weights[name], w, args.cost_pct)
            ret, drifted = period_return(prices_adj, w, rebal_date, next_date, taxa_caixa=rf)
            equity[name] *= (1 - cost) * (1 + ret)
            history[name].append({
                "date": rebal_date, "next_date": next_date, "n_assets": len(w),
                "return_pct": ret * 100, "cost_pct": cost * 100, "equity": equity[name],
                # guarda a composição: é o que permite exportar a carteira de
                # cada data e responder "quais ativos apareceram mais?",
                # "a carteira gira muito?", "as duas estratégias escolhem os
                # mesmos ativos?" - perguntas que os números agregados escondem
                "weights": dict(w),
            })
            prev_weights[name] = drifted

    return history, prices_adj, dates


def benchmark_curve(prices_adj, dates, ticker):
    series = prices_adj[ticker].dropna() if ticker in prices_adj.columns else None
    if series is None or series.empty:
        return None
    out, eq = [], 1.0
    for i, d in enumerate(dates[:-1]):
        w = series.loc[(series.index >= d) & (series.index <= dates[i + 1])]
        if len(w) < 2:
            continue
        r = float(w.iloc[-1] / w.iloc[0] - 1)
        eq *= (1 + r)
        out.append({"date": d, "next_date": dates[i + 1], "return_pct": r * 100, "equity": eq})
    return pd.DataFrame(out)


RETORNO_MAX_PERIODO = 5.0    # +400% num trimestre
RETORNO_MIN_PERIODO = -0.95  # -95% num trimestre


def universe_equal_weight_curve(prices_adj, dates, adtv_map, min_adtv):
    """1/N sobre o universo líquido - o teste de sanidade central.

    DOIS FILTROS, e ambos existem por um motivo concreto:

    1. RETORNO IMPLAUSÍVEL. A AZEV3 aparece cotada a R$ 0,0001 por parte de
       740 pregões e depois salta para R$ 302,82 - fator de 3 milhões num
       único dia. Não é evento de mercado: é o `adj_close` do Yahoo ficando
       inconsistente na virada de um grupamento. Um único caso desses levou o
       1/N a reportar 10.292.549% de retorno acumulado, inutilizando a
       referência. Retornos fora da faixa são descartados, não zerados - o
       dado é desconhecido, não nulo.

    2. NEGOCIAÇÃO EFETIVA NO PERÍODO. O filtro anterior usava `adtv_map`, que
       é a liquidez de HOJE, aplicada retroativamente a todos os períodos. Um
       papel líquido agora passava no filtro em 2011, quando talvez nem
       negociasse. Exigir preço nas duas pontas da janela é uma aproximação
       melhor: se não há cotação, não havia como comprar."""
    liquid = [t for t in prices_adj.columns
              if t != "^BVSP" and (adtv_map.get(t) or 0) >= min_adtv]
    out, eq = [], 1.0
    descartados = 0
    for i, d in enumerate(dates[:-1]):
        window = prices_adj.loc[d:dates[i + 1], liquid]
        if len(window) < 2:
            continue
        inicio, fim = window.iloc[0], window.iloc[-1]
        # só quem tem preço nas duas pontas era negociável no período
        validos = inicio.notna() & fim.notna() & (inicio > 0)
        rets = (fim[validos] / inicio[validos] - 1)
        plausivel = rets.between(RETORNO_MIN_PERIODO, RETORNO_MAX_PERIODO)
        descartados += int((~plausivel).sum())
        rets = rets[plausivel]
        if rets.empty:
            continue
        eq *= (1 + float(rets.mean()))
        out.append({"date": d, "next_date": dates[i + 1],
                    "return_pct": float(rets.mean()) * 100, "equity": eq,
                    "n_ativos": int(len(rets))})
    if descartados:
        print(f"  (1/N: {descartados} retornos implausíveis descartados - "
              f"provável erro de ajuste de proventos na fonte)")
    return pd.DataFrame(out)


def anchor_curve(df):
    """Prepara a curva para exibição/análise: adiciona um ponto de ANCORAGEM
    (retorno 0%, equity=1.0) na primeira data de rebalanceamento, e usa a
    data de FIM de cada período (next_date) para os pontos seguintes - não a
    de início. Sem isso, o primeiro ponto plotado já vem com o retorno do
    primeiro período embutido (nunca existe um "antes de qualquer coisa
    acontecer" na curva), e o drawdown a partir do capital inicial fica
    subestimado se o primeiro período for de perda."""
    if df is None or df.empty:
        return df
    anchor = {"date": df["date"].iloc[0], "equity": 1.0}
    display_rows = [anchor] + [{"date": r["next_date"], "equity": r["equity"]} for _, r in df.iterrows()]
    return pd.DataFrame(display_rows)


def summarize(name, df, periods_per_year=4):
    if df is None or df.empty:
        return None
    rets = df["return_pct"] / 100
    total = df["equity"].iloc[-1] - 1
    years = len(rets) / periods_per_year
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    vol = rets.std() * np.sqrt(periods_per_year)
    downside = np.minimum(rets, 0) ** 2
    dd_dev = np.sqrt(downside.mean() * periods_per_year)
    # drawdown a partir do capital inicial (equity=1.0), não só entre os
    # pontos já registrados - captura uma queda logo no primeiro período
    curve = pd.concat([pd.Series([1.0]), df["equity"]], ignore_index=True)
    max_dd = float((curve / curve.cummax() - 1).min())
    return {
        "estratégia": name, "retorno_total_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2), "vol_anual_pct": round(vol * 100, 2),
        "sharpe": round(cagr / vol, 3) if vol > 0 else None,
        "sortino": round(cagr / dd_dev, 3) if dd_dev > 0 else None,
        "max_drawdown_pct": round(max_dd * 100, 2), "periodos": len(rets),
        "custo_total_pct": round(df["cost_pct"].sum(), 2) if "cost_pct" in df else 0.0,
    }


def plot_backtest(curves, output_path="backtest_equity.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 11, "axes.titleweight": "bold"})
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.set_facecolor("#fbfbfb")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, color="#e3e3e3", linewidth=0.8)

    styles = {
        "Estratégia (Markowitz)": ("#d62728", 2.4, "-"),
        "Estratégia (peso igual)": ("#e0a400", 2.4, "-"),
        "1/N do universo": ("#2f6fb3", 1.8, "--"),
        "Ibovespa": ("#222222", 1.8, "-"),
    }
    for name, df in curves.items():
        if df is None or df.empty:
            continue
        color, lw, ls = styles.get(name, ("#999999", 1.2, "-"))
        ax.plot(df["date"], (df["equity"] - 1) * 100, label=name, color=color, linewidth=lw, linestyle=ls)

    ax.axhline(0, color="#c2c2c2", linewidth=1)
    ax.set_xlabel("Data")
    ax.set_ylabel("Retorno acumulado (%)")
    ax.set_title("Backtest Walk-Forward - retorno fora da amostra", loc="left", fontsize=14)
    ax.legend(frameon=False)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def parse_args():
    p = argparse.ArgumentParser(description="Backtest walk-forward do screener + otimizador.")
    p.add_argument("--rebalance-freq", choices=list(REBALANCE_FREQUENCIES), default="quarterly",
                    help="Frequência de rebalanceamento. Padrão: quarterly. "
                         "'monthly' NÃO aumenta a informação disponível (fundamentos só mudam "
                         "a cada divulgação) - ver aviso impresso no resultado.")
    p.add_argument("--n-assets", type=int, default=DEFAULT_N_ASSETS)
    p.add_argument("--piotroski-min", type=int, default=DEFAULT_PIOTROSKI_MIN,
                    help="Piso do F-Score. Use 0 para desligar o filtro.")
    p.add_argument("--by-sector", action="store_true",
                    help="Seleciona os melhores de CADA setor em vez do ranking geral.")
    p.add_argument("--n-per-sector", type=int, default=2)
    p.add_argument("--sectors", nargs="*", default=None,
                    help="Restringe a estes setores (nomes do Yahoo, ex: 'Utilities' 'Energy').")
    p.add_argument("--min-adtv", type=float, default=DEFAULT_MIN_ADTV,
                    help="Piso de liquidez em R$/dia.")
    p.add_argument("--cost-pct", type=float, default=DEFAULT_COST_PCT)
    p.add_argument("--min-weight", type=float, default=2.0)
    p.add_argument("--max-weight", type=float, default=35.0)
    p.add_argument("--lookback-days", type=int, default=756)
    p.add_argument("--min-history-days", type=int, default=DEFAULT_MIN_HISTORY_DAYS)
    p.add_argument("--risk-free-rate", type=float, default=14.25,
                    help="Fallback caso a série histórica da Selic não seja obtida.")
    p.add_argument("--fallback", choices=["equal", "minvar", "cash"], default="minvar",
                    help="O que fazer quando a otimização falha. minvar (padrão): mínima "
                         "variância, que usa só a covariância e não depende da estimativa "
                         "de retorno - se ela também falhar, cai para peso igual. "
                         "equal: peso igual direto. cash: 100%% em caixa rendendo a Selic "
                         "(ver ressalva na docstring de optimize_weights).")
    p.add_argument("--mu-shrinkage", type=float, default=0.0)
    p.add_argument("--l2-gamma", type=float, default=0.0)
    return p.parse_args()


ARQUIVO_CARTEIRAS = "backtest_carteiras.csv"


def exportar_carteiras(history, arquivo=ARQUIVO_CARTEIRAS):
    """Grava a composição de cada carteira, em cada rebalanceamento.

    POR QUE: os números agregados (Sharpe, CAGR) escondem o que a estratégia
    de fato fez. Com a composição em mãos dá para responder quais ativos
    apareceram mais, se a carteira gira muito, e se as duas estratégias
    escolhem os mesmos papéis - nesta configuração escolhem, já que a seleção
    vem do screener e só a regra de peso difere. Ver isso explícito ajuda a
    entender de onde vem a diferença de resultado entre elas."""
    linhas = []
    for estrategia, registros in history.items():
        for r in registros:
            for ticker, peso in (r.get("weights") or {}).items():
                linhas.append({
                    "estrategia": estrategia,
                    "data": pd.Timestamp(r["date"]).date(),
                    "ticker": ticker,
                    "peso_pct": round(float(peso) * 100, 2),
                    "retorno_periodo_pct": round(r["return_pct"], 2),
                })
    if not linhas:
        return
    df = pd.DataFrame(linhas).sort_values(["estrategia", "data", "peso_pct"],
                                           ascending=[True, True, False])
    df.to_csv(arquivo, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 78)
    print("COMPOSIÇÃO DAS CARTEIRAS")
    print("=" * 78)
    print(f"Detalhe completo em: {arquivo} ({len(df)} linhas)\n")

    # os ativos que mais apareceram, na estratégia otimizada
    principal = "markowitz" if "markowitz" in history else list(history)[0]
    d = df[df["estrategia"] == principal]
    n_datas = d["data"].nunique()
    freq = (d.groupby("ticker")
              .agg(periodos=("data", "nunique"), peso_medio=("peso_pct", "mean"))
              .sort_values("periodos", ascending=False))
    freq["pct_periodos"] = (freq["periodos"] / n_datas * 100).round(0)
    print(f"Ativos mais frequentes ({principal}, {n_datas} rebalanceamentos):")
    print(freq.head(15).to_string())
    print(f"\n{len(freq)} ativos distintos passaram pela carteira ao longo do período.")

    # giro: quanto da carteira muda de um rebalanceamento para o outro
    datas = sorted(d["data"].unique())
    trocas = []
    for i in range(1, len(datas)):
        ant = set(d[d["data"] == datas[i - 1]]["ticker"])
        atual = set(d[d["data"] == datas[i]]["ticker"])
        if ant:
            trocas.append(len(atual - ant) / len(atual) * 100)
    if trocas:
        print(f"\nGiro médio: {sum(trocas)/len(trocas):.0f}% dos ativos trocados "
              f"a cada rebalanceamento (mín {min(trocas):.0f}%, máx {max(trocas):.0f}%).")

    # as duas estratégias escolhem os mesmos ativos?
    if len(history) > 1:
        outra = [k for k in history if k != principal][0]
        d2 = df[df["estrategia"] == outra]
        iguais = 0
        for dt in datas:
            a = set(d[d["data"] == dt]["ticker"])
            b = set(d2[d2["data"] == dt]["ticker"])
            if a and a == b:
                iguais += 1
        print(f"\n'{principal}' e '{outra}' selecionaram os MESMOS ativos em "
              f"{iguais} de {len(datas)} rebalanceamentos"
              + (" - a diferença entre elas é só a regra de peso." if iguais == len(datas) else "."))


def main():
    args = parse_args()
    if args.piotroski_min == 0:
        args.piotroski_min = None
    engine = create_engine(os.environ["DATABASE_URL"])

    history, prices_adj, dates = run_backtest(engine, args)

    with engine.connect() as conn:
        adtv_rows = conn.execute(text("SELECT ticker, avg_daily_value_brl FROM market_metrics")).fetchall()
    adtv_map = {r[0]: (float(r[1]) if r[1] is not None else None) for r in adtv_rows}

    curves_full = {
        "1/N do universo (histórico completo)": universe_equal_weight_curve(prices_adj, dates, adtv_map, args.min_adtv),
        "Ibovespa (histórico completo)": benchmark_curve(prices_adj, dates, "^BVSP"),
    }

    strategy_dates = sorted({h["date"] for h in history["markowitz"]} | {h["date"] for h in history["equal"]})
    if strategy_dates:
        matched_dates = [d for d in dates if d >= strategy_dates[0]]
    else:
        matched_dates = dates

    curves = {
        "Estratégia (Markowitz)": pd.DataFrame(history["markowitz"]),
        "Estratégia (peso igual)": pd.DataFrame(history["equal"]),
        "1/N do universo": universe_equal_weight_curve(prices_adj, matched_dates, adtv_map, args.min_adtv),
        "Ibovespa": benchmark_curve(prices_adj, matched_dates, "^BVSP"),
    }

    ppy = PERIODS_PER_YEAR[args.rebalance_freq]

    if strategy_dates:
        print("\n" + "=" * 78)
        print(f"CONTEXTO - histórico completo das referências (desde {dates[0].date()}, {len(dates)-1} períodos)")
        print("A Estratégia não existe nesta janela inteira - ela só começa a operar quando o")
        print("primeiro conjunto de fundamentos fica disponível. Esta tabela é só para você ver")
        print("o que o mercado fez ANTES da estratégia poder existir.")
        print("=" * 78)
        full_summaries = [s for s in (summarize(n, d, periods_per_year=ppy) for n, d in curves_full.items()) if s]
        if full_summaries:
            print(pd.DataFrame(full_summaries).to_string(index=False))

    print("\n" + "=" * 78)
    print(f"RESULTADO (fora da amostra) - PERÍODO COMPARÁVEL, desde {strategy_dates[0].date() if strategy_dates else dates[0].date()}")
    print("Esta é a comparação justa: todas as 4 curvas medidas na MESMA janela.")
    print("=" * 78)
    summaries = [s for s in (summarize(n, d, periods_per_year=ppy) for n, d in curves.items()) if s]
    if summaries:
        print(pd.DataFrame(summaries).to_string(index=False))

    path = plot_backtest({name: anchor_curve(df) for name, df in curves.items()})
    print(f"\nGráfico salvo em: {path}")

    exportar_carteiras(history)

    print("\n" + "!" * 78)
    print("LEIA ANTES DE INTERPRETAR:")
    print("  1. O universo só contém empresas que existem HOJE (viés de sobrevivência).")
    print("     Empresas que quebraram no período não estão aqui - o resultado é otimista.")
    print(f"  2. São apenas {summaries[0]['periodos'] if summaries else 0} períodos. É pouco para conclusão estatística.")
    print("  3. Não inclui imposto de renda.")
    print("  4. Se a estratégia não superar '1/N do universo' de forma consistente,")
    print("     a complexidade do screener + otimizador não está se pagando.")
    if args.rebalance_freq == "monthly":
        print("  5. Frequência mensal: os fundamentos só mudam a cada divulgação (trimestral,")
        print("     no máximo) - a maioria dos rebalanceamentos mensais reage ao MESMO retrato")
        print("     fundamentalista de antes, só com preço diferente. Mais pontos na curva não")
        print("     significa mais informação independente; e o turnover extra tem custo real.")
    print("!" * 78)


if __name__ == "__main__":
    main()