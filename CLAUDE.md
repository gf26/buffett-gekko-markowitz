# CLAUDE.md — Buffett Gekko Markowitz

> Documento de contexto para o Claude Code. Leia inteiro antes de mexer em
> qualquer coisa. Ele existe porque boa parte das decisões deste projeto tem
> um "por quê" que não está visível no código, e várias armadilhas já custaram
> horas de depuração — repeti-las seria desperdício.

---

## 1. O que é este projeto

Sistema de **screening fundamentalista + otimização de portfólio para ações
da B3** (bolsa brasileira). Substitui um conjunto de notebooks Jupyter frágeis
(`pickle`/`shelve`, tudo manual, rodando no PC do usuário) por um sistema
cloud-native que se atualiza sozinho.

Objetivo final: um web app onde o usuário filtra ações por critérios
fundamentalistas, monta uma carteira otimizada e vê o backtest da estratégia.

**Repositório:** `github.com/gf26/buffett-gekko-markowitz`

---

## 2. Sobre o usuário (importante para calibrar as respostas)

- **Não é desenvolvedor.** Tem base sólida em finanças e análise, e
  experiência prévia com scripts Python, mas não vem de engenharia de
  software. Explique decisões técnicas em termos de consequência prática, não
  de jargão.
- **Trabalha no GitHub Codespaces.** Roda os scripts lá, cola a saída, e
  itera. Não tem ambiente local configurado.
- **Valoriza entender o porquê.** Perguntas dele frequentemente revelam
  problemas reais (ele detectou o problema dos pesos grudados nos limites, a
  falta de restrição de peso, a inconsistência do ponto inicial dos gráficos).
  Leve as dúvidas dele a sério — várias viraram correções importantes.
- **Prefere ser consultado antes de decisões de arquitetura**, não depois.
  Ele pediu explicitamente: "questione se houver decisões adicionais a serem
  tomadas".

### Estilo de trabalho que funcionou bem até aqui

1. **Testar antes de entregar.** Todo script foi validado com dados
   sintéticos que reproduzem a estrutura real antes de ir para o usuário.
   Isso pegou vários bugs que teriam custado uma rodada de ida-e-volta:
   `CD_CONTA` lido como float, acentos quebrando casamento de nomes, sobra de
   caixa negativa na discretização, ordenação invertida, `pd.io.common.BytesIO`
   inexistente.
2. **Falhar com aviso, nunca em silêncio.** Vários bugs graves aqui foram
   silenciosos (retornavam vazio sem erro). Quando algo não encontrar dado,
   **imprima o motivo**.
3. **Discordar quando fizer sentido.** Houve momentos em que a recomendação
   certa foi contrária ao pedido dele (ex: não migrar de fonte de dados antes
   do backtest; não usar rebalanceamento mensal achando que resolveria a falta
   de dados). Ele respondeu bem a isso — explique o raciocínio.
4. **Nunca inventar dado.** Em vez de chutar tickers de memória, buscar e
   confirmar. Dado errado entrando silenciosamente no banco é o pior
   resultado possível aqui.

---

## 3. Stack e infraestrutura

| Componente | Escolha | Observações |
|---|---|---|
| Banco | **Supabase** (Postgres, free tier) | Projeto `hcfsvwkrrzskgejaviqq` |
| Conexão | **Connection pooler, porta 6543** | ⚠️ NÃO usar o host direto — dá erro de IPv6. `postgresql://postgres.hcfsvwkrrzskgejaviqq:[SENHA]@aws-1-sa-east-1.pooler.supabase.com:6543/postgres` |
| Agendamento | **GitHub Actions** | Workflows em `.github/workflows/` |
| Dev | **GitHub Codespaces** | `DATABASE_URL` já está como secret do Codespaces e do Actions |
| Dados de preço | **Yahoo Finance** (`yfinance`) | Diário |
| Dados fundamentalistas | **Yahoo** (atual) + **CVM** (em migração) | Ver seção 7 |
| Taxa livre de risco | **API do Banco Central** (SGS série 432) | Selic meta, gratuita, sem chave |

### Dependências travadas (não desfaça)
```
yfinance>=0.2.40,<1.0.0   # 1.0 quebrou a API
pandas>=2.0,<3.0.0        # 3.0 quebrou compatibilidade
```

---

## 4. Arquitetura em etapas

### Etapa 1 — Ingestão (✅ rodando sozinha)
- `ingest_prices.py` — OHLCV + dividendos + splits, diário (Actions,
  seg-sex 21:00 UTC)
- `ingest_fundamentals.py` — 6 demonstrativos + `company_info`, semanal
  (domingo 06:00 UTC)
- `seed_tickers.py` — carga inicial do universo (272 tickers, one-time)

### Etapa 2 — Índices e ranking (✅ rodando sozinha)
Roda **diariamente** (`daily_compute_scores.yml`, encadeado após
`daily_prices.yml`), nesta ordem obrigatória:

1. `compute_market_metrics.py` — CAGR, vol, Sharpe, Sortino, drawdown,
   **liquidez (ADTV)**
2. `compute_fundamental_ratios.py` — P/L, P/VPA, margens, **Piotroski F-Score**
3. `compute_quality_scores.py` — Altman Z, Beneish M, VTI
4. `compute_magic_formula.py` — EV, Earnings Yield, FCF Yield, ROC, Gross
   Profitability
5. `compute_composite_score.py` — peer groups, ROE, ranking composto

**Por que os cálculos rodam diário mas a ingestão de fundamentos é semanal:**
os índices que dependem de preço (P/L, P/VPA, EY, market cap) mudam todo dia
mesmo com o balanço parado. Os scripts de cálculo não chamam o Yahoo (só leem
o Supabase), então rodar diário é barato. Já a *ingestão* de fundamentos
chamaria o Yahoo 7x mais vezes para trazer o mesmo dado — só aumenta risco de
rate-limit.

### Etapa 3 — Otimizador (🔧 funcional, em refinamento)
`portfolio_optimizer.py` — **biblioteca, não job agendado.**

- Resolve a fronteira eficiente por **otimização convexa** (PyPortfolioOpt),
  não Monte Carlo. Foi Monte Carlo antes; a troca deixou exato e mais rápido.
- Covariância: **Ledoit-Wolf** por padrão (`--covariance sample` para comparar)
- Restrições de peso por ativo (`--min-weight`, `--max-weight`)
- Discretização em ações inteiras (`discretize_allocation`), com suporte a
  mercado fracionário da B3
- Filtro de liquidez (`--min-liquidity`, padrão sugerido: R$ 50.000/dia)
- Gráficos: fronteira, retorno acumulado, correlação, composição

### Etapa 4 — Backtest (🔧 funcional, resultados preliminares)
`backtest.py` + `scoring.py`

- Walk-forward, rebalanceamento configurável (`--rebalance-freq`)
- `scoring.py` reconstrói o ranking para datas passadas **em memória** — foi
  preciso escrever do zero porque os `compute_*.py` só guardam o retrato de
  hoje (sobrescrito a cada execução), então não existe histórico de rankings.

### Etapa 5 — Web app (⏳ não iniciada)
Provavelmente Streamlit ou FastAPI + frontend. Ver seção 9.

---

## 5. ⚠️ Armadilhas conhecidas — leia antes de depurar

Estas custaram tempo real. Todas causavam **falha silenciosa**.

### Banco / infraestrutura
- **IPv6 no Supabase**: use sempre o pooler na 6543, nunca o host direto.
- **Tipos numpy no psycopg2**: converta `np.float64` → `float` nativo antes de
  gravar, ou dá erro obscuro.
- **`execute_values` em vez de inserção linha a linha** — a diferença de
  performance é enorme.

### CVM (ver seção 7)
- **`CD_CONTA` DEVE ser lido como string.** O pandas lê `"1"` como `1.0` e
  `"1.01"` como `1.01`, e aí **nenhum** mapeamento de conta casa. Falha
  silenciosa, resultado vazio, zero erro.
- **`CD_CVM` tem zero-padding inconsistente**: arquivos detalhados
  (BPA/BPP/DRE/DFC) trazem `"001023"`, o arquivo resumo traz `"1023"`.
  Normalize com `.str.lstrip("0")` **em todos os pontos de leitura**.
- **`ORDEM_EXERC`**: cada arquivo traz o exercício corrente ('ÚLTIMO') E o
  anterior ('PENÚLTIMO'). Filtre, ou os valores duplicam.
- **`ESCALA_MOEDA`**: pode vir 'MIL'. Normalize, ou os números saem 1000x
  errados.
- **`VERSAO`**: reapresentações geram múltiplas versões do mesmo período.
  Fique com a maior.
- **Acentos**: CVM grava `PETROLEO`, Yahoo grava `PETRÓLEO`. Remova acentos
  antes de casar nomes.

### Yahoo Finance
- **Algumas empresas vêm em DÓLAR rotuladas como BRL.** Confirmado para VALE3
  e EMBJ3 (razão de ~6,19 = câmbio). O campo `financialCurrency` diz "BRL"
  mesmo assim — ou seja, **não dá para confiar nesse campo**. Isso significa
  que P/VPA, ROE, EY dessas empresas estão errados hoje. A migração para CVM
  corrige.
- **Limite de histórico**: ~3-4 períodos anuais, ~4 trimestrais. É limite
  duro da API. **Pagar o Yahoo Finance Premium NÃO resolve** — o acesso extra
  é só pelo site, não pela API (verificado).

### Backtest
- **`first_seen_at` não serve para datas anteriores à migração.** A coluna foi
  criada com `DEFAULT now()`, então toda linha pré-existente ganhou a data da
  migração. Usá-la para reconstruir 2022 filtra tudo fora. Por isso
  `snapshot_financials(use_first_seen=False)` é o padrão. Religar só quando o
  sistema tiver rodado tempo suficiente para a coluna significar algo.
- **Comparar períodos diferentes é enganoso.** A estratégia só existe a partir
  de meados de 2024 (precisa de 2 exercícios para o Piotroski). Comparar isso
  com 1/N medido desde 2022 não é maçã com maçã — o `backtest.py` já separa
  "contexto" de "período comparável" por causa disso.

---

## 6. Decisões de projeto e o raciocínio por trás

### Otimização
- **Convexa > Monte Carlo**: o problema de Markowitz é convexo, tem solução
  exata. Simular pesos aleatórios era aproximação desnecessária.
- **Ledoit-Wolf > covariância amostral**: a amostral é ruidosa, principalmente
  quando o nº de ativos se aproxima do nº de observações. Uma linha de código,
  ganho grande.
- **`mu_shrinkage` e `l2_gamma` existem por um motivo real**: a otimização
  média-variância é um "maximizador de erro de estimativa" (Michaud, 1989).
  Com 3 anos de dados, um spread de 79 p.p. entre o maior e o menor retorno
  esperado é quase certamente ruído. Os dois parâmetros atacam isso por
  ângulos diferentes: `mu_shrinkage` corrige a *estimativa de entrada*
  (causa), `l2_gamma` penaliza *concentração no peso* (sintoma).
- **Pesos "grudam" nos limites min/max e isso é esperado**: problemas com
  restrição de caixa têm soluções de canto; elas não se movem continuamente
  com o parâmetro, "pulam" quando cruza um limiar. Se precisar forçar menos
  concentração de forma previsível, baixar `--max-weight` é mais direto que
  aumentar gamma.

### Screener
- **Peer groups (`geral` vs `financeiro_utility`)**: bancos e utilities não
  têm "capital de giro" ou "lucro bruto" no sentido usual. Aplicar Magic
  Formula/Altman Z neles produz lixo silencioso. Por isso são rankeados
  separadamente, com ROE + P/VPA.
- **`NULL` em vez de estimativa**: testamos estimar Retained Earnings
  (Patrimônio − Capital Social) e o erro foi de 0,2% a 2.507%. Decidido deixar
  `NULL`. Vale como princípio geral aqui.
- **Grupo `financeiro_utility` é assimétrico de propósito** (1 índice de cada
  lado, vs 2 no `geral`) — falta de dado, não escolha. Simetrizar não é
  prioridade.
- **VTI é o elo mais fraco**: fórmula de produto comercial (Sather Research),
  limiares arbitrários, sem paper público. Se precisar cortar algo por
  simplicidade, corte o VTI antes do Piotroski (que tem tese revisada por
  pares e resultado replicado).
- **Altman Z e Beneish M**: mantidos na base, mas o usuário não pretende usá-los
  no app. Candidatos a remoção se precisar de espaço.

### Backtest
- **Duas variantes sempre** (Markowitz e peso igual): separa "meu screener
  escolhe boas ações?" de "minha otimização de pesos ajuda?". São perguntas
  diferentes e podem ter respostas opostas.
- **1/N do universo é a referência crítica** (DeMiguel, Garlappi & Uppal,
  2009): se a complexidade não bate a carteira ingênua fora da amostra, ela
  não está se pagando.

---

## 7. Migração CVM (em andamento — estado atual)

**Motivação:** o Yahoo dá só ~3-4 anos de fundamentos, o que limita o backtest
a ~8 trimestres. A CVM (dados.cvm.gov.br) tem DFP desde 2010 e ITR desde 2011,
grátis, sem chave, e é a **fonte primária oficial** (bolsai/brapi/Partnr todos
derivam dela).

**Benefício extra decisivo:** os arquivos anuais da CVM são imutáveis — o
arquivo de 2015 contém as empresas que existiam em 2015, **incluindo as que
quebraram depois**. É a única forma de corrigir o viés de sobrevivência.

### Fases
| Fase | Status | Script |
|---|---|---|
| 1. Validação de compatibilidade | ✅ concluída — **92,8%** de concordância | `cvm_fase1_validacao.py` |
| 2. Mapeamento ticker↔CVM | ✅ concluída (269 empresas / 271 tickers) | `cvm_mapear_tickers.py` |
| 3. Ingestor histórico | 🔧 funcional, ainda não rodado em produção | `cvm_ingestor.py` |
| 4. Universo histórico completo | 🔧 em revisão manual | `cvm_verificar_universo.py`, `cvm_priorizar_ausentes.py`, `cvm_cruzar_com_existentes.py` |

### Descobertas importantes da Fase 1
- **Patrimônio Líquido e Lucro Líquido devem usar a variante "controladores",
  não a consolidada.** A CVM reporta o consolidado (incluindo minoritários);
  o Yahoo reporta o atribuível aos controladores. Usar a errada dá ~63% de
  concordância; a certa dá 91,6% e 80,2%.
  - PL controladores = conta `2.03` − conta `2.03.09`
  - LL controladores = conta `3.11.01` (já vem pronta)
- **Bancos usam plano de contas diferente na CVM** — a conta `2.03` não
  significa a mesma coisa. Setor financeiro fica ~72% de concordância.
- **CapEx é o ponto fraco**: não tem código fixo na CVM, fica em subcontas de
  `6.02` com nome variável. Localizado por descrição, cobertura parcial. Afeta
  o FCF Yield.

### Decisão de sobreposição
Onde CVM e Yahoo coincidem, **a CVM sobrescreve** (é a fonte oficial). A
coluna `financials.source` registra a origem. O código de leitura não muda.

### ⚠️ Limitação estrutural de `ticker_cvm_map`
A tabela é **1 ticker → 1 cd_cvm**. Mas empresas que se reestruturaram têm
**vários registros CVM ao longo da vida**, e capturar o histórico completo
exigiria N registros por ticker. Se isso virar necessário, a tabela precisa de
chave composta `(ticker, cd_cvm)`. Ainda não foi feito.

---

## 8. Estado atual e pendências imediatas

### Rodando em produção
- Ingestão diária de preços ✅
- Ingestão semanal de fundamentos (Yahoo) ✅
- Cálculo diário de todos os índices ✅

### Feito mas não rodado em produção
- `cvm_ingestor.py` — testado em dry-run (7.527 linhas para 2024). Falta rodar
  a carga completa 2010-2025.

### Em revisão manual pelo usuário
- `empresas_cvm_lacuna_real.csv` (521 empresas) — candidatas a ticker novo.
  Confirmado até agora: **JBS = JBSS3.SA** (cd_cvm 20575).
  Suspeitas fortes não confirmadas: LEVE3 (Mahle, confirmado por busca),
  OIBR3 (Oi), BRPR3 (BR Properties), FRIO3 (Metalfrio) — **nenhuma dessas está
  na tabela `tickers` hoje**.
- `empresas_cvm_registro_antigo_de_ticker_existente.csv` (64 linhas) — precisa
  preencher a coluna `confirmar` e rodar `--carregar`.
  ⚠️ **O casamento por similaridade produziu erros reais**: "Banco ABC Brasil"
  casou com Banco do Brasil, "Equatorial Maranhão" com "Equatorial Pará"
  (0,93 de similaridade!). Só `EXATO` é seguro sem revisar.

### Resultado preliminar do backtest (8 trimestres, out-of-sample)
| Estratégia | Retorno total | Sharpe |
|---|---|---|
| Markowitz | 48,78% | 0,931 |
| Peso igual (mesmo screener) | 38,82% | 0,872 |
| 1/N do universo | 11,88% | 0,247 |
| Ibovespa | 41,11% | 0,923 |

**Leitura:** o screener parece ter sinal real (peso igual >> 1/N, mesma regra
de peso, muda só a seleção). Mas empata com o Ibovespa — ou seja, ainda não há
evidência de que o pacote completo supere um ETF de índice. Amostra pequena
demais para conclusão; e o período foi de alta forte (nenhum trimestre de
crise real na amostra).

---

## 9. Roteiro futuro (do relatório de análise quantitativa)

Prioridade decrescente:

1. ✅ Filtro de liquidez — feito
2. ✅ Point-in-time (lag de 3 meses) — feito
3. ✅ Motor de backtest walk-forward — feito
4. ✅ Ledoit-Wolf — feito
5. 🔲 **Carga completa da CVM** — destrava backtest com 15 anos em vez de 2
6. 🔲 **Custos de transação mais realistas + política de rebalanceamento**
7. 🔲 **Information Coefficient por indicador** — descobrir empiricamente
   quais dos ~20 indicadores têm poder preditivo. Provavelmente elimina
   metade, o que é uma vitória.
8. 🔲 **Deflated Sharpe Ratio** — descontar o Sharpe pelo nº de variantes
   testadas (Bailey & López de Prado)
9. 🔲 **HRP e Black-Litterman** como alternativas de alocação
10. 🔲 **Web app**

### Considerações para o app (já discutidas)
- Três regimes de latência: screener (ms, consulta direta), otimização
  (segundos, chamada síncrona), backtest (minutos, **precisa de fila de
  tarefas**, não cabe em request HTTP).
- Parâmetros que o usuário quer expor na interface: piso de liquidez, piso do
  Piotroski, seleção por setor (top-N por setor em vez de ranking geral),
  periodicidade de rebalanceamento, pesos dos indicadores.
- **Nunca expor `DATABASE_URL` ao cliente.**

---

## 10. Referências que embasam decisões deste projeto

- **Piotroski (2000)** — o F-Score implementado
- **DeMiguel, Garlappi & Uppal (2009)** — o teste 1/N
- **Michaud (1989)** — o "enigma" da otimização (pesos grudados)
- **Ledoit & Wolf (2004)** — encolhimento de covariância
- **Novy-Marx (2013)** — Gross Profitability
- **Bailey & López de Prado (2014)** — Deflated Sharpe Ratio
- Livros: Ang, *Asset Management*; Chan, *Quantitative Trading*; Grinold &
  Kahn, *Active Portfolio Management*; López de Prado, *Advances in Financial
  Machine Learning* (caps. 7, 11-12)

---

## 11. Convenções de código

- Comentários e mensagens ao usuário **em português**
- Nomes de função/variável em inglês ou português, o que for mais claro no
  contexto (o código atual mistura — não é problema)
- Comentários explicam **por quê**, não o quê
- Todo script tem docstring no topo explicando propósito, decisões e
  limitações conhecidas
- Scripts de cálculo: idempotentes, com `ON CONFLICT DO UPDATE`
- Sempre imprimir progresso e diagnóstico — o usuário roda no terminal e cola
  a saída
