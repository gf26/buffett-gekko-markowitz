# Atualização manual dos dados da CVM

O servidor da CVM bloqueia conexões vindas de datacenter (Codespaces, GitHub
Actions), mesmo respondendo normalmente ao navegador. Por isso o download é
manual. **Não é urgente nem frequente** — veja "Quando fazer" no fim.

---

## Procedimento (15-20 min)

### 1. Descobrir o que falta
No terminal do Codespace:
```
python cvm_fonte.py --listar
```
Ele imprime as URLs exatas dos arquivos que faltam. Se quiser só os anuais
(sem trimestrais), use `--sem-trimestral`.

### 2. Baixar pelo navegador
Copie as URLs e baixe. Duas dicas que economizam tempo:
- Abrir várias abas de uma vez e deixar baixando em paralelo
- Os arquivos vão para a pasta de downloads padrão do seu computador

### 3. Colocar na pasta do projeto
No VS Code (Codespace), **arraste os arquivos** da sua pasta de downloads para
a pasta `dados_cvm/` na árvore de arquivos à esquerda. Arrastar e soltar
funciona direto.

Se a pasta não existir: `mkdir -p dados_cvm`

### 4. ⚠️ Verificar antes de usar
```
python cvm_fonte.py --verificar
```
**Não pule este passo.** O erro mais comum é o navegador salvar uma página de
erro com o nome do arquivo `.zip` — o arquivo existe, parece certo na listagem,
e só quebra lá na frente. A verificação pega isso em segundos.

Se algum aparecer como PROBLEMA, baixe aquele de novo.

### 5. Rodar a ingestão
```
python cvm_ingestor.py --de 2010 --ate 2025 --trimestral
python cvm_validar_carga.py
```

---

## Quando fazer

| Situação | Frequência |
|---|---|
| Carga histórica inicial (2010 até ano passado) | Uma vez |
| Atualizar o ano corrente | 2x por ano é suficiente |

**Por que 2x por ano basta:** o Yahoo continua alimentando os dados correntes
automaticamente, toda semana. A CVM serve para *profundidade histórica* e
*qualidade* — se você atrasar a atualização dela, o screener continua
funcionando normalmente. O dado novo da CVM só importa quando você for rodar
um backtest querendo aquele período com dado oficial.

**Melhores momentos:**
- **Maio/junho** — depois que a maioria das empresas entregou o DFP do ano
  anterior (prazo regulatório é fim de março)
- **Novembro/dezembro** — pega os ITRs dos três primeiros trimestres

---

## Para não esquecer

O jeito mais simples: **crie dois eventos recorrentes anuais no seu
calendário** (1º de junho e 1º de dezembro), com este arquivo no link ou na
descrição. Leva 2 minutos para configurar e resolve de vez — sem depender de
automação que também poderia falhar silenciosamente.

Alternativa dentro do GitHub: abrir duas issues com data-alvo no título
(ex: "Atualizar dados CVM — junho/2026"). Aparecem toda vez que você entra
no repositório.

---

## Notas

- `dados_cvm/` deve estar no `.gitignore` — são centenas de MB, não vão para o
  repositório.
- Os arquivos ficam em cache: uma vez baixado, o script não tenta baixar de
  novo. Os anos fechados (2010-2024) você baixa uma vez e nunca mais mexe.
- Se algum dia o Codespace conseguir acessar a CVM direto (a faixa de IP varia
  entre Codespaces), os scripts baixam sozinhos sem você fazer nada — o
  download manual é só o plano B automático.
