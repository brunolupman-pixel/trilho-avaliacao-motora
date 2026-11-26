"""Sincronia de relógio joystick -> bancada (arquitetura: "Sincronia joystick ↔ bancada").

Cada troca de ida e volta (PING/PONG, a cada ~100 ms) dá quatro instantes:
t1 (joystick envia), t2 (bancada recebe), t3 (bancada responde), t4 (joystick
recebe). Diferença entre relógios: θ = ((t2 - t1) + (t3 - t4)) / 2;
atraso da troca: δ = (t4 - t1) - (t3 - t2).

Trocas com atraso alto (rádio ocupado) são descartadas; com as demais ajusta-se
uma reta θ(t) = a + b·(t_joy - t_ref), que captura a diferença e a deriva dos
cristais. Em tempo real a reta usa os últimos 20 s; depois da partida, a
partida inteira (mais preciso).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class ModeloRelogio:
    a: float = 0.0          # us
    b: float = 0.0          # deriva (adimensional, ~1e-5)
    t_ref: float = 0.0      # us, relógio do joystick
    residuo_us: float = float("nan")
    n: int = 0
    valido: bool = False

    def para_bancada(self, t_joy):
        return t_joy + self.a + self.b * (np.asarray(t_joy, dtype=float) - self.t_ref)

    def resumo(self) -> dict:
        return {"diferenca_ms": round(self.a / 1000, 3), "deriva_ppm": round(self.b * 1e6, 2),
                "residuo_ms": None if np.isnan(self.residuo_us) else round(self.residuo_us / 1000, 4),
                "trocas_usadas": self.n, "valido": self.valido}


def _ajustar(t1: np.ndarray, theta: np.ndarray, delta: np.ndarray) -> ModeloRelogio:
    if len(t1) == 0:
        return ModeloRelogio()
    # mantém as trocas mais rápidas: atraso até o 40º percentil (+0,3 ms de folga)
    limite = np.percentile(delta, 40) + 300.0
    m = delta <= limite
    t, th = t1[m], theta[m]
    t_ref = float(np.median(t))
    if len(t) < 3 or np.ptp(t) < 0.5e6:
        a = float(np.median(th))
        res = float(np.std(th - a)) if len(th) > 1 else float("nan")
        return ModeloRelogio(a, 0.0, t_ref, res, len(t), len(t) >= 1)
    x = t - t_ref
    sel = np.ones(len(x), bool)
    for _ in range(3):  # rejeita valores atípicos (3 desvios)
        A = np.column_stack([np.ones(sel.sum()), x[sel]])
        (a, b), *_ = np.linalg.lstsq(A, th[sel], rcond=None)
        r = th - (a + b * x)
        s = max(np.std(r[sel]), 20.0)
        novo = np.abs(r) < 3 * s
        if novo.sum() < 3 or (novo == sel).all():
            break
        sel = novo
    res = float(np.sqrt(np.mean((th[sel] - (a + b * x[sel])) ** 2)))
    return ModeloRelogio(float(a), float(b), t_ref, res, int(sel.sum()), True)


def theta_delta(t1, t2, t3, t4):
    t1, t2, t3, t4 = (np.asarray(v, dtype=float) for v in (t1, t2, t3, t4))
    return ((t2 - t1) + (t3 - t4)) / 2.0, (t4 - t1) - (t3 - t2)


class SincroniaTempoReal:
    """Reta ajustada com as trocas dos últimos `janela_s` segundos."""

    def __init__(self, janela_s: float = 20.0) -> None:
        self.janela_us = janela_s * 1e6
        self.registros: deque = deque()
        self.modelo = ModeloRelogio()
        self._desde_ajuste = 0

    def adicionar(self, t1: int, t2: int, t3: int, t4: int) -> None:
        self.registros.append((t1, t2, t3, t4))
        while self.registros and self.registros[-1][0] - self.registros[0][0] > self.janela_us:
            self.registros.popleft()
        self._desde_ajuste += 1
        if self._desde_ajuste >= 3 or not self.modelo.valido:
            self._desde_ajuste = 0
            r = np.array(self.registros, dtype=float)
            th, de = theta_delta(r[:, 0], r[:, 1], r[:, 2], r[:, 3])
            self.modelo = _ajustar(r[:, 0], th, de)

    def reiniciar(self) -> None:
        self.registros.clear()
        self.modelo = ModeloRelogio()


def ajustar_partida(registros: np.ndarray) -> ModeloRelogio:
    """registros: (N, 4) com t1..t4 já desenrolados. Usa a partida inteira."""
    if len(registros) == 0:
        return ModeloRelogio()
    r = np.asarray(registros, dtype=float)
    th, de = theta_delta(r[:, 0], r[:, 1], r[:, 2], r[:, 3])
    return _ajustar(r[:, 0], th, de)
