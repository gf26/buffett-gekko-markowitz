"""
Série de preços unificada: brapi onde existe, ajuste próprio onde não existe.

O RACIOCÍNIO
------------
A brapi entrega preços já ajustados para 476 tickers, com histórico desde
2000. Reconstruir o ajuste desses seria trabalho desperdiçado - e foi o que
estávamos fazendo ao refinar o tratamento de ITUB4, SANB11 e VIVT3, todos
cobertos por ela.

O ajuste próprio existe para os ~450 tickers que a brapi NÃO tem: empresas que
fecharam capital de vez (APER3, HGTX3, CIEL3, ENBR3) e que só existem no
COTAHIST. São exatamente elas que causam o viés de sobrevivência, e por isso
o esforço vale.

A validação (`validar_ajuste.py`) serve para medir o erro do método ONDE AS
DUAS FONTES EXISTEM, e assim saber que confiança ter no grupo em que só temos
a nossa. Resultado: 99,8% a 100% dos retornos diários coincidem, com
divergência concentrada em datas de evento societário.

O QUE ESTE SCRIPT FAZ
---------------------
1. Baixa a série ajustada da brapi para os tickers que ela cobre.
2. Para os demais, usa `prices_ajustados` (COTAHIST + ajuste próprio).
3. Grava tudo em `prices_final`, com uma coluna `fonte` dizendo de onde veio.
4. Mede quantos tickers SEM brapi têm eventos proporcionais não localizados -
   que é o número que mede o risco real, não os 37 avisos totais.

Uso:
    BRAPI_TOKEN="..." DATABASE_URL="..." python precos_unificados.py --dry-run
    BRAPI_TOKEN="..." DATABASE_URL="..." python precos_unificados.py
    BRAPI_TOKEN="..." DATABASE_URL="..." python precos_unificados.py --so-diagnostico
"""
import argparse
import os
import sys
import time

import pandas as pd
import requests
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

import ajustar_precos as ap

URL = "https://brapi.dev/api/quote/{ticker}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices_final (
    ticker     TEXT NOT NULL,
    date       DATE NOT NULL,
    adj_close  NUMERIC NOT NULL,
    volume     NUMERIC,
    fonte      TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_final_date ON prices_final(date);
CREATE INDEX IF NOT EXISTS idx_prices_final_fonte ON prices_final(fonte);
"""


def serie_brapi(ticker, token):
    try:
        r = requests.get(URL.format(ticker=ticker),
                          params={"token": token, "range": "max", "interval": "1d"},
                          timeout=60)
    except requests.exceptions.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        res = (r.json().get("results") or [{}])[0]
    except ValueError:
        return None
    h = res.get("historicalDataPrice") or []
    if not h:
        return None
    d = pd.DataFrame(h)
    d["date"] = pd.to_datetime(d["date"], unit="s").dt.normalize()
    col = "adjustedClose" if "adjustedClose" in d.columns else "close"
    d["adj_close"] = pd.to_numeric(d[col], errors="coerce")
    d["volume"] = pd.to_numeric(d.get("volume"), errors="coerce")
    d = d.dropna(subset=["adj_close"])
    d = d[d["adj_close"] > 0]
    if d.empty:
        return None
    d = d.drop_duplicates("date", keep="last")
    return d[["date", "adj_close", "volume"]]


def diagnostico_sem_brapi(engine, com_brapi):
    """Quantos tickers SEM cobertura da brapi têm evento não localizado.

    É este o número que mede o risco: um evento proporcional não aplicado
    deixa um degrau de 50-99% na série. Nos tickers que a brapi cobre isso é
    irrelevante (usamos a série dela); nos que ela não cobre, é o erro que
    resta."""
    px, ev = ap.carregar(engine)
    evt = dict(tuple(ev.groupby("ticker")))
    sem_cob, com_aviso, detalhes = [], [], []
    for tk, g in px.groupby("ticker"):
        if tk in com_brapi:
            continue
        sem_cob.append(tk)
        e = evt.get(tk)
        if e is None or e.empty:
            continue
        _, avisos = ap.ajustar_ticker(g, e)
        prop = [a for a in avisos if "NÃO aplicado" in a]
        if prop:
            com_aviso.append(tk)
            detalhes.extend(f"{tk} {a}" for a in prop)

    print("\n" + "=" * 78)
    print("RISCO REAL - tickers em que dependemos do ajuste próprio")
    print("=" * 78)
    print(f"{len(sem_cob)} tickers sem cobertura da brapi")
    print(f"  {len(com_aviso)} com evento proporcional NÃO localizado")
    if sem_cob:
        print(f"  ({len(com_aviso)/len(sem_cob)*100:.1f}% do grupo que depende de nós)")
    if detalhes:
        print("\nCasos (cada um deixa degrau de 50-99% na série):")
        for d in detalhes[:25]:
            print(f"  {d}")
        if len(detalhes) > 25:
            print(f"  ... e mais {len(detalhes) - 25}")
    else:
        print("\n  Nenhum. Os avisos que existiam são todos de tickers que a")
        print("  brapi cobre - e para esses usamos a série dela.")
    return sem_cob, com_aviso


def gravar(engine, partes):
    with engine.begin() as conn:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    df = pd.concat(partes, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    linhas = [(r.ticker, r.date, float(r.adj_close),
                None if pd.isna(r.volume) else float(r.volume), r.fonte)
              for r in df.itertuples(index=False)]
    LOTE = 50_000
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prices_final")
            for i in range(0, len(linhas), LOTE):
                execute_values(cur, """
                    INSERT INTO prices_final (ticker, date, adj_close, volume, fonte)
                    VALUES %s
                    ON CONFLICT (ticker, date) DO UPDATE SET
                        adj_close = EXCLUDED.adj_close, fonte = EXCLUDED.fonte
                """, linhas[i:i + LOTE], page_size=5000)
                conn.commit()
                print(f"    gravadas {min(i + LOTE, len(linhas)):,} de {len(linhas):,}")
    finally:
        conn.close()
    return len(linhas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.environ.get("BRAPI_TOKEN"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--so-diagnostico", action="store_true",
                    help="Só mede o risco, sem buscar séries nem gravar.")
    p.add_argument("--pausa", type=float, default=0.25)
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])

    with engine.connect() as conn:
        nossos = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT ticker FROM prices_ajustados ORDER BY ticker")).fetchall()]
        # a brapi cobre quem tem provento registrado - é o melhor indicador
        # disponível de que ela conhece o ticker
        com_brapi = {r[0] for r in conn.execute(text(
            "SELECT DISTINCT ticker FROM proventos_brapi")).fetchall()}
    print(f"{len(nossos)} tickers em prices_ajustados")
    print(f"{len(com_brapi & set(nossos))} deles a brapi cobre\n")

    if args.so_diagnostico:
        diagnostico_sem_brapi(engine, com_brapi)
        return

    if not args.token:
        sys.exit("Token não informado.")

    partes, usou_brapi, usou_nosso, falhou = [], [], [], []
    for i, tk in enumerate(nossos, 1):
        if tk in com_brapi:
            s = serie_brapi(tk, args.token)
            time.sleep(args.pausa)
            if s is not None:
                s = s.copy()
                s.insert(0, "ticker", tk)
                s["fonte"] = "brapi"
                partes.append(s)
                usou_brapi.append(tk)
                if i % 50 == 0:
                    print(f"  {i}/{len(nossos)}...")
                continue
            falhou.append(tk)
        with engine.connect() as conn:
            d = pd.read_sql(text("""
                SELECT ticker, date, adj_close, volume FROM prices_ajustados
                WHERE ticker = :tk AND adj_close > 0 ORDER BY date
            """), conn, params={"tk": tk})
        if not d.empty:
            d["fonte"] = "cotahist"
            partes.append(d)
            usou_nosso.append(tk)
        if i % 50 == 0:
            print(f"  {i}/{len(nossos)}...")

    print(f"\n{len(usou_brapi)} tickers com série da brapi")
    print(f"{len(usou_nosso)} tickers com ajuste próprio (COTAHIST)")
    if falhou:
        print(f"{len(falhou)} tinham provento na brapi mas a série falhou - "
              f"caíram no ajuste próprio")

    diagnostico_sem_brapi(engine, set(usou_brapi))

    if args.dry_run:
        total = sum(len(x) for x in partes)
        print(f"\n{total:,} linhas prontas (dry-run - nada gravado)")
        return
    n = gravar(engine, partes)
    print(f"\n{n:,} linhas gravadas em prices_final.")

    with engine.connect() as conn:
        r = pd.read_sql(text("""
            SELECT fonte, COUNT(*) AS linhas, COUNT(DISTINCT ticker) AS tickers,
                   MIN(date) AS de, MAX(date) AS ate
            FROM prices_final GROUP BY fonte ORDER BY fonte
        """), conn)
    print()
    print(r.to_string(index=False))
    print("\nUse `prices_final` daqui em diante. A coluna `fonte` permite")
    print("auditar de onde veio cada série.")


if __name__ == "__main__":
    main()
