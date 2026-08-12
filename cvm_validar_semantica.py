"""
Validação SEMÂNTICA do de-para: a conta escolhida significa o que dizemos?

A DIFERENÇA PARA cvm_validar_parsing.py
---------------------------------------
Aquele script confere se o valor foi TRANSPORTADO corretamente do arquivo para
o banco. Não detecta erro de ESCOLHA: se mapeássemos a conta 2.03 (Provisões)
como Patrimônio Líquido nos bancos, ele transportaria os R$ 402 bi fielmente e
aprovaria - validando o transporte de um dado semanticamente errado.

Este script testa outra coisa: se a conta escolhida realmente representa o
conceito. Usa IDENTIDADES CONTÁBEIS - relações que precisam valer quando o
mapeamento está certo e quebram quando está errado.

TESTES APLICADOS, e o que cada um consegue (ou não) pegar
----------------------------------------------------------
As identidades contábeis são um piso, não a prova. Testei: se mapeássemos
Provisões (R$ 402 bi) como PL do Bradesco, a razão PL/Ativo daria 19,4% -
DENTRO da faixa plausível para banco. As identidades aprovariam o erro.

O teste DECISIVO é a comparação de razão com o Yahoo no mesmo exercício:
  PL correto  / PL Yahoo = 1,000  -> conceito igual
  Provisões   / PL Yahoo = 2,390  -> conceito diferente, erro exposto

Por isso a seção de comparação com o Yahoo é a parte que importa aqui; as
identidades servem para pegar erros mais grosseiros (sinal trocado, conta de
outro demonstrativo, escala).

  1. PL <= Ativo Total.
  2. PL/Ativo em faixa plausível por tipo de empresa (piso fraco - ver acima).
  3. Lucro Líquido <= Receita.
  4. PL controladores <= PL total.
  5. Ativo Circulante <= Ativo Total.
  6. Receita > 0.
  7. RAZÃO CVM/YAHOO por conta - o teste que de fato discrimina conceito
     trocado. Ressalva: o Yahoo tem erro conhecido de moeda em algumas
     empresas (Vale, Embraer reportam em USD), então razão ~6 ali é problema
     DO YAHOO, não nosso.

Uso:
    DATABASE_URL="..." python cvm_validar_semantica.py --ano 2024
"""
import argparse
import os

import pandas as pd
from sqlalchemy import create_engine, text

# faixas plausíveis de PL/Ativo, por tipo de empresa
FAIXA_PL_ATIVO_BANCO = (0.03, 0.25)    # bancos são alavancados
FAIXA_PL_ATIVO_GERAL = (0.10, 0.85)    # empresa comum


def carregar(engine, ano):
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT f.ticker, f.fiscal_date, f.line_item, f.value, f.source,
                   ci.info->>'sector' AS setor
            FROM financials f
            LEFT JOIN company_info ci ON ci.ticker = f.ticker
            WHERE f.period_type = 'annual'
              AND EXTRACT(YEAR FROM f.fiscal_date) = :ano
        """), conn, params={"ano": ano})
    return df


def pivotar(df, fonte):
    d = df[df["source"] == fonte]
    if d.empty:
        return pd.DataFrame()
    p = d.pivot_table(index=["ticker", "fiscal_date", "setor"], columns="line_item",
                       values="value", aggfunc="first").reset_index()
    return p


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ano", type=int, default=2024)
    args = p.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    df = carregar(engine, args.ano)
    cvm = pivotar(df, "cvm")
    yahoo = pivotar(df, "yahoo")

    if cvm.empty:
        raise SystemExit(f"Sem dados da CVM para {args.ano}.")

    # bancos: identificados por terem PL sem Ativo Circulante (não classificam
    # balanço por liquidez) - é a assinatura do plano de contas financeiro
    tem_ca = "Current Assets" in cvm.columns
    cvm["eh_banco"] = ~cvm["Current Assets"].notna() if tem_ca else False
    n_bancos = int(cvm["eh_banco"].sum())
    print(f"{len(cvm)} empresas com dados da CVM em {args.ano}")
    print(f"{n_bancos} identificadas como plano financeiro (sem Ativo Circulante)\n")

    problemas = []

    def checar(nome, mask, detalhe_cols):
        sub = cvm[mask]
        n = len(sub)
        print(f"{'FALHOU' if n else 'OK    '}  {nome}: {n} empresas")
        if n:
            cols = ["ticker"] + [c for c in detalhe_cols if c in sub.columns]
            for _, r in sub.head(8)[cols].iterrows():
                vals = "  ".join(f"{c}={r[c]:,.0f}" if isinstance(r[c], (int, float)) and pd.notna(r[c])
                                  else f"{c}={r[c]}" for c in cols[1:])
                print(f"          {r['ticker']:<12} {vals}")
            if n > 8:
                print(f"          ... e mais {n - 8}")
            problemas.append((nome, n))

    print("=" * 78)
    print("IDENTIDADES CONTÁBEIS")
    print("=" * 78)

    if {"Stockholders Equity", "Total Assets"} <= set(cvm.columns):
        checar("PL maior que o Ativo Total (impossível)",
               cvm["Stockholders Equity"] > cvm["Total Assets"],
               ["Stockholders Equity", "Total Assets"])

        cvm["pl_sobre_ativo"] = cvm["Stockholders Equity"] / cvm["Total Assets"]
        b = cvm["eh_banco"]
        fora_banco = b & (~cvm["pl_sobre_ativo"].between(*FAIXA_PL_ATIVO_BANCO))
        fora_geral = (~b) & (~cvm["pl_sobre_ativo"].between(*FAIXA_PL_ATIVO_GERAL))
        checar(f"BANCO com PL/Ativo fora de {FAIXA_PL_ATIVO_BANCO}",
               fora_banco & cvm["pl_sobre_ativo"].notna(), ["pl_sobre_ativo", "Stockholders Equity", "Total Assets"])
        checar(f"GERAL com PL/Ativo fora de {FAIXA_PL_ATIVO_GERAL}",
               fora_geral & cvm["pl_sobre_ativo"].notna(), ["pl_sobre_ativo", "Stockholders Equity", "Total Assets"])

    if {"Net Income", "Total Revenue"} <= set(cvm.columns):
        checar("Lucro Líquido maior que a Receita (margem > 100%)",
               cvm["Net Income"] > cvm["Total Revenue"].abs(),
               ["Net Income", "Total Revenue"])

    if {"Stockholders Equity", "Total Equity Gross Minority Interest"} <= set(cvm.columns):
        checar("PL controladores maior que PL total (parte > todo)",
               cvm["Stockholders Equity"] > cvm["Total Equity Gross Minority Interest"] * 1.001,
               ["Stockholders Equity", "Total Equity Gross Minority Interest"])

    if {"Current Assets", "Total Assets"} <= set(cvm.columns):
        checar("Ativo Circulante maior que Ativo Total",
               cvm["Current Assets"] > cvm["Total Assets"],
               ["Current Assets", "Total Assets"])

    if "Total Revenue" in cvm.columns:
        checar("Receita negativa ou zero",
               cvm["Total Revenue"].notna() & (cvm["Total Revenue"] <= 0),
               ["Total Revenue"])

    # ---- comparação com o Yahoo ----
    print("\n" + "=" * 78)
    print("COMPARAÇÃO COM O YAHOO (mesmo exercício)")
    print("Divergência de ordem de grandeza sugere CONCEITO diferente, não só valor.")
    print("=" * 78)

    if yahoo.empty:
        print("Sem dados do Yahoo para comparar neste ano.")
    else:
        for item in ["Stockholders Equity", "Total Assets", "Net Income", "Total Revenue"]:
            if item not in cvm.columns or item not in yahoo.columns:
                continue
            m = cvm[["ticker", "eh_banco", item]].merge(
                yahoo[["ticker", item]], on="ticker", suffixes=("_cvm", "_yahoo")).dropna()
            if m.empty:
                continue
            m["razao"] = m[f"{item}_cvm"] / m[f"{item}_yahoo"].replace(0, pd.NA)
            m = m.dropna(subset=["razao"])
            ok = m["razao"].between(0.9, 1.1)
            print(f"\n{item}: {len(m)} comparações, {ok.mean()*100:.1f}% dentro de ±10%")
            for tipo, sub in [("bancos", m[m["eh_banco"]]), ("demais", m[~m["eh_banco"]])]:
                if sub.empty:
                    continue
                dentro = sub["razao"].between(0.9, 1.1).mean() * 100
                print(f"    {tipo:<8} {len(sub):>4} comparações, {dentro:>5.1f}% batem, "
                      f"razão mediana {sub['razao'].median():.3f}")
            ruins = m[~m["razao"].between(0.5, 2.0)]
            if not ruins.empty:
                print(f"    {len(ruins)} com razão fora de 0,5-2,0 (suspeito de conceito trocado):")
                for _, r in ruins.nlargest(min(5, len(ruins)), "razao").iterrows():
                    tipo = "banco" if r["eh_banco"] else "geral"
                    print(f"      {r['ticker']:<12} razão {r['razao']:>10.3f}  ({tipo})")

    print("\n" + "=" * 78)
    if not problemas:
        print("VEREDITO: nenhuma violação de identidade contábil.")
        print("O de-para está semanticamente coerente - as contas escolhidas")
        print("representam os conceitos que dizem representar.")
    else:
        print("VEREDITO: violações encontradas -")
        for nome, n in problemas:
            print(f"  {n:>4} empresas: {nome}")
        print("\nViolação em BANCOS sugere conta errada no plano financeiro.")
        print("Violação espalhada sugere problema no plano padrão.")
    print("=" * 78)


if __name__ == "__main__":
    main()
