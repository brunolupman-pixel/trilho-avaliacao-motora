"""Limpar um vidro sujo (caminho A, guarda-chuva).

O pano segue a posição no arco; a sujeira some por onde ele passa, um pouco a
cada passada. As bordas do vidro só são limpas quando o paciente chega perto
das pontas do arco, o que incentiva amplitude completa.
"""

from __future__ import annotations

import math

import numpy as np
import pygame

from ..geometria import caminho_guarda_chuva
from .base import ALERTA, BOM, DESTAQUE, FUNDO, SUAVE, TEXTO, EstadoJoystick, Jogo

N_SETORES = 48


class JogoVidro(Jogo):
    nome = "vidro"
    caminho = "A"

    def preparar(self) -> None:
        self.geo = caminho_guarda_chuva()
        resist = float(self.dif.extra.get("resistencia_bordas", 2.0))
        self.sujeira = np.ones(N_SETORES)
        borda = int(N_SETORES * 0.12)
        self.resistencia = np.ones(N_SETORES)
        self.resistencia[:borda] = resist
        self.resistencia[-borda:] = resist
        self.borda = borda
        self.fr = 0.0
        self.passadas: list[dict] = []
        self._passada = {"min": None, "max": None, "t0": None}
        self._sentido = 0
        self.por_passada = 1.0 / max(1, self.dif.repeticoes)

    def _angulo(self, fracao: float) -> float:
        return math.pi * (1.0 - fracao)            # ponta esquerda = π, direita = 0

    def passo(self, e: EstadoJoystick, dt: float) -> None:
        f = float(np.clip(e.fracao, 0, 1))
        self.fr = f
        # pano limpa o setor sob ele, proporcional ao quanto se moveu
        k = int(min(N_SETORES - 1, f * N_SETORES))
        mov = abs(e.v_mm_s) * dt / self.geo.comprimento_mm   # fração do arco percorrida
        perto_ponta = f < 0.08 or f > 0.92
        for j in range(max(0, k - 2), min(N_SETORES, k + 3)):
            if (j < self.borda or j >= N_SETORES - self.borda) and not perto_ponta:
                continue
            self.sujeira[j] = max(0.0, self.sujeira[j] - mov * N_SETORES * self.por_passada
                                  / (5 * self.resistencia[j]))
        # passadas: cada troca de sentido fecha uma passada
        p = self._passada
        p["min"] = f if p["min"] is None else min(p["min"], f)
        p["max"] = f if p["max"] is None else max(p["max"], f)
        p["t0"] = e.t if p["t0"] is None else p["t0"]
        sentido = 1 if e.v_mm_s > 15 else -1 if e.v_mm_s < -15 else 0
        if sentido and self._sentido and sentido != self._sentido:
            amp = (p["max"] - p["min"]) * 100
            self.passadas.append({"amplitude_pct": amp, "duracao_s": e.t - p["t0"]})
            self.evento("passada", round(amp, 1))
            self._passada = {"min": f, "max": f, "t0": e.t}
        if sentido:
            self._sentido = sentido
        if self.sujeira.max() < 0.02:
            self.encerrar("vidro limpo")

    def desenhar(self) -> None:
        tela, W, H = self.tela, self.W, self.H
        tela.fill(FUNDO)
        cx, cy, R = W // 2, int(H * 0.78), int(min(W * 0.36, H * 0.62))
        # vidro: setores com sujeira (marrom translúcido sobre azul)
        pygame.draw.circle(tela, (40, 70, 110), (cx, cy), R, draw_top_right=True, draw_top_left=True)
        for j in range(N_SETORES):
            a0 = math.pi * (1 - j / N_SETORES)
            a1 = math.pi * (1 - (j + 1) / N_SETORES)
            suj = self.sujeira[j]
            if suj <= 0.01:
                continue
            cor = (int(40 + 80 * suj), int(70 + 30 * suj), int(110 - 70 * suj))
            pts = [(cx, cy)] + [(cx + R * math.cos(a), cy - R * math.sin(a))
                                for a in np.linspace(a0, a1, 4)]
            pygame.draw.polygon(tela, cor, pts)
        pygame.draw.arc(tela, TEXTO, (cx - R, cy - R, 2 * R, 2 * R), 0, math.pi, 4)
        pygame.draw.line(tela, SUAVE, (cx, cy), (cx, cy + 60), 6)   # haste do guarda-chuva
        # pano no arco (mesma geometria do caminho real)
        a = self._angulo(self.fr)
        px, py = cx + R * 0.82 * math.cos(a), cy - R * 0.82 * math.sin(a)
        pygame.draw.circle(tela, BOM, (int(px), int(py)), 34)
        pygame.draw.circle(tela, TEXTO, (int(px), int(py)), 34, 3)
        for borda_a in (math.pi, 0.0):
            bx, by = cx + R * math.cos(borda_a), cy - R * math.sin(borda_a)
            pygame.draw.circle(tela, ALERTA, (int(bx), int(by)), 10)
        limpo = 100 * (1 - self.sujeira.mean())
        self.barra(40, 60, 320, 34, limpo / 100, DESTAQUE, f"Limpo: {limpo:.0f} %")
        self.escrever(f"Passadas: {len(self.passadas)}", (40, 120), TEXTO)
        self.escrever("Chegue até as pontas para limpar as bordas", (W // 2, H - 40), SUAVE,
                      centro=True)

    def metricas(self) -> dict:
        amps = [p["amplitude_pct"] for p in self.passadas]
        durs = [p["duracao_s"] for p in self.passadas]
        return {
            "passadas": len(self.passadas),
            "amplitude_media_pct": float(np.mean(amps)) if amps else None,
            "duracao_media_passada_s": float(np.mean(durs)) if durs else None,
            "ritmo_passadas_min": (60 * len(durs) / sum(durs)) if durs and sum(durs) else None,
            "limpo_pct": float(100 * (1 - self.sujeira.mean())),
        }
