# Pendências conhecidas

Coisas decididas mas ainda não feitas, e limitações que precisam de tratamento.
Cada item diz **por que existe** e **o que fazer** — comentário no código se
perde, isto aqui não.

---

## 1. Valor de mercado por classe de ação — 🔜 PRÓXIMA AÇÃO

**Estado:** o dado necessário está no banco (item 2 concluído). Falta
reescrever o cálculo no `scoring.py`. É o caminho crítico: até isso ser feito,
o backtest continua rodando com valor de mercado incorreto.

**O problema:** hoje temos só o TOTAL de ações da empresa, sem saber quantas
são ON e quantas são PN. Isso causa dois erros:

- **Units** (BPAC11, SANB11, TAEE11...): o preço negociado é de um pacote
  (ex: 1 ON + 2 PN), mas a contagem é de ações individuais. Multiplicar
  direto infla o valor de mercado em 200-300%.
- **Qualquer empresa com duas classes**: multiplicamos o total de ações por
  UM preço só, ignorando que ON e PN negociam com preços diferentes. O
  spread entre ITUB3 e ITUB4 é relevante.

**A gambiarra atual** (`scoring.py`, função `_fatores_unit`): infere o tamanho
do pacote pela razão entre o preço da unit e o de alguma classe individual da
mesma empresa. Funciona, mas é aproximado, e falha quando nenhuma classe
individual negocia — nesses casos imprime um aviso e o valor segue inflado.

**A solução definitiva:** o arquivo `fre_cia_aberta_capital_social_AAAA.csv`
traz `Quantidade_Acoes_Ordinarias` e `Quantidade_Acoes_Preferenciais`
separadas. Com isso:

```
market_cap = (qtd_ON × preço_da_ON) + (qtd_PN × preço_da_PN)
```

Sem inferência, sem aproximação, e corrige também as empresas sem unit.

**O que fazer quando o FRE entrar:**
1. Ingerir `Quantidade_Acoes_Ordinarias` e `Quantidade_Acoes_Preferenciais`
   como line items separados
2. Reescrever o cálculo de market_cap em `build_metrics_snapshot`
3. **Apagar a função `_fatores_unit` inteira**

---

## 2. Ações em circulação antes de 2020 — ✅ CONCLUÍDO

**Estado:** carregado. 13.674 linhas, 339 tickers, exercícios de 2009 a 2025,
em `source = 'cvm_fre'`, com contagem separada de ordinárias e preferenciais.

**Como ficou:** o `capital_social` do FRE não traz "a posição do exercício" —
traz o HISTÓRICO DE APROVAÇÕES DE CAPITAL, uma linha por aprovação, com sua
`Data_Autorizacao_Aprovacao`. O ingestor acumula as aprovações de todos os
arquivos e, para cada exercício, aplica a que estava vigente em 31/12. Isso
torna o dado point-in-time e independente de qual arquivo foi lido.

Descoberto porque a CEG oscilava entre 259 milhões e 51,9 bilhões de ações
conforme o exercício: são os números pré e pós-grupamento de 2016, e a versão
anterior escolhia entre eles de forma arbitrária.

**Por quê:** a CVM só publica `composicao_capital` a partir de 2020. Sem
número de ações não há valor de mercado, e sem valor de mercado não existem
P/L, P/VPA, Earnings Yield nem FCF Yield — todo o lado de valuation do
screener.

**Fontes descartadas depois de testar:**
- ITR: não tem o arquivo em nenhum ano anterior a 2020
- FCA (`valor_mobiliario`): lista QUAIS valores mobiliários existem, não
  QUANTOS. Não tem coluna de quantidade
- brapi: fundamentos só no plano pago

**Fonte confirmada:** FRE (`fre_cia_aberta_capital_social_AAAA.csv`), testado
com 2013. Tem a quantidade, cobre o período, e — crucialmente — a **unidade é
consistente** (Petrobras 13,04 bi, Vale 5,37 bi, Renner 126 mi, todos
conferindo com a realidade de 2013).

**O que fazer:**
1. Baixar `fre_cia_aberta_AAAA.zip` de 2010 a 2025 (16 arquivos)
2. Confirmar que o layout se mantém nos extremos (2010 e 2024)
3. Escrever o ingestor, usando **`Tipo_Capital = 'Capital Integralizado'`**
   — o arquivo traz quatro tipos, e usar o errado distorce muito: a Vale tem
   5,37 bi de integralizado contra 10,8 bi de *autorizado* (que é um teto
   estatutário, não ações existentes)
4. Deduplicar por versão: o arquivo repete linhas da mesma empresa

---

## 3. Escala inconsistente em `composicao_capital` — ✅ RESOLVIDO (fonte descartada)

**Confirmado com dado:** comparando `composicao_capital` com o FRE no exercício
de 2024 — 201 empresas (62%) em unidades, 96 (30%) em milhares, 26 (8%) em
outra situação. Não há coluna que indique a unidade usada, então não há
correção confiável possível.

**Ação tomada:** as três contagens de ações vindas de `source = 'cvm'` foram
apagadas. O FRE é a fonte única.

**Efeito colateral aceito:** 12 tickers ficaram sem contagem (BAZA3, BDLL3/4,
BMGB4, HETA4, HOOT4, ITUB3/4, LWSA3, SHOW3, TIMS3, VBBR3). Itaú, TIM e Vibra
faltarem no FRE é implausível — vale investigar se usam outro `Tipo_Capital`
ou se o CNPJ não está casando. **NÃO** recuperar via `composicao_capital`:
seria trocar "sem dado" por "dado errado em 38% dos casos".

---

## 3-bis. Registro histórico: o problema original de escala

**Estado:** dados carregados estão errados para parte das empresas.

**O problema:** o arquivo mistura unidades sem indicar qual usou. No mesmo
arquivo de 2024: Banco do Brasil com 5.730.834.040 (unidades) e Lojas Renner
com 1.059.550 (milhares — a Renner tem ~1,06 bilhão de ações).

**Consequência:** valor de mercado 1000x subestimado para a maioria das
empresas em 2020-2025, contaminando todo o valuation e o backtest.

**Solução prevista:** o FRE substitui essa fonte por completo, com unidade
consistente. Resolve junto com o item 2.

**Se o FRE não vier:** usar o LPA (conta 3.99.01) como árbitro de escala —
onde `Lucro Líquido ÷ LPA` for ~1000x o valor do arquivo, multiplicar por
1000.

---

## 3b. Buraco no exercício de 2022 — ✅ RESOLVIDO

Resolvido de graça pela mudança para lógica point-in-time (item 2): como a
posição vem da aprovação vigente e não da data de referência do documento,
2022 passou a ter 331 tickers. A explicação do problema fica abaixo por ser
útil se algo parecido reaparecer.

### O que era

A CVM mudou a convenção da `Data_Referencia` do FRE entre os arquivos de 2022
e 2023:

- Até o arquivo 2022: referência em 1º de janeiro (= posição do fim do ano
  ANTERIOR). O arquivo de 2022 descreve o fechamento de 2021.
- A partir do arquivo 2023: referência em 31 de dezembro do próprio ano. O
  arquivo de 2024 descreve o fechamento de 2024 (verificado: Renner com
  1.059.549.692, idêntico à `composicao_capital` de 2024).

Resultado: **ninguém descreve o fechamento de 2022** — o exercício fica com
uma dezena de registros em vez de ~700.

**Solução:** preencher 2022 com a `composicao_capital` (que cobre 2020-2025),
usando o FRE de 2021 e 2023 como referência cruzada para conferir a escala
daquele ano.

---

## 4. Seguradoras sem receita (2 empresas)

BBSE3 e CXSE3 aparecem com `Total Revenue = 0`. Usam um terceiro plano de
contas (nem o padrão, nem o de bancos), com a receita em conta diferente da
`3.01`.

**O que fazer:** mesmo método que resolveu os bancos —
`python cvm_descobrir_plano_bancos.py --dump <cd_cvm> --ano 2024` e montar o
de-para com base na evidência.

---

## 5. Dois tickers sem mapeamento CVM

**WDCN3** (WDC Networks) e **OBTC3** (OranjeBTC) — os códigos foram
encontrados manualmente (25895 e 27910), mas a OBTC3 só obteve registro de
companhia aberta em setembro/2025, então não há DFP anterior.

---

## 6. Viés de sobrevivência (limitação estrutural)

Empresas deslistadas antes do início da coleta não estão na base. O Yahoo
remove tickers que saem da bolsa em vez de arquivá-los, e a brapi tem a mesma
limitação no plano gratuito.

**Consequência:** todo backtest é otimista, de forma não mensurável.

**Caminho possível:** os arquivos anuais da CVM são imutáveis — o de 2015
contém as empresas que existiam em 2015. Dá para reconstruir o universo
histórico a partir deles. Mas faltariam os **preços** dessas empresas, que é
o problema mais difícil.

---

## 7. CapEx com cobertura parcial

Não tem código fixo na CVM — fica em subcontas de `6.02` com nome variável.
Localizado por descrição, cobertura incompleta. Afeta o FCF Yield.

---

## 8. Convenção de Sortino inconsistente

`compute_market_metrics.py` e `portfolio_optimizer.py` usam denominadores
diferentes para o semi-desvio. Ambas as convenções são legítimas, mas os
números não são comparáveis entre si. Padronizar.

---

## 9. Limite de espaço do Supabase — DECISÃO PENDENTE

**Estado atual:** 424 MB de 500 MB usados (85%). Restam ~76 MB.

**O que já está no limite:**

| Objeto | Tamanho |
|---|---|
| `prices_daily` | 154 MB (1,17 mi de linhas) |
| `financials` | ~112 MB + 46 MB de índice |
| `prices_daily_pkey` | 44 MB |
| `idx_prices_daily_date` | 15 MB — **usado 35 vezes, contra 1,2 mi do pkey** |

**Cargas previstas:**

- **FRE** (~12.900 linhas): ~2,5 MB. Cabe sem problema.
- **COTAHIST da B3** (cotações históricas de tudo que negociou): este é o
  problema. Traria preços diários de 800-1.200 tickers ao longo de 15+ anos —
  estimados **3 a 4 milhões de linhas, ou 400-500 MB**. Não cabe, nem perto.

**Por que o COTAHIST importa:** é o que resolve o viés de sobrevivência por
completo (item 6) e o que habilita os 49 fatores do JKP que dependem só de
preço diário (momentum, low risk, short-term reversal, seasonality, size —
ver `RELATORIO_JKP.md`).

### As três opções

**A. Carregar só os tickers ausentes.** Os 336 identificados pelo
`ticker_historico`. Estimativa: ~110 MB. Exige limpeza antes (ver abaixo) e
ainda deixa pouca folga.

**B. Frequência mensal para o universo histórico.** Fechamento mensal em vez
de diário para os deslistados: ~5 MB. Resolve o viés de sobrevivência para
backtest trimestral, mas **inviabiliza momentum, volatilidade e reversão** —
ou seja, sacrifica justamente os fatores que o COTAHIST viabilizaria.

**C. Upgrade do Supabase para o plano Pro** (8 GB). Custo da ordem de US$ 25
por mês. Resolve de vez e não exige escolher entre profundidade e cobertura.

**Recomendação registrada:** se o objetivo é pesquisa de fatores — e é —, a
opção **C** é a mais coerente. A e B são adaptações a uma restrição que custa
pouco para eliminar, e ambas sacrificam exatamente o que dá valor ao
COTAHIST. Decisão do usuário, ainda não tomada.

### Espaço recuperável sem custo (independente da decisão)

1. **`idx_prices_daily_date`** (15 MB): 35 usos contra 1,2 milhão do índice de
   chave primária. Candidato a `DROP INDEX`.
2. **Preços anteriores a 2007** — o corte atual é 2005, feito para permitir
   lookback de 5 anos. Cortar em 2007 devolveria alguns MB ao custo de
   encurtar a janela dos primeiros rebalanceamentos.
3. **Normalizar `line_item`, `statement` e `period_type`** para IDs numéricos.
   Hoje cada linha repete strings longas ("Total Equity Gross Minority
   Interest") no dado E no índice de 5 colunas. Reduziria drasticamente os
   ~158 MB de `financials`. É invasivo (mexe em todo o código de leitura), mas
   é o maior ganho disponível sem pagar nada.

---

## 10. Variações implausíveis na contagem de ações (69 casos)

A verificação de sanidade do `cvm_ingestor_fre.py` sinaliza 69 variações
acima de 10x entre exercícios consecutivos. **A maioria é real** — aumento de
capital, IPO e incorporação produzem saltos assim:

- PRIO3 (50,6x em 2013): a Petro Rio nasceu da HRT, reestruturação pesada
- VAMO3 (41,3x em 2021): IPO; antes era subsidiária de capital fechado
- OSXB3 (95,9x em 2012): captação, seguida de recuperação judicial
- EQPA3 (29,9x em 2013): incorporação

**Três são suspeitos e compartilham a mesma assinatura** — um valor
absurdamente pequeno no exercício anterior:

| Ticker | Exercício | De | Para |
|---|---|---|---|
| RVEE3 | 2025 | **1.000** | 80.094.177 |
| LPSB3 | 2012 | **64.000** | 57.078.658 |
| TECN3 | 2013 | **268.565** | 77.478.413 |

Mil ações é número de empresa recém-constituída, não de listada. A hipótese é
que seja a primeira aprovação de capital da empresa — de quando ainda era
fechada — sendo aplicada a um exercício em que ela já negociava.

**Correção sugerida (não implementada):** piso de sanidade descartando
posições abaixo de ~100 mil ações, implausível para empresa listada. Pegaria
os três sem afetar os casos legítimos.

**Prioridade:** baixa. São 3 tickers em 339, e o efeito é valor de mercado
subestimado neles — o que os faria parecer baratos no screener. Vale corrigir
antes de confiar em qualquer resultado que os inclua.