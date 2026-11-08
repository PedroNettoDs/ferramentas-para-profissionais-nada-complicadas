#!/usr/bin/env python3
"""Gera a thumbnail do vídeo localmente, usando o ícone do Alfred Pennyworth
como elemento fixo (mordomo comentando o vídeo) e um fundo gerado por IA de
imagem local (SD-Turbo via diffusers), no estilo do canal Mister Netto.

Uso:
    python3 gerar_thumbnail.py PASTA_DO_PROJETO
    python3 gerar_thumbnail.py PASTA_DO_PROJETO --contexto contexto.json

PASTA_DO_PROJETO é a pasta que o editar_video.py criou em
~/Vídeos/Projetos/<nome>/ (precisa ter plano_edicao.json lá dentro).

Sem --contexto e rodando num terminal interativo, o script PERGUNTA o
título, o estilo de cena e a frase de impacto da thumbnail. Com --contexto
(um JSON com as chaves "titulo", "estilo", "frase"), pula as perguntas —
esse é o encaixe pensado pra, no futuro, o próprio Alfred (o assistente
local do usuário) gerar esse JSON e chamar o script sem interação humana.
Sem terminal interativo e sem --contexto, usa valores padrão tirados do
plano_edicao.json.

A cada execução cria um .env_thumb temporário ao lado do script (diffusers,
transformers, accelerate, safetensors, rembg) e apaga no final, igual aos
outros scripts deste repositório. Usa o torch e o Pillow já instalados no
sistema (venv criado com --system-site-packages) pra não baixar o torch de
novo.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent / ".env_thumb"
DEPENDENCIAS = ["diffusers", "transformers", "accelerate", "safetensors", "rembg[cpu]"]

REPO_DIR = Path(__file__).resolve().parent
ALFRED_ORIGEM = Path.home() / "dev" / "Alfred-Pennyworth" / "docs" / "Alfred.png"
ALFRED_LOCAL = REPO_DIR / "assets" / "alfred.png"

LARGURA, ALTURA = 1280, 720
MODELO_SD = "stabilityai/sd-turbo"


def executar_em_env_temporario():
    if os.environ.get("SCRIPT_ENV_ATIVO") == "1":
        return
    code = 1
    try:
        shutil.rmtree(ENV_DIR, ignore_errors=True)
        print("Criando .env_thumb temporário (herda torch/Pillow do sistema)...", file=sys.stderr)
        venv.create(ENV_DIR, with_pip=True, system_site_packages=True)
        py = ENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "python"
        subprocess.run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                        *DEPENDENCIAS], check=True)
        r = subprocess.run([py, str(Path(__file__).resolve()), *sys.argv[1:]],
                            env={**os.environ, "SCRIPT_ENV_ATIVO": "1"})
        code = r.returncode
    except KeyboardInterrupt:
        code = 130
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Falha ao preparar o .env_thumb: {e}", file=sys.stderr)
    finally:
        shutil.rmtree(ENV_DIR, ignore_errors=True)
    sys.exit(code)


def garantir_alfred_local() -> Path:
    if not ALFRED_LOCAL.exists():
        if not ALFRED_ORIGEM.exists():
            sys.exit(f"Ícone do Alfred não encontrado em {ALFRED_ORIGEM}. "
                      f"Copie manualmente pra {ALFRED_LOCAL} e rode de novo.")
        ALFRED_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ALFRED_ORIGEM, ALFRED_LOCAL)
        print(f"Ícone do Alfred copiado pra {ALFRED_LOCAL}", file=sys.stderr)
    return ALFRED_LOCAL


def carregar_plano(pasta: Path) -> dict:
    caminho = pasta / "plano_edicao.json"
    if not caminho.is_file():
        sys.exit(f"{caminho} não existe. Rode editar_video.py nesse vídeo primeiro.")
    return json.loads(caminho.read_text(encoding="utf-8"))


def obter_contexto(pasta: Path, plano: dict, arquivo_contexto: str | None) -> dict:
    """Devolve {"titulo", "estilo", "frase"}. Fonte, em ordem de prioridade:
    1) --contexto (JSON já pronto, pensado pro Alfred alimentar no futuro);
    2) perguntas interativas, se tem terminal;
    3) valores padrão tirados do plano_edicao.json."""
    titulo_padrao = plano.get("nome_projeto", pasta.name)
    legendas = plano.get("legendas_sugeridas") or []
    frase_padrao = legendas[0]["texto"] if legendas else titulo_padrao.upper()
    estilo_padrao = "cena dramática, estúdio de criação de conteúdo, luzes neon, alto contraste"

    if arquivo_contexto:
        dados = json.loads(Path(arquivo_contexto).read_text(encoding="utf-8"))
        return {
            "titulo": dados.get("titulo", titulo_padrao),
            "estilo": dados.get("estilo", estilo_padrao),
            "frase": dados.get("frase", frase_padrao),
        }

    if not sys.stdin.isatty():
        return {"titulo": titulo_padrao, "estilo": estilo_padrao, "frase": frase_padrao}

    print("\nInformações da thumbnail (Enter pra usar o padrão entre colchetes):")
    titulo = input(f"Título [{titulo_padrao}]: ").strip() or titulo_padrao
    estilo = input(f"Estilo da cena de fundo [{estilo_padrao}]: ").strip() or estilo_padrao
    frase = input(f"Frase de impacto [{frase_padrao}]: ").strip() or frase_padrao
    return {"titulo": titulo, "estilo": estilo, "frase": frase}


def gerar_fundo(estilo: str):
    import torch
    from diffusers import AutoPipelineForText2Image

    print(f"Carregando {MODELO_SD}...", file=sys.stderr)
    pipe = AutoPipelineForText2Image.from_pretrained(
        MODELO_SD, torch_dtype=torch.float16, variant="fp16")
    pipe = pipe.to("cuda" if torch.cuda.is_available() else "cpu")

    prompt = (f"youtube thumbnail background, {estilo}, vibrant saturated colors, "
              f"cinematic lighting, no text, no people, no watermark")
    print("Gerando fundo (SD-Turbo, 1 passo)...", file=sys.stderr)
    imagem = pipe(prompt=prompt, num_inference_steps=1, guidance_scale=0.0,
                  height=512, width=512).images[0]
    return imagem


def cobrir_canvas(imagem, largura: int, altura: int):
    from PIL import Image

    razao_destino = largura / altura
    razao_origem = imagem.width / imagem.height
    if razao_origem > razao_destino:
        nova_altura = altura
        nova_largura = round(altura * razao_origem)
    else:
        nova_largura = largura
        nova_altura = round(largura / razao_origem)
    imagem = imagem.resize((nova_largura, nova_altura), Image.LANCZOS)
    esquerda = (nova_largura - largura) // 2
    topo = (nova_altura - altura) // 2
    return imagem.crop((esquerda, topo, esquerda + largura, topo + altura))


def recortar_alfred(caminho_alfred: Path):
    from rembg import remove
    from PIL import Image

    print("Removendo fundo do Alfred (rembg)...", file=sys.stderr)
    with open(caminho_alfred, "rb") as f:
        recortado = remove(f.read())
    import io
    return Image.open(io.BytesIO(recortado)).convert("RGBA")


def montar_thumbnail(fundo, alfred, titulo: str, frase: str):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    canvas = cobrir_canvas(fundo, LARGURA, ALTURA).convert("RGBA")

    # vinheta escura pra legibilidade do texto
    vinheta = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(vinheta).rectangle(
        [0, ALTURA - 260, LARGURA, ALTURA], fill=140)
    vinheta = vinheta.filter(ImageFilter.GaussianBlur(60))
    escuro = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
    canvas = Image.composite(escuro, canvas, vinheta)

    # Alfred no canto inferior direito, tipo "comentarista"
    alt_alvo = round(ALTURA * 0.78)
    esc = alt_alvo / alfred.height
    alfred_r = alfred.resize((round(alfred.width * esc), alt_alvo), Image.LANCZOS)
    pos_x = LARGURA - alfred_r.width + 30
    pos_y = ALTURA - alfred_r.height
    canvas.alpha_composite(alfred_r, (pos_x, pos_y))

    draw = ImageDraw.Draw(canvas)
    fonte_titulo = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
    fonte_frase = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)

    def texto_com_contorno(xy, texto, fonte, cor_texto, cor_contorno, largura_max):
        linhas = quebrar_linhas(draw, texto, fonte, largura_max)
        x, y = xy
        for linha in linhas:
            for dx in (-3, 0, 3):
                for dy in (-3, 0, 3):
                    if dx or dy:
                        draw.text((x + dx, y + dy), linha, font=fonte, fill=cor_contorno)
            draw.text((x, y), linha, font=fonte, fill=cor_texto)
            y += fonte.size + 10
        return y

    y = texto_com_contorno((50, 40), titulo.upper(), fonte_titulo,
                            (255, 255, 255, 255), (0, 0, 0, 255), LARGURA - 500)
    texto_com_contorno((50, y + 20), frase, fonte_frase,
                        (255, 214, 0, 255), (0, 0, 0, 255), LARGURA - 500)

    return canvas.convert("RGB")


def quebrar_linhas(draw, texto, fonte, largura_max):
    palavras = texto.split()
    linhas, atual = [], ""
    for p in palavras:
        teste = f"{atual} {p}".strip()
        if draw.textlength(teste, font=fonte) <= largura_max:
            atual = teste
        else:
            if atual:
                linhas.append(atual)
            atual = p
    if atual:
        linhas.append(atual)
    return linhas


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pasta_projeto", help="pasta do projeto (~/Vídeos/Projetos/<nome>/)")
    p.add_argument("--contexto", help="JSON com titulo/estilo/frase prontos (pula as perguntas)")
    args = p.parse_args()

    pasta = Path(args.pasta_projeto).expanduser().resolve()
    if not pasta.is_dir():
        sys.exit(f"Pasta não encontrada: {pasta}")

    alfred_png = garantir_alfred_local()
    plano = carregar_plano(pasta)
    contexto = obter_contexto(pasta, plano, args.contexto)

    fundo = gerar_fundo(contexto["estilo"])
    alfred = recortar_alfred(alfred_png)
    thumb = montar_thumbnail(fundo, alfred, contexto["titulo"], contexto["frase"])

    saida = pasta / "thumbnail.png"
    thumb.save(saida)
    print(f"Thumbnail salva em: {saida}")


if __name__ == "__main__":
    executar_em_env_temporario()
    main()
