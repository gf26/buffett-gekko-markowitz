"""
CVM - Mapeamento ticker -> empresa da CVM (CNPJ / código CVM).

POR QUE ISTO EXISTE
-------------------
A CVM identifica empresas por CNPJ; nós identificamos por ticker. Não existe
tabela oficial pública ligando os dois. O casamento automático por nome
resolve ~70%, mas falha em abreviações imprevisíveis da CVM ("CIA SANEAMENTO
BASICO EST SAO PAULO" vs "Companhia de Saneamento Básico do Estado de São
Paulo").

Este script não tenta ser esperto: ele gera um CSV com as MELHORES SUGESTÕES
ranqueadas por similaridade, para você confirmar ou corrigir manualmente. É
trabalho único - CNPJ não muda - e destrava o histórico inteiro (2010-2026)
daquele ticker.

FLUXO
-----
1. python cvm_mapear_tickers.py --gerar
     -> baixa a CVM, casa o que der automaticamente, e escreve
        'mapeamento_tickers_cvm.csv' com sugestões para o resto.

2. Você abre o CSV (Excel, ou o próprio VS Code) e revisa a coluna
   'cd_cvm_escolhido'. Onde a sugestão estiver certa, copie o valor de
   'sugestao_N_cd_cvm'. Onde estiver errada, procure e corrija. Onde a
   empresa realmente não existir na CVM (ex: BDR, fundo, ticker
   deslistado antigo), escreva NAO_EXISTE.

3. python cvm_mapear_tickers.py --carregar
     -> lê o CSV revisado e grava na tabela 'ticker_cvm_map' do banco.

Uso:
    DATABASE_URL="postgresql://..." python cvm_mapear_tickers.py --gerar
    DATABASE_URL="postgresql://..." python cvm_mapear_tickers.py --carregar
"""
import argparse
import difflib
import io
import os
import unicodedata
import zipfile

import pandas as pd
import requests
from sqlalchemy import create_engine, text

CVM_DFP_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip"
CSV_MAPEAMENTO = "mapeamento_tickers_cvm.csv"
N_SUGESTOES = 3


def normalizar_nome(nome):
    if not isinstance(nome, str):
        return ""
    n = unicodedata.normalize("NFKD", nome)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.upper()
    for lixo in [" S.A.", " S/A", " SA ", " S.A", " LTDA", " HOLDING", " PARTICIPACOES",
                  " CIA", " COMPANHIA", ".", ",", "-", "  "]:
        n = n.replace(lixo, " ")
    return " ".join(n.split()).strip()


def baixar_empresas_cvm(ano):
    """Lista de empresas que entregaram DFP no ano (CNPJ, nome, código CVM)."""
    url = CVM_DFP_URL.format(ano=ano)
    print(f"Baixando {url} ...")
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    nome_arq = f"dfp_cia_aberta_{ano}.csv"
    with zf.open(nome_arq) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1",
                          dtype={"CNPJ_CIA": str, "CD_CVM": str})
    if "CD_CVM" in df.columns:
        # normaliza zeros à esquerda - alguns arquivos da CVM trazem o código
        # com padding ("001023"), outros não ("1023"); sem isso, o mesmo
        # código vira "duas empresas diferentes" ao comparar/casar tabelas
        df["CD_CVM"] = df["CD_CVM"].str.strip().str.lstrip("0")
        df.loc[df["CD_CVM"] == "", "CD_CVM"] = "0"
    cols = [c for c in ["CNPJ_CIA", "DENOM_CIA", "CD_CVM"] if c in df.columns]
    empresas = df[cols].drop_duplicates("CD_CVM").reset_index(drop=True)
    empresas["nome_norm"] = empresas["DENOM_CIA"].apply(normalizar_nome)
    print(f"  {len(empresas)} empresas na CVM em {ano}")
    return empresas


def gerar_csv(engine, ano):
    empresas = baixar_empresas_cvm(ano)

    with engine.connect() as conn:
        tickers = pd.read_sql(text("""
            SELECT ticker, name FROM tickers WHERE active = TRUE ORDER BY ticker
        """), conn)
    tickers["nome_norm"] = tickers["name"].apply(normalizar_nome)
    print(f"  {len(tickers)} tickers ativos no banco")

    nomes_cvm = empresas["nome_norm"].tolist()
    mapa_nome_para_cvm = dict(zip(empresas["nome_norm"], zip(empresas["CD_CVM"], empresas["CNPJ_CIA"], empresas["DENOM_CIA"])))

    linhas = []
    exatos = 0
    for _, t in tickers.iterrows():
        nn = t["nome_norm"]
        linha = {
            "ticker": t["ticker"],
            "nome_yahoo": t["name"],
            "cd_cvm_escolhido": "",
            "confianca": "",
        }

        if nn and nn in mapa_nome_para_cvm:
            cd, cnpj, denom = mapa_nome_para_cvm[nn]
            linha["cd_cvm_escolhido"] = cd
            linha["confianca"] = "EXATO"
            linha["sugestao_1_nome"] = denom
            linha["sugestao_1_cd_cvm"] = cd
            exatos += 1
        else:
            # sugestões por similaridade, para revisão manual
            parecidos = difflib.get_close_matches(nn, nomes_cvm, n=N_SUGESTOES, cutoff=0.4)
            linha["confianca"] = "REVISAR" if parecidos else "SEM_SUGESTAO"
            for i, p in enumerate(parecidos, start=1):
                cd, cnpj, denom = mapa_nome_para_cvm[p]
                linha[f"sugestao_{i}_nome"] = denom
                linha[f"sugestao_{i}_cd_cvm"] = cd
                linha[f"sugestao_{i}_similaridade"] = round(
                    difflib.SequenceMatcher(None, nn, p).ratio(), 2)

        linhas.append(linha)

    saida = pd.DataFrame(linhas)
    # ordena para o trabalho manual ficar agrupado: primeiro o que precisa revisão
    ordem = {"REVISAR": 0, "SEM_SUGESTAO": 1, "EXATO": 2}
    saida = saida.sort_values(
        by=["confianca", "ticker"], key=lambda s: s.map(ordem) if s.name == "confianca" else s
    )
    saida.to_csv(CSV_MAPEAMENTO, index=False, encoding="utf-8-sig")

    n_revisar = (saida["confianca"] == "REVISAR").sum()
    n_sem = (saida["confianca"] == "SEM_SUGESTAO").sum()
    print(f"\nArquivo gerado: {CSV_MAPEAMENTO}")
    print(f"  {exatos} casaram automaticamente (confianca=EXATO, já preenchidos)")
    print(f"  {n_revisar} precisam de revisão (confianca=REVISAR, com sugestões)")
    print(f"  {n_sem} sem sugestão nenhuma (confianca=SEM_SUGESTAO)")
    print("\nPróximo passo: abra o CSV, preencha 'cd_cvm_escolhido' nas linhas REVISAR/SEM_SUGESTAO")
    print("(copiando de 'sugestao_N_cd_cvm' quando a sugestão estiver certa, ou escrevendo")
    print("NAO_EXISTE quando a empresa não estiver na CVM), e rode --carregar.")


def carregar_csv(engine):
    if not os.path.exists(CSV_MAPEAMENTO):
        raise SystemExit(f"{CSV_MAPEAMENTO} não encontrado - rode --gerar primeiro.")

    df = pd.read_csv(CSV_MAPEAMENTO, dtype=str).fillna("")
    validos = df[(df["cd_cvm_escolhido"].str.strip() != "")
                  & (df["cd_cvm_escolhido"].str.upper() != "NAO_EXISTE")]

    if validos.empty:
        raise SystemExit("Nenhum mapeamento preenchido no CSV.")

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ticker_cvm_map (
                ticker      TEXT PRIMARY KEY REFERENCES tickers(ticker),
                cd_cvm      TEXT NOT NULL,
                nome_cvm    TEXT,
                confianca   TEXT,
                updated_at  TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ticker_cvm_map_cd ON ticker_cvm_map(cd_cvm)"))

        for _, r in validos.iterrows():
            conn.execute(text("""
                INSERT INTO ticker_cvm_map (ticker, cd_cvm, nome_cvm, confianca)
                VALUES (:ticker, :cd_cvm, :nome, :conf)
                ON CONFLICT (ticker) DO UPDATE SET
                    cd_cvm = EXCLUDED.cd_cvm, nome_cvm = EXCLUDED.nome_cvm,
                    confianca = EXCLUDED.confianca, updated_at = now()
            """), {
                "ticker": r["ticker"].strip(),
                "cd_cvm": r["cd_cvm_escolhido"].strip(),
                "nome": r.get("sugestao_1_nome", "").strip() or None,
                "conf": r.get("confianca", "").strip() or None,
            })

    print(f"Carregados {len(validos)} mapeamentos em 'ticker_cvm_map'.")
    nao_existe = (df["cd_cvm_escolhido"].str.upper() == "NAO_EXISTE").sum()
    vazios = (df["cd_cvm_escolhido"].str.strip() == "").sum()
    if nao_existe:
        print(f"  {nao_existe} marcados como NAO_EXISTE (ignorados, como esperado)")
    if vazios:
        print(f"  {vazios} ainda em branco - esses tickers ficarão sem histórico da CVM")


def main():
    p = argparse.ArgumentParser(description="Mapeia tickers para empresas da CVM.")
    p.add_argument("--gerar", action="store_true", help="Gera o CSV com sugestões para revisão manual.")
    p.add_argument("--carregar", action="store_true", help="Carrega o CSV revisado no banco.")
    p.add_argument("--ano", type=int, default=2024, help="Ano da CVM usado como referência de nomes.")
    args = p.parse_args()

    if not (args.gerar or args.carregar):
        p.error("escolha --gerar ou --carregar")

    engine = create_engine(os.environ["DATABASE_URL"])
    if args.gerar:
        gerar_csv(engine, args.ano)
    if args.carregar:
        carregar_csv(engine)


if __name__ == "__main__":
    main()