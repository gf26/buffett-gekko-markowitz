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

### Rodada 1 — com taxa livre de risco FIXA (14,25%) — ⚠️ DESCARTADA

Estas três foram medidas antes de descobrirmos que a taxa era constante para
os 14 anos. Ficam registradas só para contar quantas variantes foram testadas.

| # | Parâmetros | Sharpe |
|---|---|---|
| 1 | min 2% / max 35% | 0,389 |
| 2 | min 5% / max 20% | 0,518 |
| 3 | min 5% / max 20% + mu-shrinkage 0,5 | 0,531 |

### Rodada 2 — com Selic histórica (2,00% a 15,00%)

Todas com `min-weight 5`, `max-weight 20`, `mu-shrinkage 0.5`. Muda só o que
fazer quando a otimização falha (9 dos 56 rebalanceamentos).

| # | Fallback | Retorno | CAGR | Vol | **Sharpe** | Sortino | Max DD | Custo |
|---|---|---|---|---|---|---|---|---|
| 4 | equal | 412,87% | 12,39% | 22,64% | 0,547 | 1,029 | −34,49% | 6,12% |
| 5 | **minvar** (adotado) | 425,88% | 12,59% | 22,64% | **0,556** | 1,083 | −30,91% | 6,15% |
| 6 | cash | 481,27% | 13,40% | 20,86% | **0,642** | 1,300 | −27,83% | 6,01% |

Referências (não dependem desses parâmetros):

| Referência | Retorno | CAGR | Vol | Sharpe | Sortino | Max DD |
|---|---|---|---|---|---|---|
| Estratégia (peso igual) | 363,87% | 11,58% | 20,96% | 0,553 | 1,028 | −32,35% |
| 1/N do universo | 366,88% | 11,63% | 22,78% | 0,511 | 0,958 | −32,78% |
| Ibovespa | 222,45% | 8,72% | 19,10% | 0,457 | 0,845 | −27,84% |

**Total: 6 variantes testadas.**

---

## Por que `cash` venceu e mesmo assim não foi adotado

O fallback `cash` teve o melhor resultado em todas as métricas — inclusive
drawdown menor que o do próprio Ibovespa. Ainda assim, `minvar` foi adotado.

**São 9 decisões, e 7 concentradas em 2015-2016.** Toda a vantagem vem de
estar em caixa naquela janela: a bolsa caiu forte em 2015 e a Selic estava em
14,25%, então caixa rendia 3,4% ao trimestre enquanto o mercado caía. Não é
uma regra testada 56 vezes — é essencialmente **uma aposta que deu certo**,
repetida em trimestres consecutivos do mesmo evento.

**O gatilho não é previsão, é consequência mecânica.** A otimização falha
quando a média dos últimos 756 pregões cai abaixo da Selic. Com juros a 14,25%
e três anos de bolsa fraca para trás, isso acontecia automaticamente. O
critério não previu a queda — reagiu a ela.

**Os 2 casos de 2024 mostram o outro lado.** Ali a falha foi `infeasible`
(numérica, não econômica) e ir para caixa não tinha justificativa alguma.
Aconteceu mesmo assim.

**Muda a natureza do que está sendo testado.** Com `cash`, as duas estratégias
passaram a divergir em 9 dos 56 períodos — antes selecionavam exatamente os
mesmos ativos em todos. Deixou de ser regra de peso e virou decisão de
alocação entre bolsa e renda fixa, sem que os dois efeitos possam ser
separados.

**Para testar `cash` de verdade** seria preciso um gatilho explícito (não o
subproduto de uma falha numérica) e um período com mais de um ciclo de alta de
juros.

---

## Composição das carteiras (variante 5)

- **86 ativos distintos** passaram pela carteira em 56 rebalanceamentos.
- **Mais frequentes:** VLID3 (43% dos períodos), UNIP6 (41%), VIVT3 e LEVE3
  (30%), UGPA3 (29%), TOTS3 (27%).
- **Giro médio: 28%** dos ativos trocados por rebalanceamento (mín 0%, máx
  100%) — relacionado direto aos ~6,15% de custo acumulado.
- **Markowitz e peso igual selecionaram os MESMOS ativos em 56 de 56**
  rebalanceamentos. A seleção vem do screener; a diferença entre as duas é
  exclusivamente a regra de peso.

Detalhe completo em `backtest_carteiras.csv`.

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