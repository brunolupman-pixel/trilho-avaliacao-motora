"""Laço do jogo (tela do paciente) e fontes de entrada.

* ``FonteSimulada`` reproduz uma partida simulada em tempo real pelo mesmo
  motor de fusão do núcleo de aquisição (filtro de Kalman, só passado), como
  aconteceria com o hardware.
* ``FonteMouse`` permite desenvolver e testar os jogos sem hardware.

O profissional pode pausar (P) ou encerrar (Esc) a qualquer momento.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pygame

from ..dados import DadosBrutos
from ..fusao import MotorFusao, converter_tof
from ..geometria import Caminho, obter_caminho
from ..sincronia import SincroniaAoVivo
from ..transicoes import TabelaTransicoes
from .base import EstadoJoystick, Jogo
from .bolo import JogoBolo
from .carro import JogoCarro
from .vidro import JogoVidro

JOGOS = {"carro": JogoCarro, "vidro": JogoVidro, "bolo": JogoBolo}
JOGO_DO_CAMINHO = {"C": "carro", "A": "vidro", "B": "bolo"}


class FonteSimulada:
    def __init__(self, dados: DadosBrutos, tabela: TabelaTransicoes | None) -> None:
        self.caminho = obter_caminho(dados.meta["caminho"])
        self.motor = MotorFusao(self.caminho, tabela, dados.meta["calibracao_imu"])
        self.motor.estado_ir = {k: int(v) for k, v in self.caminho.estado_sensores(0.0).items()}
        self.atraso_ir = tabela.atraso_ir_s if tabela else 0.0
        self.sinc = SincroniaAoVivo()
        self._sinc = dados.sinc.to_numpy()
        self._imu = dados.imu[["seq", "t_joy_us", "ax", "ay", "gz"]].to_numpy()
        self._ir = dados.ir[["t_banc_us", "sensor", "estado"]].to_numpy()
        tof = converter_tof(dados)
        self._tof = tof.to_numpy() if len(tof) else np.zeros((0, 2))
        self._carga = None
        if not dados.carga.empty and "calibracao_carga" in dados.meta:
            cal = dados.meta["calibracao_carga"]
            pts = np.array(cal["pontos"])
            coef = np.polyfit(pts[:, 0], pts[:, 1], 2)
            F = np.polyval(coef, dados.carga["bruto"].to_numpy(float) - cal["tara_bruto"])
            self._carga = np.stack([dados.carga["t_joy_us"].to_numpy(float), F], axis=1)
        self.duracao = float(self._ir[-1, 0] / 1e6) if len(self._ir) else 0.0
        self.duracao = max(self.duracao, float(self._sinc[-1, 2] / 1e6))
        self._i = {"sinc": 0, "imu": 0, "ir": 0, "tof": 0, "carga": 0}
        self._forca: float | None = None

    def ler(self, t: float) -> EstadoJoystick:
        """Alimenta o motor com tudo o que chegou até o instante t (relógio da bancada)."""
        i = self._i
        while i["sinc"] < len(self._sinc) and self._sinc[i["sinc"], 2] / 1e6 <= t:
            self.sinc.adicionar(*map(int, self._sinc[i["sinc"]]))
            i["sinc"] += 1
        if not self.sinc.pronta:
            return EstadoJoystick(t, 0.0, 0.0, 0.0, None, True)
        fila = []
        while i["imu"] < len(self._imu):
            r = self._imu[i["imu"]]
            tb = self.sinc.para_bancada(r[1]) / 1e6
            if tb > t:
                break
            fila.append((tb, 1, ("imu", r)))
            i["imu"] += 1
        while i["ir"] < len(self._ir) and self._ir[i["ir"], 0] / 1e6 <= t:
            r = self._ir[i["ir"]]
            fila.append((r[0] / 1e6 - self.atraso_ir, 0, ("ir", r)))
            i["ir"] += 1
        while i["tof"] < len(self._tof) and self._tof[i["tof"], 0] <= t:
            fila.append((self._tof[i["tof"], 0], 0, ("tof", self._tof[i["tof"]])))
            i["tof"] += 1
        fila.sort(key=lambda x: (x[0], x[1]))
        for tb, _, (tipo, r) in fila:
            if tipo == "imu":
                self.motor.imu(tb, int(r[2]), int(r[3]), int(r[4]), seq=int(r[0]))
            elif tipo == "ir":
                self.motor.ir(tb, int(r[1]), int(r[2]))
            else:
                self.motor.tof(tb, float(r[1]))
        if self._carga is not None:
            while (i["carga"] < len(self._carga)
                   and self.sinc.para_bancada(self._carga[i["carga"], 0]) / 1e6 <= t):
                self._forca = float(self._carga[i["carga"], 1])
                i["carga"] += 1
        e = self.motor.estado()
        return EstadoJoystick(t, e["fracao"], e["s_mm"], e["v"] * 1000, self._forca, e["repouso"])


class FonteMouse:
    """Posição horizontal do mouse = posição ao longo do caminho; botão = apertar."""

    def __init__(self, caminho: Caminho) -> None:
        self.caminho = caminho
        self._ant: tuple[float, float] | None = None
        self.duracao = float("inf")

    def ler(self, t: float) -> EstadoJoystick:
        w = pygame.display.get_surface().get_width()
        x, _ = pygame.mouse.get_pos()
        f = float(np.clip(x / max(1, w - 1), 0, 1))
        s = f * self.caminho.comprimento_mm
        v = 0.0
        if self._ant is not None and t > self._ant[0]:
            v = (s - self._ant[1]) / (t - self._ant[0])
        self._ant = (t, s)
        forca = 45.0 if pygame.mouse.get_pressed()[0] else 15.0
        return EstadoJoystick(t, f, s, v, forca if self.caminho.tem_tof else None, abs(v) < 5)


def executar(jogo: Jogo, fonte, tela_cheia: bool = False, tamanho=(1280, 720),
             max_quadros: int | None = None, capturas: dict[int, str] | None = None,
             tempo_real: bool = True, fps: int = 60) -> dict:
    """Roda o jogo até terminar. Devolve métricas e eventos do jogo."""
    if max_quadros is not None and not tempo_real:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    flags = pygame.FULLSCREEN if tela_cheia else 0
    tela = pygame.display.set_mode((0, 0) if tela_cheia else tamanho, flags)
    pygame.display.set_caption(f"Trilho — {jogo.nome}")
    relogio = pygame.time.Clock()
    jogo.iniciar(tela)
    t_sessao = 0.0
    inicio = time.perf_counter()
    pausado = False
    quadro = 0
    try:
        while not jogo.terminou():
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                    jogo.encerrar("interrompido pelo profissional")
                elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_p:
                    pausado = not pausado
                    jogo.evento("pausa" if pausado else "retomada")
            dt = 1.0 / fps
            if tempo_real:
                dt = relogio.tick(fps) / 1000.0
            if not pausado:
                t_sessao += dt
                estado = fonte.ler(t_sessao)
                jogo.atualizar(estado, dt)
                if t_sessao >= getattr(fonte, "duracao", float("inf")):
                    jogo.encerrar("fim dos dados")
            jogo.desenhar()
            if pausado:
                jogo.escrever("PAUSA (P para continuar)", (jogo.W // 2, jogo.H // 2),
                              grande=True, centro=True)
            pygame.display.flip()
            quadro += 1
            if capturas and quadro in capturas:
                Path(capturas[quadro]).parent.mkdir(parents=True, exist_ok=True)
                pygame.image.save(tela, capturas[quadro])
            if max_quadros is not None and quadro >= max_quadros:
                jogo.encerrar("limite de quadros")
    finally:
        pygame.quit()
    return {"jogo": jogo.nome, "metricas": jogo.metricas(),
            "eventos": [e.__dict__ for e in jogo.eventos],
            "duracao_real_s": time.perf_counter() - inicio}
