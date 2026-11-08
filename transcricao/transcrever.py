#!/usr/bin/env python3
"""Transcreve áudio ou vídeo localmente com faster-whisper (sem enviar nada pra internet).

Uso:
    python3 transcrever.py ARQUIVO
    python3 transcrever.py ARQUIVO -m medium -l pt
    python3 transcrever.py ARQUIVO -o saida.txt --formato srt

O script decide o que fazer com base na extensão do arquivo:
    - vídeo (.mp4, .mkv, .mov, .webm, .avi, .flv, .wmv, .m4v):
      extrai o áudio com ffmpeg para um .wav temporário e transcreve esse .wav.
    - áudio (.mp3, .wav, .m4a, .ogg, .flac, .aac, .wma, .opus):
      transcreve o arquivo diretamente.
    - qualquer outra extensão: erro.

A cada execução o script cria um ambiente .env temporário ao lado dele,
instala as dependências (faster-whisper) e apaga o .env no final.
Requer: python3-venv e ffmpeg.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent / ".env"
DEPENDENCIAS = ["faster-whisper"]

EXTENSOES_VIDEO = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv", ".m4v"}
EXTENSOES_AUDIO = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus"}


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


def extrair_audio(video: Path, destino_wav: Path):
    """Extrai o áudio do vídeo para um .wav 16kHz mono (formato ideal pro whisper)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(destino_wav)]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(r.returncode)


def formatar_timestamp(segundos: float) -> str:
    ms = round(segundos * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def escrever_saida(segments, formato: str, caminho: Path):
    with open(caminho, "w", encoding="utf-8") as f:
        if formato == "txt":
            for seg in segments:
                f.write(seg.text.strip() + "\n")
        elif formato == "srt":
            for i, seg in enumerate(segments, start=1):
                f.write(f"{i}\n")
                f.write(f"{formatar_timestamp(seg.start)} --> {formatar_timestamp(seg.end)}\n")
                f.write(seg.text.strip() + "\n\n")
        elif formato == "vtt":
            f.write("WEBVTT\n\n")
            for seg in segments:
                inicio = formatar_timestamp(seg.start).replace(",", ".")
                fim = formatar_timestamp(seg.end).replace(",", ".")
                f.write(f"{inicio} --> {fim}\n")
                f.write(seg.text.strip() + "\n\n")


def main():
    p = argparse.ArgumentParser(description="Transcrever áudio ou vídeo localmente com whisper")
    p.add_argument("arquivo", help="arquivo de áudio ou vídeo a transcrever")
    p.add_argument("-o", "--saida", help="arquivo de saída (padrão: ao lado do arquivo original)")
    p.add_argument("-m", "--modelo", default="small",
                   help="modelo whisper: tiny, base, small, medium, large-v3 (padrão: small)")
    p.add_argument("-l", "--idioma", default=None,
                   help="código do idioma, ex.: pt, en (padrão: detectar automaticamente)")
    p.add_argument("--formato", choices=["txt", "srt", "vtt"], default="txt",
                   help="formato da saída (padrão: txt)")
    p.add_argument("--dispositivo", choices=["cpu", "cuda", "auto"], default="auto",
                   help="dispositivo de inferência (padrão: auto)")
    args = p.parse_args()

    arquivo = Path(args.arquivo).expanduser()
    if not arquivo.is_file():
        sys.exit(f"Arquivo não encontrado: {arquivo}")

    extensao = arquivo.suffix.lower()
    saida = Path(args.saida).expanduser() if args.saida else arquivo.with_suffix(f".{args.formato}")

    audio_temporario = None
    try:
        if extensao in EXTENSOES_VIDEO:
            if not shutil.which("ffmpeg"):
                sys.exit("ffmpeg não encontrado. Rode: sudo apt install ffmpeg")
            print("Vídeo detectado, extraindo áudio com ffmpeg...", file=sys.stderr)
            audio_temporario = Path(tempfile.mkstemp(suffix=".wav")[1])
            extrair_audio(arquivo, audio_temporario)
            caminho_audio = audio_temporario
        elif extensao in EXTENSOES_AUDIO:
            print("Áudio detectado, transcrevendo diretamente...", file=sys.stderr)
            caminho_audio = arquivo
        else:
            sys.exit(f"Extensão não suportada: {extensao}\n"
                      f"Vídeo: {sorted(EXTENSOES_VIDEO)}\nÁudio: {sorted(EXTENSOES_AUDIO)}")

        from faster_whisper import WhisperModel

        print(f"Carregando modelo '{args.modelo}'...", file=sys.stderr)
        compute_type = "float16" if args.dispositivo == "cuda" else "int8"
        modelo = WhisperModel(args.modelo, device=args.dispositivo, compute_type=compute_type)

        print("Transcrevendo...", file=sys.stderr)
        segments, info = modelo.transcribe(str(caminho_audio), language=args.idioma)
        print(f"Idioma detectado: {info.language} (confiança {info.language_probability:.2f})",
              file=sys.stderr)

        segments = list(segments)
        escrever_saida(segments, args.formato, saida)
        print(f"Transcrição salva em: {saida}")
    finally:
        if audio_temporario is not None:
            audio_temporario.unlink(missing_ok=True)


if __name__ == "__main__":
    executar_em_env_temporario()
    main()
