"""
Separa, na lista de empresas "ausentes", dois grupos bem diferentes:

  (A) LACUNA REAL: a empresa não está em lugar nenhum da sua base -
      candidata de verdade a ticker novo.
  (B) REGISTRO ANTIGO DE EMPRESA JÁ RASTREADA: a empresa já tem ticker em
      `tickers`, só que sob um CD_CVM diferente (ela teve mais de um registro
      na CVM ao longo da vida - fusão, reestruturação societária, etc). Não
      precisa de ticker novo - precisa só ADICIONAR esse CD_CVM antigo como
      mais uma linha em `ticker_cvm_map`, apontando para o MESMO ticker que
      já existe. Isso importa porque, sem isso, o histórico anterior à
      reestruturação fica de fora mesmo a empresa já estando rastreada.

Como cvm_mapear_tickers.py só fez o casamento uma vez (nome atual do Yahoo
contra nome mais recente da CVM), muitos desses registros antigos nunca
tiveram chance de casar. Este script faz uma segunda passada, agora contra
TODOS os nomes de tickers já existentes (não só os ainda sem mapeamento).

Uso:
    DATABASE_URL="..." python cvm_cruzar_com_existentes.py
"""
import difflib
import os
import unicodedata

import pandas as pd
from sqlalchemy import create_engine, text

ENTRADA = "empresas_cvm_prioridade_revisao.csv"
SAIDA_REGISTRO_ANTIGO = "empresas_cvm_registro_antigo_de_ticker_existente.csv"
SAIDA_LACUNA_REAL = "empresas_cvm_lacuna_real.csv"


def normalizar_nome(nome):
    if not isinstance(nome, str):
        return ""
    n = unicodedata.normalize("NFKD", nome)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.upper()
    for lixo in [" S.A.", " S/A", " SA ", " S.A", " LTDA", " HOLDING", " PARTICIPACOES",
                  " EM RECUPERACAO JUDICIAL", " EM LIQUIDACAO", " EM LIQUIDACAO EXTRAJUDICIAL",
                  " FALIDA", " CIA", " COMPANHIA", ".", ",", "-", "  "]:
        n = n.replace(lixo, " ")
    return " ".join(n.split()).strip()


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    ausentes = pd.read_csv(ENTRADA, dtype=str)
    print(f"Lidas {len(ausentes)} empresas de {ENTRADA}")

    with engine.connect() as conn:
        tickers = pd.read_sql(text("SELECT ticker, name FROM tickers WHERE name IS NOT NULL"), conn)
    tickers["nome_norm"] = tickers["name"].apply(normalizar_nome)
    print(f"{len(tickers)} tickers já existentes na base para comparar.")

    nomes_existentes = tickers["nome_norm"].tolist()
    mapa_nome_ticker = dict(zip(tickers["nome_norm"], zip(tickers["ticker"], tickers["name"])))

    ausentes["nome_norm"] = ausentes["nome"].apply(normalizar_nome)

    registro_antigo, lacuna_real = [], []
    for _, r in ausentes.iterrows():
        nn = r["nome_norm"]
        linha = r.drop("nome_norm").to_dict()

        if nn in mapa_nome_ticker:
            ticker, nome_yahoo = mapa_nome_ticker[nn]
            linha["ticker_existente"] = ticker
            linha["nome_yahoo"] = nome_yahoo
            linha["similaridade"] = 1.0
            registro_antigo.append(linha)
            continue

        parecidos = difflib.get_close_matches(nn, nomes_existentes, n=1, cutoff=0.72)
        if parecidos:
            ticker, nome_yahoo = mapa_nome_ticker[parecidos[0]]
            sim = difflib.SequenceMatcher(None, nn, parecidos[0]).ratio()
            linha["ticker_existente"] = ticker
            linha["nome_yahoo"] = nome_yahoo
            linha["similaridade"] = round(sim, 2)
            registro_antigo.append(linha)
        else:
            lacuna_real.append(linha)

    df_antigo = pd.DataFrame(registro_antigo).sort_values("similaridade", ascending=False)
    df_novo = pd.DataFrame(lacuna_real)

    df_antigo.to_csv(SAIDA_REGISTRO_ANTIGO, index=False, encoding="utf-8-sig")
    df_novo.to_csv(SAIDA_LACUNA_REAL, index=False, encoding="utf-8-sig")

    print(f"\n{len(df_antigo)} parecem ser registro antigo de ticker JÁ existente -> {SAIDA_REGISTRO_ANTIGO}")
    print(f"  (revisão rápida: confirma o ticker sugerido e roda o carregamento em lote)")
    print(f"{len(df_novo)} não bateram com nada existente -> {SAIDA_LACUNA_REAL}")
    print(f"  (candidatas reais a ticker novo - este é o grupo bem menor pra revisar com calma)")


if __name__ == "__main__":
    main()