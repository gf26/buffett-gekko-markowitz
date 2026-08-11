"""
CVM - verificação de empresas ausentes na lista de tickers.

O QUE ISTO FAZ
--------------
Baixa o cadastro completo de companhias abertas da CVM (inclui registros
ATIVOS e CANCELADOS - ou seja, também empresas deslistadas) e compara com a
tabela `ticker_cvm_map` que você já populou, apontando quais empresas
registradas na CVM não têm ticker nenhum mapeado.

O QUE ISTO NÃO FAZ (limitação real, não é possível contornar com este dado)
-----------------------------------------------------------------------
O cadastro da CVM identifica EMPRESAS (CNPJ), não CLASSES DE AÇÃO. Ele não
diz "esta empresa tinha ação ON". Uma empresa pode ter tido só PN, só units,
ou ter sido uma emissora de dívida sem ação nenhuma na bolsa. Este script
entrega uma lista de NOMES para você (ou eu, pesquisando um por um) checar -
não uma lista pronta de "tickers ON esquecidos".

Esse é, ainda assim, o filtro certo: reduz de "todas as ~2500 empresas que
algum dia já pediram registro na CVM" para "as ~200-300 que não estão na sua
lista de 272 e realmente merecem uma checada".

Uso:
    DATABASE_URL="..." python cvm_verificar_universo.py
"""
import io
import os

import pandas as pd
import requests
from sqlalchemy import create_engine, text

import cvm_fonte

URL_CADASTRO = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"
CSV_SAIDA = "empresas_cvm_nao_mapeadas.csv"

# Nomes candidatos para cada campo - a estrutura exata deste arquivo não pôde
# ser confirmada de antemão (bloqueio de robots.txt até no dicionário de
# dados), então tentamos várias grafias plausíveis e avisamos qual foi usada.
CANDIDATOS_COLUNA = {
    "cnpj": ["CNPJ_CIA", "CNPJ"],
    "cd_cvm": ["CD_CVM", "CODIGO_CVM"],
    "nome": ["DENOM_SOCIAL", "DENOM_COMERC", "DENOM_CIA", "NOME_EMPRESARIAL"],
    "situacao": ["SIT", "SITUACAO", "SIT_REG"],
    "dt_registro": ["DT_REG", "DATA_REGISTRO"],
    "dt_cancelamento": ["DT_CANCEL", "DATA_CANCELAMENTO"],
    "categoria": ["CATEG_REG", "CATEGORIA_REGISTRO"],
}


def achar_coluna(df, chave):
    for nome in CANDIDATOS_COLUNA[chave]:
        if nome in df.columns:
            return nome
    return None


def baixar_cadastro():
    # usa o cache local (dados_cvm/) quando disponível - o servidor da CVM
    # às vezes bloqueia conexões vindas do Codespace por faixa de IP
    conteudo = cvm_fonte.obter_cadastro()
    # encoding e separador seguem o mesmo padrão dos outros arquivos da CVM,
    # mas com fallback caso este arquivo específico use vírgula
    try:
        df = pd.read_csv(io.BytesIO(conteudo), sep=";", encoding="latin-1", dtype=str)
        if df.shape[1] <= 1:
            raise ValueError("só 1 coluna - separador provavelmente errado")
    except Exception:
        df = pd.read_csv(io.BytesIO(conteudo), sep=",", encoding="latin-1", dtype=str)
    print(f"  {len(df)} linhas, {len(df.columns)} colunas")
    return df


def main():
    engine = create_engine(os.environ["DATABASE_URL"])

    cad = baixar_cadastro()
    print("\nColunas encontradas no arquivo:", list(cad.columns))

    col_cnpj = achar_coluna(cad, "cnpj")
    col_cvm = achar_coluna(cad, "cd_cvm")
    col_nome = achar_coluna(cad, "nome")
    col_sit = achar_coluna(cad, "situacao")
    col_dtreg = achar_coluna(cad, "dt_registro")
    col_dtcancel = achar_coluna(cad, "dt_cancelamento")
    col_cat = achar_coluna(cad, "categoria")

    print("\nMapeamento de colunas usado:")
    for chave, col in [("CNPJ", col_cnpj), ("Código CVM", col_cvm), ("Nome", col_nome),
                        ("Situação", col_sit), ("Data registro", col_dtreg),
                        ("Data cancelamento", col_dtcancel), ("Categoria", col_cat)]:
        print(f"  {chave:<20} -> {col or '(NÃO ENCONTRADA - ver lista de colunas acima e ajustar CANDIDATOS_COLUNA)'}")

    if not col_cvm or not col_nome:
        raise SystemExit(
            "\nNão encontrei as colunas essenciais (código CVM e nome). "
            "Confira a lista de colunas impressa acima e me avise os nomes reais "
            "para eu ajustar o script."
        )

    # normaliza zeros à esquerda - a experiência com o cvm_ingestor.py mostrou
    # que arquivos diferentes da CVM usam padding diferente para o mesmo código
    cad[col_cvm] = cad[col_cvm].astype(str).str.strip().str.lstrip("0")
    cad.loc[cad[col_cvm] == "", col_cvm] = "0"

    cad = cad.drop_duplicates(subset=[col_cvm])

    with engine.connect() as conn:
        mapeados = pd.read_sql(text("SELECT DISTINCT cd_cvm FROM ticker_cvm_map"), conn)
    ja_mapeados = set(mapeados["cd_cvm"].astype(str).str.strip())
    print(f"\n{len(ja_mapeados)} códigos CVM já mapeados para algum ticker.")

    ausentes = cad[~cad[col_cvm].astype(str).str.strip().isin(ja_mapeados)].copy()
    print(f"{len(ausentes)} empresas da CVM SEM ticker mapeado (de {len(cad)} no cadastro).")

    cols_saida = [c for c in [col_cvm, col_nome, col_sit, col_cat, col_dtreg, col_dtcancel] if c]
    ausentes = ausentes[cols_saida].rename(columns={
        col_cvm: "cd_cvm", col_nome: "nome",
        **({col_sit: "situacao"} if col_sit else {}),
        **({col_cat: "categoria"} if col_cat else {}),
        **({col_dtreg: "data_registro"} if col_dtreg else {}),
        **({col_dtcancel: "data_cancelamento"} if col_dtcancel else {}),
    })

    # ordena para o trabalho manual: canceladas primeiro (são as candidatas mais
    # prováveis a "empresa que existia e você não pegou porque já tinha saído")
    if "situacao" in ausentes.columns:
        ausentes["_cancelada"] = ausentes["situacao"].str.upper().str.contains("CANCEL", na=False)
        ausentes = ausentes.sort_values(by=["_cancelada", "nome"], ascending=[False, True]).drop(columns="_cancelada")

    ausentes.to_csv(CSV_SAIDA, index=False, encoding="utf-8-sig")
    print(f"\nArquivo gerado: {CSV_SAIDA}")

    if "situacao" in ausentes.columns:
        print("\nResumo por situação:")
        print(ausentes["situacao"].value_counts().to_string())
        canceladas = ausentes[ausentes["situacao"].str.upper().str.contains("CANCEL", na=False)]
        print(f"\n{len(canceladas)} têm registro CANCELADO - são as candidatas mais prováveis a")
        print("empresas deslistadas que faltam na sua lista de 272. Comece a revisão por elas.")

    print("\nPróximo passo: abra o CSV e revise os nomes. Pra cada uma que você reconhecer como")
    print("tendo tido ação ON na B3, adicione um ticker novo em `tickers` e um mapeamento em")
    print("`ticker_cvm_map` (mesmo fluxo do cvm_mapear_tickers.py) - depois rode o cvm_ingestor.py")
    print("de novo só para essa empresa.")


if __name__ == "__main__":
    main()