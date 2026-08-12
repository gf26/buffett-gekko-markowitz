# Pendências conhecidas

Coisas decididas mas ainda não feitas, e limitações que precisam de tratamento.
Cada item diz **por que existe** e **o que fazer** — comentário no código se
perde, isto aqui não.

---

## 1. Valor de mercado por classe de ação (bloqueado: aguardando FRE)

**Estado:** solução temporária em produção.

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

## 2. Ações em circulação antes de 2020 (bloqueado: site da CVM fora do ar)

**Estado:** sem dados. O backtest não consegue selecionar carteira em
2010-2019.

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

## 3. Escala inconsistente em `composicao_capital` (2020-2025)

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
