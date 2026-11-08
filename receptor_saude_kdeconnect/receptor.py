#!/usr/bin/env python3
"""
Receptor de dados de saude (sono, batimento, etc) sincronizados do W68 Pro Max
via app do celular -> KDE Connect -> esta pasta.

Fica esperando o celular aparecer disponivel na rede (kdeconnect-cli), e quando
arquivos .json/.csv novos chegam na pasta de Downloads (destino padrao do
plugin de compartilhamento do KDE Connect), copia e organiza em SAIDA_DIR.
"""

import csv
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

DOWNLOADS_DIR = Path.home() / "Downloads"
SAIDA_DIR = Path(__file__).parent / "dados_recebidos"
INTERVALO_SEGUNDOS = 10

SAIDA_DIR.mkdir(exist_ok=True)


def dispositivo_disponivel() -> str | None:
    """Retorna o nome do dispositivo se ele estiver visivel na rede, senao None."""
    try:
        saida = subprocess.run(
            ["kdeconnect-cli", "-a"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    # "- Galaxy S23 FE: <id> (emparelhado e acessivel)" vai para o stdout;
    # a contagem "N dispositivo(s) encontrado(s)" vai para o stderr.
    linhas = [l for l in saida.splitlines() if l.strip().startswith("-")]
    if linhas:
        nomes = subprocess.run(
            ["kdeconnect-cli", "-l", "--id-name-only"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return nomes or None
    return None


def parse_arquivo(caminho: Path) -> dict | list | None:
    try:
        if caminho.suffix.lower() == ".json":
            return json.loads(caminho.read_text(encoding="utf-8"))
        if caminho.suffix.lower() == ".csv":
            with caminho.open(newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
    except (json.JSONDecodeError, csv.Error, UnicodeDecodeError) as e:
        print(f"  erro ao ler {caminho.name}: {e}")
        return None
    return None


def processar_novos_arquivos(ja_vistos: set[str]) -> None:
    for caminho in list(DOWNLOADS_DIR.glob("*.json")) + list(DOWNLOADS_DIR.glob("*.csv")):
        if caminho.name in ja_vistos:
            continue
        ja_vistos.add(caminho.name)

        dados = parse_arquivo(caminho)
        if dados is None:
            continue

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destino = SAIDA_DIR / f"{timestamp}_{caminho.name}"
        shutil.copy2(caminho, destino)
        print(f"  recebido: {caminho.name} -> {destino}")


def main() -> None:
    ja_vistos: set[str] = {p.name for p in DOWNLOADS_DIR.glob("*.json")} | {
        p.name for p in DOWNLOADS_DIR.glob("*.csv")
    }
    print(f"Monitorando {DOWNLOADS_DIR} e aguardando celular na rede...")

    while True:
        nome = dispositivo_disponivel()
        if nome:
            print(f"[{datetime.now():%H:%M:%S}] dispositivo disponivel: {nome}")
            processar_novos_arquivos(ja_vistos)
        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nEncerrado.")
