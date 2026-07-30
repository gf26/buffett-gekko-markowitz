"""
Helpers de point-in-time (PIT) - fundação para o futuro motor de backtest.

PROBLEMA QUE ISTO RESOLVE
-------------------------
O balanço com fiscal_date = 2024-12-31 não estava disponível em 2024-12-31.
Empresas brasileiras publicam o resultado anual tipicamente entre fevereiro e
março do ano seguinte. Usar aquele dado para tomar decisão em 31/12/2024 é
look-ahead bias: você estaria "vendo o futuro", e qualquer backtest ficaria
otimista de um jeito que não se reproduz com dinheiro real.

O Yahoo Finance não informa a data de publicação. Duas mitigações:

  (a) first_seen_at (coluna em `financials`): quando NÓS vimos o dado pela
      primeira vez. É honesto, mas só vale daqui pra frente - tudo que já foi
      coletado tem first_seen_at = data da migração, não a publicação real.

  (b) LAG CONSERVADOR (este módulo): assumir que o dado do exercício X só
      ficou disponível N meses depois do fim do exercício. Funciona
      retroativamente, é simples de auditar, e erra para o lado seguro.

Este módulo implementa (b) e usa (a) quando disponível e mais restritivo.

ATENÇÃO: nada aqui está ligado ao pipeline atual ainda - o screener roda sobre
dados de HOJE, onde o problema não se aplica. Isto existe para o motor de
backtest, que é o próximo passo do roteiro.
"""
from datetime import date

import pandas as pd
from sqlalchemy import text

# Meses assumidos entre o fim do exercício e a disponibilidade pública do dado.
# 3 meses cobre a maioria dos balanços ANUAIS brasileiros (publicação até março
# para exercício encerrado em dezembro). Para trimestrais, 45-60 dias é o
# prazo regulatório usual, então 2 meses é razoável.
DEFAULT_LAG_MONTHS_ANNUAL = 3
DEFAULT_LAG_MONTHS_QUARTERLY = 2


def available_from(fiscal_date, period_type="annual",
                    lag_months_annual=DEFAULT_LAG_MONTHS_ANNUAL,
                    lag_months_quarterly=DEFAULT_LAG_MONTHS_QUARTERLY):
    """A partir de que data assumimos que este demonstrativo era público."""
    lag = lag_months_annual if period_type == "annual" else lag_months_quarterly
    return pd.Timestamp(fiscal_date) + pd.DateOffset(months=lag)


def load_financials_as_of(engine, as_of_date, tickers=None, statement=None,
                           period_type="annual", use_first_seen=True, **lag_kwargs):
    """
    Carrega de `financials` apenas o que já estaria DISPONÍVEL em as_of_date.

    Um registro é considerado disponível se:
      - fiscal_date + lag <= as_of_date  (regra conservadora, sempre aplicada)
      - E, se use_first_seen=True e o registro tiver first_seen_at, também
        first_seen_at <= as_of_date (mais restritivo quando conhecido)

    Devolve o formato tidy de sempre (ticker, statement, period_type,
    fiscal_date, line_item, value), já filtrado - e mantendo, para cada
    combinação, apenas o exercício mais recente disponível naquela data.
    """
    as_of = pd.Timestamp(as_of_date)

    sql = """
        SELECT ticker, statement, period_type, fiscal_date, line_item, value, first_seen_at
        FROM financials
        WHERE period_type = :period_type
    """
    params = {"period_type": period_type}
    if tickers:
        sql += " AND ticker = ANY(:tickers)"
        params["tickers"] = list(tickers)
    if statement:
        sql += " AND statement = :statement"
        params["statement"] = statement

    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params=params)

    if df.empty:
        return df

    df["fiscal_date"] = pd.to_datetime(df["fiscal_date"])
    df["available_from"] = df.apply(
        lambda r: available_from(r["fiscal_date"], r["period_type"], **lag_kwargs), axis=1
    )

    mask = df["available_from"] <= as_of
    if use_first_seen and "first_seen_at" in df.columns:
        seen = pd.to_datetime(df["first_seen_at"], errors="coerce", utc=True).dt.tz_localize(None)
        mask &= seen.isna() | (seen <= as_of)

    df = df[mask]
    if df.empty:
        return df

    # para cada (ticker, statement, line_item), fica só o exercício mais recente
    # que já era público na data
    idx = df.groupby(["ticker", "statement", "line_item"])["fiscal_date"].idxmax()
    return df.loc[idx].drop(columns=["available_from", "first_seen_at"]).reset_index(drop=True)


def build_universe_as_of(engine, as_of_date):
    """
    Universo de tickers que existiam e negociavam em as_of_date - a defesa
    contra viés de sobrevivência.

    Inclui tickers hoje inativos, desde que a deslistagem tenha ocorrido DEPOIS
    da data consultada. Depende de `delisted_date` estar preenchido (ver
    vw_candidatos_deslistagem no schema_camada1.sql); enquanto não estiver, o
    resultado ainda carrega viés - e esta função avisa quando é o caso.
    """
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ticker, delisted_date FROM tickers
            WHERE (delisted_date IS NULL OR delisted_date > :as_of)
        """), {"as_of": as_of_date}).fetchall()

        total_inactive = conn.execute(text(
            "SELECT COUNT(*) FROM tickers WHERE active = FALSE AND delisted_date IS NULL"
        )).scalar()

    if total_inactive:
        print(f"AVISO de viés de sobrevivência: {total_inactive} tickers inativos sem "
              f"delisted_date preenchido. O universo retornado está incompleto - "
              f"veja vw_candidatos_deslistagem.")

    return [r[0] for r in rows]
