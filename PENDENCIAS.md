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

**Efeito colateral RESOLVIDO:** os 12 tickers que ficaram sem contagem
(BAZA3, BDLL3/4, BMGB4, HETA4, HOOT4, ITUB3/4, LWSA3, SHOW3, TIMS3, VBBR3)
foram recuperados. Eram duas causas distintas:

1. **Bug no mapeamento CNPJ → CD_CVM.** Um mesmo CNPJ tem dois registros na
   CVM, um CANCELADO (antigo) e um ATIVO. O cadastro lista o cancelado
   primeiro e o código pegava "o primeiro que aparece" — o Itaú ia para o
   código 1279 em vez do 19348, e como 1279 não está na `ticker_cvm_map`,
   caía silenciosamente em "fora do universo". Mesmo padrão em Vibra
   (14249 × 24295) e BMG (1716 × 24600). Corrigido: o registro ATIVO vence.

2. **Ausência de `Capital Integralizado`.** TIM e Bardella só declaram
   "Capital Emitido"; Banco da Amazônia só "Capital Subscrito". Resolvido com
   cascata — ver item 12.

Cobertura foi de 339 para **350 tickers**.

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

## 6. Viés de sobrevivência — 🔧 EM RESOLUÇÃO

**Progresso:** o COTAHIST foi carregado (1,32 mi de preços, 1.080 tickers,
2010-2026) e ajustado por proventos e eventos societários (1.311.044 preços
ajustados, 925 tickers após filtrar BDRs). Contra 351 tickers do Yahoo.

Tickers que existiam e o Yahoo não tem: CCRO3 (CCR/Motiva), CIEL3 (Cielo),
ENBR3 (EDP), SGPS3, TRPL4, VALE5, ELET6, CPLE6, RAPT4, SANB4, GOLL4 e
centenas de outros.

**Falta:** validar a série ajustada contra a brapi em tickers líquidos. Se
bater, o método está provado e vale para os ~600 deslistados que só o COTAHIST
tem — e aí o viés fica de fato resolvido.

### Registro do que era o problema

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

---

## 11. Fonte de preços: Yahoo, COTAHIST ou brapi — DECISÃO PENDENTE

**Situação atual:** todos os preços vêm do Yahoo (1,17 mi de linhas em
`prices_daily`). Os fundamentos vêm da CVM. As linhas do Yahoo em
`financials` são resíduo de 2021+ mais campos que a CVM não cobre.

### O problema com o Yahoo

Erros recorrentes, cada um descoberto por depuração:

| Erro | Evidência |
|---|---|
| Moeda errada | Vale e Embraer em USD rotulados como BRL (razão ~6,19); `financialCurrency` não é confiável |
| Histórico curto | ~3-4 períodos de fundamentos; limite duro da API, Premium não resolve |
| Remove deslistados | APER3, HGTX3, BIDI11 desaparecem — causa direta do viés de sobrevivência |
| Ajuste de proventos quebrado | AZEV3 cotada a R$ 0,0001 por 740 pregões, saltando para R$ 302,82; OSXB3 idem. Um caso levou o 1/N a reportar 10.292.549% de retorno |

### Alternativa A: COTAHIST (B3) para histórico + Yahoo/brapi para recente

**A favor:** é a fonte PRIMÁRIA, vem da própria bolsa, é imutável e contém
tudo que negociou — inclusive deslistados. Resolve o viés de sobrevivência e
habilita os 49 fatores do JKP que dependem de preço diário.

**Contra:** traz preços SEM ajuste por proventos e desdobramentos. O ajuste
teria que ser feito com as tabelas `dividends` e `splits`.

**Confiabilidade do ajuste próprio — avaliação honesta:**

- *Splits e grupamentos:* alta confiabilidade. É aritmética simples, e a
  tabela `splits` já existe desde 2000.
- *Dividendos e JCP:* média. A fórmula padrão (fator = 1 − provento/preço na
  data-com) é bem estabelecida, mas depende da tabela `dividends` estar
  completa e com as datas certas. Lacunas produzem degraus na série.
- *Bonificações, subscrições, incorporações:* baixa. São os casos que mais
  quebram, e são justamente os que o Yahoo também erra.

**O ponto a favor mesmo assim:** o ajuste passa a ser auditável e sob seu
controle, em vez de uma caixa-preta que às vezes devolve R$ 0,0001. Erros
viram bugs que dá para investigar, não mistérios da fonte.

**Validação possível:** comparar a série ajustada por nós com a da brapi (ou
com o Yahoo, onde ele não está quebrado) para tickers líquidos. Concordância
alta valida a metodologia; divergência aponta onde o ajuste falha. Essa
comparação sozinha já justifica um mês de plano pago da brapi.

### Alternativa B: brapi

**A favor:** menos manutenção, sem parsing, sem descobrir variantes de plano
de contas. Custo comparável ao do Supabase Pro.

**A verificar antes de decidir (nenhum destes foi confirmado):**
1. Mantém histórico de DESLISTADOS? Se não, o viés de sobrevivência continua
   e a vantagem principal do COTAHIST se perde.
2. Cobre 2010 nos fundamentos?
3. Fornece DATA DE PUBLICAÇÃO? Sua base tem `published_date` real da CVM em
   100% dos fundamentos. APIs comerciais costumam entregar o valor corrigido,
   não o que estava público na data — o que introduz look-ahead invisível.
4. Os preços são ajustados? Por qual metodologia?

### Arquitetura: consultar a API ou gravar no banco?

**Gravar no banco, sempre.** Consultar a API a cada operação seria inviável:

- O backtest reconstrói o ranking para 56+ datas, cada uma exigindo
  fundamentos de ~300 empresas e preços de 15 anos. Seriam dezenas de
  milhares de chamadas por execução.
- Cada execução consumiria cota e levaria minutos em vez de segundos.
- Sem cache local, backtests deixam de ser reproduzíveis — a fonte pode
  revisar dados entre duas execuções.

O padrão correto é o que já existe: **ingestão periódica grava no banco, os
scripts leem do banco.** Trocar de fonte muda o script de ingestão, não a
arquitetura.

### Consumo estimado de cota

Com ingestão diária (o padrão atual) e ~350 tickers:

| Uso | Chamadas/mês |
|---|---|
| Cotações diárias, 350 tickers, 21 pregões | ~7.350 |
| Fundamentos, atualização trimestral | ~1.400 |
| **Total recorrente** | **~9.000** |

O **plano gratuito (15.000/mês) provavelmente basta** para o uso recorrente —
desde que a carga histórica inicial venha do COTAHIST, não da API. O gargalo
do plano gratuito não é o volume: é que fundamentos e `range=max` são
bloqueados por plano, não por cota (verificado: HTTP 403 nos módulos
`balanceSheetHistory` e `incomeStatementHistory`).

### Recomendação registrada

**Híbrido:** COTAHIST para o histórico (2010-2024) e brapi para o incremento
corrente, substituindo o Yahoo. Isso ataca o viés de sobrevivência com a
fonte primária e elimina a peça mais problemática do pipeline atual.

Antes de assinar qualquer coisa, testar os 4 pontos da Alternativa B — se a
brapi não tiver deslistados nem data de publicação, ela substitui o Yahoo mas
não substitui a CVM nem o COTAHIST.

---

## 12. Cascata de tipos de capital — implementada, com ressalva

**O que é:** o FRE declara o capital em quatro tipos. O ingestor usa, nesta
ordem de preferência:

1. `Capital Integralizado` — efetivamente pago. É o correto.
2. `Capital Subscrito` — comprometido pelos sócios, podendo haver parcela a
   integralizar.
3. `Capital Emitido` — autorizado a emitir.

`Capital Autorizado` fica **fora** de propósito: é teto estatutário, não ações
existentes (a Vale tem 5,37 bi integralizados contra 10,8 bi autorizados).

**Por que a cascata existe:** exigir só Integralizado deixava TIM, Bardella e
Banco da Amazônia sem contagem nenhuma.

**A ressalva, e ela é maior do que se previa:** 329 empresas (526 aprovações
de 5.536, ~9,5%) não têm Integralizado e usam Subscrito ou Emitido. Esperava-se
3 empresas.

A maioria são securitizadoras e veículos financeiros fora do universo, mas há
empresas reais na lista (Algar Telecom, Aegea, Aliansce). Onde há capital
subscrito e não integralizado, a contagem **superestima** as ações em
circulação — e portanto o valor de mercado, fazendo a empresa parecer mais
cara do que é.

**Rastreabilidade:** o tipo usado é impresso no log da execução, mas **não
gravado no banco** — a tabela `financials` não tem coluna para isso. Se a
auditoria por ticker virar necessária, seria preciso uma coluna nova ou uma
tabela auxiliar.

**Como saber quais dos seus tickers estão afetados:** ver o comando de
diagnóstico registrado junto a este item na conversa; ele cruza os arquivos
FRE com a `ticker_cvm_map`.

**Prioridade:** média. Não bloqueia nada, mas convém saber quais tickers do
universo ativo dependem de tipo não-integralizado antes de confiar no
valuation deles.

---

## 13. Avisos residuais no ajuste de preços (18 casos)

Depois de três rodadas de correção (fator invertido, tentativa dupla de base
do provento, faixa de cisão ampliada), sobraram 18 avisos em 1,31 milhão de
preços. Cada um deixa um degrau na série daquele ticker naquela data — não
impede o uso, mas um fator de momentum veria movimento que não existiu.

### HBTS5 — 13 dos 18, causa desconhecida

A Habitasul aparece com dividendos de 5 a 20 vezes o preço da ação, de forma
consistente por 13 anos (2011 a 2026). Bruto e reescalado dão o mesmo valor,
porque não há evento proporcional posterior — a reescala não tem o que
corrigir.

Não é erro pontual da fonte: é padrão sistemático. Hipóteses não verificadas:
a brapi pode estar reportando o provento de outra classe de ação, ou o
montante total em vez do valor por ação.

**Impacto prático: nulo.** HBTS5 é papel de liquidez muito baixa, descartado
pelo filtro de R$ 50 mil/dia.

### Cisões com salto pequeno — 4 casos

CSAN3 (0,957), SANB3 (0,975), SANB11 (0,972), VIVR3 (0,979). Saltos de 2-4%
são indistinguíveis de movimento normal de mercado. **Não ajustar é o
comportamento correto** — forçar um fator confundiria ruído com evento.

### CEGR3 e FIGE4 — 1 caso cada

Provento acima do preço sem evento proporcional que explique. Mesma categoria
do HBTS5, em menor escala.

---

## 14. Tickers com liquidez residual na base

O ajuste revelou papéis com histórico tecnicamente presente mas praticamente
inútil:

- **CALI3**: 214 pregões em 16 anos, fator de ajuste acumulado de 0,0015
- **CEBR6**: fator 0,0051
- **NUTR3**: fator 0,0100

Não são erros — são papéis que quase não negociam. O filtro de liquidez já os
descarta do universo investível, mas eles entram em estatísticas agregadas
(como o 1/N do universo) se não forem filtrados explicitamente.

**Ação sugerida:** ao construir o painel de dados point-in-time (item 2 do
`relatorio_analise_quant.md`), aplicar um piso de pregões por ano além do piso
de volume financeiro. Um papel com 13 pregões por ano não tem preço confiável
em nenhuma data de rebalanceamento.