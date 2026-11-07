"""Fonte de entrada do jogo a partir da ESP32 da bancada (protótipo com LM393).

A ESP32 acompanha a sequência dos sensores IR da espiral e manda, pelo USB,
uma linha a cada avanço:

    P,<ms>,<fase>,<passo>,<total>
      fase  : 0 = ida (borda -> centro), 1 = volta (centro -> borda)
      passo : quantos pontos da sequência já foram cumpridos (0..total)

As outras linhas (texto para o Monitor Serial) são ignoradas.

Os sensores só dão a posição em pontos soltos. Entre um ponto e outro, a
colher continua andando na velocidade do último trecho, sem passar do próximo
ponto; se nenhum sensor dispara por muito tempo, ela para (pausa de verdade).
"""

from __future__ import annotations

import threading
import time

import serial
import serial.tools.list_ports

from ..geometria import Caminho
from .base import EstadoJoystick


def achar_porta_esp32() -> str | None:
    """Procura a ESP32 entre as portas USB (chips CP210x ou CH340)."""
    for p in serial.tools.list_ports.comports():
        texto = f"{p.description} {p.manufacturer} {p.hwid}".upper()
        if any(chip in texto for chip in ("CP210", "CH340", "CH910", "USB SERIAL", "UART")):
            return p.device
    return None


class FonteEsp32Espiral:
    """Posição ao longo da espiral a partir dos pontos de passagem da ESP32."""

    # se nenhum sensor disparar por este múltiplo do último intervalo, considera parado
    FATOR_PARADA = 2.5
    VELOCIDADE_INICIAL_S = 2.0   # palpite: 2 s entre pontos antes de ter dados

    def __init__(self, caminho: Caminho, porta: str | None = None, baud: int = 115200,
                 conexao=None) -> None:
        self.caminho = caminho
        self.duracao = float("inf")
        if conexao is None:
            porta = porta or achar_porta_esp32()
            if porta is None:
                raise RuntimeError("ESP32 não encontrada. Confira o cabo ou use --porta COM9")
            print(f"Conectando na ESP32 em {porta}")
            conexao = serial.Serial(porta, baud, timeout=0.1)
        self._serial = conexao
        self._trava = threading.Lock()
        self._marcos: list[tuple[float, float]] = []   # (instante, fração), mais novo no fim
        self._total = 10
        self._f_mostrada = 0.0
        self._t_ant: float | None = None
        self._rodando = True
        threading.Thread(target=self._ler_serial, daemon=True).start()

    # ---------- leitura da USB (em paralelo com o jogo) ----------
    def _ler_serial(self) -> None:
        buffer = b""
        while self._rodando:
            try:
                buffer += self._serial.read(256)
            except (serial.SerialException, OSError):
                return
            while b"\n" in buffer:
                linha, buffer = buffer.split(b"\n", 1)
                self.receber_linha(linha.decode(errors="ignore").strip())

    def receber_linha(self, linha: str, agora: float | None = None) -> None:
        if not linha.startswith("P,"):
            return
        partes = linha.split(",")
        if len(partes) != 5:
            return
        try:
            fase, passo, total = int(partes[2]), int(partes[3]), int(partes[4])
        except ValueError:
            return
        if total <= 0:
            return
        avanço = min(1.0, passo / total)
        fracao = avanço if fase == 0 else 1.0 - avanço
        agora = time.monotonic() if agora is None else agora
        with self._trava:
            self._total = total
            if not self._marcos or abs(self._marcos[-1][1] - fracao) > 1e-9:
                self._marcos.append((agora, fracao))
                del self._marcos[:-3]

    # ---------- chamado pelo jogo a cada quadro ----------
    def ler(self, t: float, agora: float | None = None) -> EstadoJoystick:
        agora = time.monotonic() if agora is None else agora
        with self._trava:
            marcos = list(self._marcos)
            total = self._total
        L = self.caminho.comprimento_mm
        alvo, taxa = 0.0, 0.0
        if marcos:
            t_m, f_m = marcos[-1]
            if len(marcos) >= 2:
                t_a, f_a = marcos[-2]
                intervalo = max(0.05, t_m - t_a)
                taxa = (f_m - f_a) / intervalo
            else:
                intervalo = self.VELOCIDADE_INICIAL_S
                taxa = (1.0 / total) / intervalo
            parado = (agora - t_m) > self.FATOR_PARADA * intervalo
            if parado:
                taxa = 0.0
            # anda na velocidade do último trecho, sem passar do próximo ponto
            limite = 0.95 / total
            avancar = max(-limite, min(limite, taxa * (agora - t_m)))
            alvo = min(1.0, max(0.0, f_m + avancar))
        dt = 0.0 if self._t_ant is None else max(0.0, t - self._t_ant)
        self._t_ant = t
        self._f_mostrada += (alvo - self._f_mostrada) * min(1.0, dt * 8.0)
        v = taxa * L
        return EstadoJoystick(t, self._f_mostrada, self._f_mostrada * L, v, None, abs(v) < 5)

    def reiniciar_esp32(self) -> None:
        self._serial.write(b"r")

    def fechar(self) -> None:
        self._rodando = False
        try:
            self._serial.close()
        except Exception:
            pass
