"""Carro (caminho C, retilíneo): a posição funciona como alavanca de motor de barco.

No centro fica o neutro; à frente, empuxo proporcional ao quanto se avança;
atrás, empuxo de ré, que primeiro desacelera o carro e depois o faz andar para
trás. Uma zona morta em torno do neutro evita que tremores mexam o carro.
Placas de velocidade e semáforos exigem mudar a velocidade (movimento repetido).
Com a mão relaxada (força abaixo do limite) o carro ganha bônus; acima do
limite, um indicador discreto avisa, sem punir.
"""

from __future__ import annotations

import math
import random

import numpy as np
import pygame

from .base import ALERTA, BOM, DESTAQUE, FUNDO, RUIM, SUAVE, TEXTO, EstadoJoystick, Jogo


class JogoCarro(Jogo):
    nome = "carro"
    caminho = "C"

    def preparar(self) -> None:
        ex = self.dif.extra
        self.neutro = float(ex.get("neutro", 0.5))           # calibrado por paciente
        self.zona_morta = float(ex.get("zona_morta", 0.06))
        self.v_max = 1.0                                       # velocidade normalizada
        self.v = 0.0
        self.distancia = 0.0
        self.rng = random.Random(ex.get("semente", 3))
        self.pedida = self.dif.velocidade_alvo
        self.semaforo = "verde"
        self.proxima_troca = 6.0
        self.t_estimulo: float | None = None
        self.tipo_estimulo: str | None = None
        self.erros: list[float] = []
        self.reacoes: list[float] = []
        self.tempo_relaxado = 0.0
        self.tempo_total = 0.0
        self.bonus = 0
        self._acum_relaxado = 0.0
        self.forca: float | None = None
        self.comando = 0.0

    def _comando(self, fracao: float) -> float:
        u = (fracao - self.neutro) / 0.5
        if abs(u) < self.zona_morta / 0.5:
            return 0.0
        return float(np.clip(u, -1.0, 1.0))

    def passo(self, e: EstadoJoystick, dt: float) -> None:
        tr = self.t - (self.t0 or 0.0)
        self.comando = self._comando(e.fracao)
        # empuxo: o carro responde com atraso de primeira ordem
        alvo = self.comando * self.v_max
        self.v += (alvo - self.v) * min(1.0, dt * 1.5)
        self.distancia += self.v * dt * 400
        self.tempo_total += dt

        # placas e semáforos
        if tr >= self.proxima_troca:
            if self.rng.random() < 0.4:
                self.semaforo = "vermelho" if self.semaforo == "verde" else "verde"
                self.tipo_estimulo = self.semaforo
                self.evento("semaforo", self.semaforo)
            else:
                self.semaforo = "verde"
                self.pedida = self.rng.choice([0.3, 0.5, 0.7, 0.9])
                self.pedida = min(self.pedida, self.dif.velocidade_alvo + 0.3)
                self.tipo_estimulo = "placa"
                self.evento("placa", self.pedida)
            self.t_estimulo = self.t
            self.proxima_troca = tr + self.rng.uniform(5.0, 9.0)
        pedida = 0.0 if self.semaforo == "vermelho" else self.pedida
        self.erros.append(abs(self.v - pedida))
        if self.t_estimulo is not None:
            atingiu = abs(self.v - pedida) < 0.1
            if atingiu:
                self.reacoes.append(self.t - self.t_estimulo)
                self.evento("reacao", round(self.t - self.t_estimulo, 3))
                self.t_estimulo = None

        # força de preensão: bônus por mão relaxada
        self.forca = e.forca_N
        if e.forca_N is not None:
            if e.forca_N <= self.dif.limite_forca_N:
                self.tempo_relaxado += dt
                self._acum_relaxado += dt
                if self._acum_relaxado >= 5.0:
                    self.bonus += 1
                    self._acum_relaxado = 0.0
                    self.evento("bonus", self.bonus)
            else:
                self._acum_relaxado = 0.0

    # ------------------------------------------------------------------ desenho
    def desenhar(self) -> None:
        tela, W, H = self.tela, self.W, self.H
        tela.fill(FUNDO)
        x0, w = W // 2 - W // 8, W // 4
        pygame.draw.rect(tela, (44, 48, 58), (x0, 0, w, H))
        faixa = int(self.distancia) % 80
        for y in range(-80, H + 80, 80):
            pygame.draw.rect(tela, TEXTO, (W // 2 - 4, y + faixa, 8, 40))
        # carro
        cx, cy = W // 2, int(H * 0.72)
        pygame.draw.rect(tela, DESTAQUE, (cx - 32, cy - 55, 64, 110), border_radius=14)
        pygame.draw.rect(tela, FUNDO, (cx - 24, cy - 35, 48, 28), border_radius=6)
        if self.v < -0.02:
            self.escrever("RÉ", (cx, cy + 75), ALERTA, centro=True)
        # semáforo (forma + texto, não só cor)
        sx, sy = x0 + w + 40, 60
        pygame.draw.rect(tela, (30, 30, 30), (sx, sy, 60, 130), border_radius=10)
        acesa_verm = self.semaforo == "vermelho"
        pygame.draw.circle(tela, RUIM if acesa_verm else (70, 30, 30), (sx + 30, sy + 35), 22)
        pygame.draw.circle(tela, BOM if not acesa_verm else (30, 70, 30), (sx + 30, sy + 95), 22)
        self.escrever("PARE" if acesa_verm else "SIGA", (sx + 30, sy + 155),
                      RUIM if acesa_verm else BOM, centro=True)
        # placa de velocidade
        if not acesa_verm:
            px, py = x0 - 90, 120
            pygame.draw.circle(tela, TEXTO, (px, py), 52)
            pygame.draw.circle(tela, RUIM, (px, py), 52, 9)
            self.escrever(f"{int(self.pedida * 100)}", (px, py), FUNDO, grande=True, centro=True)
        # velocímetro e alavanca
        self.barra(40, H - 150, 300, 36, abs(self.v), DESTAQUE, f"Velocidade {abs(self.v) * 100:.0f}",
                   marca=0.0 if acesa_verm else self.pedida)
        self.barra(40, H - 60, 300, 30, (self.comando + 1) / 2, SUAVE, "Alavanca (centro = neutro)",
                   marca=0.5)
        if self.forca is not None:
            lim = self.dif.limite_forca_N
            frac = min(1.0, self.forca / (2 * lim))
            relax = self.forca <= lim
            self.barra(W - 340, H - 150, 300, 36, frac, BOM if relax else ALERTA,
                       "Mão relaxada" if relax else "Aperte menos", marca=0.5)
            self.escrever(f"Bônus: {self.bonus}", (W - 340, H - 90), TEXTO)
        restante = max(0.0, self.dif.duracao_s - (self.t - (self.t0 or self.t)))
        self.escrever(f"{math.ceil(restante)} s", (W - 120, 30), SUAVE)

    def metricas(self) -> dict:
        return {
            "erro_velocidade_medio": float(np.mean(self.erros)) if self.erros else None,
            "tempo_reacao_medio_s": float(np.mean(self.reacoes)) if self.reacoes else None,
            "reacoes": len(self.reacoes),
            "tempo_forca_abaixo_limite_s": self.tempo_relaxado,
            "tempo_forca_abaixo_limite_pct": (100 * self.tempo_relaxado / self.tempo_total
                                              if self.tempo_total else None),
            "bonus": self.bonus,
        }
