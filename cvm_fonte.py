"""
Camada de acesso aos arquivos da CVM, com cache local.

PROBLEMA QUE ISTO RESOLVE
-------------------------
O servidor da CVM às vezes fica inacessível de dentro do Codespace (timeout),
mesmo estando no ar e respondendo normalmente ao navegador. A causa provável
é bloqueio por faixa de IP: o Codespace roda num datacenter, e órgãos
públicos frequentemente restringem esse tipo de origem. Como o IP muda a cada
Codespace novo, isso funciona um dia e para de funcionar no outro - sem nada
ter mudado no código.

Além disso, mesmo quando funciona, baixar 16 anos de arquivos a cada execução
é lento e frágil: uma falha no meio da carga obriga a recomeçar tudo.

COMO FUNCIONA
-------------
Antes de baixar, procura o arquivo em `dados_cvm/`. Se estiver lá, usa o
local. Se não, tenta baixar e salva no cache para as próximas vezes.

QUANDO O DOWNLOAD NÃO FUNCIONA (seu caso agora)
------------------------------------------------
1. Crie a pasta:            mkdir -p dados_cvm
2. Baixe os arquivos pelo NAVEGADOR (funciona, você confirmou) e arraste
   para dentro dessa pasta no Codespace (o VS Code aceita arrastar e soltar).
3. Rode os scripts normalmente - eles vão achar os arquivos no cache.

Os arquivos necessários estão listados por `python cvm_fonte.py --listar`.

⚠️ Adicione `dados_cvm/` ao .gitignore - são centenas de MB, não devem ir
para o repositório.
"""
import argparse
import io
import os
import zipfile

import requests

PASTA_CACHE = "dados_cvm"
URL_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA"

URL_DFP = f"{URL_BASE}/DOC/DFP/DADOS/dfp_cia_aberta_{{ano}}.zip"
URL_ITR = f"{URL_BASE}/DOC/ITR/DADOS/itr_cia_aberta_{{ano}}.zip"
URL_CADASTRO = f"{URL_BASE}/CAD/DADOS/cad_cia_aberta.csv"

TIMEOUT = 300


def _caminho_cache(nome_arquivo):
    return os.path.join(PASTA_CACHE, nome_arquivo)




def obter_bytes(url, nome_arquivo=None, permitir_download=True):
    """Devolve o conteúdo do arquivo, do cache local ou baixando.

    nome_arquivo: como o arquivo se chama dentro de `dados_cvm/`. Se omitido,
    usa o último trecho da URL (que é o nome com que o navegador salva)."""
    nome_arquivo = nome_arquivo or url.rsplit("/", 1)[-1]
    caminho = _caminho_cache(nome_arquivo)

    if os.path.exists(caminho):
        tamanho = os.path.getsize(caminho)
        print(f"  [cache] {nome_arquivo} ({tamanho / 1_000_000:.1f} MB)")
        with open(caminho, "rb") as f:
            return f.read()

    if not permitir_download:
        raise FileNotFoundError(
            f"{caminho} não encontrado e download desabilitado.\n"
            f"Baixe manualmente de {url} e salve em {PASTA_CACHE}/"
        )

    print(f"  [download] {url} ...", end=" ", flush=True)
    conteudo = None
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        if resp.status_code == 404:
            print("não existe (404)")
            return None
        resp.raise_for_status()
        conteudo = resp.content
        print(f"{len(conteudo) / 1_000_000:.1f} MB")
    except requests.exceptions.RequestException as e:
        print(f"falhou ({type(e).__name__})")
        raise SystemExit(
            f"\nNão consegui baixar {nome_arquivo} da CVM.\n"
            f"\nO servidor da CVM bloqueia conexões de datacenter (Codespaces), mesmo\n"
            f"respondendo normalmente ao navegador. Faça assim:\n"
            f"  1. Baixe pelo navegador: {url}\n"
            f"  2. Arraste o arquivo para a pasta {PASTA_CACHE}/ no VS Code\n"
            f"  3. Rode o script de novo\n"
            f"\nPara ver tudo que falta de uma vez: python cvm_fonte.py --listar"
        )

    os.makedirs(PASTA_CACHE, exist_ok=True)
    with open(caminho, "wb") as f:
        f.write(conteudo)
    print(f"  [cache] salvo em {caminho}")
    return conteudo


def obter_zip(url, nome_arquivo=None, permitir_download=True):
    conteudo = obter_bytes(url, nome_arquivo, permitir_download)
    return zipfile.ZipFile(io.BytesIO(conteudo)) if conteudo else None


def obter_dfp(ano, **kw):
    return obter_zip(URL_DFP.format(ano=ano), f"dfp_cia_aberta_{ano}.zip", **kw)


def obter_itr(ano, **kw):
    return obter_zip(URL_ITR.format(ano=ano), f"itr_cia_aberta_{ano}.zip", **kw)


def obter_cadastro(**kw):
    return obter_bytes(URL_CADASTRO, "cad_cia_aberta.csv", **kw)


def listar_necessarios(de=2010, ate=2025, trimestral=True):
    itens = [("cad_cia_aberta.csv", URL_CADASTRO)]
    for ano in range(de, ate + 1):
        itens.append((f"dfp_cia_aberta_{ano}.zip", URL_DFP.format(ano=ano)))
    if trimestral:
        for ano in range(max(de, 2011), ate + 1):
            itens.append((f"itr_cia_aberta_{ano}.zip", URL_ITR.format(ano=ano)))
    return itens


def verificar_arquivos():
    """Confere se os arquivos no cache são válidos de verdade.

    O erro mais comum ao baixar manualmente é o navegador salvar uma página de
    erro HTML com o nome do .zip, ou o download ser interrompido no meio. Nos
    dois casos o arquivo EXISTE (então --listar mostra como presente) mas
    quebra na hora de usar. Esta checagem pega isso antes."""
    if not os.path.isdir(PASTA_CACHE):
        print(f"Pasta {PASTA_CACHE}/ não existe ainda.")
        return

    arquivos = sorted(os.listdir(PASTA_CACHE))
    if not arquivos:
        print(f"Pasta {PASTA_CACHE}/ está vazia.")
        return

    print(f"Verificando {len(arquivos)} arquivos em {PASTA_CACHE}/\n")
    ok, problemas = [], []

    for nome in arquivos:
        caminho = _caminho_cache(nome)
        tamanho = os.path.getsize(caminho)

        if tamanho < 10_000:
            problemas.append((nome, f"muito pequeno ({tamanho} bytes) - provável página de erro"))
            continue

        if nome.endswith(".zip"):
            try:
                with zipfile.ZipFile(caminho) as z:
                    if z.testzip() is not None:
                        problemas.append((nome, "zip corrompido"))
                        continue
                    n = len(z.namelist())
                ok.append((nome, f"{tamanho / 1_000_000:.1f} MB, {n} arquivos internos"))
            except zipfile.BadZipFile:
                problemas.append((nome, "não é um zip válido - download interrompido ou página de erro salva com nome .zip"))
        elif nome.endswith(".csv"):
            with open(caminho, "rb") as f:
                inicio = f.read(200).lower()
            if b"<html" in inicio or b"<!doctype" in inicio:
                problemas.append((nome, "é HTML, não CSV - o navegador salvou uma página de erro"))
            else:
                ok.append((nome, f"{tamanho / 1_000_000:.1f} MB"))
        else:
            ok.append((nome, f"{tamanho / 1_000_000:.1f} MB (tipo não verificado)"))

    for nome, info in ok:
        print(f"  OK       {nome:<35} {info}")
    for nome, motivo in problemas:
        print(f"  PROBLEMA {nome:<35} {motivo}")

    print()
    if problemas:
        print(f"{len(problemas)} arquivo(s) com problema - baixe de novo antes de rodar o ingestor.")
    else:
        print(f"Todos os {len(ok)} arquivos estão íntegros.")


def main():
    p = argparse.ArgumentParser(description="Gerencia o cache local de arquivos da CVM.")
    p.add_argument("--listar", action="store_true", help="Lista os arquivos necessários e o que já está no cache.")
    p.add_argument("--verificar", action="store_true", help="Confere se os arquivos baixados são válidos (não corrompidos).")
    p.add_argument("--de", type=int, default=2010)
    p.add_argument("--ate", type=int, default=2025)
    p.add_argument("--sem-trimestral", action="store_true")
    args = p.parse_args()

    if args.verificar:
        verificar_arquivos()
        return

    itens = listar_necessarios(args.de, args.ate, not args.sem_trimestral)
    presentes = [n for n, _ in itens if os.path.exists(_caminho_cache(n))]
    faltando = [(n, u) for n, u in itens if not os.path.exists(_caminho_cache(n))]

    print(f"Cache em ./{PASTA_CACHE}/")
    print(f"  {len(presentes)} de {len(itens)} arquivos presentes\n")

    if faltando:
        print(f"FALTANDO ({len(faltando)}) - baixe pelo navegador e coloque em {PASTA_CACHE}/:\n")
        for nome, url in faltando:
            print(f"  {url}")
        print(f"\nDica: dá para baixar vários de uma vez colando essas URLs no navegador,")
        print(f"ou usando o gerenciador de downloads. Depois arraste todos para a pasta")
        print(f"{PASTA_CACHE}/ no VS Code.")
    else:
        print("Tudo presente - pode rodar os scripts da CVM.")


if __name__ == "__main__":
    main()
