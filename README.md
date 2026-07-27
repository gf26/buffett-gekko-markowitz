# Buffet Gekko - Screener e Otimizador de Portfólio da B3

Um banco de dados de verdade na nuvem (Supabase), que se atualiza sozinho
(GitHub Actions), com um pipeline de índices fundamentalistas e de mercado
por cima - substituindo os notebooks originais (`pickle`/`shelve`, tudo
manual) por um sistema que roda sozinho, sem depender do seu computador
ligado.

**Status:** ✅ Etapa 1 (cache de dados) e Etapa 2 (índices/scores/ranking)
concluídas e rodando. 🔜 Etapa 3 (otimizador de portfólio) e Etapa 4 (app) em
andamento.

---

# Etapa 1: Cache Persistente de Dados

## A arquitetura (por quê)

| Peça | Ferramenta | Por quê | Custo |
|---|---|---|---|
| Banco de dados | Supabase (Postgres) | Hospedado, sem servidor pra você administrar, dá pra consultar de qualquer app depois (inclusive Streamlit) | Grátis (free tier) |
| Agendador | GitHub Actions | Já vem com qualquer repositório, roda no horário que você definir, sem precisar deixar nada ligado | Grátis (2.000 min/mês em repo privado - este job usa uma fração disso) |
| Código | 2 scripts Python simples | Um para preços/dividendos (diário), outro para dados fundamentalistas (semanal, pois mudam pouco) | - |
| Ambiente de configuração/testes | GitHub Codespaces | Terminal Linux completo no navegador, sem instalar nada no seu PC | Grátis (120 core-hours/mês na conta pessoal, dá ~60h numa máquina de 2 núcleos) |

Nenhuma dessas peças exige que você seja desenvolvedor no dia a dia: depois de
configurado uma vez, você só olha os resultados.

## Passo a passo

### 1. Criar o banco de dados (Supabase)
1. Crie uma conta grátis em supabase.com e um novo projeto.
2. Vá em **SQL Editor** e cole o conteúdo de `schema.sql`, depois clique em Run.
   Isso cria as tabelas: `tickers`, `prices_daily`, `dividends`, `splits`,
   `financials`, `company_info`, `ingestion_log`.
   - Se aparecer um aviso de "Row Level Security" (RLS) ao rodar, escolha
     **"Run and enable RLS"** - não atrapalha os scripts (eles conectam
     direto como usuário do Postgres, o RLS só afeta as chaves públicas
     `anon`/`authenticated`) e é mais seguro.
3. Clique em **Connect** (topo da página) ou vá em **Project Settings → Database**
   e copie a connection string da seção **Connection pooling**, modo **Transaction**
   (porta `6543`) - **não** use o endereço direto `db.xxxxxxxx.supabase.co`, pois
   ele só resolve em IPv6 e falha com `Network is unreachable` tanto no Codespaces
   quanto no GitHub Actions. A string do pooler se parece com:
   `postgresql://postgres.xxxxxxxx:[SUA-SENHA]@aws-0-[regiao].pooler.supabase.com:6543/postgres`
   Guarde essa string - é o `DATABASE_URL` usado nos scripts.

### 2. Criar o repositório no GitHub
1. Crie um repositório **privado** novo (ex: `bgm-data`).
2. Suba todos os arquivos desta pasta para ele, **incluindo a pasta oculta**
   `.github/workflows/` no mesmo caminho (é onde o GitHub procura por
   agendamentos). Subir arquivo por arquivo pela interface web do GitHub é a
   causa mais comum de essa pasta ficar faltando - o jeito mais confiável é
   criá-la de dentro do Codespaces (veja o passo 4).

### 3. Guardar a senha do banco com segurança
1. No repositório, vá em **Settings → Secrets and variables → Actions**.
2. Clique em **New repository secret**.
   - Nome: `DATABASE_URL`
   - Valor: a connection string do passo 1.3 (a do **pooler**, porta `6543`)
   Isso mantém a senha fora do código - ninguém que veja o repositório a vê.

### 4. Configurar e popular a lista de tickers (via GitHub Codespaces)
1. No repositório, botão verde **`< > Code`** → aba **Codespaces** → **Create
   codespace on main**. Espere carregar - abre um VS Code completo no navegador.
2. No terminal do Codespaces:
   ```
   pip install -r requirements.txt
   export DATABASE_URL="postgresql://postgres.xxxxxxxx:[SUA-SENHA]@aws-0-[regiao].pooler.supabase.com:6543/postgres"
   python seed_tickers.py
   ```
   Deve terminar com `Seeded/verified 447 tickers.` (ou o número de linhas do
   seu `tickers.csv`).
3. Se a pasta `.github/workflows/` ainda não existir no repositório (confirme
   com `find . -name "*.yml"`), crie-a agora:
   ```
   mkdir -p .github/workflows
   ```
   e cole o conteúdo de `daily_prices.yml` e `weekly_fundamentals.yml` (deste
   pacote) nos respectivos arquivos dentro dela. Depois:
   ```
   git add .
   git commit -m "add scheduled workflows"
   git push
   ```
   Se o `git push` for rejeitado (`fetch first` / `non-fast-forward` - comum
   se você também mexeu no repositório pela interface web), rode primeiro:
   ```
   git pull --no-rebase
   ```
   Se abrir um editor pedindo uma mensagem de commit de merge, apenas salve e
   feche (no terminal, `Ctrl+X` → `Y` → `Enter`; se abrir como aba do VS Code,
   `Ctrl+S` e feche a aba) - depois rode `git push` de novo.

**Sugestão:** 447 tickers é bastante para o Yahoo Finance tolerar todo santo
dia sem risco de bloqueio temporário. Se notar erros de "rate limit" nos logs,
considere reduzir para os tickers que você realmente acompanha (ex: os que têm
liquidez suficiente para entrar no seu screener), e ampliar depois aos poucos.

**Nota sobre performance:** os scripts `ingest_prices.py` e
`ingest_fundamentals.py` gravam no banco em lotes (`execute_values`, até 1.000
linhas por viagem de rede), não linha por linha - isso importa porque, com o
*connection pooler* do Supabase, gravar linha a linha pode levar minutos só
para um ticker com histórico longo (ex: o índice `^BVSP`).

### 5. Testar e deixar os jobs rodando sozinhos
Não precisa fazer mais nada para o agendamento em si - assim que o secret
`DATABASE_URL` existir e os workflows estiverem no repositório, eles já ficam
agendados:
- `daily_prices.yml` → todo dia útil, às 21:00 UTC (18h em Brasília, após o
  fechamento da B3), baixa preços, dividendos e splits novos (só o que ainda
  não está no banco).
- `weekly_fundamentals.yml` → todo domingo às 06:00 UTC, atualiza balanço,
  DRE, fluxo de caixa e informações da empresa.

**Para testar sem esperar o horário agendado** (recomendado na primeira vez):
1. Na aba **Actions** do repositório, clique no workflow desejado na lista à
   esquerda (`Daily Price Ingestion` ou `Weekly Fundamentals Ingestion`).
2. Clique em **Run workflow** (canto superior direito) → confirme **Run
   workflow** no menu que abre (branch `main`).
3. Repita para o outro workflow - pode disparar os dois ao mesmo tempo, eles
   rodam em máquinas separadas e não competem entre si.
4. Atualize a página: a execução aparece com ícone amarelo (rodando) → depois
   ✅ (sucesso) ou ❌ (falhou). Clique na execução para ver os logs ao vivo,
   incluindo o progresso ticker a ticker.

Diferente do Codespaces, uma execução do GitHub Actions roda numa máquina
temporária que existe só para aquele job - não depende do navegador aberto,
não tem timeout de inatividade, e termina sozinha quando o script acaba (ou
depois de 6h, o teto máximo, bem acima do que este job precisa).

### 6. Conferir se está funcionando
Na tabela `ingestion_log` do Supabase (aba **Table Editor**), cada execução
grava uma linha por ticker com `status = 'ok'` ou `'error'` e a mensagem de
erro, se houver. É o seu painel de saúde do sistema, sem precisar abrir logs
do GitHub. Também vale olhar `prices_daily` e `financials` para ver o volume
de linhas crescendo.

## O que NÃO está nesta primeira etapa (de propósito)
Para manter este passo simples e focado em "ter os dados sempre disponíveis e
atualizados", ficou de fora:
- Cálculo dos índices (P/VPA, ROE, Piotroski, Altman Z, Beneish M, Sharpe,
  Sortino, etc.) - ver **Etapa 2**, abaixo.
- O otimizador de portfólio (fronteira eficiente com pesos aleatórios) - **Etapa 3**.
- Um app/dashboard para interagir com tudo isso - **Etapa 4**.

---

# Etapa 2: Índices, Scores e Ranking

**Status:** ✅ concluída e rodando. Lê os dados brutos da etapa 1 e grava
índices calculados de volta no Supabase, em `fundamental_ratios` e
`market_metrics`. Nenhuma chamada nova ao Yahoo Finance - só leitura do banco
+ conta em Python.

## Scripts e o que cada um calcula

| Script | Calcula | Tabela |
|---|---|---|
| `compute_market_metrics.py` | Retorno total, CAGR, volatilidade anualizada, Sharpe, Sortino, drawdown máximo (últimos ~3 anos) | `market_metrics` |
| `compute_fundamental_ratios.py` | P/VPA, P/S, P/L, P/Caixa, D/E, crescimento, dividend yield/payout, margens, **Piotroski F-Score** (com fallback para TTM trimestral quando falta demonstrativo anual) | `fundamental_ratios` |
| `compute_quality_scores.py` | Altman Z-Score, Beneish M-Score, Value Trap Indicator (VTI, fórmula própria do projeto original) | `fundamental_ratios` |
| `compute_magic_formula.py` | Magic Formula (Greenblatt: Earnings Yield + Return on Capital), FCF Yield, Gross Profitability (Novy-Marx) | `fundamental_ratios` |
| `compute_composite_score.py` | Ranking final combinado, comparável entre grupos de pares | `fundamental_ratios` |

Ordem de execução (cada um depende do anterior): `compute_market_metrics.py`
→ `compute_fundamental_ratios.py` → `compute_quality_scores.py` →
`compute_magic_formula.py` → `compute_composite_score.py`.

## Como o ranking final é calculado
1. **Grupo de pares**: cada ticker é classificado como `geral` ou
   `financeiro_utility` (setores Financial Services e Utilities, cujo balanço
   não se encaixa no conceito de "capital empregado" da Magic Formula).
2. **`geral`**: valuation = média dos percentis de Earnings Yield + FCF
   Yield; qualidade = média dos percentis de Return on Capital + Gross
   Profitability.
3. **`financeiro_utility`**: valuation = percentil de P/VPA (invertido, menor
   é melhor); qualidade = percentil de ROE.
4. `composite_percentile` = média de valuation e qualidade, dentro do grupo
   de pares - isso é o que torna um banco comparável com uma indústria na
   mesma tabela ("top 10% do seu grupo" significa a mesma coisa nos dois
   casos).
5. Duas versões ficam salvas lado a lado: **sem filtro** (`composite_percentile`/
   `composite_rank`, bom para swing trade) e **com filtro de qualidade**
   (`composite_percentile_quality`/`composite_rank_quality`, calculado só
   entre quem tem Piotroski F-Score ≥ 7, bom para buy & hold).
   `ranking_status` explica a situação de cada ticker (`ok`,
   `reprovado_piotroski`, `piotroski_desconhecido`, `dados_insuficientes`).

## Decisões e limitações conhecidas (documentadas para não esquecer)
- **Altman Z e Beneish M**: mantidos na base, mas não entram no
  `composite_percentile` nem estão previstos para uso no app - candidatos a
  remoção futura se precisar de espaço/simplicidade.
- **VTI**: implementado na versão "clássica" (thresholds estáticos, a mesma
  do script original do projeto). A versão mais nova (v7.0, Sather Research,
  baseada em variação ao longo de ≥2 anos) é proprietária/paga - migração
  fica para uma versão futura que use uma API dedicada da B3 (ver seção
  "Ideias descartadas por enquanto").
- **`gross_margin_pct` e Altman Z ficam `NULL` para bancos/seguradoras** de
  propósito - "lucro bruto" e "capital de giro" não fazem sentido contábil
  para esse tipo de negócio; forçar um número aqui seria pior que deixar em
  branco.
- **`Retained Earnings` ausente em ~86 tickers**: testamos uma estimativa
  (Patrimônio Líquido − Capital Social) e o erro ficou grande e inconsistente
  nos casos validados - decidido deixar `NULL` em vez de estimar.
- **Grupo `financeiro_utility` só tem 1 índice de cada lado** (ROE + P/VPA),
  enquanto o grupo `geral` tem 2 de cada - "assimétrico" de propósito, não é
  prioridade simetrizar agora.
- **Histórico de fundamentos limitado a ~3-4 anos anuais / 4 trimestres** -
  é um limite da própria API do Yahoo Finance (confirmado - nem pagando o
  Yahoo Finance Premium isso muda via API, só no site). Ver "Ideias
  descartadas por enquanto".

## Views úteis
- `vw_screener` - a planilha completa, junta tudo (valuation, margens,
  scores, ranking, métricas de mercado) numa linha por ticker.
- `vw_data_coverage`, `vw_latest_prices`, `vw_company_info_flat`,
  `vw_ultima_execucao` - painéis auxiliares de saúde/navegação dos dados
  brutos (etapa 1).

## Automação
`daily_compute_scores.yml` roda os 5 scripts todo dia, encadeado depois do
`daily_prices.yml` (que já é diário). Rodar os scripts de **cálculo** todo
dia é barato (não chamam o Yahoo, só leem o Supabase) e tem benefício real:
índices que dependem do preço (P/L, P/VPA, Earnings Yield, market cap, o
ranking final) ficam atualizados com o fechamento mais recente, mesmo entre
uma atualização de fundamentos e outra. A **ingestão** de fundamentos
(`ingest_fundamentals.py`, que sim chama o Yahoo) continua semanal de
propósito - rodar mais que isso não traria dado novo (empresas não publicam
balanço todo dia) e só aumentaria o risco de rate-limit.

## Ideias descartadas por enquanto (registradas para o futuro)
- **API dedicada de dados da B3** (ex: brapi.dev, bolsai - dados oficiais da
  CVM) no lugar de, ou complementar ao, Yahoo Finance - resolveria o limite
  de ~3-4 anos de fundamentos. Não é urgente porque o objetivo agora é
  terminar com Yahoo Finance (inclusive pensando em tickers globais depois).
  Fundamentus **não tem API oficial** - só site com HTML/Excel, exigiria
  scraping frágil; as APIs dedicadas acima são a alternativa melhor caso essa
  migração vire prioridade.

---

# Etapa 3: Otimizador de Portfólio

**Status:** 🔜 próxima etapa. Vai reconstruir a lógica do
`Buffet_Gekko_v8_Portfolio.ipynb` original (simulação de Monte Carlo com
pesos aleatórios, fronteira eficiente) de forma vetorizada, para rodar rápido
o suficiente para um app interativo.

# Etapa 4: Web App

**Status:** 🔜 depois da etapa 3. App (provavelmente Streamlit) conectando no
mesmo Supabase, com filtros/pesos ajustáveis no screener e o otimizador de
portfólio rodando ao vivo.