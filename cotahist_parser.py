"""
Parser do COTAHIST - cotações históricas da B3.

POR QUE ESTA FONTE
------------------
O Yahoo é hoje a única fonte de preços, e tem três problemas que o COTAHIST
resolve:

1. REMOVE TICKERS DESLISTADOS. APER3, HGTX3, BIDI11 simplesmente somem. É a
   causa direta do viés de sobrevivência - o backtest mede uma carteira de
   sobreviventes, e o 1/N do universo aparece com desempenho inflado.
2. AJUSTE DE PROVENTOS QUEBRADO. A AZEV3 aparece cotada a R$ 0,0001 por 740
   pregões e depois salta para R$ 302,82.
3. É FONTE SECUNDÁRIA. O COTAHIST vem da própria bolsa, é imutável, e contém
   todos os papéis negociados em cada pregão desde 1986.

FORMATO
-------
Arquivo posicional de largura fixa, 245 caracteres por linha, campos definidos
pelo layout oficial da B3. Valores monetários vêm em centavos (dividir por
100) e sem ajuste por proventos ou desdobramentos.

O QUE ESTE MÓDULO NÃO FAZ
-------------------------
Não ajusta preços. O COTAHIST traz o preço NEGOCIADO na data; para série
comparável ao longo do tempo é preciso aplicar proventos e splits - trabalho
do módulo de ajuste, que usa as tabelas `dividends` e `splits`.

Uso:
    python cotahist_parser.py --arquivo dados_cvm/COTAHIST_A2015.ZIP --resumo
    python cotahist_parser.py --arquivo dados_cvm/COTAHIST_A2015.ZIP --tickers PETR4 VALE3
"""
import argparse
import io
import os
import zipfile

import pandas as pd

# Layout oficial do COTAHIST. Posições em base 1 conforme o documento da B3;
# convertidas para base 0 no parser.
CAMPOS = {
    "TIPREG":   (1, 2),      # 00=header, 01=cotação, 99=trailer
    "DATA":     (3, 10),     # AAAAMMDD
    "CODBDI":   (11, 12),    # 02=lote padrão, 96=fracionário, 12=fundos...
    "CODNEG":   (13, 24),    # ticker
    "TPMERC":   (25, 27),    # 010=vista, 020=fracionário, 070/080=opções...
    "NOMRES":   (28, 39),    # nome resumido da empresa (12 caracteres)
    "ESPECI":   (40, 49),    # ON, PN, UNT, CI...
    "PRAZOT":   (50, 52),
    "MODREF":   (53, 56),    # moeda de referência
    "PREABE":   (57, 69),    # abertura (centavos)
    "PREMAX":   (70, 82),    # máxima
    "PREMIN":   (83, 95),    # mínima
    "PREMED":   (96, 108),   # média
    "PREULT":   (109, 121),  # ÚLTIMO (fechamento)
    "PREOFC":   (122, 134),
    "PREOFV":   (135, 147),
    "TOTNEG":   (148, 152),  # número de negócios
    "QUATOT":   (153, 170),  # quantidade de títulos
    "VOLTOT":   (171, 188),  # volume financeiro (centavos)
    "PREEXE":   (189, 201),
    "INDOPC":   (202, 202),
    "DATVEN":   (203, 210),
    "FATCOT":   (211, 217),  # fator de cotação (1 ou 1000)
    "PTOEXE":   (218, 230),
    "CODISI":   (231, 242),  # ISIN
    "DISMES":   (243, 245),
}

# Só interessa o mercado à vista em lote padrão. O fracionário duplica o mesmo
# papel com liquidez artificialmente baixa; opções, termo e futuro não são
# ações.
TPMERC_VISTA = "010"
CODBDI_LOTE_PADRAO = "02"

# Campos monetários vêm em centavos
CAMPOS_MOEDA = ["PREABE", "PREMAX", "PREMIN", "PREMED", "PREULT", "PREOFC", "PREOFV"]


def _fatiar(linha, campo):
    ini, fim = CAMPOS[campo]
    return linha[ini - 1:fim]


def ler_cotahist(caminho, apenas_vista=True, tickers=None):
    """Lê um arquivo COTAHIST (zip ou txt) e devolve um DataFrame.

    apenas_vista: restringe ao mercado à vista em lote padrão.
    tickers: lista para filtrar (sem sufixo .SA), ou None para todos."""
    if not os.path.exists(caminho):
        raise SystemExit(f"Arquivo não encontrado: {caminho}")

    if caminho.lower().endswith(".zip"):
        zf = zipfile.ZipFile(caminho)
        interno = [n for n in zf.namelist() if n.upper().endswith(".TXT")]
        if not interno:
            raise SystemExit(f"Nenhum .TXT dentro de {caminho}")
        fh = io.TextIOWrapper(zf.open(interno[0]), encoding="latin-1")
    else:
        fh = open(caminho, encoding="latin-1")

    alvo = {t.upper().replace(".SA", "") for t in tickers} if tickers else None
    linhas = []
    with fh:
        for linha in fh:
            if not linha.startswith("01"):   # 00=header, 99=trailer
                continue
            if apenas_vista:
                if _fatiar(linha, "TPMERC").strip() != TPMERC_VISTA:
                    continue
                if _fatiar(linha, "CODBDI").strip() != CODBDI_LOTE_PADRAO:
                    continue
            codneg = _fatiar(linha, "CODNEG").strip()
            if alvo and codneg not in alvo:
                continue
            linhas.append({
                "ticker": codneg,
                "date": _fatiar(linha, "DATA").strip(),
                "nome_pregao": _fatiar(linha, "NOMRES").strip(),
                "especie": _fatiar(linha, "ESPECI").strip(),
                "isin": _fatiar(linha, "CODISI").strip(),
                "open": _fatiar(linha, "PREABE"),
                "high": _fatiar(linha, "PREMAX"),
                "low": _fatiar(linha, "PREMIN"),
                "close": _fatiar(linha, "PREULT"),
                "media": _fatiar(linha, "PREMED"),
                "negocios": _fatiar(linha, "TOTNEG"),
                "quantidade": _fatiar(linha, "QUATOT"),
                "volume": _fatiar(linha, "VOLTOT"),
                "fator_cotacao": _fatiar(linha, "FATCOT"),
            })

    if not linhas:
        return pd.DataFrame()

    df = pd.DataFrame(linhas)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")

    for c in ["open", "high", "low", "close", "media"]:
        df[c] = pd.to_numeric(df[c], errors="coerce") / 100
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") / 100
    for c in ["negocios", "quantidade", "fator_cotacao"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # FATOR DE COTAÇÃO: alguns papéis são cotados por LOTE DE MIL, não por
    # unidade. Nesses casos o campo vem 1000 e o preço precisa ser dividido -
    # senão a ação aparece mil vezes mais cara do que é.
    lote_mil = df["fator_cotacao"] == 1000
    if lote_mil.any():
        for c in ["open", "high", "low", "close", "media"]:
            df.loc[lote_mil, c] = df.loc[lote_mil, c] / 1000

    return df.dropna(subset=["date", "close"]).sort_values(["ticker", "date"])


def resumo(df):
    print(f"\n{len(df):,} registros")
    print(f"{df['ticker'].nunique():,} tickers distintos")
    print(f"período: {df['date'].min().date()} a {df['date'].max().date()}")
    print(f"pregões: {df['date'].nunique():,}")

    print("\nPor espécie:")
    print(df["especie"].value_counts().head(10).to_string())

    print("\n10 tickers com mais volume financeiro:")
    top = (df.groupby(["ticker", "nome_pregao"])["volume"].sum()
             .sort_values(ascending=False).head(10))
    for (tk, nome), v in top.items():
        print(f"  {tk:<8} {nome:<14} R$ {v/1e9:>8,.1f} bi")

    fc = df["fator_cotacao"].value_counts()
    if len(fc) > 1:
        print(f"\nFatores de cotação encontrados: {dict(fc)}")
        print("  (1000 = papel cotado por lote de mil; preço já foi dividido)")


def main():
    p = argparse.ArgumentParser(description="Lê arquivos COTAHIST da B3.")
    p.add_argument("--arquivo", required=True, help="Caminho do COTAHIST (.ZIP ou .TXT)")
    p.add_argument("--tickers", nargs="*", help="Filtrar por tickers específicos")
    p.add_argument("--resumo", action="store_true", help="Mostra estatísticas do arquivo")
    p.add_argument("--todos-mercados", action="store_true",
                    help="Não restringe ao mercado à vista em lote padrão")
    p.add_argument("--salvar", help="Grava o resultado em CSV")
    args = p.parse_args()

    print(f"Lendo {args.arquivo}...")
    df = ler_cotahist(args.arquivo, apenas_vista=not args.todos_mercados,
                       tickers=args.tickers)
    if df.empty:
        raise SystemExit("Nenhum registro encontrado com os filtros aplicados.")

    if args.resumo:
        resumo(df)
    else:
        print(df.head(20).to_string(index=False))

    if args.salvar:
        df.to_csv(args.salvar, index=False, encoding="utf-8-sig")
        print(f"\nSalvo em {args.salvar}")


if __name__ == "__main__":
    main()
