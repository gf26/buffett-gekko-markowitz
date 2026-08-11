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

FLUXO
-----
1. python cvm_cruzar_com_existentes.py --gerar
     -> gera os dois CSVs. O de "registro antigo" já vem com uma coluna
        'confirmar' preenchida com SIM nas linhas EXATO (seguras) - as outras
        (ALTA/BAIXA) ficam em branco, esperando você revisar e preencher SIM
        (aceita o ticker sugerido) ou escrever o ticker certo à mão, se a
        sugestão estiver errada.
2. Você revisa o CSV, preenche a coluna 'confirmar'.
3. python cvm_cruzar_com_existentes.py --carregar
     -> lê o CSV revisado, e para toda linha com 'confirmar' preenchido,
        grava (ticker, cd_cvm) em ticker_cvm_map. Linhas em branco são
        ignoradas (ainda não decididas).

Uso:
    DATABASE_URL="..." python cvm_cruzar_com_existentes.py --gerar
    DATABASE_URL="..." python cvm_cruzar_com_existentes.py --carregar
"""
import argparse
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


def main_gerar(engine):
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
            linha["confianca"] = "EXATO"
            registro_antigo.append(linha)
            continue

        parecidos = difflib.get_close_matches(nn, nomes_existentes, n=1, cutoff=0.72)
        if parecidos:
            ticker, nome_yahoo = mapa_nome_ticker[parecidos[0]]
            sim = difflib.SequenceMatcher(None, nn, parecidos[0]).ratio()
            linha["ticker_existente"] = ticker
            linha["nome_yahoo"] = nome_yahoo
            linha["similaridade"] = round(sim, 2)
            # mesmo similaridade alta erra: nomes "templados" por região/produto
            # ("Equatorial Maranhão" vs "Equatorial Pará") batem 0.93 e são
            # empresas DIFERENTES - por isso nenhuma faixa de similaridade é
            # tratada como "segura para carregar sem olhar", exceto 1.0 exato
            linha["confianca"] = "EXATO" if sim >= 0.999 else ("ALTA - AINDA ASSIM CONFIRME" if sim >= 0.90 else "BAIXA - REVISE COM CUIDADO")
            registro_antigo.append(linha)
        else:
            lacuna_real.append(linha)

    df_antigo = pd.DataFrame(registro_antigo)
    ordem_confianca = {"EXATO": 0, "ALTA - AINDA ASSIM CONFIRME": 1, "BAIXA - REVISE COM CUIDADO": 2}
    df_antigo["_ordem"] = df_antigo["confianca"].map(ordem_confianca)
    df_antigo = df_antigo.sort_values(["_ordem", "similaridade"], ascending=[True, False]).drop(columns="_ordem")
    # coluna que você preenche na revisão: já vem "SIM" nas EXATO (seguras),
    # em branco nas outras - você decide preenchendo SIM (aceita o
    # ticker_existente sugerido) ou digitando o ticker certo, se souber que a
    # sugestão errou
    df_antigo["confirmar"] = df_antigo["confianca"].apply(lambda c: "SIM" if c == "EXATO" else "")
    df_novo = pd.DataFrame(lacuna_real)

    df_antigo.to_csv(SAIDA_REGISTRO_ANTIGO, index=False, encoding="utf-8-sig")
    df_novo.to_csv(SAIDA_LACUNA_REAL, index=False, encoding="utf-8-sig")

    print(f"\n{len(df_antigo)} parecem ser registro antigo de ticker JÁ existente -> {SAIDA_REGISTRO_ANTIGO}")
    if not df_antigo.empty:
        for nivel, n in df_antigo["confianca"].value_counts().items():
            print(f"  {nivel}: {n}")
    print(f"  As linhas EXATO já vêm com 'confirmar' = SIM (seguras). Para as outras,")
    print(f"  preencha 'confirmar' com SIM (aceita o ticker sugerido) ou digite o ticker")
    print(f"  certo à mão, se a sugestão estiver errada. Depois rode --carregar.")
    print(f"{len(df_novo)} não bateram com nada existente -> {SAIDA_LACUNA_REAL}")
    print(f"  (candidatas reais a ticker novo - este é o grupo bem menor pra revisar com calma)")


def main_carregar(engine):
    if not os.path.exists(SAIDA_REGISTRO_ANTIGO):
        raise SystemExit(f"{SAIDA_REGISTRO_ANTIGO} não encontrado - rode --gerar primeiro.")

    df = pd.read_csv(SAIDA_REGISTRO_ANTIGO, dtype=str).fillna("")
    revisadas = df[df["confirmar"].str.strip() != ""].copy()
    if revisadas.empty:
        print("Nenhuma linha com 'confirmar' preenchido - nada para carregar.")
        return

    # se 'confirmar' tiver "SIM", usa o ticker_existente sugerido; qualquer
    # outro valor não-vazio é tratado como "o usuário escreveu o ticker certo
    # à mão" (substitui a sugestão)
    revisadas["ticker_final"] = revisadas.apply(
        lambda r: r["ticker_existente"] if r["confirmar"].strip().upper() == "SIM" else r["confirmar"].strip(),
        axis=1,
    )

    with engine.begin() as conn:
        for _, r in revisadas.iterrows():
            # vigente=FALSE: estes são registros HISTÓRICOS da empresa (ela
            # mudou de CD_CVM depois). Com a chave composta (ticker, cd_cvm),
            # eles SOMAM ao mapeamento atual em vez de substituí-lo - é o que
            # permite o ingestor juntar o histórico pré e pós-reestruturação.
            conn.execute(text("""
                INSERT INTO ticker_cvm_map (ticker, cd_cvm, nome_cvm, confianca, vigente)
                VALUES (:ticker, :cd_cvm, :nome, :conf, FALSE)
                ON CONFLICT (ticker, cd_cvm) DO UPDATE SET
                    nome_cvm = EXCLUDED.nome_cvm,
                    confianca = EXCLUDED.confianca, updated_at = now()
            """), {
                "ticker": r["ticker_final"], "cd_cvm": r["cd_cvm"],
                "nome": r["nome"], "conf": "registro_antigo_confirmado",
            })

    print(f"Carregadas {len(revisadas)} linhas em ticker_cvm_map (como registros históricos).")

    with engine.connect() as conn:
        multi = conn.execute(text("""
            SELECT ticker, COUNT(*) AS n FROM ticker_cvm_map
            GROUP BY ticker HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC, ticker
        """)).fetchall()
    if multi:
        print(f"\n{len(multi)} tickers agora têm mais de um registro CVM (histórico + vigente):")
        for ticker, n in multi[:15]:
            print(f"    {ticker}: {n} registros")
        if len(multi) > 15:
            print(f"    ... e mais {len(multi) - 15}")
        print("\nRode o cvm_ingestor.py de novo para puxar o histórico desses registros antigos.")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--gerar", action="store_true")
    p.add_argument("--carregar", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not (args.gerar or args.carregar):
        raise SystemExit("Escolha --gerar ou --carregar")
    engine = create_engine(os.environ["DATABASE_URL"])
    if args.gerar:
        main_gerar(engine)
    if args.carregar:
        main_carregar(engine)