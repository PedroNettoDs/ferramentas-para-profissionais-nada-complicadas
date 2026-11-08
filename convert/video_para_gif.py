#!/usr/bin/env python3
"""Converte um vídeo em GIF usando ffmpeg (com paleta otimizada).

Uso:
    python3 video_para_gif.py VIDEO
    python3 video_para_gif.py VIDEO -o saida.gif --fps 12 --largura 640
    python3 video_para_gif.py VIDEO --inicio 5 --duracao 10

Por padrão o GIF é salvo ao lado do vídeo, com o mesmo nome e extensão .gif.
A cada execução o script cria um ambiente .env temporário ao lado dele e
apaga o .env no final.
Requer: python3-venv e ffmpeg.
"""
import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent / ".env"
DEPENDENCIAS = []  # só usa a biblioteca padrão e o ffmpeg do sistema


def executar_em_env_temporario():
    """Roda o script dentro de um .env criado agora e removido ao terminar."""
    if os.environ.get("SCRIPT_ENV_ATIVO") == "1":
        return
    code = 1
    try:
        shutil.rmtree(ENV_DIR, ignore_errors=True)
        print("Criando .env temporário...", file=sys.stderr)
        venv.create(ENV_DIR, with_pip=bool(DEPENDENCIAS))
        py = ENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "python"
        if DEPENDENCIAS:
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
    p = argparse.ArgumentParser(description="Converter vídeo em GIF")
    p.add_argument("video", help="arquivo de vídeo de entrada")
    p.add_argument("-o", "--saida", help="arquivo .gif de saída")
    p.add_argument("--fps", type=int, default=15, help="quadros por segundo (padrão: 15)")
    p.add_argument("--largura", type=int, default=720,
                   help="largura em pixels, 0 mantém o original (padrão: 720)")
    p.add_argument("--inicio", type=float, help="segundo inicial do trecho")
    p.add_argument("--duracao", type=float, help="duração do trecho em segundos")
    p.add_argument("--sem-loop", action="store_true", help="toca só uma vez")
    args = p.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg não encontrado. Rode: sudo apt install ffmpeg")

    video = Path(args.video).expanduser()
    if not video.is_file():
        sys.exit(f"Arquivo não encontrado: {video}")
    saida = Path(args.saida).expanduser() if args.saida else video.with_suffix(".gif")

    escala = f",scale={args.largura}:-1:flags=lanczos" if args.largura > 0 else ""
    filtro = (
        f"fps={args.fps}{escala},split[a][b];"
        "[a]palettegen=stats_mode=diff[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    if args.inicio is not None:
        cmd += ["-ss", str(args.inicio)]
    if args.duracao is not None:
        cmd += ["-t", str(args.duracao)]
    cmd += ["-i", str(video), "-an", "-vf", filtro,
            "-loop", "-1" if args.sem_loop else "0", str(saida)]

    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(r.returncode)
    print(f"GIF salvo em: {saida} ({saida.stat().st_size / 1_048_576:.1f} MB)")


if __name__ == "__main__":
    executar_em_env_temporario()
    main()
