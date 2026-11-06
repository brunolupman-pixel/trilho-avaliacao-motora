"""Mexer massa de bolo numa tigela (caminho B, espiral).

A tigela tem a forma da espiral; a colher segue o joystick da borda ao centro
e de volta, e a massa vai ficando homogênea com o percurso. Velocidade acima do
limite faz a massa respingar (incentiva movimento controlado); pausas longas
deixam a massa "empelotada".
"""

from __future__ import annotations

import random

import numpy as np
import pygame

from ..geometria import caminho_espiral
from .base import ALERTA, BOM, DESTAQUE, FUNDO, RUIM, SUAVE, TEXTO, EstadoJoystick, Jogo

N_TRECHOS = 60


class JogoBolo(Jogo):
    nome = "bolo"
    caminho = "B"

    def preparar(self) -> None:
        self.geo = caminho_espiral()
        # limite de velocidade em mm/s (dificuldade do profissional)
        self.limite = float(self.dif.extra.get("limite_mm_s", 120.0))
        self.homog = np.zeros(N_TRECHOS)
        self.fr = 0.0
        self.respingos = 0
        self.gotas: list[list[float]] = []
        self.pausas = 0
        self.pelotas = 0
        self._parado = 0.0
        self._acima = False
        self.velocidades: list[float] = []
        self.idas_voltas = 0
        self._fase = "ida"
        self._t_fase = None
        self.tempos = {"ida": [], "volta": []}
        self.rng = random.Random(5)
        pts = self.geo.pontos_mm[::10]
        self._pts = pts

    def _tela(self, xy_mm: np.ndarray) -> tuple[int, int]:
        esc = min(self.W, self.H) * 0.42 / 95.0
        return (int(self.W / 2 + xy_mm[0] * esc), int(self.H / 2 - xy_mm[1] * esc))

    def passo(self, e: EstadoJoystick, dt: float) -> None:
        f = float(np.clip(e.fracao, 0, 1))
        self.fr = f
        vel = abs(e.v_mm_s)
        if self._t_fase is None:
            self._t_fase = e.t
        if vel > 5:
            self.velocidades.append(vel)
        k = int(min(N_TRECHOS - 1, f * N_TRECHOS))
        if vel > self.limite:
            if not self._acima:
                self.respingos += 1
                self.evento("respingo", round(vel, 1))
                x, y = self._tela(self.geo.ponto(e.s_mm))
                for _ in range(10):
                    self.gotas.append([x, y, self.rng.uniform(-200, 200),
                                       self.rng.uniform(-260, -60), 0.8])
            self._acima = True
        else:
            self._acima = False
            if vel > 5:
                self.homog[k] = min(1.0, self.homog[k] + dt * vel / 60.0 / max(1, self.dif.repeticoes))
        # pausas longas empelotam a massa
        if vel < 5:
            self._parado += dt
            if 2.0 <= self._parado < 2.0 + dt:
                self.pausas += 1
                self.pelotas += 1
                self.homog[k] = max(0.0, self.homog[k] - 0.2)
                self.evento("pausa_longa", round(e.t, 2))
        else:
            self._parado = 0.0
        # ida (borda → centro) e volta
        if self._fase == "ida" and f > 0.97:
            self.tempos["ida"].append(e.t - self._t_fase)
            self._fase, self._t_fase = "volta", e.t
            self.evento("centro")
        elif self._fase == "volta" and f < 0.03:
            self.tempos["volta"].append(e.t - self._t_fase)
            self._fase, self._t_fase = "ida", e.t
            self.idas_voltas += 1
            self.evento("borda", self.idas_voltas)
            if self.idas_voltas >= self.dif.repeticoes:
                self.encerrar("repetições concluídas")
        for g in self.gotas:
            g[0] += g[2] * dt
            g[1] += g[3] * dt
            g[3] += 600 * dt
            g[4] -= dt
        self.gotas = [g for g in self.gotas if g[4] > 0]

    def desenhar(self) -> None:
        tela, W, H = self.tela, self.W, self.H
        tela.fill(FUNDO)
        esc = min(W, H) * 0.42 / 95.0
        pygame.draw.circle(tela, (230, 230, 235), (W // 2, H // 2), int(105 * esc))
        pygame.draw.circle(tela, SUAVE, (W // 2, H // 2), int(105 * esc), 6)
        # massa: cor por trecho da espiral (mais clara = mais homogênea)
        n = len(self._pts)
        for i in range(n - 1):
            k = int(min(N_TRECHOS - 1, i / n * N_TRECHOS))
            h = self.homog[k]
            cor = (int(200 + 50 * h), int(150 + 80 * h), int(80 + 60 * h))
            pygame.draw.line(tela, cor, self._tela(self._pts[i]), self._tela(self._pts[i + 1]), 14)
        # colher
        x, y = self._tela(self.geo.ponto(self.fr * self.geo.comprimento_mm))
        pygame.draw.circle(tela, DESTAQUE, (x, y), 20)
        pygame.draw.circle(tela, TEXTO, (x, y), 20, 3)
        for g in self.gotas:
            pygame.draw.circle(tela, (240, 200, 120), (int(g[0]), int(g[1])), 6)
        homog = 100 * self.homog.mean()
        self.barra(40, 60, 300, 34, homog / 100, BOM, f"Massa lisa: {homog:.0f} %")
        self.escrever(f"Respingos: {self.respingos}", (40, 120), RUIM if self.respingos else TEXTO)
        self.escrever(f"Idas e voltas: {self.idas_voltas}/{self.dif.repeticoes}", (40, 160), TEXTO)
        if self._acima:
            self.escrever("Mais devagar!", (W // 2, 50), ALERTA, grande=True, centro=True)

    def metricas(self) -> dict:
        v = np.array(self.velocidades)
        return {
            "regularidade_cv": float(v.std() / v.mean()) if len(v) > 10 else None,
            "respingos": self.respingos,
            "pausas_longas": self.pausas,
            "tempo_ida_medio_s": float(np.mean(self.tempos["ida"])) if self.tempos["ida"] else None,
            "tempo_volta_medio_s": (float(np.mean(self.tempos["volta"]))
                                    if self.tempos["volta"] else None),
            "idas_voltas": self.idas_voltas,
            "homogeneidade_pct": float(100 * self.homog.mean()),
        }
