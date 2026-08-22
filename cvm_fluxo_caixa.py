"""
Fluxo de caixa e LPA da CVM - campos que faltavam na base.

O QUE ENTRA
-----------
1. FLUXO DE CAIXA (DFC, método indireto, consolidado)
   - Operating Cash Flow  (conta 6.01)
   - Investing Cash Flow  (conta 6.02)
   - Free Cash Flow       (6.01 + 6.02)
   - Capital Expenditure  (subcontas de 6.02, por descrição)

2. LUCRO POR AÇÃO (DRE, contas 3.99.01 e 3.99.02), separado por classe.

POR QUE ISTO SUBSTITUI A BRAPI
------------------------------
A fórmula do FCF da brapi foi identificada por engenharia reversa:
`FCO + fluxo de investimento`. Comparando exercícios equivalentes, nosso
cálculo bate com o dela em 97,4% dos casos, dentro de ±5%.

Como a brapi não fornece data de publicação e usa a mesma fonte (CVM),
calcular aqui dá o mesmo número COM point-in-time real. Não há motivo para
depender dela nesses campos.

⚠️ A investigação inicial parecia mostrar divergência de 12% no FCO. Era
artefato: comparávamos o DFP de 2024 com o exercício de 2025 da brapi. Duas
rodadas de investigação foram gastas nisso antes de conferir o período.

O CAPEX NÃO É NECESSÁRIO PARA O FCF
-----------------------------------
Isso resolve o item 7 do PENDENCIAS por outro caminho. A CVM permite 2.303
descrições distintas nas subcontas de 6.02, e isolar CapEx de forma confiável
é inviável para todas as empresas. Mas o FCF usa o 6.02 INTEIRO, que é conta
universal.

O CapEx isolado entra mesmo assim, com cobertura de ~83% por descrição,
porque os fatores capx_gr1/gr2/gr3 do JKP precisam dele. Onde não for
identificável, fica NULL - nunca estimativa.

LPA POR CLASSE
--------------
As contas 3.99.01.x aparecem em 469 empresas, mas o CÓDIGO NÃO DETERMINA A
CLASSE: 3.99.01.02 é "PN" em 91 empresas, "PNA" em 16 e "ON" em 18. A classe
vem da descrição - mesmo padrão do plano de contas de bancos.

Uso:
    DATABASE_URL="..." python cvm_fluxo_caixa.py --de 2010 --ate 2025 --dry-run
    DATABASE_URL="..." python cvm_fluxo_caixa.py --de 2010 --ate 2025
"""
import argparse
import os
import re
import warnings

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

import cvm_fonte

warnings.filterwarnings("ignore", category=pd.errors.DtypeWarning)

# Grupos SEM captura: com captura, o str.contains do pandas emite UserWarning.
# Validado contra as descrições reais - separa "aquisição de imobilizado"
# (CapEx) de "aquisição de subsidiárias" e "participação acionária" (não são).
# CapEx: duas formas de identificar, mais uma lista de exclusão.
#
# FORMA 1 - verbo + objeto: "Aquisição de imobilizado", "Adições ao
#   imobilizado e intangível", "Aplicação no imobilizado".
#
# FORMA 2 - só o objeto: 21 empresas descrevem simplesmente "Imobilizado",
#   "Intangível", "No Imobilizado", "Em imobilizado". Aceitas apenas quando o
#   VALOR É NEGATIVO - saída de caixa. Com valor positivo seria alienação, não
#   investimento.
#
# EXCLUSÕES: participação societária e aplicação financeira também aparecem em
#   6.02 e NÃO são CapEx. "Investimentos", "Aumento de Capital em
#   Controladas", "Aplicações financeiras" precisam ficar de fora mesmo
#   quando negativos.
RE_CAPEX_VERBO = re.compile(
    r"(?:aquisi|adi[cç]|aplica|compra|investiment|acr[ée]scim|aument)"
    r".{0,40}(?:imobiliz|intang|ativo fixo|ativo n[ãa]o circulante"
    r"|propriedade.{0,15}investiment|ativo biol[óo]gic)"
    r"|(?:imobiliz|intang).{0,30}(?:aquisi|adi[cç]|adquir|acr[ée]scim)",
    re.IGNORECASE)

RE_CAPEX_OBJETO = re.compile(
    r"^\W*(?:n[oa]s?\s+|em\s+|d[oa]s?\s+|\(?\s*(?:acr[ée]scim|aument|redu[cç])\w*\)?"
    r"[\s\)]*(?:e\s+)?(?:redu[cç]\w*\s*)?(?:d[oe]s?\s+|n[oa]s?\s+)?)*"
    r"(?:ativo\s+)?(?:imobilizado|intang[íi]ve[li]s?|ativo\s+fixo|ativo\s+biol[óo]gico)"
    r"\s*(?:e\s+(?:o\s+)?(?:ativo\s+)?(?:imobilizado|intang[íi]ve[li]s?))?\W*$",
    re.IGNORECASE)

# aparecem em 6.02 e NÃO são CapEx
RE_NAO_CAPEX = re.compile(
    r"aplica[cç].{0,20}financ|t[íi]tulo|valores mobili|dividend|juros"
    r"|controlad|coligad|subsidi[áa]ri|participa[cç][ãa]o|SCP|SPE"
    r"|m[úu]tuo|partes relacionad|caixa restrito|capital em|combina[cç][ãa]o de neg"
    r"|aliena[cç]|resgate|venda",
    re.IGNORECASE)


# Descrições confirmadas MANUALMENTE, após revisão do que escapava das
# regras automáticas. A CVM permite 2.303 descrições distintas em 6.02, e a
# partir de certo ponto olhar o que sobrou é mais barato e mais seguro que
# ampliar o regex - cada ampliação captura menos e arrisca mais.
#
# "Ativo Permanente" é a nomenclatura pré-2008 do ativo não circulante, e
# engloba imobilizado e intangível.
DESCRICOES_CAPEX_MANUAIS = {
    "Adição ao Ativo Permanente",
    "Adições ao Ativo Permanente",
    "Aquisições Líquidas de Bens do Ativo Permanente",
    "Aquisições do Ativo Permanente",
    "Ativos Imobilizados",
    "Ativos Intangiveis",
    "Ativos Intangíveis",
}

# Confirmadas como NÃO-CapEx. Falso positivo é pior que cobertura incompleta:
# contamina o indicador sem avisar. "Investimentos" e "Aumento de Capital"
# parecem CapEx e são participação societária.
DESCRICOES_NAO_CAPEX_MANUAIS = {
    "(Aquisição) e baixas em investimentos",
    "Adiantamento a Funcionarios e Fornecedores",
    "Adiantamento para Futuro Aumento de Capital",
    "Adiantamento para futuro aumento de capital",
    "Ajustes Acumulados de Conversão de Moedas",
    "Aplicações em Investimentos",
    "Aportes/aumento de capital",
    "Aquisição de Bens e Investimentos",
    "Aquisição de Investimentos",
    "Aquisição de ativo",
    "Aquisição de bens e investimentos",
    "Ativos tangíveis",
    "Aumento de Capital",
    "Aumento de Investimento",
    "Auste de Conversão de Moedas",
    "Depósitos Judiciais",
    "Incorporação",
    "Integralização de Capital Social",
    "Investimento em Participações",
    "Investimentos",
    "Investimentos em participações",
    "Ivestimentos",
    "Outras aplicações em atividades de investimento",
    "Outros",
    "Outros Investimentos",
    "Recebimento de Empréstimos de Empresas Ligadas",
    "Transferencia de caixa cisão",
}


def eh_capex(descricao, valor):
    """CapEx é sempre saída de caixa. Valor positivo é alienação."""
    ds = str(descricao)
    limpo = ds.strip()
    # listas manuais têm prioridade sobre as regras automáticas
    if limpo in DESCRICOES_NAO_CAPEX_MANUAIS:
        return False
    if limpo in DESCRICOES_CAPEX_MANUAIS:
        return True
    if RE_NAO_CAPEX.search(ds):
        return False
    if RE_CAPEX_VERBO.search(ds):
        return True
    # só o substantivo: exige saída de caixa para não confundir com alienação
    return bool(RE_CAPEX_OBJETO.match(ds)) and valor is not None and valor < 0


CLASSES = ["PNE", "PND", "PNC", "PNB", "PNA", "PN", "ON"]   # do mais específico


def datas_publicacao(ano, prefixo="dfp"):
    """DT_RECEB por empresa, do arquivo-resumo.

    A data de recebimento pela CVM - que é quando o dado ficou público - NÃO
    está nos arquivos de DFC e DRE, só no resumo (`dfp_cia_aberta_AAAA.csv`).
    Sem juntar por CD_CVM, os campos entram sem `published_date` e perdem o
    point-in-time, que é justamente o motivo de calculá-los aqui em vez de
    usar uma API pronta."""
    obter = cvm_fonte.obter_dfp if prefixo == "dfp" else cvm_fonte.obter_itr
    zf = obter(ano)
    if zf is None:
        return {}
    alvo = f"{prefixo}_cia_aberta_{ano}.csv"
    if alvo not in zf.namelist():
        return {}
    with zf.open(alvo) as f:
        d = pd.read_csv(f, sep=";", encoding="latin-1", dtype={"CD_CVM": str})
    if "DT_RECEB" not in d.columns or "CD_CVM" not in d.columns:
        return {}
    d["CD_CVM"] = d["CD_CVM"].astype(str).str.strip().str.lstrip("0")
    if "VERSAO" in d.columns:
        d["VERSAO"] = pd.to_numeric(d["VERSAO"], errors="coerce")
        d = d.sort_values("VERSAO")
    d = d.drop_duplicates("CD_CVM", keep="last")
    return dict(zip(d["CD_CVM"], d["DT_RECEB"]))


def ler(ano, nome, prefixo="dfp"):
    obter = cvm_fonte.obter_dfp if prefixo == "dfp" else cvm_fonte.obter_itr
    zf = obter(ano)
    if zf is None:
        return pd.DataFrame()
    alvo = f"{prefixo}_cia_aberta_{nome}_{ano}.csv"
    if alvo not in zf.namelist():
        return pd.DataFrame()
    with zf.open(alvo) as f:
        d = pd.read_csv(f, sep=";", encoding="latin-1",
                         dtype={"CD_CONTA": str, "CD_CVM": str})
    if "ORDEM_EXERC" in d.columns:
        d = d[d["ORDEM_EXERC"] == "ÚLTIMO"]
    if d.empty:
        return d
    d["CD_CONTA"] = d["CD_CONTA"].str.strip()
    d["CD_CVM"] = d["CD_CVM"].str.strip().str.lstrip("0")
    esc = (d["ESCALA_MOEDA"].eq("MIL").map({True: 1000, False: 1})
           if "ESCALA_MOEDA" in d.columns else 1)
    d["v"] = pd.to_numeric(d["VL_CONTA"], errors="coerce") * esc
    return d


def classe_da_descricao(ds):
    """Classe de ação a partir da descrição. O código não serve: 3.99.01.02 é
    PN em 91 empresas, PNA em 16 e ON em 18."""
    s = str(ds).strip().upper()
    for c in CLASSES:
        if re.search(rf"\b{c}\b", s):
            return c
    return None


def extrair_fluxo(ano, prefixo="dfp"):
    d = ler(ano, "DFC_MI_con", prefixo)
    if d.empty:
        return []
    pubs = datas_publicacao(ano, prefixo)
    linhas = []
    for cd, g in d.groupby("CD_CVM"):
        fco = g[g["CD_CONTA"] == "6.01"]["v"]
        inv = g[g["CD_CONTA"] == "6.02"]["v"]
        if fco.empty:
            continue
        fco = float(fco.iloc[0])
        dt = g["DT_FIM_EXERC"].iloc[0]
        reg = {"cd_cvm": cd, "fiscal_date": dt, "published_date": pubs.get(cd),
                "Operating Cash Flow": fco}
        if not inv.empty:
            iv = float(inv.iloc[0])
            reg["Investing Cash Flow"] = iv
            # FCF AMPLO = FCO + investimento total. O 6.02 é negativo (saída de
            # caixa), então somar equivale a subtrair o investimento.
            #
            # É a definição que a brapi usa - verificado: nosso investimento
            # total bate com o que ela subtrai do FCO em 100% dos casos, dentro
            # de ±5%. Mas o 6.02 inclui aplicações financeiras e compra de
            # participações, o que distorce bancos: o Banco do Brasil aparece
            # com R$ 168 bi de "investimento" que é movimentação de carteira.
            reg["Free Cash Flow"] = fco + iv

        sub = g[g["CD_CONTA"].str.startswith("6.02.")]
        if not sub.empty:
            mask = [eh_capex(ds, v) for ds, v in zip(sub["DS_CONTA"], sub["v"])]
            cap = sub.loc[mask, "v"].sum()
            if cap:
                capex = abs(float(cap))
                reg["Capital Expenditure"] = capex
                # FCF ESTRITO = FCO - CapEx, só investimento produtivo.
                # Convive com o amplo de propósito: são conceitos diferentes e
                # merecem nomes diferentes. Para o fator fcf_me do JKP, o
                # estrito é o adequado; o amplo serve para comparar com fontes
                # de mercado, que usam a definição larga.
                reg["Free Cash Flow (CapEx)"] = fco - capex
        linhas.append(reg)
    return linhas


def extrair_lpa(ano, prefixo="dfp"):
    d = ler(ano, "DRE_con", prefixo)
    if d.empty:
        return []
    alvo = d[d["CD_CONTA"].str.startswith(("3.99.01.", "3.99.02."))].copy()
    if alvo.empty:
        return []
    pubs = datas_publicacao(ano, prefixo)
    alvo["classe"] = alvo["DS_CONTA"].apply(classe_da_descricao)
    alvo = alvo.dropna(subset=["classe", "v"])
    linhas = []
    for (cd, classe), g in alvo.groupby(["CD_CVM", "classe"]):
        dt = g["DT_FIM_EXERC"].iloc[0]
        reg = {"cd_cvm": cd, "fiscal_date": dt, "published_date": pubs.get(cd)}
        b = g[g["CD_CONTA"].str.startswith("3.99.01.")]["v"]
        dl = g[g["CD_CONTA"].str.startswith("3.99.02.")]["v"]
        if not b.empty:
            reg[f"Basic EPS {classe}"] = float(b.iloc[0])
        if not dl.empty:
            reg[f"Diluted EPS {classe}"] = float(dl.iloc[0])
        if len(reg) > 3:
            linhas.append(reg)
    return linhas


def para_financials(regs, cd_para_tickers, period_type):
    out = []
    for r in regs:
        tickers = cd_para_tickers.get(r["cd_cvm"])
        if not tickers:
            continue
        fd = pd.to_datetime(r["fiscal_date"], errors="coerce")
        if pd.isna(fd):
            continue
        pub = pd.to_datetime(r.get("published_date"), errors="coerce")
        pub = None if pd.isna(pub) else pub.date()
        for k, v in r.items():
            if k in ("cd_cvm", "fiscal_date", "published_date") or v is None or pd.isna(v):
                continue
            stmt = "cash_flow" if "Cash Flow" in k or "Expenditure" in k else "income_statement"
            for tk in tickers:
                out.append((tk, stmt, period_type, fd.date(), k, float(v), pub, "cvm"))
    return out


def carregar_tickers(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ticker, cd_cvm FROM ticker_cvm_map")).fetchall()
    m = {}
    for t, cd in rows:
        m.setdefault(str(cd).strip().lstrip("0"), []).append(t)
    return m


def gravar(engine, linhas):
    vistos, unicas = set(), []
    for l in linhas:
        k = (l[0], l[1], l[2], l[3], l[4])
        if k not in vistos:
            vistos.add(k)
            unicas.append(l)
    conn = engine.raw_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM financials WHERE source='cvm'
                  AND line_item IN ('Operating Cash Flow','Investing Cash Flow',
                                     'Free Cash Flow','Free Cash Flow (CapEx)',
                                     'Capital Expenditure')
            """)
            cur.execute("DELETE FROM financials WHERE source='cvm' AND line_item LIKE '%%EPS %%'")
            LOTE = 50_000
            for i in range(0, len(unicas), LOTE):
                execute_values(cur, """
                    INSERT INTO financials
                        (ticker, statement, period_type, fiscal_date, line_item, value,
                         published_date, source)
                    VALUES %s
                    ON CONFLICT (ticker, statement, period_type, fiscal_date, line_item)
                    DO UPDATE SET value = EXCLUDED.value,
                                  published_date = EXCLUDED.published_date
                """, unicas[i:i + LOTE], page_size=5000)
                conn.commit()
    finally:
        conn.close()
    return len(unicas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2025)
    p.add_argument("--trimestral", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    mapa = carregar_tickers(engine)
    print(f"{len(mapa)} empresas mapeadas\n")

    todas = []
    for ano in range(args.de, args.ate + 1):
        for prefixo, pt in ([("dfp", "annual")] +
                             ([("itr", "quarterly")] if args.trimestral else [])):
            fl = extrair_fluxo(ano, prefixo)
            lp = extrair_lpa(ano, prefixo)
            linhas = (para_financials(fl, mapa, pt) + para_financials(lp, mapa, pt))
            if linhas:
                n_fcf = sum(1 for l in linhas if l[4] == "Free Cash Flow")
                n_cap = sum(1 for l in linhas if l[4] == "Capital Expenditure")
                n_eps = sum(1 for l in linhas if "EPS" in l[4])
                print(f"  {ano} {prefixo}: {len(fl):>4} empresas com fluxo, "
                      f"{len(lp):>4} com LPA -> {len(linhas):>5} linhas "
                      f"(FCF {n_fcf}, CapEx {n_cap}, EPS {n_eps})")
                todas.extend(linhas)

    if not todas:
        raise SystemExit("Nada extraído.")

    df = pd.DataFrame(todas, columns=["ticker", "stmt", "pt", "fiscal_date",
                                       "line_item", "v", "pub", "src"])
    print(f"\n{len(df):,} linhas, {df['ticker'].nunique()} tickers")
    cob = df["pub"].notna().mean() * 100
    print(f"  com data de publicação: {cob:.1f}%")
    if cob < 90:
        print("  ⚠️ cobertura baixa - sem published_date estes campos não têm")
        print("     point-in-time, que é a razão de calculá-los aqui.")
    print("\nPor campo:")
    print(df["line_item"].value_counts().head(12).to_string())

    cap = df[df["line_item"] == "Capital Expenditure"]["ticker"].nunique()
    fcf = df[df["line_item"] == "Free Cash Flow"]["ticker"].nunique()
    if fcf:
        print(f"\nCapEx identificável em {cap} de {fcf} tickers com FCF "
              f"({cap/fcf*100:.0f}%) - onde não é, fica NULL")

    if args.dry_run:
        print("\n(dry-run - nada gravado)")
        return
    n = gravar(engine, todas)
    print(f"\n{n:,} linhas gravadas.")


if __name__ == "__main__":
    main()