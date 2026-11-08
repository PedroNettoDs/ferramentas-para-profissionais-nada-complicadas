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
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent / ".env"
DEPENDENCIAS = ["yt-dlp", "curl_cffi<0.17,>=0.10"]  # yt-dlp só suporta curl_cffi 0.5.10 ou 0.10.x-0.16.x


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


NAVEGADORES_SUPORTADOS = ("chrome", "firefox", "edge", "brave", "opera", "vivaldi", "safari")


def perguntar_navegador_cookies():
    """Pergunta no terminal, por menu numerado, se deve usar cookies de algum navegador."""
    if not sys.stdin.isatty():
        return None
    resp = input("Usar cookies de login do navegador para evitar bloqueio do YouTube? [s/N]: ").strip().lower()
    if resp not in ("s", "sim", "y", "yes"):
        return None
    print("Escolha o navegador:")
    for i, nav in enumerate(NAVEGADORES_SUPORTADOS, 1):
        print(f"  {i}) {nav}")
    escolha = input(f"Número [1-{len(NAVEGADORES_SUPORTADOS)}]: ").strip()
    if escolha.isdigit() and 1 <= int(escolha) <= len(NAVEGADORES_SUPORTADOS):
        return NAVEGADORES_SUPORTADOS[int(escolha) - 1]
    print("Opção inválida, seguindo sem cookies.", file=sys.stderr)
    return None


def perguntar_tor():
    """Pergunta no terminal se o download deve passar pela rede Tor (anonimizar)."""
    if not sys.stdin.isatty():
        return False
    resp = input("Anonimizar o download via Tor? [s/N]: ").strip().lower()
    return resp in ("s", "sim", "y", "yes")


TOR_SOCKS_PORT = 9050


def _porta_em_uso(porta):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", porta)) == 0


@contextlib.contextmanager
def rede_tor():
    """Usa um Tor já ativo na porta 9050 (ex.: serviço do sistema) ou sobe um processo
    próprio, espera o bootstrap e encerra ao sair."""
    if not shutil.which("tor"):
        print("tor não está instalado. Rode: sudo apt install -y tor", file=sys.stderr)
        sys.exit(1)

    if _porta_em_uso(TOR_SOCKS_PORT):
        print(f"Tor já ativo na porta {TOR_SOCKS_PORT}, reaproveitando.", file=sys.stderr)
        yield f"socks5://127.0.0.1:{TOR_SOCKS_PORT}"
        print("Conexão Tor (do sistema) mantida como estava.", file=sys.stderr)
        return

    data_dir = tempfile.mkdtemp(prefix="tor_data_")
    proc = subprocess.Popen(
        ["tor", "--SocksPort", str(TOR_SOCKS_PORT), "--DataDirectory", data_dir,
         "--ControlPort", "0", "--Log", "notice stdout"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        print("Iniciando rede Tor...", file=sys.stderr)
        for linha in proc.stdout:
            if "Bootstrapped 100%" in linha:
                print("Tor conectado.", file=sys.stderr)
                break
            if proc.poll() is not None:
                raise RuntimeError("tor encerrou antes de conectar")
        else:
            raise RuntimeError("tor encerrou antes de conectar")
        yield f"socks5://127.0.0.1:{TOR_SOCKS_PORT}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)
        print("Conexão Tor encerrada.", file=sys.stderr)


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

    navegador = perguntar_navegador_cookies()
    usar_tor = perguntar_tor()

    def montar_opts(proxy=None):
        return {
            **({"cookiesfrombrowser": (navegador,)} if navegador else {}),
            **({"proxy": proxy} if proxy else {}),
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

    if usar_tor:
        with rede_tor() as proxy, yt_dlp.YoutubeDL(montar_opts(proxy)) as ydl:
            code = ydl.download(urls)
    else:
        with yt_dlp.YoutubeDL(montar_opts()) as ydl:
            code = ydl.download(urls)
    sys.exit(code)


if __name__ == "__main__":
    executar_em_env_temporario()
    main()
