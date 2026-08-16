# Resultados de backtest — registro de variantes testadas

Anotar cada variante testada é defesa contra o problema de múltiplos testes:
testar muitas configurações e ficar com a melhor infla o resultado de um jeito
que não se reproduz fora da amostra. Este arquivo existe para que o número de
tentativas seja conhecido quando for hora de interpretar o resultado.

**Configuração comum a todas:** rebalanceamento trimestral, 10 ativos,
Piotroski ≥ 7, custo 0,15% por operação, piso de liquidez R$ 50 mil/dia,
lookback 756 dias, 56 períodos (2012-06 a 2026-03).

---

## Variantes testadas

| # | Parâmetros alterados | Retorno | CAGR | Vol | **Sharpe** | Sortino | Max DD | Custo |
|---|---|---|---|---|---|---|---|---|
| 1 | padrão (min 2% / max 35%) | 298,13% | 10,37% | 26,65% | **0,389** | 0,665 | −43,25% | 6,85% |
| 2 | min 5% / max 20% | 393,28% | 12,07% | 23,31% | **0,518** | 0,961 | −34,52% | 6,08% |
| 3 | min 5% / max 20% + mu-shrinkage 0,5 | 398,94% | 12,17% | 22,90% | **0,531** | 0,982 | −34,07% | 6,13% |

Linhas de referência (não dependem desses parâmetros, então são idênticas nas
três variantes):

| Referência | Retorno | CAGR | Vol | Sharpe | Sortino | Max DD |
|---|---|---|---|---|---|---|
| Estratégia (peso igual) | 375,05% | 11,77% | 21,91% | 0,537 | 1,030 | −32,12% |
| 1/N do universo | 366,88% | 11,63% | 22,78% | 0,511 | 0,958 | −32,78% |
| Ibovespa | 222,45% | 8,72% | 19,10% | 0,457 | 0,845 | −27,84% |

---

## Leitura

**Restringir os pesos foi o que mais mudou.** Sharpe de 0,389 para 0,518 só
com a faixa de 2-35% indo para 5-20%, e o drawdown melhorou 9 pontos. Confirma
a hipótese de que o problema do Markowitz aqui era concentração — soluções de
canto empurrando peso para os extremos com base em estimativas ruidosas.

**O mu-shrinkage acrescentou pouco** (0,518 → 0,531). O ganho marginal sugere
que a restrição de peso já capturava a maior parte do efeito.

**Mesmo na melhor variante, o Markowitz não supera o peso igual** em Sharpe
(0,531 contra 0,537) nem em Sortino (0,982 contra 1,030), e ainda paga 6,13%
de custo contra 5,27%. Em retorno bruto ele ganha (398,94% contra 375,05%),
mas ajustado a risco e a custo, não.

**Nenhuma variante bate o 1/N de forma convincente.** Peso igual 0,537 contra
1/N 0,511 é uma diferença pequena — e o 1/N não paga os 5,27% de custo.

---

## ⚠️ Bug identificado: taxa livre de risco constante

O `--risk-free-rate` tem padrão **14,25%** e é aplicado a **todos os 56
períodos**. A Selic no período variou de ~2% (2020) a 14,25% (2025).

Isso distorce a otimização de máximo Sharpe: em 2020, com Selic real de 2%,
exigir retorno esperado acima de 14,25% faz quase nenhum ativo qualificar. É
o que produz as falhas registradas no log:

    "at least one of the assets must have an expected return exceeding
     the risk-free rate"

Na variante 3 foram 7 rebalanceamentos que caíram para peso igual por falha de
otimização — ou seja, a curva "Markowitz" é na verdade híbrida, e o efeito não
é uniforme entre variantes (variante 1 teve 4 falhas, variante 3 teve 7).

**Correção necessária:** usar a Selic vigente em cada data de rebalanceamento.
O `portfolio_optimizer.py` já tem `fetch_selic_rate_anual()` consultando a API
do Banco Central (série 432) — falta o backtest usar a série histórica em vez
de um valor fixo.

---

## Sobre a comparação com o Ibovespa

O 1/N supera o Ibovespa por margem grande (366,88% contra 222,45%). Parte
disso é efeito real e documentado; parte é artefato. Ver análise na seção
seguinte antes de tirar conclusões.

### Por que 1/N supera um índice ponderado por valor

**Efeitos reais:**

1. **Prêmio de tamanho.** O Ibovespa é ponderado por valor de mercado, então é
   dominado por Vale, Petrobras, Itaú e Bradesco. O 1/N dá o mesmo peso a uma
   empresa de R$ 500 milhões e a uma de R$ 400 bilhões — ou seja, é uma aposta
   estrutural em small caps. É um dos 13 temas do JKP (Size), e historicamente
   remunerado.

2. **Prêmio de rebalanceamento.** Voltar a pesos iguais a cada trimestre vende
   mecanicamente o que subiu e compra o que caiu. Em ativos voláteis e não
   perfeitamente correlacionados, isso adiciona retorno sem exigir previsão.

**O artefato, e ele é grande:**

3. **Viés de sobrevivência.** O universo de 350 tickers contém apenas empresas
   que existem HOJE. As que quebraram entre 2012 e 2026 não estão lá. O
   Ibovespa, sendo índice real, carrega o desempenho efetivo das empresas que
   depois falharam.

   Ou seja: o 1/N aqui **não é uma alternativa que você poderia ter seguido**.
   É uma carteira de sobreviventes montada com informação do futuro.

### Consequência para a interpretação

**Estratégia contra 1/N: hoje é comparável, mas o teste real ainda não
aconteceu.** Ambas partem do mesmo universo enviesado, então o viés não
favorece uma sobre a outra. Mas ele **suprime a vantagem que o screener
deveria ter** — e isso é decisivo:

- O 1/N, num universo completo, carregaria TODA empresa que quebrou.
- A estratégia carregaria só as que passavam em Piotroski ≥ 7 e no piso de
  liquidez.

Se o filtro de qualidade funciona, ele deveria evitar parte das falências. Num
universo onde ninguém quebrou, essa vantagem simplesmente não tem como
aparecer. O empate atual (0,537 contra 0,511) é medido num cenário que remove
justamente o que o screener existe para fazer.

**Portanto: a conclusão de que "a complexidade não se paga" é PREMATURA.** Ela
só pode ser testada depois do COTAHIST, com deslistadas no universo dos dois
lados.

**Qualquer uma delas contra o Ibovespa: comparação INJUSTA.** As duas estão
infladas em relação ao índice, que carrega o desempenho real das empresas que
faliram. A vantagem aparente sobre o Ibovespa não é evidência de que a
estratégia funciona.

### Sobre a correção do viés

O COTAHIST elimina o viés dos DOIS lados ao mesmo tempo — não de um só. O
efeito esperado é assimétrico: o 1/N deve piorar mais que a estratégia, porque
absorve integralmente as falências que o filtro de qualidade evitaria. Se isso
não acontecer, aí sim há evidência de que o screener não agrega.
