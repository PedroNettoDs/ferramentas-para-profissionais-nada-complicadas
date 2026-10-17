#!/usr/bin/env python3
"""Baixa vídeos do Pornhub usando o título como nome do arquivo.

Uso:
    python3 baixar_vid.py URL [URL ...]
    python3 baixar_vid.py -o pasta_destino URL
    python3 baixar_vid.py -f lista.txt        # uma URL por linha

A cada execução o script cria um ambiente .env temporário ao lado dele,
instala as dependências (yt-dlp, curl_cffi) e apaga o .env no final.
Requer: python3-venv e ffmpeg (para juntar áudio/vídeo).
"""
import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent / ".env"
DEPENDENCIAS = ["yt-dlp", "curl_cffi"]


def executar_em_env_temporario():
    """Roda o script dentro de um .env criado agora e removido ao terminar."""
    if os.environ.get("SCRIPT_ENV_ATIVO") == "1":
        return
    code = 1
    try:
        shutil.rmtree(ENV_DIR, ignore_errors=True)
        print("Criando .env temporário...", file=sys.stderr)
        venv.create(ENV_DIR, with_pip=True)
        py = ENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "python"
        subprocess.run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                        *DEPENDENCIAS], check=True)
        r = subprocess.run([py, str(Path(__file__).resolve()), *sys.argv[1:]],
                           env={**os.environ, "SCRIPT_ENV_ATIVO": "1"})
        code = r.returncode
    except KeyboardInterrupt:
        code = 130
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Falha ao preparar o .env: {e}", file=sys.stderr)
    finally:
        shutil.rmtree(ENV_DIR, ignore_errors=True)
    sys.exit(code)


def main():
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget

    p = argparse.ArgumentParser(description="Baixar vídeos do Pornhub pelo título")
    p.add_argument("urls", nargs="*", help="URLs dos vídeos")
    p.add_argument("-f", "--file", help="arquivo com uma URL por linha")
    p.add_argument("-o", "--output", default=".", help="pasta de destino (padrão: atual)")
    p.add_argument("-q", "--quality", type=int, default=None,
                   help="altura máxima, ex.: 720 (padrão: melhor disponível)")
    args = p.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            urls += [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    if not urls:
        p.error("informe ao menos uma URL")

    fmt = "bv*+ba/b"
    if args.quality:
        fmt = f"bv*[height<={args.quality}]+ba/b[height<={args.quality}]/b"

    opts = {
        "format": fmt,
        # Título como nome do arquivo; restrictfilenames=False mantém acentos/espaços
        "outtmpl": f"{args.output}/%(title)s.%(ext)s",
        "restrictfilenames": False,
        "windowsfilenames": True,   # remove caracteres inválidos (/ : * ? etc.)
        "merge_output_format": "mp4",
        "noplaylist": True,
        "continuedl": True,
        "retries": 5,
        # Sem isso o site responde 403; precisa do pacote curl_cffi
        "impersonate": ImpersonateTarget("chrome"),
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        code = ydl.download(urls)
    sys.exit(code)


if __name__ == "__main__":
    executar_em_env_temporario()
    main()
