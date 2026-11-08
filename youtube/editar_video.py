#!/usr/bin/env python3
"""Pipeline de pré-edição com IA local pro Filmcraft (ArtCraft).

Uso:
    python3 editar_video.py CAMINHO_DO_VIDEO
    python3 editar_video.py CAMINHO_DO_VIDEO --whisper medium --llm gemma4:12b

O que faz:
    1. Transcreve o vídeo localmente (faster-whisper).
    2. Manda a transcrição pra um LLM local (Ollama) com o estilo de edição do
       canal Mister Netto, pedindo um plano de corte (jump cuts) e sugestões
       de zoom/legenda/sfx/trilha.
    3. Cria Videos/Projetos/<nome extraído>/ e vai salvando o progresso:
       transcricao.json, plano_edicao.json, legendas.srt, notas_edicao.txt.
    4. Gera projeto.fcproj (Filmcraft) com os jump cuts já montados na timeline,
       a partir de um modelo otimizado (tracks e configs padrão).
    5. Abre/organiza a janela do Filmcraft no monitor Samsung e troca pra aba
       Importar. NÃO renderiza nada — isso fica pra você revisar e aprovar.

Requer: ffmpeg, ffprobe, Ollama rodando local (localhost:11434), xdotool/wmctrl
(opcional, só pra organizar a janela).
A cada execução cria um .env temporário ao lado do script (faster-whisper) e
apaga no final, igual transcrever.py.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
import venv
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent / ".env"
DEPENDENCIAS = ["faster-whisper"]

TICKS_POR_SEGUNDO = 254016000000
PASTA_PROJETOS = Path.home() / "Vídeos" / "Projetos"
MODELO_OTIMIZADO = Path(__file__).resolve().parent / "filmcraft_modelo_otimizado.json"
OLLAMA_URL = "http://localhost:11434/api/generate"

ESTILO_MISTER_NETTO = """\
Atue como editor de vídeo sênior focado em conteúdo autêntico para YouTube.
Edite seguindo a identidade do canal "Mister Netto":

1. ESTRUTURA E RITMO:
- Corte seco (jump cuts) em respiros e silêncios desnecessários, mantendo a
  conversa fluida e dinâmica.
- Comece o vídeo direto na ação ou na premissa principal, sem introduções
  genéricas.

2. TRATAMENTO VISUAL:
- Punch-in zoom (aproximação rápida de câmera) em momentos de quebra de
  expectativa, frustração cômica ou reações espontâneas.
- Legendas dinâmicas apenas em palavras-chave ou frases de impacto/punchlines.

3. DESIGN DE ÁUDIO E HUMOR:
- Voz natural e conversacional, tom cru e autêntico.
- Efeitos sonoros pontuais e sutis pra valorizar humor irônico/sarcástico.
- Música de fundo sutil: descontraída/cômica em gameplay, lo-fi/atmosférica
  em momentos reflexivos.
"""


def executar_em_env_temporario():
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


def slugify(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^a-zA-Z0-9]+", "-", texto).strip("-").lower()
    return texto or "projeto-sem-nome"


def seg_para_ticks(segundos: float) -> int:
    return round(segundos * TICKS_POR_SEGUNDO)


def extrair_audio(video: Path, destino_wav: Path):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(destino_wav)]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(r.returncode)


def _ler_wav_mono16k(caminho: Path):
    """Lê um .wav PCM 16-bit mono 16kHz como array float32, sem depender do PyAV
    (faster-whisper usa PyAV pra decodificar arquivos, e a versão instalada do av
    não é compatível com a API que faster-whisper espera)."""
    import numpy as np
    import wave

    with wave.open(str(caminho), "rb") as w:
        dados = w.readframes(w.getnframes())
    audio = np.frombuffer(dados, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def transcrever(video: Path, modelo: str, idioma: str | None):
    from faster_whisper import WhisperModel

    audio_tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        print("Extraindo áudio...", file=sys.stderr)
        extrair_audio(video, audio_tmp)
        print(f"Carregando modelo whisper '{modelo}'...", file=sys.stderr)
        m = WhisperModel(modelo, device="cpu", compute_type="int8")
        print("Transcrevendo...", file=sys.stderr)
        audio_array = _ler_wav_mono16k(audio_tmp)
        segments, info = m.transcribe(audio_array, language=idioma)
        segments = list(segments)
        print(f"Idioma: {info.language} (confiança {info.language_probability:.2f})",
              file=sys.stderr)
        return [{"start": s.start, "end": s.end, "texto": s.text.strip()} for s in segments]
    finally:
        audio_tmp.unlink(missing_ok=True)


def ffprobe_info(video: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(video)], capture_output=True, text=True, check=True)
    dados = json.loads(r.stdout)
    video_s = next(s for s in dados["streams"] if s["codec_type"] == "video")
    audio_s = next((s for s in dados["streams"] if s["codec_type"] == "audio"), None)
    fmt = dados["format"]
    num, den = (video_s.get("r_frame_rate") or "30/1").split("/")
    return {
        "width": int(video_s["width"]),
        "height": int(video_s["height"]),
        "fps_num": int(num),
        "fps_den": int(den) or 1,
        "codec": video_s.get("codec_name", "unknown").upper(),
        "pixel_format": video_s.get("pix_fmt", "yuv420p"),
        "bitrate": int(fmt.get("bit_rate", video_s.get("bit_rate", 0)) or 0),
        "duration_seg": float(fmt["duration"]),
        "container": fmt.get("format_name", "MPEG-4"),
        "file_size": int(fmt.get("size", 0)),
        "audio_sample_rate": int(audio_s["sample_rate"]) if audio_s else 48000,
        "audio_channels": int(audio_s["channels"]) if audio_s else 2,
        "audio_codec": (audio_s.get("codec_name", "AAC").upper() if audio_s else "AAC"),
    }


def perguntar_ollama(transcricao: list[dict], llm: str) -> dict:
    linhas = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['texto']}" for s in transcricao)
    prompt = f"""{ESTILO_MISTER_NETTO}

Aqui está a transcrição do vídeo, com timestamps em segundos:

{linhas}

Responda SOMENTE com um JSON válido (sem markdown, sem texto fora do JSON) no formato:
{{
  "nome_projeto": "nome curto (2-4 palavras) que resume o assunto do vídeo",
  "cortes_manter": [{{"start": 0.0, "end": 10.5, "motivo": "..."}}],
  "momentos_zoom": [{{"tempo": 12.3, "motivo": "punch-in na reação de surpresa"}}],
  "legendas_sugeridas": [{{"start": 5.0, "end": 6.0, "texto": "PALAVRA DE IMPACTO"}}],
  "sfx_sugeridos": [{{"tempo": 20.0, "motivo": "som seco pontuando a piada"}}],
  "trilha_sugerida": "descrição curta do estilo de trilha de fundo"
}}

Regras pra cortes_manter: cubra os trechos que DEVEM ficar no vídeo final (corte
os respiros/silêncios/redundâncias), em ordem crescente, sem sobreposição,
usando os tempos exatos da transcrição acima."""

    body = json.dumps({"model": llm, "prompt": prompt, "format": "json", "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        resposta = json.loads(resp.read())
    plano = json.loads(resposta["response"])
    return plano


# ---------- geração do .fcproj (Filmcraft) ----------

def _efeitos_video_padrao(largura: float, altura: float) -> list:
    cx, cy = largura / 2, altura / 2
    return [
        {"effect": "motion", "enabled": True, "params": {
            "anchor": {"value": {"Vec2": {"x": cx, "y": cy}}},
            "anti_flicker": {"value": {"Float": 0.0}},
            "position": {"value": {"Vec2": {"x": cx, "y": cy}}},
            "rotation": {"value": {"Float": 0.0}},
            "scale": {"value": {"Float": 100.0}},
            "scale_width": {"value": {"Float": 100.0}},
            "uniform_scale": {"value": {"Bool": True}}}},
        {"effect": "opacity", "enabled": True, "params": {
            "blend": {"value": {"Choice": 0}}, "opacity": {"value": {"Float": 100.0}}}},
        {"effect": "time_remap", "enabled": True, "params": {
            "speed": {"value": {"Float": 100.0}}}},
    ]


def _efeitos_audio_padrao() -> list:
    return [
        {"effect": "volume", "enabled": True, "params": {
            "bypass": {"value": {"Bool": False}}, "level": {"value": {"Float": 0.0}}}},
        {"effect": "channel_volume", "enabled": True, "params": {
            "bypass": {"value": {"Bool": False}},
            "left": {"value": {"Float": 0.0}}, "right": {"value": {"Float": 0.0}}}},
        {"effect": "panner", "enabled": True, "params": {"balance": {"value": {"Float": 0.0}}}},
        {"effect": "denoise", "enabled": True, "params": {"amount": {"value": {"Float": 100.0}}},
         "essential": True},
    ]


def _track_vazia(tid: int, kind: str, nome: str) -> dict:
    return {"id": tid, "kind": kind, "name": nome, "items": [], "transitions": [], "locked": False,
            "sync_lock": True, "enabled": True, "muted": False, "solo": False, "channels": "Stereo",
            "volume_db": 0.0, "pan": 0.0, "effects": [],
            "mixer": {"mode": "Read", "output": None, "record_arm": False, "solo_safe": False,
                      "input_map": "Stereo"}}


def gerar_fcproj(nome_projeto: str, video: Path, probe: dict, cortes: list[dict]) -> dict:
    largura, altura = probe["width"], probe["height"]
    duracao_ticks = seg_para_ticks(probe["duration_seg"])

    media_item = {
        "id": 1, "name": video.name, "label": "Iris",
        "kind": {"Media": {
            "media": {"File": {"path": str(video)}},
            "info": {
                "name": video.name, "kind": "Movie", "duration": duracao_ticks,
                "video": {"width": largura, "height": altura,
                          "frame_rate": {"num": probe["fps_num"], "den": probe["fps_den"]},
                          "par": [1, 1], "codec": probe["codec"], "pixel_format": probe["pixel_format"],
                          "color": {"matrix": "Bt709", "transfer": "Bt709", "primaries": "Bt709",
                                    "range": "Limited"},
                          "has_alpha": False, "bitrate": probe["bitrate"]},
                "audio_streams": [{"sample_rate": probe["audio_sample_rate"],
                                    "channels": probe["audio_channels"],
                                    "codec": probe["audio_codec"], "bits_per_sample": None}],
                "container": probe["container"], "start_timecode": None,
                "file_size": probe["file_size"]},
            "interpret": {"frame_rate": None, "par": None, "ignore_alpha": False,
                          "invert_alpha": False},
            "mark_in": None, "mark_out": None, "markers": [], "offline": False, "proxy": None,
            "identity": {"size": probe["file_size"],
                         "fingerprint": hashlib.sha1(
                             f"{video}{probe['file_size']}".encode()).hexdigest()[:16]}}},
        "metadata": {}, "created": 1,
    }

    v1_items, a1_items = [], []
    cursor = 0
    next_id = 9
    for corte in cortes:
        dur = seg_para_ticks(corte["end"] - corte["start"])
        if dur <= 0:
            continue
        origem = seg_para_ticks(corte["start"])
        link_id = next_id
        vid_id = next_id + 1
        aud_id = next_id + 2
        next_id += 3
        v1_items.append({
            "id": vid_id, "item": 1, "name": video.name, "label": "Iris", "start": cursor,
            "duration": dur, "source_in": origem, "speed": 1.0, "reverse": False, "enabled": True,
            "link": link_id, "group": None, "effects": _efeitos_video_padrao(largura, altura),
            "markers": [], "gain_db": 0.0, "frame_hold": None, "scale_to_frame": False})
        a1_items.append({
            "id": aud_id, "item": 1, "name": video.name, "label": "Iris", "start": cursor,
            "duration": dur, "source_in": origem, "speed": 1.0, "reverse": False, "enabled": True,
            "link": link_id, "group": None, "effects": _efeitos_audio_padrao(),
            "markers": [], "gain_db": 0.0, "frame_hold": None, "scale_to_frame": False})
        cursor += dur

    v1 = _track_vazia(2, "Video", "Video 1")
    v1["items"] = v1_items
    a1 = _track_vazia(5, "Audio", "Audio 1")
    a1["items"] = a1_items

    sequence_item = {
        "id": 8, "name": "Sequence 01", "label": "Forest",
        "kind": {"Sequence": {
            "settings": {
                "width": largura, "height": altura,
                "frame_rate": {"num": probe["fps_num"], "den": probe["fps_den"]}, "par": [1, 1],
                "sample_rate": probe["audio_sample_rate"], "drop_frame": False,
                "preset": f"{largura}x{altura} {probe['fps_num']}",
                "audio_master": "Stereo", "preview_codec": "ProRes 422", "max_bit_depth": False,
                "max_render_quality": False, "working_space": "Rec. 709",
                "color": {"working": "Rec709", "wide_gamut": False, "auto_tone_map": True}},
            "video_tracks": [v1, _track_vazia(3, "Video", "Video 2"),
                              _track_vazia(4, "Video", "Video 3")],
            "audio_tracks": [a1, _track_vazia(6, "Audio", "Audio 2"),
                               _track_vazia(7, "Audio", "Audio 3")],
            "markers": [], "mark_in": None, "mark_out": None, "work_area": None,
            "start_timecode": 0, "master_volume_db": 0.0, "master_effects": [],
            "master_mixer": {"mode": "Read", "output": None, "record_arm": False,
                              "solo_safe": False, "input_map": "Stereo"},
            "submix_tracks": [], "caption_tracks": []}},
        "metadata": {}, "created": 8,
    }

    return {
        "format": "filmcraft.project", "schema_version": 15, "generator": "editar_video.py",
        "project": {
            "name": nome_projeto,
            "settings": {
                "renderer": "FilmCraft GPU Acceleration (wgpu)", "video_display": "Timecode",
                "audio_display_samples": True, "default_still_duration": 1270080000000,
                "default_transition_duration_frames": 24,
                "default_audio_transition_duration": 254016000000,
                "ingest": {"enabled": False, "action": "createProxies", "destination": None,
                           "preset": ""},
                "title_safe": [20.0, 20.0], "action_safe": [10.0, 10.0], "capture_format": "DV",
                "scratch": {"captured": None, "videoPreviews": None, "audioPreviews": None,
                            "autoSave": None}},
            "root": {"id": 0, "name": nome_projeto, "children": [{"Item": 1}, {"Item": 8}]},
            "items": {"1": media_item, "8": sequence_item}, "next_id": next_id}}


def salvar_modelo_otimizado(probe: dict):
    """Salva/atualiza o modelo de projeto otimizado (settings padrão, sem mídia)."""
    modelo = gerar_fcproj("Modelo Otimizado", Path("placeholder.mp4"), probe, [])
    MODELO_OTIMIZADO.write_text(json.dumps(modelo, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Modelo otimizado salvo em: {MODELO_OTIMIZADO}", file=sys.stderr)


def escrever_srt(legendas: list[dict], caminho: Path):
    def ts(segundos: float) -> str:
        ms = round(segundos * 1000)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(caminho, "w", encoding="utf-8") as f:
        for i, leg in enumerate(legendas, start=1):
            f.write(f"{i}\n{ts(leg['start'])} --> {ts(leg['end'])}\n{leg['texto']}\n\n")


def escrever_notas(plano: dict, caminho: Path):
    with open(caminho, "w", encoding="utf-8") as f:
        f.write("Momentos de punch-in zoom sugeridos:\n")
        for m in plano.get("momentos_zoom", []):
            f.write(f"  {m['tempo']:.1f}s — {m['motivo']}\n")
        f.write("\nEfeitos sonoros sugeridos:\n")
        for s in plano.get("sfx_sugeridos", []):
            f.write(f"  {s['tempo']:.1f}s — {s['motivo']}\n")
        f.write(f"\nTrilha sugerida: {plano.get('trilha_sugerida', '')}\n")
        f.write("\nAplique esses pontos manualmente no Filmcraft (zoom via efeito Motion,\n"
                "sfx/trilha arrastando pra Audio 2/3). Legendas já vêm em legendas.srt,\n"
                "importe pelo painel Texto.\n")


# ---------- janela do Filmcraft ----------

def organizar_janela_filmcraft(caminho_fcproj: Path):
    if not (shutil.which("xdotool") and shutil.which("wmctrl")):
        print("xdotool/wmctrl ausentes, pulando organização de janela.", file=sys.stderr)
        return
    r = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True)
    janela = next((l.split()[0] for l in r.stdout.splitlines() if "FilmCraft" in l), None)
    if not janela:
        print("Filmcraft não está aberto, abrindo...", file=sys.stderr)
        subprocess.Popen(["flatpak", "run", "ai.storyteller.filmcraft"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sleep", "8"])
        r = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True)
        janela = next((l.split()[0] for l in r.stdout.splitlines() if "FilmCraft" in l), None)
    if not janela:
        print("Não achei a janela do Filmcraft, abra manualmente.", file=sys.stderr)
        return
    subprocess.run(["xdotool", "windowsize", janela, "1060", "980"])
    subprocess.run(["xdotool", "windowmove", janela, "20", "940"])
    subprocess.run(["xdotool", "windowactivate", janela])
    print("Janela do Filmcraft organizada no monitor Samsung.", file=sys.stderr)
    abrir_projeto_no_filmcraft(janela, caminho_fcproj)


def _clicar(x: int, y: int, cliques: int = 1):
    for _ in range(cliques):
        subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "--clearmodifiers", "1"])


def abrir_projeto_no_filmcraft(janela: str, caminho_fcproj: Path):
    """Abre caminho_fcproj no Filmcraft via diálogo nativo (Ctrl+O), criando um
    projeto novo com o vídeo já importado. O diálogo xdg-desktop-portal-kde não
    abre sempre na mesma posição/tamanho (coordenada de clique é instável) e,
    pior, não ganha foco de teclado sozinho — tem que ativar a janela dele
    explicitamente por nome, senão as teclas vazam pro Filmcraft por baixo."""
    antes = {l.split()[0] for l in subprocess.run(
        ["wmctrl", "-l"], capture_output=True, text=True).stdout.splitlines()}

    subprocess.run(["xdotool", "windowactivate", janela])
    time.sleep(0.5)
    subprocess.run(["xdotool", "key", "ctrl+o"])
    time.sleep(1.5)
    dialogo = subprocess.run(["xdotool", "search", "--name", "xdg-desktop-portal-kde"],
                              capture_output=True, text=True).stdout.strip().splitlines()
    if not dialogo:
        print("Diálogo de abrir projeto não apareceu, abra manualmente.", file=sys.stderr)
        return
    subprocess.run(["xdotool", "windowactivate", dialogo[-1]])
    time.sleep(0.5)
    subprocess.run(["xdotool", "key", "ctrl+l"])
    time.sleep(0.5)
    subprocess.run(["xdotool", "key", "ctrl+a"])
    subprocess.run(["xdotool", "type", "--delay", "20", str(caminho_fcproj.parent) + "/"])
    time.sleep(0.5)
    subprocess.run(["xdotool", "key", "Return"])
    time.sleep(1.2)
    subprocess.run(["xdotool", "windowactivate", dialogo[-1]])
    time.sleep(0.3)
    subprocess.run(["xdotool", "key", "alt+n"])  # mnemônico do campo "Nome:"
    time.sleep(0.3)
    subprocess.run(["xdotool", "key", "ctrl+a"])
    subprocess.run(["xdotool", "type", "--delay", "20", caminho_fcproj.name])
    time.sleep(0.5)
    subprocess.run(["xdotool", "key", "Return"])

    novas = set()
    for _ in range(10):  # espera até 5s pelo prompt de salvar, se aparecer
        time.sleep(0.5)
        depois = {l.split()[0] for l in subprocess.run(
            ["wmctrl", "-l"], capture_output=True, text=True).stdout.splitlines()}
        novas = depois - antes - {janela}
        if novas:
            break
    if novas:
        print("Projeto anterior tinha alterações não salvas, descartando...", file=sys.stderr)
        _clicar(510, 1467)  # botão "Não salvar"
        time.sleep(1.5)

    _clicar(120, 959)  # aba "Importar"
    print(f"Projeto aberto no Filmcraft: {caminho_fcproj}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", help="caminho do vídeo a editar")
    p.add_argument("--whisper", default="small", help="modelo whisper (padrão: small)")
    p.add_argument("--idioma", default="pt", help="idioma da transcrição (padrão: pt)")
    p.add_argument("--llm", default="gemma4:12b", help="modelo Ollama (padrão: gemma4:12b)")
    args = p.parse_args()

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        sys.exit(f"Arquivo não encontrado: {video}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("ffmpeg/ffprobe não encontrados.")

    print("Analisando vídeo (ffprobe)...", file=sys.stderr)
    probe = ffprobe_info(video)
    salvar_modelo_otimizado(probe)

    transcricao = transcrever(video, args.whisper, args.idioma)

    pasta_tmp = PASTA_PROJETOS / "_tmp"
    pasta_tmp.mkdir(parents=True, exist_ok=True)
    (pasta_tmp / "transcricao.json").write_text(
        json.dumps(transcricao, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Transcrição salva (progresso): {pasta_tmp / 'transcricao.json'}", file=sys.stderr)

    print(f"Pedindo plano de edição ao Ollama ({args.llm})...", file=sys.stderr)
    try:
        plano = perguntar_ollama(transcricao, args.llm)
    except Exception as e:
        print(f"Ollama falhou ({e}), usando vídeo inteiro sem cortes.", file=sys.stderr)
        plano = {"nome_projeto": video.stem, "cortes_manter": [
            {"start": 0.0, "end": probe["duration_seg"], "motivo": "fallback sem IA"}],
            "momentos_zoom": [], "legendas_sugeridas": [], "sfx_sugeridos": [],
            "trilha_sugerida": ""}

    nome_projeto = slugify(plano.get("nome_projeto", video.stem))
    pasta = PASTA_PROJETOS / nome_projeto
    pasta.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pasta_tmp / "transcricao.json"), str(pasta / "transcricao.json"))

    (pasta / "plano_edicao.json").write_text(
        json.dumps(plano, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Plano de edição salvo (progresso): {pasta / 'plano_edicao.json'}", file=sys.stderr)

    legendas = plano.get("legendas_sugeridas") or []
    if legendas:
        escrever_srt(legendas, pasta / "legendas.srt")
    escrever_notas(plano, pasta / "notas_edicao.txt")

    cortes = plano.get("cortes_manter") or [{"start": 0.0, "end": probe["duration_seg"],
                                              "motivo": "sem cortes"}]
    fcproj = gerar_fcproj(nome_projeto, video, probe, cortes)
    caminho_fcproj = pasta / "projeto.fcproj"
    caminho_fcproj.write_text(json.dumps(fcproj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Projeto Filmcraft salvo: {caminho_fcproj}", file=sys.stderr)

    organizar_janela_filmcraft(caminho_fcproj)

    print("\nPronto. Revise os cortes no Filmcraft, aplique zoom/sfx/trilha conforme "
          f"{pasta / 'notas_edicao.txt'}, importe {pasta / 'legendas.srt'} se houver, "
          "e só renderize depois de aprovar manualmente.")


if __name__ == "__main__":
    executar_em_env_temporario()
    main()
