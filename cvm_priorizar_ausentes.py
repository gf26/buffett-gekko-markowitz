"""
Prioriza a lista de empresas ausentes (gerada por cvm_verificar_universo.py)
para revisão manual - sem baixar nada de novo, só reorganiza o CSV que já
existe.

FILTROS APLICADOS
------------------
1. CATEG_REG contendo 'A' (Categoria A): só essas empresas podem emitir ação
   ao público. Categoria B só emite outros valores mobiliários (debênture,
   por exemplo) - nunca teve e nunca vai ter ação ON. Isso corta uma fatia
   grande da lista sem risco de descartar candidata real.
   (Se você quiser conferir a lista completa sem esse filtro, tudo que foi
   excluído também é salvo, em separado, para auditoria.)

2. Ordena: ATIVO primeiro (lacunas do screener HOJE - prioridade alta),
   CANCELADA depois (relevante para o viés de sobrevivência do backtest -
   prioridade mais baixa, mas ainda vale revisar depois).

Uso:
    python cvm_priorizar_ausentes.py
"""
import pandas as pd

ENTRADA = "empresas_cvm_nao_mapeadas.csv"
SAIDA_PRIORIZADA = "empresas_cvm_prioridade_revisao.csv"
SAIDA_EXCLUIDAS = "empresas_cvm_categoria_b_excluidas.csv"


def main():
    df = pd.read_csv(ENTRADA, dtype=str)
    print(f"Lidas {len(df)} empresas de {ENTRADA}")

    if "categoria" not in df.columns:
        print("Aviso: coluna 'categoria' não veio no CSV - não dá para filtrar por Categoria A/B.")
        priorizadas, excluidas = df, df.iloc[0:0]
    else:
        # a palavra "Categoria" já contém a letra "A" - por isso comparamos a
        # ÚLTIMA letra do valor (que é o que de fato distingue A de B), não
        # se a string "contém" A em qualquer posição
        ultima_letra = df["categoria"].astype(str).str.strip().str[-1].str.upper()
        eh_categoria_a = ultima_letra == "A"
        priorizadas = df[eh_categoria_a].copy()
        excluidas = df[~eh_categoria_a].copy()
        print(f"  {len(priorizadas)} em Categoria A (podem ter tido ação) - vão para revisão")
        print(f"  {len(excluidas)} em outra categoria (não podem ter ação ao público) - descartadas")

    if "situacao" in priorizadas.columns:
        priorizadas["_ativa"] = priorizadas["situacao"].str.upper().eq("ATIVO")
        priorizadas = priorizadas.sort_values(by=["_ativa", "nome"], ascending=[False, True]).drop(columns="_ativa")
        n_ativas = (priorizadas["situacao"].str.upper() == "ATIVO").sum()
        print(f"\n  {n_ativas} ATIVAS (revisar primeiro - são lacunas do screener hoje)")
        print(f"  {len(priorizadas) - n_ativas} CANCELADAS/outras (revisar depois - relevantes pro backtest)")

    priorizadas.to_csv(SAIDA_PRIORIZADA, index=False, encoding="utf-8-sig")
    excluidas.to_csv(SAIDA_EXCLUIDAS, index=False, encoding="utf-8-sig")

    print(f"\nGerado: {SAIDA_PRIORIZADA} ({len(priorizadas)} linhas) - comece por aqui")
    print(f"Gerado: {SAIDA_EXCLUIDAS} ({len(excluidas)} linhas) - só para auditoria, não precisa abrir")


if __name__ == "__main__":
    main()