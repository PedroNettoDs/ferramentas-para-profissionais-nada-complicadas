#!/usr/bin/env python3
"""Gera descrição de YouTube a partir de um projeto já processado pelo editar_video.py.

Uso:
    python3 gerar_descricao.py CAMINHO_DA_PASTA_DO_PROJETO
    python3 gerar_descricao.py CAMINHO_DA_PASTA_DO_PROJETO --llm gemma4:12b

CAMINHO_DA_PASTA_DO_PROJETO é a pasta em ~/Vídeos/Projetos/<slug>/ já criada
pelo editar_video.py, contendo transcricao.json e plano_edicao.json.

O que faz:
    1. Lê a transcrição e o plano de edição do projeto.
    2. Pede pro Ollama local um parágrafo de descrição + hashtags, no tom do
       canal Mister Netto.
    3. Monta a lista de capítulos a partir de cortes_manter, recalculando os
       timestamps na timeline EDITADA (não os tempos originais do vídeo-fonte).
    4. Junta tudo com uma linha de créditos e salva em descricao.txt dentro
       da pasta do projeto.

Não depende de nada fora da biblioteca padrão (Ollama é chamado via HTTP com
urllib), então não cria .env temporário.

Requer: Ollama rodando local (localhost:11434).
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/generate"

ESTILO_MISTER_NETTO = """\
Você escreve descrições de vídeo pro canal "Mister Netto" no YouTube.
Tom: irreverente, direto, sem papo corporativo, sem clichê de "nesse vídeo
você vai aprender". Fale como alguém de verdade, não como um robô de
marketing.
"""


def perguntar_ollama_descricao(nome_projeto: str, transcricao: list[dict], llm: str) -> dict:
    texto_completo = " ".join(s["texto"] for s in transcricao)
    prompt = f"""{ESTILO_MISTER_NETTO}

Assunto do vídeo: {nome_projeto}

Transcrição completa do vídeo:

{texto_completo}

Responda SOMENTE com um JSON válido (sem markdown, sem texto fora do JSON) no formato:
{{
  "descricao": "parágrafo curto e engajante descrevendo o vídeo, em português do Brasil",
  "hashtags": ["#exemplo1", "#exemplo2"]
}}"""

    body = json.dumps({"model": llm, "prompt": prompt, "format": "json", "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        resposta = json.loads(resp.read())
    return json.loads(resposta["response"])


def _ts(segundos: float) -> str:
    segundos = round(segundos)
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def gerar_capitulos(cortes: list[dict]) -> list[str]:
    capitulos = []
    cursor = 0.0
    for corte in cortes:
        dur = corte["end"] - corte["start"]
        if dur <= 0:
            continue
        titulo = corte.get("motivo") or "Trecho"
        capitulos.append(f"{_ts(cursor)} {titulo}")
        cursor += dur
    return capitulos


def nome_do_autor() -> str:
    try:
        r = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, check=True)
        nome = r.stdout.strip()
        return nome or "Mister Netto"
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        return "Mister Netto"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pasta_projeto",
                    help="pasta do projeto (~/Vídeos/Projetos/<slug>/) com transcricao.json e "
                         "plano_edicao.json já gerados pelo editar_video.py")
    p.add_argument("--llm", default="gemma4:12b", help="modelo Ollama (padrão: gemma4:12b)")
    args = p.parse_args()

    pasta = Path(args.pasta_projeto).expanduser().resolve()
    caminho_transcricao = pasta / "transcricao.json"
    caminho_plano = pasta / "plano_edicao.json"
    if not caminho_transcricao.is_file() or not caminho_plano.is_file():
        sys.exit(f"Pasta de projeto inválida, não achei transcricao.json/plano_edicao.json em: {pasta}")

    transcricao = json.loads(caminho_transcricao.read_text(encoding="utf-8"))
    plano = json.loads(caminho_plano.read_text(encoding="utf-8"))
    nome_projeto = plano.get("nome_projeto", pasta.name)

    print(f"Pedindo descrição ao Ollama ({args.llm})...", file=sys.stderr)
    try:
        resultado = perguntar_ollama_descricao(nome_projeto, transcricao, args.llm)
        descricao = resultado.get("descricao", "").strip()
        hashtags = resultado.get("hashtags") or []
    except Exception as e:
        print(f"Ollama falhou ({e}), pulando descrição gerada por IA.", file=sys.stderr)
        descricao = ""
        hashtags = []

    cortes = plano.get("cortes_manter") or []
    capitulos = gerar_capitulos(cortes)

    autor = nome_do_autor()
    linha_creditos = f"Vídeo editado por {autor} — canal Mister Netto."

    partes = []
    if descricao:
        partes.append(descricao)
    if capitulos:
        partes.append("\n".join(capitulos))
    if hashtags:
        partes.append(" ".join(hashtags))
    partes.append(linha_creditos)

    caminho_descricao = pasta / "descricao.txt"
    caminho_descricao.write_text("\n\n".join(partes) + "\n", encoding="utf-8")
    print(f"Descrição salva em: {caminho_descricao}")


if __name__ == "__main__":
    main()
