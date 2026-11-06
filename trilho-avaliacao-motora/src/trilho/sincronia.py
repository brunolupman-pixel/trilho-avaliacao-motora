"""Sincronia de relógios joystick ↔ bancada (a bancada é o relógio mestre).

A cada ~100 ms o joystick troca mensagens de ida e volta com a bancada por
ESP-NOW, no estilo NTP/PTP:

    t1 = envio (joystick)      t2 = chegada (bancada)
    t3 = resposta (bancada)    t4 = chegada (joystick)

    diferença θ = ((t2 - t1) + (t3 - t4)) / 2     (bancada − joystick)
    atraso    δ = (t4 - t1) - (t3 - t2)

Depois da sessão, ajusta-se uma reta θ(t_joy) = θ0 + deriva·t_joy só com as
trocas de menor atraso (as mais confiáveis), e todo carimbo do joystick é
convertido para o relógio da bancada. O resíduo é guardado como indicador de
qualidade da sessão (meta: ≤ 1 ms).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd


def offsets_e_atrasos(sinc: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t1, t2, t3, t4 = (sinc[c].to_numpy(dtype=np.float64) for c in ("t1_us", "t2_us", "t3_us", "t4_us"))
    theta = ((t2 - t1) + (t3 - t4)) / 2.0
    delta = (t4 - t1) - (t3 - t2)
    t_meio = (t1 + t4) / 2.0
    return t_meio, theta, delta


@dataclass
class AjusteRelogio:
    """t_banc = t_joy + theta0 + deriva · (t_joy - t_ref)   (tudo em µs)."""

    theta0_us: float
    deriva: float                 # adimensional (ex.: 40e-6 = 40 ppm)
    t_ref_us: float
    residuo_rms_us: float
    trocas_usadas: int
    trocas_total: int

    def para_bancada(self, t_joy_us: np.ndarray | float) -> np.ndarray:
        t = np.asarray(t_joy_us, dtype=np.float64)
        return t + self.theta0_us + self.deriva * (t - self.t_ref_us)

    def como_dict(self) -> dict:
        return {
            "theta0_us": self.theta0_us, "deriva_ppm": self.deriva * 1e6,
            "residuo_rms_us": self.residuo_rms_us, "trocas_usadas": self.trocas_usadas,
            "trocas_total": self.trocas_total,
        }


def ajustar_relogio(sinc: pd.DataFrame, fracao_melhores: float = 0.4,
                    max_atraso_us: float = 20_000.0) -> AjusteRelogio:
    """Ajuste pós-sessão (diferença + deriva do cristal) com toda a sessão.

    Descarta trocas com atraso alto (rádio ocupado). Usa a fração de menor
    atraso em cada janela de tempo, para não concentrar o ajuste num trecho.
    """
    if len(sinc) < 3:
        raise ValueError("trocas de sincronia insuficientes")
    t, theta, delta = offsets_e_atrasos(sinc)
    validas = delta < max_atraso_us
    t, theta, delta = t[validas], theta[validas], delta[validas]
    # seleção por janelas de ~10 trocas
    sel = np.zeros(len(t), dtype=bool)
    janela = 10
    for i in range(0, len(t), janela):
        bloco = np.arange(i, min(i + janela, len(t)))
        k = max(1, int(round(len(bloco) * fracao_melhores)))
        sel[bloco[np.argsort(delta[bloco])[:k]]] = True
    ts, ths, ds = t[sel], theta[sel], delta[sel]
    t_ref = float(ts.mean())
    # mínimos quadrados ponderados: trocas mais rápidas pesam mais
    w = 1.0 / np.maximum(ds - ds.min() + 50.0, 50.0)
    A = np.stack([np.ones_like(ts), ts - t_ref], axis=1)
    W = np.sqrt(w)[:, None]
    coef, *_ = np.linalg.lstsq(A * W, ths * W[:, 0], rcond=None)
    residuo = ths - A @ coef
    return AjusteRelogio(float(coef[0]), float(coef[1]), t_ref,
                         float(np.sqrt(np.mean(residuo ** 2))), int(sel.sum()), len(sinc))


class SincroniaAoVivo:
    """Estimativa em tempo real (para o jogo), com as trocas mais recentes."""

    def __init__(self, n_trocas: int = 60) -> None:
        self._trocas: deque[tuple[float, float, float]] = deque(maxlen=n_trocas)
        self._ajuste: AjusteRelogio | None = None

    def adicionar(self, t1: int, t2: int, t3: int, t4: int) -> None:
        theta = ((t2 - t1) + (t3 - t4)) / 2.0
        delta = (t4 - t1) - (t3 - t2)
        self._trocas.append(((t1 + t4) / 2.0, theta, delta))
        if len(self._trocas) >= 3:
            df = np.array(self._trocas)
            t, th, de = df[:, 0], df[:, 1], df[:, 2]
            melhores = np.argsort(de)[: max(3, len(de) // 3)]
            t_ref = float(t[melhores].mean())
            if np.ptp(t[melhores]) > 1e5:
                deriva, theta0 = np.polyfit(t[melhores] - t_ref, th[melhores], 1)
            else:
                deriva, theta0 = 0.0, float(th[melhores].mean())
            self._ajuste = AjusteRelogio(float(theta0), float(deriva), t_ref, 0.0,
                                         len(melhores), len(de))

    @property
    def pronta(self) -> bool:
        return self._ajuste is not None

    def para_bancada(self, t_joy_us: float) -> float:
        if self._ajuste is None:
            raise RuntimeError("sincronia ainda sem trocas suficientes")
        return float(self._ajuste.para_bancada(t_joy_us))
