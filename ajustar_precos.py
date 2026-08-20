"""
Ajuste de preços do COTAHIST por proventos e eventos societários.

POR QUE PRECISA EXISTIR
-----------------------
O COTAHIST traz o preço NEGOCIADO em cada pregão, sem ajuste. A série fica com
degraus: uma ação que pagou dividendo aparece caindo, e uma que desdobrou
aparece despencando. Sem ajuste, qualquer cálculo de retorno está errado.

O Yahoo já entrega ajustado, mas com dois problemas comprovados: remove
tickers deslistados (causa do viés de sobrevivência) e às vezes quebra - a
AZEV3 aparece cotada a R$ 0,0001 por 740 pregões. Fazer o ajuste aqui torna-o
auditável: erros viram bugs investigáveis em vez de mistérios da fonte.

OS TRÊS TIPOS DE EVENTO
-----------------------
1. DINHEIRO (DIVIDENDO, JCP, RENDIMENTO, REST CAP DIN)
   O preço cai pelo valor distribuído. Fator = 1 - valor/preço_anterior.
   REST CAP DIN entra aqui: devolve capital em dinheiro, e o efeito no preço
   é idêntico ao de um dividendo. A diferença é tributária, não de preço.

2. PROPORCIONAL (DESDOBRAMENTO, GRUPAMENTO, BONIFICACAO)
   Vem com `fator` = razão nova/antiga, verificado contra casos conhecidos:
   MGLU3 1:8 em 2019 -> 8.0; WEGE3 1:2 em 2021 -> 2.0; grupamento do AERI3
   1:20 -> 0.05. O preço é dividido pelo fator.

3. CISÃO (CIS RED CAP) - o caso problemático
   A fonte grava `fator = 100.0` em 24 dos 27 registros: Bradesco, Itaú,
   Cyrela, MRV, Pão de Açúcar, Telebras... Empresas diferentes com o mesmo
   valor exato é preenchimento padrão, não razão real. Aplicá-lo
   multiplicaria o preço do Itaú por 100.

   Verificado no COTAHIST: na cisão do Itaú (2021-10-01), o preço foi de
   R$ 29,67 para R$ 24,34 - queda de 18%, que é o peso do XP Inc., não 1:100.

   SOLUÇÃO: usar a fonte só para saber QUANDO houve cisão, e medir o fator no
   próprio salto de preço. O dado tem a resposta.

   Limitação: o salto mistura o efeito da cisão com o movimento normal do
   mercado naquele dia. Para uma cisão de 18%, o ruído de 1-2% é aceitável.
   Proteção: só ajusta se o salto estiver em faixa plausível; fora disso,
   sinaliza para revisão em vez de aplicar um número duvidoso.

MÉTODO
------
Ajuste retroativo, como é padrão: o preço de HOJE fica intacto e os
anteriores são multiplicados pelo fator acumulado. Assim a série mais recente
é comparável com o que se vê no mercado.

Uso:
    DATABASE_URL="..." python ajustar_precos.py --dry-run
    DATABASE_URL="..." python ajustar_precos.py --tickers ITUB4 MGLU3 PETR4
    DATABASE_URL="..." python ajustar_precos.py
"""
import argparse
import os

import numpy as np
import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

TIPOS_DINHEIRO = {"DIVIDENDO", "JCP", "RENDIMENTO", "REST CAP DIN"}
TIPOS_PROPORCIONAIS = {"DESDOBRAMENTO", "GRUPAMENTO", "BONIFICACAO"}
# rótulo usado quando dois ou mais eventos proporcionais caem na mesma data
TIPO_COMBINADO = "PROPORCIONAL COMBINADO"
TIPOS_PROPORCIONAIS_TODOS = TIPOS_PROPORCIONAIS | {TIPO_COMBINADO}
TIPO_CISAO = "CIS RED CAP"

# faixa em que um salto é plausivelmente uma cisão. Fora dela, o evento é
# sinalizado em vez de ajustado - um salto de 60% pode ser cisão grande ou
# outra coisa acontecendo no mesmo pregão.
# Faixa em que um salto é plausivelmente uma cisão.
#
# Mínimo 0,20: o Pão de Açúcar cindiu o Assaí em 2021 e caiu 72% (salto de
# 0,281). Com o mínimo em 0,30 esse caso era descartado.
#
# Máximo 0,95: saltos acima disso (CSAN3 0,957, SANB11 0,972, VIVR3 0,979)
# são indistinguíveis de movimento normal de mercado. Forçar um fator ali
# confundiria ruído com evento.
CISAO_MIN, CISAO_MAX = 0.20, 0.95

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices_ajustados (
    ticker      TEXT NOT NULL,
    date        DATE NOT NULL,
    close       NUMERIC,
    adj_close   NUMERIC,
    fator_ajuste NUMERIC,
    volume      NUMERIC,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_ajustados_date ON prices_ajustados(date);
"""


# BDR = ação estrangeira negociada aqui, fora do escopo de um screener da B3.
# O código termina em dois dígitos começando com 3 (AAPL34, BIJH39). Ações e
# units brasileiras usam 3, 4, 5, 6 ou 11 - nunca 3x.
RE_BDR = r"^[A-Z]{4}3[0-9]$"


def carregar(engine, tickers=None):
    cond, params = ["close IS NOT NULL", "close > 0",
                    f"ticker !~ '{RE_BDR}'"], {}
    if tickers:
        cond.append("ticker = ANY(:tk)")
        params["tk"] = tickers
    with engine.connect() as conn:
        px = pd.read_sql(text(f"""
            SELECT ticker, date, close, volume FROM prices_cotahist
            WHERE {' AND '.join(cond)} ORDER BY ticker, date
        """), conn, params=params)
        ev = pd.read_sql(text("""
            SELECT ticker, ex_date, tipo, valor, fator FROM proventos_brapi
            WHERE ex_date <= CURRENT_DATE
        """), conn)
    px["date"] = pd.to_datetime(px["date"])
    ev["ex_date"] = pd.to_datetime(ev["ex_date"])
    return px, ev


def fator_cisao(serie, data_ex):
    """Mede o fator da cisão pelo salto de preço.

    `data_ex` aqui é a data-COM (lastDatePrior da brapi): a última data em que
    o papel ainda dá direito ao evento. O salto acontece no pregão SEGUINTE.

    No Itaú: data-com 2021-10-01 com fechamento de R$ 29,67; o preço cai para
    R$ 24,34 em 04/10. Comparar em torno de 01/10 (em vez de depois dela) dava
    razão de 1,026 e o evento era descartado como implausível.

    Devolve (fator, motivo). Fator None significa que o salto não é plausível
    como cisão e o evento deve ser sinalizado, não ajustado."""
    antes = serie[serie.index <= data_ex]
    depois = serie[serie.index > data_ex]
    if antes.empty or depois.empty:
        return None, "sem preço antes ou depois"
    p_antes, p_depois = float(antes.iloc[-1]), float(depois.iloc[0])
    if p_antes <= 0:
        return None, "preço anterior inválido"
    r = p_depois / p_antes
    if not (CISAO_MIN <= r <= CISAO_MAX):
        return None, f"salto de {r:.3f} fora da faixa plausível"
    return r, f"medido no salto ({p_antes:.2f} -> {p_depois:.2f})"


JANELA_EVENTO = 5   # pregões de tolerância em torno da data informada


def localizar_evento(close, data_ex, fator_esperado, janela=JANELA_EVENTO):
    """Encontra o pregão em que o evento proporcional de fato ocorreu.

    POR QUE NÃO CONFIAR NA DATA DA FONTE: ela é inconsistente. No MGLU3, o
    desdobramento 1:8 é informado em 06/08/2019 e o preço cai exatamente nesse
    dia (276,00 -> 36,60). No ITUB4, o grupamento 1:100 é informado em
    01/11/2011 mas o preço não muda ali (32,73 -> 32,13) - o COTAHIST já
    estava grupado antes. Nenhuma convenção fixa serve para os dois.

    A SOLUÇÃO: o fator é CONHECIDO (8,0 no Magalu, 0,01 no Itaú); só a data é
    incerta. Procurar numa janela o pregão em que a razão entre fechamentos
    consecutivos se aproxima do fator esperado localiza o evento sem
    adivinhar seu tamanho. Um salto de exatamente 8x é inconfundível.

    Devolve (data_do_evento, razao_observada) ou (None, motivo) quando nenhum
    pregão da janela mostra o salto - caso em que o evento não deve ser
    aplicado. Foi assim que descobrimos que o desdobramento da WEGE3 em 2015
    existe no preço mas NÃO está na fonte."""
    idx = close.index
    pos = idx.searchsorted(data_ex)
    ini, fim = max(1, pos - janela), min(len(idx), pos + janela + 1)
    if ini >= fim:
        return None, "fora do histórico de preços"

    # razão esperada entre o fechamento do dia e o do pregão anterior:
    # num desdobramento 1:8 o preço cai para 1/8
    alvo = 1.0 / fator_esperado
    melhor, melhor_erro = None, None
    for i in range(ini, fim):
        anterior, atual = float(close.iloc[i - 1]), float(close.iloc[i])
        if anterior <= 0:
            continue
        r = atual / anterior
        erro = abs(r / alvo - 1)
        if melhor_erro is None or erro < melhor_erro:
            melhor, melhor_erro = idx[i], erro

    # 25% de tolerância: o salto carrega o movimento normal do dia junto.
    # O MGLU3 caiu 86,7% num desdobramento de 87,5% - resíduo de 6%, que é o
    # retorno real da ação naquele pregão.
    # 40% de tolerância: o salto carrega o movimento normal do dia. Casos
    # reais ficavam logo acima de 25% - BALM3 com 33%, BPAC11 e CEED3 com 29%,
    # CTKA3 com 43%. Como o alvo é uma razão específica (10x, 100x), 40% ainda
    # é seletivo: um movimento normal de mercado nunca chega perto de 10x.
    if melhor_erro is None or melhor_erro > 0.40:
        return None, (f"nenhum salto próximo de {alvo:.4f} em ±{janela} pregões"
                       + (f" (melhor: erro de {melhor_erro:.0%})" if melhor_erro else ""))
    return melhor, melhor_erro


def fator_proporcional(tipo, f):
    """Fator de um evento proporcional, com o sentido conferido.

    A convenção da fonte é `fator` = razão nova/antiga: desdobramento vem > 1
    (MGLU3 1:8 -> 8.0), grupamento vem < 1 (AERI3 1:20 -> 0.05). Verificado
    contra casos conhecidos.

    Mas 8 registros de 396 vêm invertidos - o BMEB3 tem GRUPAMENTO com fator
    2.0, que aplicado como está dividiria o preço em vez de multiplicar. Como
    o TIPO do evento define o sentido esperado, a inconsistência é detectável
    e corrigível: 2.0 num grupamento significa "2 viram 1", ou seja, 0.5.

    São 2% dos casos. Medir o fator no salto de preço para TODOS os eventos
    resolveria também, mas trocaria uma regra correta em 98% por uma medição
    sujeita a ruído de mercado."""
    if f is None or pd.isna(f) or f <= 0:
        return None
    f = float(f)
    if tipo == "GRUPAMENTO" and f > 1:
        return 1.0 / f
    if tipo == "DESDOBRAMENTO" and f < 1:
        return 1.0 / f
    return f


def escala_provento(ev_t, data_ex):
    """Fator para trazer o provento à base de ações VIGENTE na data-ex.

    O PROBLEMA: a brapi informa o provento na base de ações de HOJE, enquanto
    o COTAHIST traz o preço da ÉPOCA. Quando houve grupamento no meio, as duas
    bases divergem por ordens de grandeza.

    Caso real: SANB3 negociava a R$ 0,14 em 2013 e a brapi informa dividendo
    de R$ 1,56 - onze vezes o preço da ação, impossível. Houve grupamento
    depois, então R$ 1,56 é por ação NOVA; por ação da época seria bem menos.

    A CORREÇÃO: multiplicar o provento pelo produto dos fatores proporcionais
    ocorridos DEPOIS da data-ex. Num grupamento de 1:55 (fator 0,018), o
    provento de R$ 1,56 vira R$ 0,028 - coerente com preço de R$ 0,13.

    Aplica-se só a eventos POSTERIORES à data-ex: os anteriores já estão
    refletidos na base em que o provento foi declarado."""
    posteriores = ev_t[(ev_t["ex_date"] >= data_ex)
                       & ev_t["tipo"].str.upper().isin(TIPOS_PROPORCIONAIS)
                       & ev_t["fator"].notna()]
    if posteriores.empty:
        return 1.0
    f = 1.0
    for _, r in posteriores.iterrows():
        v = fator_proporcional(str(r["tipo"]).upper(), r["fator"])
        if v:
            f *= v
    return f if f > 0 else 1.0


def ajustar_ticker(px_t, ev_t):
    """Série ajustada de um ticker. Devolve (df, avisos)."""
    px_t = px_t.sort_values("date").set_index("date")
    close = px_t["close"]
    fator = pd.Series(1.0, index=close.index)
    avisos = []

    # COMBINA EVENTOS PROPORCIONAIS DA MESMA DATA.
    #
    # É prática comum no Brasil grupar e desdobrar em sequência para acertar a
    # quantidade de ações. BMEB3 em 2021-12-02 tem GRUPAMENTO 0,5 E
    # DESDOBRAMENTO 20 no mesmo dia; CASH3 em 2023-05-31 tem 0,01 e 10.
    #
    # Processados um a um, nenhum bate com o salto observado - mas o produto
    # bate: 0,5 x 20 = 10 no BMEB3. Consolidar antes de localizar resolve.
    ev_t = ev_t.copy()
    ev_t["_tipo"] = ev_t["tipo"].astype(str).str.upper()
    prop = ev_t[ev_t["_tipo"].isin(TIPOS_PROPORCIONAIS) & ev_t["fator"].notna()]
    outros = ev_t[~ev_t.index.isin(prop.index)]
    if len(prop) > 0:
        combinados = []
        for d, g in prop.groupby("ex_date"):
            f = 1.0
            for _, r in g.iterrows():
                v = fator_proporcional(r["_tipo"], r["fator"])
                if v:
                    f *= v
            tipo_repr = g["_tipo"].iloc[0] if len(g) == 1 else "PROPORCIONAL COMBINADO"
            combinados.append({"ex_date": d, "tipo": tipo_repr, "valor": None, "fator": f})
        ev_t = pd.concat([outros, pd.DataFrame(combinados)], ignore_index=True)

    for _, e in ev_t.sort_values("ex_date").iterrows():
        d, tipo = e["ex_date"], str(e["tipo"]).upper()

        # ALINHAMENTO DEPENDE DO TIPO DE EVENTO - verificado nos dados:
        #
        # PROPORCIONAIS: o COTAHIST já reflete o evento NA data que a fonte
        #   informa. MGLU3 cai de 276,00 (05/08/2019) para 36,60 (06/08) e a
        #   fonte informa 06/08; ITUB4 já estava grupado antes de 01/11/2011.
        #   Ajustar "até a data inclusive" criava um salto artificial de 694%
        #   no Magalu. O ajuste vale para datas ESTRITAMENTE ANTERIORES.
        #
        # DINHEIRO E CISÃO: o efeito aparece no pregão SEGUINTE. Na cisão do
        #   Itaú, o preço vai de 29,67 (01/10/2021, a data informada) para
        #   24,34 (04/10). Ajustar até a data inclusive é o correto.
        # proporcionais têm a data localizada pelo salto (ver
        # localizar_evento); dinheiro e cisão usam a data informada, cujo
        # efeito aparece no pregão seguinte
        anteriores = fator.index <= d
        if tipo not in TIPOS_PROPORCIONAIS_TODOS and not anteriores.any():
            continue

        if tipo in TIPOS_DINHEIRO:
            v = e["valor"]
            if pd.isna(v) or v <= 0:
                continue
            ant = close[close.index <= d]
            if ant.empty:
                continue
            p = float(ant.iloc[-1])   # fechamento da data-com
            if p <= 0:
                continue

            # TENTATIVA DUPLA: a fonte não diz em qual base de ações o provento
            # está, e usa bases diferentes em registros diferentes.
            #
            #   SANB3 2013: fonte diz R$ 1,56, preço era R$ 0,13 - impossível.
            #     Houve grupamento 1:55 depois; reescalado dá R$ 0,028. OK.
            #   BMEB3 2010: fonte diz R$ 0,86, preço era R$ 15,60 - plausível
            #     direto. Reescalar pelo desdobramento 20:1 posterior daria
            #     R$ 34,39, impossível.
            #
            # Não há campo que distinga os dois. O PREÇO serve de árbitro: um
            # provento é sempre menor que o valor da ação (senão a empresa
            # distribuiria mais do que vale). Fica a base que é consistente.
            #
            # FRAGILIDADE CONHECIDA: quando ambas as bases couberem, escolhe o
            # bruto sem certeza. Acontece em papel de preço alto com evento
            # proporcional pequeno posterior.
            bruto = float(v)
            reescalado = bruto * escala_provento(ev_t, d)
            if bruto < p:
                v_usado = bruto
            elif reescalado < p:
                v_usado = reescalado
            else:
                avisos.append(f"{d.date()} {tipo}: nem bruto ({bruto:.4f}) nem "
                               f"reescalado ({reescalado:.4f}) cabem no preço {p:.4f} - ignorado")
                continue
            fator.loc[anteriores] *= (1 - v_usado / p)

        elif tipo in TIPOS_PROPORCIONAIS_TODOS:
            f = float(e["fator"]) if pd.notna(e["fator"]) else None
            if not f:
                continue
            # localiza o pregão do evento pelo salto, não pela data da fonte
            data_real, info = localizar_evento(close, d, f)
            if data_real is None:
                avisos.append(f"{d.date()} {tipo} (fator {f:.4f}): {info} - NÃO aplicado")
                continue
            fator.loc[fator.index < data_real] /= f

        elif tipo == TIPO_CISAO:
            f, motivo = fator_cisao(close, d)
            if f is None:
                avisos.append(f"{d.date()} CISÃO: {motivo} - NÃO ajustada")
                continue
            fator.loc[anteriores] *= f

    out = pd.DataFrame({
        "close": close,
        "adj_close": close * fator,
        "fator_ajuste": fator,
        "volume": px_t["volume"],
    })
    return out.reset_index(), avisos


def gravar(engine, df):
    with engine.begin() as conn:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.date
    linhas = [(r.ticker, r.date,
                None if pd.isna(r.close) else float(r.close),
                None if pd.isna(r.adj_close) else float(r.adj_close),
                None if pd.isna(r.fator_ajuste) else float(r.fator_ajuste),
                None if pd.isna(r.volume) else float(r.volume))
              for r in d.itertuples(index=False)]
    # GRAVAÇÃO EM LOTES.
    #
    # O execute_values monta UMA instrução SQL com todas as linhas. Com 1,32
    # milhão delas, a string fica grande demais e o processo trava montando o
    # comando - parecia loop infinito, e era só uma consulta gigante.
    #
    # Em lotes de 50 mil, cada instrução é administrável e o progresso fica
    # visível.
    LOTE = 50_000
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prices_ajustados")
            total = len(linhas)
            for i in range(0, total, LOTE):
                execute_values(cur, """
                    INSERT INTO prices_ajustados (ticker, date, close, adj_close, fator_ajuste, volume)
                    VALUES %s
                    ON CONFLICT (ticker, date) DO UPDATE SET
                        adj_close = EXCLUDED.adj_close, fator_ajuste = EXCLUDED.fator_ajuste
                """, linhas[i:i + LOTE], page_size=5000)
                conn.commit()
                print(f"    gravadas {min(i + LOTE, total):,} de {total:,}")
    finally:
        conn.close()
    return len(linhas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    alvos = [t.upper().replace(".SA", "") for t in args.tickers] if args.tickers else None
    print("Carregando preços e eventos...")
    px, ev = carregar(engine, alvos)
    print(f"  {len(px):,} preços, {px['ticker'].nunique()} tickers")
    print(f"  {len(ev):,} eventos, {ev['ticker'].nunique()} tickers\n")

    ev_por_ticker = dict(tuple(ev.groupby("ticker")))
    partes, todos_avisos, sem_eventos = [], [], 0

    for tk, g in px.groupby("ticker"):
        e = ev_por_ticker.get(tk)
        if e is None or e.empty:
            sem_eventos += 1
            g = g.copy()
            g["adj_close"] = g["close"]
            g["fator_ajuste"] = 1.0
            partes.append(g[["ticker", "date", "close", "adj_close", "fator_ajuste", "volume"]])
            continue
        out, avisos = ajustar_ticker(g, e)
        out.insert(0, "ticker", tk)
        partes.append(out)
        todos_avisos.extend(f"{tk} {a}" for a in avisos)

    df = pd.concat(partes, ignore_index=True)
    print(f"{len(df):,} preços ajustados")
    print(f"  {sem_eventos} tickers sem eventos (adj_close = close)")

    # quanto o ajuste mexeu
    mexeu = df[df["fator_ajuste"] < 0.999]
    print(f"  {mexeu['ticker'].nunique()} tickers tiveram algum ajuste")
    if not mexeu.empty:
        f = df.groupby("ticker")["fator_ajuste"].min().sort_values()
        print("\n  Maiores ajustes acumulados (fator mínimo por ticker):")
        for tk, v in f.head(8).items():
            print(f"    {tk:<8} {v:.6f}  (preço antigo x {v:.4f})")

    if todos_avisos:
        print(f"\n{len(todos_avisos)} avisos:")
        for a in todos_avisos[:20]:
            print(f"  {a}")
        if len(todos_avisos) > 20:
            print(f"  ... e mais {len(todos_avisos) - 20}")

    if args.dry_run:
        print("\n(dry-run - nada gravado)")
        return
    n = gravar(engine, df)
    print(f"\n{n:,} linhas gravadas em prices_ajustados.")
    print("\nPróximo passo: validar a série ajustada contra a da brapi em tickers")
    print("líquidos. Se bater, o método está provado e pode ser aplicado aos")
    print("deslistados que só o COTAHIST tem.")


if __name__ == "__main__":
    main()