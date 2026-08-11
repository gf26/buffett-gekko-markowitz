"""
Mapeia tickers ainda sem correspondência na CVM.

CONTEXTO
--------
Foram adicionados ~69 tickers ao universo (classes PN, units e empresas que
faltavam). Eles estão em `tickers`, mas não em `ticker_cvm_map` - então o
ingestor da CVM os ignora e eles ficariam sem fundamentos históricos.

ESTRATÉGIA
----------
1. PREFIXO (alta confiança): na B3, os 4 primeiros caracteres do código
   identificam a EMPRESA. BBDC3 e BBDC4 são a mesma empresa; ITUB3/ITUB4,
   TAEE3/TAEE11, ALUP3/ALUP11 idem. Se já existe um ticker mapeado com o
   mesmo prefixo, o novo herda o mesmo cd_cvm - fundamentos são da empresa,
   não da classe de ação.

2. O QUE SOBRAR precisa de tratamento manual: são empresas genuinamente
   novas (Raízen, Banco BMG...), que não têm nenhuma outra classe já
   mapeada. O script gera um CSV com sugestões por nome, no mesmo formato
   do cvm_mapear_tickers.py.

Uso:
    DATABASE_URL="..." python cvm_mapear_novos.py --gerar
    DATABASE_URL="..." python cvm_mapear_novos.py --carregar
"""
import argparse
import difflib
import os
import unicodedata

import pandas as pd
from sqlalchemy import create_engine, text

import cvm_fonte

CSV_PENDENTES = "mapeamento_novos_pendentes.csv"


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


def prefixo(ticker):
    """Os 4 primeiros caracteres do código B3 identificam a empresa."""
    return str(ticker).strip().upper().replace(".SA", "")[:4]


def carregar_estado(engine):
    with engine.connect() as conn:
        tickers = pd.read_sql(text("SELECT ticker, name FROM tickers WHERE active = TRUE"), conn)
        mapeados = pd.read_sql(text("SELECT ticker, cd_cvm, nome_cvm FROM ticker_cvm_map"), conn)
    return tickers, mapeados


def gerar(engine, ano):
    tickers, mapeados = carregar_estado(engine)
    ja_mapeados = set(mapeados["ticker"])
    pendentes = tickers[~tickers["ticker"].isin(ja_mapeados)].copy()

    print(f"{len(tickers)} tickers ativos, {len(ja_mapeados)} já mapeados")
    print(f"{len(pendentes)} pendentes\n")
    if pendentes.empty:
        print("Nada a fazer.")
        return

    # índice de prefixo -> cd_cvm, a partir do que já está mapeado
    mapeados["prefixo"] = mapeados["ticker"].apply(prefixo)
    por_prefixo = (mapeados.drop_duplicates("prefixo")
                            .set_index("prefixo")[["cd_cvm", "nome_cvm"]].to_dict("index"))

    pendentes["prefixo"] = pendentes["ticker"].apply(prefixo)
    pendentes["cd_cvm_sugerido"] = ""
    pendentes["origem"] = ""
    pendentes["nome_cvm"] = ""

    for i, r in pendentes.iterrows():
        hit = por_prefixo.get(r["prefixo"])
        if hit:
            pendentes.at[i, "cd_cvm_sugerido"] = hit["cd_cvm"]
            pendentes.at[i, "nome_cvm"] = hit["nome_cvm"] or ""
            pendentes.at[i, "origem"] = "PREFIXO (mesma empresa, outra classe)"

    por_prefixo_ok = pendentes[pendentes["origem"] != ""]
    sem_prefixo = pendentes[pendentes["origem"] == ""].copy()

    print(f"{len(por_prefixo_ok)} resolvidos por PREFIXO (outra classe de empresa já mapeada)")
    if not por_prefixo_ok.empty:
        for _, r in por_prefixo_ok.head(15).iterrows():
            print(f"    {r['ticker']:<12} -> cd_cvm {r['cd_cvm_sugerido']:<8} ({r['nome_cvm'][:40]})")
        if len(por_prefixo_ok) > 15:
            print(f"    ... e mais {len(por_prefixo_ok) - 15}")

    # para o resto, sugere por nome contra o cadastro da CVM
    if not sem_prefixo.empty:
        print(f"\n{len(sem_prefixo)} sem correspondência por prefixo - buscando por nome na CVM...")
        zf = cvm_fonte.obter_dfp(ano)
        if zf is None:
            print("  não consegui abrir o DFP - as sugestões por nome ficarão vazias")
        else:
            with zf.open(f"dfp_cia_aberta_{ano}.csv") as f:
                emp = pd.read_csv(f, sep=";", encoding="latin-1", dtype={"CD_CVM": str})
            emp["CD_CVM"] = emp["CD_CVM"].str.strip().str.lstrip("0")
            emp = emp.drop_duplicates("CD_CVM")
            emp["nome_norm"] = emp["DENOM_CIA"].apply(normalizar_nome)
            nomes = emp["nome_norm"].tolist()
            mapa = dict(zip(emp["nome_norm"], zip(emp["CD_CVM"], emp["DENOM_CIA"])))

            for i, r in sem_prefixo.iterrows():
                nn = normalizar_nome(r["name"])
                if nn in mapa:
                    cd, denom = mapa[nn]
                    sem_prefixo.at[i, "cd_cvm_sugerido"] = cd
                    sem_prefixo.at[i, "nome_cvm"] = denom
                    sem_prefixo.at[i, "origem"] = "NOME EXATO"
                else:
                    p = difflib.get_close_matches(nn, nomes, n=1, cutoff=0.6)
                    if p:
                        cd, denom = mapa[p[0]]
                        sim = difflib.SequenceMatcher(None, nn, p[0]).ratio()
                        sem_prefixo.at[i, "cd_cvm_sugerido"] = cd
                        sem_prefixo.at[i, "nome_cvm"] = denom
                        sem_prefixo.at[i, "origem"] = f"NOME PARECIDO ({sim:.2f}) - CONFIRME"
                    else:
                        sem_prefixo.at[i, "origem"] = "SEM SUGESTÃO - preencher à mão"

    saida = pd.concat([por_prefixo_ok, sem_prefixo], ignore_index=True)
    saida["confirmar"] = saida["origem"].apply(
        lambda o: "SIM" if o.startswith("PREFIXO") or o == "NOME EXATO" else "")
    cols = ["ticker", "name", "cd_cvm_sugerido", "nome_cvm", "origem", "confirmar"]
    saida[cols].to_csv(CSV_PENDENTES, index=False, encoding="utf-8-sig")

    print(f"\nArquivo gerado: {CSV_PENDENTES}")
    print(saida["origem"].str.split(" ").str[0].value_counts().to_string())
    print("\nLinhas PREFIXO e NOME EXATO já vêm com confirmar=SIM (seguras).")
    print("As demais precisam da sua revisão. Depois: --carregar")


def carregar(engine):
    if not os.path.exists(CSV_PENDENTES):
        raise SystemExit(f"{CSV_PENDENTES} não encontrado - rode --gerar primeiro.")
    df = pd.read_csv(CSV_PENDENTES, dtype=str).fillna("")
    validos = df[(df["confirmar"].str.strip() != "") & (df["cd_cvm_sugerido"].str.strip() != "")]
    if validos.empty:
        print("Nenhuma linha confirmada.")
        return

    with engine.begin() as conn:
        for _, r in validos.iterrows():
            conn.execute(text("""
                INSERT INTO ticker_cvm_map (ticker, cd_cvm, nome_cvm, confianca, vigente)
                VALUES (:t, :c, :n, :conf, TRUE)
                ON CONFLICT (ticker, cd_cvm) DO UPDATE SET
                    nome_cvm = EXCLUDED.nome_cvm, updated_at = now()
            """), {"t": r["ticker"].strip(), "c": r["cd_cvm_sugerido"].strip(),
                   "n": r["nome_cvm"].strip() or None, "conf": r["origem"][:40]})

    print(f"{len(validos)} mapeamentos carregados.")
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(DISTINCT ticker) FROM ticker_cvm_map")).scalar()
        total = conn.execute(text("SELECT COUNT(*) FROM tickers WHERE active = TRUE")).scalar()
    print(f"Cobertura: {n} de {total} tickers ativos mapeados para a CVM.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gerar", action="store_true")
    p.add_argument("--carregar", action="store_true")
    p.add_argument("--ano", type=int, default=2024)
    args = p.parse_args()
    if not (args.gerar or args.carregar):
        raise SystemExit("Escolha --gerar ou --carregar")
    engine = create_engine(os.environ["DATABASE_URL"])
    if args.gerar:
        gerar(engine, args.ano)
    if args.carregar:
        carregar(engine)


if __name__ == "__main__":
    main()
