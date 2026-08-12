"""
COMPARAÇÃO (não grava nada): localizar contas por DESCRIÇÃO vs. por CÓDIGO.

PROBLEMA QUE MOTIVOU
--------------------
O de-para atual fixa códigos de conta. Mas o dump revelou que existem pelo
menos TRÊS estruturas para Patrimônio Líquido:

    plano            PL       Lucro Líquido    exemplo
    padrão           2.03     3.11             empresas em geral
    banco variante A 2.07     3.11             Bradesco, Banestes
    banco variante B 2.08     3.09             BTG, Pine

A detecção atual procura 2.07 com "Patrim" - então BTG e Pine NÃO são
reconhecidos como bancos, caem no plano padrão, e a conta 2.03 (que neles é
"Passivos Financeiros ao Custo Amortizado") vira Patrimônio Líquido:

    BTG:  PL real R$ 65,9 bi  ->  gravado R$ 413 bi   (6,3x)
    Pine: PL real R$ 1,07 bi  ->  gravado R$ 23,1 bi  (21,6x)

ALTERNATIVA TESTADA AQUI
------------------------
Localizar a conta pela DESCRIÇÃO, não pelo código: procurar a linha cujo
DS_CONTA diz "Patrimônio Líquido Consolidado", seja qual for o código. Depois
buscar as filhas dela (controladores / não controladores).

Funciona para as três estruturas e para variantes ainda desconhecidas - desde
que a CVM mantenha a descrição padronizada, o que os dumps sugerem que ocorre.

ESTE SCRIPT NÃO ALTERA O BANCO. Só compara e relata, para decidir se vale
trocar a estratégia.

Uso:
    DATABASE_URL="..." python cvm_comparar_estrategia.py --ano 2024
"""
import argparse
import os
import re

import pandas as pd
from sqlalchemy import create_engine, text

import cvm_fonte

ESCALA = {"UNIDADE": 1, "MIL": 1_000, "MILHAR": 1_000, "MILHÃO": 1_000_000, "MILHAO": 1_000_000}

# "Patrimônio Líquido Consolidado" - o pai, em qualquer código
RE_PL_TOTAL = re.compile(r"patrim[oô]nio\s+l[ií]quido\s+consolidado", re.IGNORECASE)
# filhas: distinguir controlador de NÃO controlador (a segunda contém a primeira)
RE_NAO_CONTROLADOR = re.compile(r"n[aã]o\s+controlador", re.IGNORECASE)
RE_CONTROLADOR = re.compile(r"controlador", re.IGNORECASE)
# lucro consolidado do período, excluindo "lucro por ação"
RE_LUCRO = re.compile(r"(lucro|preju[ií]zo).*consolidad", re.IGNORECASE)
RE_POR_ACAO = re.compile(r"por\s+a[cç][aã]o", re.IGNORECASE)


def ler(zf, ano, sigla):
    nome = f"dfp_cia_aberta_{sigla}_con_{ano}.csv"
    if nome not in zf.namelist():
        return pd.DataFrame()
    with zf.open(nome) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", decimal=".",
                          dtype={"CD_CONTA": str, "CD_CVM": str})
    df["CD_CVM"] = df["CD_CVM"].str.strip().str.lstrip("0")
    df["CD_CONTA"] = df["CD_CONTA"].str.strip()
    df["DS_CONTA"] = df["DS_CONTA"].astype(str)
    if "ORDEM_EXERC" in df.columns:
        df = df[df["ORDEM_EXERC"] == "ÚLTIMO"].copy()
    if "VERSAO" in df.columns and not df.empty:
        df["VERSAO"] = pd.to_numeric(df["VERSAO"], errors="coerce")
        df = df.sort_values("VERSAO").drop_duplicates(["CD_CVM", "DT_FIM_EXERC", "CD_CONTA"], keep="last")
    if "ESCALA_MOEDA" in df.columns:
        df["VL_CONTA"] = pd.to_numeric(df["VL_CONTA"], errors="coerce") * \
                          df["ESCALA_MOEDA"].str.upper().map(ESCALA).fillna(1)
    return df


def localizar_pl(g):
    """Patrimônio Líquido dos controladores, achando as contas por descrição.

    Devolve (valor, código_da_raiz, como_foi_obtido)."""
    pais = g[g["DS_CONTA"].str.contains(RE_PL_TOTAL, na=False)]
    if pais.empty:
        return None, None, "raiz não encontrada"
    # a raiz é a de menor profundidade (2.03 antes de 2.03.01)
    pai = pais.loc[pais["CD_CONTA"].str.count(r"\.").idxmin()]
    raiz, total = pai["CD_CONTA"], pai["VL_CONTA"]

    filhas = g[g["CD_CONTA"].str.startswith(raiz + ".")]
    # 1ª opção: conta que já dá o valor dos controladores, pronto
    contr = filhas[filhas["DS_CONTA"].str.contains(RE_CONTROLADOR, na=False)
                   & ~filhas["DS_CONTA"].str.contains(RE_NAO_CONTROLADOR, na=False)]
    if not contr.empty:
        return float(contr.iloc[0]["VL_CONTA"]), raiz, f"direto ({contr.iloc[0]['CD_CONTA']})"

    # 2ª opção: total menos a participação dos não controladores
    nao = filhas[filhas["DS_CONTA"].str.contains(RE_NAO_CONTROLADOR, na=False)]
    minor = float(nao.iloc[0]["VL_CONTA"]) if not nao.empty else 0.0
    origem = f"{raiz} - {nao.iloc[0]['CD_CONTA']}" if not nao.empty else f"{raiz} (sem minoritários)"
    return float(total) - minor, raiz, origem


def localizar_lucro(g):
    cand = g[g["DS_CONTA"].str.contains(RE_LUCRO, na=False)
             & ~g["DS_CONTA"].str.contains(RE_POR_ACAO, na=False)]
    if cand.empty:
        return None, None, "não encontrado"
    pai = cand.loc[cand["CD_CONTA"].str.count(r"\.").idxmin()]
    raiz, total = pai["CD_CONTA"], pai["VL_CONTA"]
    filhas = g[g["CD_CONTA"].str.startswith(raiz + ".")]
    contr = filhas[filhas["DS_CONTA"].str.contains(RE_CONTROLADOR, na=False)
                   & ~filhas["DS_CONTA"].str.contains(RE_NAO_CONTROLADOR, na=False)]
    if not contr.empty:
        return float(contr.iloc[0]["VL_CONTA"]), raiz, f"direto ({contr.iloc[0]['CD_CONTA']})"
    nao = filhas[filhas["DS_CONTA"].str.contains(RE_NAO_CONTROLADOR, na=False)]
    minor = float(nao.iloc[0]["VL_CONTA"]) if not nao.empty else 0.0
    return float(total) - minor, raiz, f"{raiz} - minoritários"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ano", type=int, default=2024)
    p.add_argument("--tolerancia", type=float, default=0.5)
    args = p.parse_args()

    zf = cvm_fonte.obter_dfp(args.ano)
    if zf is None:
        raise SystemExit(f"DFP {args.ano} não disponível.")

    bpp = ler(zf, args.ano, "BPP")
    dre = ler(zf, args.ano, "DRE")
    if bpp.empty:
        raise SystemExit("BPP vazio.")

    novo = []
    for cd, g in bpp.groupby("CD_CVM"):
        pl, raiz, origem = localizar_pl(g)
        if pl is None:
            continue
        gd = dre[dre["CD_CVM"] == cd]
        lucro, raiz_l, origem_l = localizar_lucro(gd) if not gd.empty else (None, None, "sem DRE")
        novo.append({"cd_cvm": cd, "pl_novo": pl, "raiz_pl": raiz, "origem_pl": origem,
                      "lucro_novo": lucro, "raiz_lucro": raiz_l})
    novo = pd.DataFrame(novo)
    print(f"{len(novo)} empresas com PL localizado por descrição\n")

    print("Distribuição das estruturas encontradas (código onde fica o PL):")
    print(novo["raiz_pl"].value_counts().to_string())
    if "raiz_lucro" in novo.columns:
        print("\nOnde fica o Lucro Líquido:")
        print(novo["raiz_lucro"].value_counts(dropna=False).to_string())

    # o que está no banco hoje
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        atual = pd.read_sql(text("""
            SELECT DISTINCT ON (m.cd_cvm, f.line_item)
                   m.cd_cvm, f.ticker, f.line_item, f.value
            FROM financials f
            JOIN ticker_cvm_map m ON m.ticker = f.ticker
            WHERE f.source = 'cvm' AND f.period_type = 'annual'
              AND EXTRACT(YEAR FROM f.fiscal_date) = :ano
              AND f.line_item IN ('Stockholders Equity', 'Net Income')
            ORDER BY m.cd_cvm, f.line_item, f.ticker
        """), conn, params={"ano": args.ano})

    if atual.empty:
        raise SystemExit("Nada da CVM no banco para comparar.")

    atual["cd_cvm"] = atual["cd_cvm"].astype(str).str.strip().str.lstrip("0")
    pl_atual = atual[atual["line_item"] == "Stockholders Equity"][["cd_cvm", "ticker", "value"]]
    pl_atual = pl_atual.rename(columns={"value": "pl_atual"})

    comp = novo.merge(pl_atual, on="cd_cvm", how="inner")
    comp["dif_pct"] = ((comp["pl_novo"] - comp["pl_atual"]).abs()
                        / comp["pl_atual"].abs().replace(0, pd.NA) * 100)
    comp["dif_pct"] = pd.to_numeric(comp["dif_pct"], errors="coerce")
    comp["igual"] = comp["dif_pct"].fillna(999) <= args.tolerancia

    print("\n" + "=" * 78)
    print(f"PATRIMÔNIO LÍQUIDO: banco atual x localização por descrição")
    print("=" * 78)
    print(f"{len(comp)} empresas comparadas")
    print(f"  {comp['igual'].sum()} IGUAIS (a troca não mudaria nada)")
    print(f"  {(~comp['igual']).sum()} DIFERENTES")

    dif = comp[~comp["igual"]].copy()
    if not dif.empty:
        dif["razao"] = dif["pl_atual"] / dif["pl_novo"]
        print(f"\nEmpresas que mudariam (ordenadas pelo tamanho da mudança):")
        d = dif.nlargest(min(25, len(dif)), "dif_pct")[
            ["ticker", "cd_cvm", "raiz_pl", "pl_atual", "pl_novo", "razao", "origem_pl"]]
        for _, r in d.iterrows():
            print(f"  {r['ticker']:<11} PL={r['raiz_pl']:<6} "
                  f"atual={r['pl_atual']/1e9:>10,.2f}bi  novo={r['pl_novo']/1e9:>10,.2f}bi  "
                  f"({r['razao']:>6.1f}x)  [{r['origem_pl']}]")

    print("\n" + "=" * 78)
    print("VERIFICAÇÃO DE SEGURANÇA - os já validados manualmente devem ficar IGUAIS")
    print("=" * 78)
    for tk in ["BBDC3.SA", "BBAS3.SA", "BEES3.SA", "B3SA3.SA", "WEGE3.SA", "PETR3.SA"]:
        r = comp[comp["ticker"] == tk]
        if r.empty:
            print(f"  {tk:<11} (não está na comparação)")
            continue
        r = r.iloc[0]
        status = "IGUAL" if r["igual"] else f"MUDARIA ({r['pl_atual']/1e9:.2f} -> {r['pl_novo']/1e9:.2f} bi)"
        print(f"  {tk:<11} {status}")

    print("\n" + "=" * 78)
    n_dif = int((~comp["igual"]).sum())
    if n_dif == 0:
        print("VEREDITO: nenhuma mudança. A estratégia por descrição reproduz o atual")
        print("exatamente - seria uma troca segura, mas sem ganho visível neste ano.")
    else:
        print(f"VEREDITO: {n_dif} empresas mudariam.")
        print("Confira acima se as mudanças CORRIGEM erros conhecidos (BTG, Pine) e se")
        print("os validados manualmente (Bradesco, BB, Banestes) permanecem iguais.")
        print("Se sim, a troca é segura e corrige o bug das variantes de plano.")
    print("=" * 78)


if __name__ == "__main__":
    main()
