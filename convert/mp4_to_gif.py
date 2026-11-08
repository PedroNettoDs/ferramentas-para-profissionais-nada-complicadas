#!/usr/bin/env python3
"""Converte um vídeo (MP4 etc.) em GIF por uma interface gráfica.

Uso:
    python3 mp4_to_gif.py [VIDEO]

A janela mostra a pré-visualização do vídeo já com a largura, as cores e o
dithering escolhidos, permite ajustar FPS e o trecho (início/fim) e exibe uma
estimativa do tamanho final do GIF antes de converter. A estimativa converte
3 amostras de 1 s do trecho com as mesmas configurações, mede nelas o tamanho
do primeiro quadro (cheio) e dos quadros seguintes (só a diferença) e extrapola
para a quantidade total de quadros; trechos de até 3 s são convertidos
inteiros, então nesse caso a estimativa é exata.

Requer: ffmpeg/ffprobe e PyQt6 (sudo apt install ffmpeg python3-pyqt6).
"""
import json
import shutil
import subprocess
import sys
import threading
from fractions import Fraction
from pathlib import Path

try:
    from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import QFont, QPixmap
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                                 QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                                 QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                                 QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget)
except ImportError:
    sys.exit("PyQt6 não encontrado. Rode: sudo apt install python3-pyqt6")

LARGURAS = [1280, 1080, 960, 854, 720, 640, 480, 360, 320, 240, 160]
LARGURA_PADRAO = 480
DITHERS = [
    ("Sierra 2-4A (padrão do ffmpeg)", "sierra2_4a"),
    ("Floyd-Steinberg", "floyd_steinberg"),
    ("Bayer (arquivo menor)", "bayer"),
    ("Nenhum (menor arquivo, mais serrilhado)", "none"),
]
AMOSTRAS = 3           # quantas amostras entram na estimativa de tamanho
DURACAO_AMOSTRA = 1.0  # segundos por amostra


def formatar_tamanho(n):
    n = float(n)
    for unidade in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidade == "GB":
            break
        n /= 1024
    texto = f"{n:.0f}" if unidade == "B" else f"{n:.1f}".replace(".", ",")
    return f"{texto} {unidade}"


def formatar_tempo(segundos):
    s = max(0, int(segundos))
    return f"{s // 60:02d}:{s % 60:02d}"


def sondar_video(caminho):
    """Lê largura, altura, fps, duração e tamanho do vídeo com ffprobe."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=width,height,r_frame_rate,duration:format=duration,size",
           "-of", "json", str(caminho)]
    dados = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
    if not dados.get("streams"):
        raise ValueError("o arquivo não tem trilha de vídeo")
    s, f = dados["streams"][0], dados.get("format", {})
    try:
        fps = float(Fraction(s.get("r_frame_rate", "0")))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "largura": s["width"],
        "altura": s["height"],
        "fps": fps,
        "duracao": float(s.get("duration") or f.get("duration") or 0),
        "tamanho": int(f.get("size") or caminho.stat().st_size),
    }


def filtro_gif(cfg, com_fps=True):
    """Monta o -vf do ffmpeg: fps, escala e paleta otimizada com dithering."""
    partes = [f"fps={cfg['fps']}"] if com_fps else []
    if cfg["largura"]:
        partes.append(f"scale={cfg['largura']}:-1:flags=lanczos")
    partes.append(f"split[a][b];[a]palettegen=max_colors={cfg['cores']}[p];"
                  f"[b][p]paletteuse=dither={cfg['dither']}:diff_mode=rectangle")
    return ",".join(partes)


def tamanhos_dos_quadros(gif):
    """Devolve (bytes do cabeçalho + paleta global, [bytes de cada quadro]) de um GIF.

    No GIF só o primeiro quadro é gravado inteiro; os seguintes guardam apenas o
    retângulo que mudou. Separar os dois é o que permite extrapolar uma amostra
    curta para o GIF completo sem superestimar.
    """
    def pular_subblocos(pos):
        while gif[pos]:
            pos += gif[pos] + 1
        return pos + 1

    def tamanho_paleta(flags):
        return 3 << ((flags & 7) + 1) if flags & 0x80 else 0

    pos = 13 + tamanho_paleta(gif[10])  # cabeçalho + descritor de tela + paleta global
    cabecalho, quadros, inicio = pos, [], pos
    while pos < len(gif):
        marcador = gif[pos]
        if marcador == 0x21:    # extensão (controle gráfico, loop etc.)
            pos = pular_subblocos(pos + 2)
        elif marcador == 0x2C:  # descritor de imagem + paleta local + dados LZW
            pos = pular_subblocos(pos + 11 + tamanho_paleta(gif[pos + 9]))
            quadros.append(pos - inicio)
            inicio = pos
        else:                   # 0x3B: fim do arquivo
            break
    return cabecalho, quadros


class Sinais(QObject):
    """Sinais emitidos pelas threads de trabalho para a thread da interface."""
    preview = pyqtSignal(int, object)      # geração, bytes PNG
    estimativa = pyqtSignal(int, object)   # geração, bytes estimados ou None
    progresso = pyqtSignal(float)          # 0 a 100
    concluido = pyqtSignal(int, str, str)  # código de saída, stderr, caminho


class Janela(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MP4 para GIF")
        self.resize(1000, 640)
        self.video = None
        self.info = None
        self._pixmap = None
        self._gen_preview = 0
        self._gen_estimativa = 0
        self._proc = None
        self._cancelado = False

        self.sinais = Sinais()
        self.sinais.preview.connect(self._mostrar_preview)
        self.sinais.estimativa.connect(self._mostrar_estimativa)
        self.sinais.progresso.connect(lambda p: self.barra.setValue(int(p)))
        self.sinais.concluido.connect(self._conversao_concluida)

        # Debounce: espera o usuário parar de mexer antes de chamar o ffmpeg.
        self._timer_preview = QTimer(self, singleShot=True, interval=150)
        self._timer_preview.timeout.connect(self.atualizar_preview)
        self._timer_estimativa = QTimer(self, singleShot=True, interval=500)
        self._timer_estimativa.timeout.connect(self.estimar_tamanho)

        self._montar_interface()

    # ------------------------------------------------------------ interface
    def _montar_interface(self):
        # Arquivo de entrada
        self.btn_abrir = QPushButton("Abrir vídeo…")
        self.btn_abrir.clicked.connect(self.escolher_video)
        self.lbl_arquivo = QLabel("Nenhum vídeo selecionado")
        self.lbl_info = QLabel("")
        linha_arquivo = QHBoxLayout()
        linha_arquivo.addWidget(self.btn_abrir)
        linha_arquivo.addWidget(self.lbl_arquivo, 1)
        linha_arquivo.addWidget(self.lbl_info)

        # Pré-visualização + posição no vídeo
        self.lbl_preview = QLabel("Abra um vídeo para ver a pré-visualização")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setFrameShape(QFrame.Shape.StyledPanel)
        self.lbl_preview.setMinimumSize(320, 240)
        self.lbl_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._slider_mudou)
        self.lbl_tempo = QLabel("00:00 / 00:00")
        linha_tempo = QHBoxLayout()
        linha_tempo.addWidget(self.slider, 1)
        linha_tempo.addWidget(self.lbl_tempo)
        coluna_preview = QVBoxLayout()
        coluna_preview.addWidget(self.lbl_preview, 1)
        coluna_preview.addLayout(linha_tempo)

        # Qualidade
        self.cb_largura = QComboBox()
        self.sp_fps = QSpinBox()
        self.sp_fps.setRange(1, 60)
        self.sp_fps.setValue(15)
        self.sp_cores = QSpinBox()
        self.sp_cores.setRange(2, 256)
        self.sp_cores.setValue(256)
        self.cb_dither = QComboBox()
        for nome, valor in DITHERS:
            self.cb_dither.addItem(nome, valor)
        form_qualidade = QFormLayout()
        form_qualidade.addRow("Largura:", self.cb_largura)
        form_qualidade.addRow("FPS:", self.sp_fps)
        form_qualidade.addRow("Cores:", self.sp_cores)
        form_qualidade.addRow("Dithering:", self.cb_dither)
        grupo_qualidade = QGroupBox("Qualidade")
        grupo_qualidade.setLayout(form_qualidade)

        # Trecho
        self.sp_inicio = QDoubleSpinBox()
        self.sp_fim = QDoubleSpinBox()
        for sp in (self.sp_inicio, self.sp_fim):
            sp.setDecimals(1)
            sp.setSuffix(" s")
        self.chk_loop = QCheckBox("Repetir sem parar (loop)")
        self.chk_loop.setChecked(True)
        form_trecho = QFormLayout()
        form_trecho.addRow("Início:", self.sp_inicio)
        form_trecho.addRow("Fim:", self.sp_fim)
        form_trecho.addRow(self.chk_loop)
        grupo_trecho = QGroupBox("Trecho")
        grupo_trecho.setLayout(form_trecho)

        # O que muda a imagem atualiza preview e estimativa; FPS e trecho, só estimativa.
        self.cb_largura.currentIndexChanged.connect(self._config_visual_mudou)
        self.cb_dither.currentIndexChanged.connect(self._config_visual_mudou)
        self.sp_cores.valueChanged.connect(self._config_visual_mudou)
        self.sp_fps.valueChanged.connect(self._agendar_estimativa)
        self.sp_inicio.valueChanged.connect(self._agendar_estimativa)
        self.sp_fim.valueChanged.connect(self._agendar_estimativa)

        # Tamanho estimado
        self.lbl_estimativa = QLabel("—")
        fonte = QFont()
        fonte.setPointSize(fonte.pointSize() + 8)
        fonte.setBold(True)
        self.lbl_estimativa.setFont(fonte)
        self.lbl_estimativa.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nota = QLabel(f"Calculado convertendo {AMOSTRAS} amostras de {DURACAO_AMOSTRA:g} s "
                      "do trecho com estas configurações.")
        nota.setWordWrap(True)
        nota.setStyleSheet("color: gray")
        self.lbl_final = QLabel("")
        self.lbl_final.setAlignment(Qt.AlignmentFlag.AlignCenter)
        coluna_tamanho = QVBoxLayout()
        coluna_tamanho.addWidget(self.lbl_estimativa)
        coluna_tamanho.addWidget(nota)
        coluna_tamanho.addWidget(self.lbl_final)
        grupo_tamanho = QGroupBox("Tamanho estimado do GIF")
        grupo_tamanho.setLayout(coluna_tamanho)

        coluna_direita = QVBoxLayout()
        coluna_direita.addWidget(grupo_qualidade)
        coluna_direita.addWidget(grupo_trecho)
        coluna_direita.addWidget(grupo_tamanho)
        coluna_direita.addStretch(1)
        painel_direito = QWidget()
        painel_direito.setLayout(coluna_direita)
        painel_direito.setFixedWidth(340)

        centro = QHBoxLayout()
        centro.addLayout(coluna_preview, 1)
        centro.addWidget(painel_direito)

        # Saída
        self.ed_saida = QLineEdit()
        self.btn_saida = QPushButton("Salvar como…")
        self.btn_saida.clicked.connect(self.escolher_saida)
        linha_saida = QHBoxLayout()
        linha_saida.addWidget(QLabel("Salvar em:"))
        linha_saida.addWidget(self.ed_saida, 1)
        linha_saida.addWidget(self.btn_saida)

        self.btn_converter = QPushButton("Converter")
        self.btn_converter.setEnabled(False)
        self.btn_converter.clicked.connect(self.converter)
        self.barra = QProgressBar()
        self.barra.setRange(0, 100)
        self.lbl_status = QLabel("")
        self.lbl_status.setMinimumWidth(220)
        linha_acao = QHBoxLayout()
        linha_acao.addWidget(self.btn_converter)
        linha_acao.addWidget(self.barra, 1)
        linha_acao.addWidget(self.lbl_status)

        raiz = QVBoxLayout(self)
        raiz.addLayout(linha_arquivo)
        raiz.addLayout(centro, 1)
        raiz.addLayout(linha_saida)
        raiz.addLayout(linha_acao)

    # -------------------------------------------------------------- arquivo
    def escolher_video(self):
        pasta = str(self.video.parent if self.video else Path.home())
        caminho, _ = QFileDialog.getOpenFileName(
            self, "Abrir vídeo", pasta,
            "Vídeos (*.mp4 *.mkv *.mov *.webm *.avi *.m4v);;Todos os arquivos (*)")
        if caminho:
            self.abrir(Path(caminho))

    def abrir(self, caminho):
        try:
            info = sondar_video(caminho)
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError,
                json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Erro ao abrir",
                                 f"Não foi possível ler o vídeo:\n{caminho}\n\n{e}")
            return
        self.video, self.info = caminho, info
        self.lbl_arquivo.setText(caminho.name)
        self.lbl_arquivo.setToolTip(str(caminho))
        self.lbl_info.setText(f"{info['largura']}×{info['altura']} · {info['fps']:g} fps · "
                              f"{formatar_tempo(info['duracao'])} · "
                              f"{formatar_tamanho(info['tamanho'])}")

        self.cb_largura.blockSignals(True)
        self.cb_largura.clear()
        self.cb_largura.addItem(f"Original ({info['largura']} px)", 0)
        for largura in LARGURAS:
            if largura < info["largura"]:
                self.cb_largura.addItem(f"{largura} px", largura)
        idx = self.cb_largura.findData(LARGURA_PADRAO)
        self.cb_largura.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_largura.blockSignals(False)

        self.sp_fps.setMaximum(max(1, round(info["fps"])) if info["fps"] else 60)
        for sp in (self.sp_inicio, self.sp_fim):
            sp.setRange(0, info["duracao"])
        self.sp_inicio.setValue(0)
        self.sp_fim.setValue(info["duracao"])
        self.slider.setValue(0)
        self._slider_mudou(0)
        self.ed_saida.setText(str(caminho.with_suffix(".gif")))
        self.lbl_final.setText("")
        self.barra.setValue(0)
        self.lbl_status.setText("")
        self.btn_converter.setEnabled(True)
        self._pixmap = None
        self._timer_preview.start()
        self._timer_estimativa.start()

    def configuracao(self):
        return {
            "largura": self.cb_largura.currentData() or 0,
            "fps": self.sp_fps.value(),
            "cores": self.sp_cores.value(),
            "dither": self.cb_dither.currentData(),
            "inicio": self.sp_inicio.value(),
            "fim": self.sp_fim.value(),
            "loop": self.chk_loop.isChecked(),
        }

    def tempo_atual(self):
        duracao = self.info["duracao"] if self.info else 0
        return self.slider.value() / self.slider.maximum() * duracao

    def _slider_mudou(self, _valor):
        duracao = self.info["duracao"] if self.info else 0
        self.lbl_tempo.setText(f"{formatar_tempo(self.tempo_atual())} / {formatar_tempo(duracao)}")
        if self.video:
            self._timer_preview.start()

    def _config_visual_mudou(self, *_):
        self._timer_preview.start()
        self._timer_estimativa.start()

    def _agendar_estimativa(self, *_):
        self._timer_estimativa.start()

    # ------------------------------------------------------- pré-visualização
    def atualizar_preview(self):
        if not self.video:
            return
        self._gen_preview += 1
        gen, cfg, video = self._gen_preview, self.configuracao(), self.video
        # Recua um pouco no fim para garantir que ainda há um quadro a decodificar.
        t = min(self.tempo_atual(), max(0.0, self.info["duracao"] - 0.5))
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-t", "0.2", "-i", str(video),
               "-vf", filtro_gif(cfg, com_fps=False), "-frames:v", "1",
               "-f", "image2", "-c:v", "png", "pipe:1"]

        def trabalho():
            r = subprocess.run(cmd, capture_output=True)
            self.sinais.preview.emit(gen, r.stdout)

        threading.Thread(target=trabalho, daemon=True).start()

    def _mostrar_preview(self, gen, dados):
        if gen != self._gen_preview or not dados:
            return
        pm = QPixmap()
        if pm.loadFromData(dados, "PNG"):
            self._pixmap = pm
            self._ajustar_preview()

    def _ajustar_preview(self):
        if self._pixmap:
            self.lbl_preview.setPixmap(self._pixmap.scaled(
                self.lbl_preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, evento):
        super().resizeEvent(evento)
        self._ajustar_preview()

    # ---------------------------------------------------- estimativa de tamanho
    def estimar_tamanho(self):
        if not self.video:
            return
        self._gen_estimativa += 1
        gen, cfg, video = self._gen_estimativa, self.configuracao(), self.video
        duracao = cfg["fim"] - cfg["inicio"]
        if duracao <= 0:
            self.lbl_estimativa.setText("Trecho inválido")
            return
        self.lbl_estimativa.setText("calculando…")
        completo = duracao <= AMOSTRAS * DURACAO_AMOSTRA  # trecho curto: converte inteiro
        if completo:
            amostras = [(cfg["inicio"], duracao)]
        else:
            amostras = []
            for i in range(AMOSTRAS):
                inicio = cfg["inicio"] + duracao * (i + 0.5) / AMOSTRAS
                amostras.append((inicio, min(DURACAO_AMOSTRA, cfg["fim"] - inicio)))

        def trabalho():
            primeiros, bytes_diff, n_diff, cabecalho = [], 0, 0, 0
            for inicio, dur in amostras:
                if gen != self._gen_estimativa:  # já existe pedido mais novo
                    return
                cmd = ["ffmpeg", "-v", "error", "-ss", f"{inicio:.3f}", "-t", f"{dur:.3f}",
                       "-i", str(video), "-vf", filtro_gif(cfg), "-f", "gif", "pipe:1"]
                r = subprocess.run(cmd, capture_output=True)
                if r.returncode != 0 or not r.stdout:
                    self.sinais.estimativa.emit(gen, None)
                    return
                if completo:
                    self.sinais.estimativa.emit(gen, len(r.stdout))
                    return
                try:
                    cabecalho, quadros = tamanhos_dos_quadros(r.stdout)
                except IndexError:
                    quadros = []
                if not quadros:
                    self.sinais.estimativa.emit(gen, None)
                    return
                primeiros.append(quadros[0])
                bytes_diff += sum(quadros[1:])
                n_diff += len(quadros) - 1
            primeiro = sum(primeiros) / len(primeiros)
            media_diff = bytes_diff / n_diff if n_diff else primeiro
            total_quadros = max(1, round(cfg["fps"] * duracao))
            self.sinais.estimativa.emit(gen, int(cabecalho + primeiro
                                                  + (total_quadros - 1) * media_diff))

        threading.Thread(target=trabalho, daemon=True).start()

    def _mostrar_estimativa(self, gen, valor):
        if gen != self._gen_estimativa:
            return
        self.lbl_estimativa.setText("Não foi possível estimar" if valor is None
                                    else f"≈ {formatar_tamanho(valor)}")

    # ------------------------------------------------------------- conversão
    def escolher_saida(self):
        caminho, _ = QFileDialog.getSaveFileName(
            self, "Salvar GIF como", self.ed_saida.text() or str(Path.home()), "GIF (*.gif)")
        if caminho:
            if not caminho.lower().endswith(".gif"):
                caminho += ".gif"
            self.ed_saida.setText(caminho)

    def converter(self):
        if self._proc:  # o botão está como "Cancelar"
            self._cancelado = True
            self._proc.terminate()
            return
        cfg = self.configuracao()
        duracao = cfg["fim"] - cfg["inicio"]
        if duracao <= 0:
            QMessageBox.warning(self, "Trecho inválido", "O fim precisa ser maior que o início.")
            return
        texto_saida = self.ed_saida.text().strip()
        if not texto_saida:
            QMessageBox.warning(self, "Sem destino", "Escolha onde salvar o GIF.")
            return
        saida = Path(texto_saida).expanduser()
        if saida.exists() and QMessageBox.question(
                self, "Substituir?", f"{saida.name} já existe. Substituir?"
        ) != QMessageBox.StandardButton.Yes:
            return

        cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
               "-ss", f"{cfg['inicio']:.3f}", "-t", f"{duracao:.3f}", "-i", str(self.video),
               "-vf", filtro_gif(cfg), "-loop", "0" if cfg["loop"] else "-1", str(saida)]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            QMessageBox.critical(self, "Erro", f"Não foi possível iniciar o ffmpeg:\n{e}")
            return
        self._proc = proc
        self._cancelado = False
        self.btn_converter.setText("Cancelar")
        self.btn_abrir.setEnabled(False)
        self.barra.setValue(0)
        self.lbl_status.setText("Convertendo…")
        self.lbl_final.setText("")

        def trabalho():
            for linha in proc.stdout:
                if linha.startswith("out_time_us="):
                    try:
                        us = int(linha[len("out_time_us="):])
                    except ValueError:  # ffmpeg escreve N/A no começo
                        continue
                    self.sinais.progresso.emit(min(100.0, us / 1e6 / duracao * 100))
            erro = proc.stderr.read()
            proc.wait()
            self.sinais.concluido.emit(proc.returncode, erro.strip(), str(saida))

        threading.Thread(target=trabalho, daemon=True).start()

    def _conversao_concluida(self, codigo, erro, saida):
        self._proc = None
        self.btn_converter.setText("Converter")
        self.btn_abrir.setEnabled(True)
        saida = Path(saida)
        if self._cancelado:
            saida.unlink(missing_ok=True)
            self.barra.setValue(0)
            self.lbl_status.setText("Cancelado")
            return
        if codigo != 0:
            self.barra.setValue(0)
            self.lbl_status.setText("Falhou")
            QMessageBox.critical(self, "Erro na conversão",
                                 erro or f"ffmpeg terminou com código {codigo}")
            return
        self.barra.setValue(100)
        self.lbl_status.setText(f"Pronto: {saida.name}")
        self.lbl_final.setText(f"Tamanho final: {formatar_tamanho(saida.stat().st_size)}")

    def closeEvent(self, evento):
        if self._proc:
            self._cancelado = True
            self._proc.terminate()
        super().closeEvent(evento)


def main():
    for ferramenta in ("ffmpeg", "ffprobe"):
        if not shutil.which(ferramenta):
            sys.exit(f"{ferramenta} não encontrado. Rode: sudo apt install ffmpeg")
    app = QApplication(sys.argv)
    janela = Janela()
    janela.show()
    if len(sys.argv) > 1:
        janela.abrir(Path(sys.argv[1]).expanduser())
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
