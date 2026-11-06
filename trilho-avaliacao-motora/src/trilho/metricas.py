"""Segmentação das tentativas e métricas clínicas.

As métricas são calculadas por tentativa (um percurso do início ao fim) e
resumidas por sessão com média e variabilidade. A lista é o ponto de partida
da arquitetura, para validar com a equipe clínica.

SPARC usa só a velocidade e por isso é a métrica principal de suavidade; as
de jerk (LDLJ) ficam como complementares (ver "Nível do sinal em movimentos
lentos").
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from .geometria import Caminho

LIMIAR_VEL = 0.010        # m/s: abaixo disso o joystick está parado
REPOUSO_MIN_S = 0.25      # repouso mínimo entre tentativas
PAUSA_MIN_S = 0.15        # pausa mínima dentro de uma tentativa
ZONA_TERMINAL_MM = 15.0   # distância à ponta para considerar o percurso completo


# ================================================================= segmentação
@dataclass
class Tentativa:
    numero: int
    i0: int                 # índice do início do movimento
    i1: int                 # índice do fim
    t0: float
    t1: float
    s_inicio_mm: float
    s_fim_mm: float
    completa: bool
    t_estimulo: float | None = None


def _intervalos(mascara: np.ndarray) -> list[tuple[int, int]]:
    """Trechos contíguos [i, j) em que a máscara é verdadeira."""
    m = np.concatenate([[False], mascara, [False]]).astype(int)
    d = np.diff(m)
    return list(zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]))


def segmentar(serie: pd.DataFrame, caminho: Caminho,
              eventos: pd.DataFrame | None = None) -> list[Tentativa]:
    """Divide a partida em tentativas, entre repousos nas pontas do caminho.

    Guarda-chuva e retilíneo: de uma ponta à outra. Espiral: da borda até o
    centro e de volta à borda. Repousos fora das pontas são pausas.
    """
    t = serie["t"].to_numpy()
    s_mm = serie["s"].to_numpy() * 1000
    v = serie["v"].to_numpy()
    dt = float(np.median(np.diff(t)))
    L = caminho.comprimento_mm
    parado = np.abs(v) < LIMIAR_VEL
    repousos = [(i, j) for i, j in _intervalos(parado) if (j - i) * dt >= REPOUSO_MIN_S]

    def ponta(i: int, j: int) -> float | None:
        s = float(np.median(s_mm[i:j]))
        if s < ZONA_TERMINAL_MM:
            return 0.0
        # na espiral o centro é a virada da tentativa, não um ponto de partida
        if s > L - ZONA_TERMINAL_MM and not caminho.vai_e_volta:
            return L
        return None

    terminais = [(i, j, ponta(i, j)) for i, j in repousos if ponta(i, j) is not None]
    estimulos = []
    if eventos is not None and len(eventos):
        estimulos = sorted(eventos.loc[eventos["tipo"] == "estimulo", "t"].tolist())
    tentativas: list[Tentativa] = []
    for (a0, a1, pa), (b0, b1, pb) in zip(terminais, terminais[1:]):
        i0, i1 = a1, b0
        if i1 - i0 < 3:
            continue
        if caminho.vai_e_volta:
            completa = pa == pb == 0.0 and s_mm[i0:i1].max() > L - 2 * ZONA_TERMINAL_MM
        else:
            completa = pa != pb
        if not completa and np.ptp(s_mm[i0:i1]) < 0.3 * L:
            continue      # agitação no lugar, não é tentativa
        est = [e for e in estimulos if t[a0] - 0.05 <= e <= t[i0]]
        tentativas.append(Tentativa(len(tentativas) + 1, int(i0), int(i1), float(t[i0]),
                                    float(t[i1]), float(s_mm[i0]), float(s_mm[i1]),
                                    bool(completa), est[-1] if est else None))
    return tentativas


# ===================================================================== métricas
def sparc(velocidade: np.ndarray, fs: float, padlevel: int = 4, fc: float = 10.0,
          limiar_amp: float = 0.05) -> float:
    """Spectral Arc Length (Balasubramanian et al., 2015). Mais perto de zero = mais suave."""
    v = np.abs(np.asarray(velocidade, float))
    if len(v) < 4 or v.max() <= 0:
        return float("nan")
    nfft = int(2 ** (np.ceil(np.log2(len(v))) + padlevel))
    f = np.arange(0, fs, fs / nfft)
    Mf = np.abs(np.fft.fft(v, nfft))
    Mf = Mf / Mf.max()
    sel = f <= fc
    f_sel, M_sel = f[sel], Mf[sel]
    acima = np.nonzero(M_sel >= limiar_amp)[0]
    f_sel = f_sel[acima[0]:acima[-1] + 1]
    M_sel = M_sel[acima[0]:acima[-1] + 1]
    if len(f_sel) < 2:
        return 0.0
    df = np.diff(f_sel) / (f_sel[-1] - f_sel[0])
    return float(-np.sum(np.sqrt(df ** 2 + np.diff(M_sel) ** 2)))


def ldlj(velocidade: np.ndarray, jerk: np.ndarray, dt: float) -> float:
    """Log dimensionless jerk (versão por velocidade): -ln(T³/v_pico² ∫ j² dt)."""
    T = len(velocidade) * dt
    vp = float(np.max(np.abs(velocidade)))
    if vp <= 0:
        return float("nan")
    integral = float(np.sum(np.asarray(jerk) ** 2) * dt)
    return float(-np.log(T ** 3 / vp ** 2 * integral))


def _pausas(v: np.ndarray, dt: float, excluir: np.ndarray | None = None) -> list[float]:
    parado = np.abs(v) < LIMIAR_VEL
    if excluir is not None:
        parado &= ~excluir
    return [(j - i) * dt for i, j in _intervalos(parado) if (j - i) * dt >= PAUSA_MIN_S]


def _retrocessos(v: np.ndarray, s: np.ndarray, sentido_esperado: np.ndarray) -> int:
    """Trechos andando contra o sentido esperado por mais de 2 mm."""
    contra = (np.sign(v) == -sentido_esperado) & (np.abs(v) > LIMIAR_VEL)
    n = 0
    for i, j in _intervalos(contra):
        if abs(s[j - 1] - s[i]) > 0.002:
            n += 1
    return n


@dataclass
class ConfigMetricas:
    forca_alvo_N: tuple[float, float] = (0.0, 30.0)   # faixa-alvo de preensão


def metricas_tentativa(serie: pd.DataFrame, tent: Tentativa, caminho: Caminho,
                       forca: pd.DataFrame | None = None,
                       cfg: ConfigMetricas | None = None) -> dict:
    cfg = cfg or ConfigMetricas()
    seg = serie.iloc[tent.i0:tent.i1 + 1]
    t = seg["t"].to_numpy()
    s = seg["s"].to_numpy()
    v = seg["v"].to_numpy()
    a = seg["a"].to_numpy()
    j = seg["j"].to_numpy()
    dt = float(np.median(np.diff(t)))
    fs = 1.0 / dt
    T = t[-1] - t[0]
    rapidez = np.abs(v)
    L = caminho.comprimento_mm / 1000

    if caminho.vai_e_volta:
        i_virada = int(np.argmax(s))
        esperado = np.where(np.arange(len(s)) <= i_virada, 1.0, -1.0)
        # a parada na virada (centro da espiral) não conta como pausa
        excluir = s > L - ZONA_TERMINAL_MM / 1000 * 2
        t_ida, t_volta = t[i_virada] - t[0], t[-1] - t[i_virada]
    else:
        esperado = np.full(len(s), np.sign(s[-1] - s[0]) or 1.0)
        excluir = None
        t_ida = t_volta = None

    a_mov = a * np.sign(v)          # aceleração no sentido do movimento
    em_mov = rapidez > LIMIAR_VEL
    picos, _ = find_peaks(rapidez, height=0.1 * rapidez.max(), prominence=0.1 * rapidez.max(),
                          distance=max(1, int(0.1 * fs)))
    pausas = _pausas(v, dt, excluir)
    i_pico = int(np.argmax(rapidez))
    m = {
        "tentativa": tent.numero,
        "completa": tent.completa,
        "t_inicio_s": tent.t0,
        "tempo_movimento_s": T,
        "tempo_reacao_s": (tent.t0 - tent.t_estimulo) if tent.t_estimulo is not None else None,
        "distancia_mm": float(np.sum(np.abs(np.diff(s))) * 1000),
        "amplitude_pct": float((s.max() - s.min()) / L * 100),
        "velocidade_media_m_s": float(np.sum(np.abs(np.diff(s))) / T) if T > 0 else None,
        "velocidade_pico_m_s": float(rapidez.max()),
        "instante_pico_pct": float((t[i_pico] - t[0]) / T * 100) if T > 0 else None,
        "aceleracao_pico_m_s2": float(np.max(a_mov[em_mov])) if em_mov.any() else 0.0,
        "desaceleracao_pico_m_s2": float(-np.min(a_mov[em_mov])) if em_mov.any() else 0.0,
        "submovimentos": int(len(picos)),
        "sparc": sparc(v, fs),
        "ldlj": ldlj(v, j, dt),
        "pausas": len(pausas),
        "pausas_duracao_s": float(np.sum(pausas)),
        "retrocessos": _retrocessos(v, s, esperado),
        "tempo_ida_s": t_ida,
        "tempo_volta_s": t_volta,
    }
    if forca is not None and len(forca):
        sel = (forca["t"] >= t[0]) & (forca["t"] <= t[-1])
        F = forca.loc[sel, "forca_N"].to_numpy()
        tf = forca.loc[sel, "t"].to_numpy()
        if len(F) > 2:
            dtf = float(np.median(np.diff(tf)))
            lo, hi = cfg.forca_alvo_N
            fora = (F < lo) | (F > hi)
            m.update({
                "forca_media_N": float(F.mean()),
                "forca_pico_N": float(F.max()),
                "forca_cv": float(F.std() / F.mean()) if F.mean() > 0 else None,
                "forca_fora_faixa_s": float(fora.sum() * dtf),
                "forca_fora_faixa_pct": float(fora.mean() * 100),
            })
    return m


def metricas_partida(serie: pd.DataFrame, caminho: Caminho, forca: pd.DataFrame | None = None,
                     eventos: pd.DataFrame | None = None,
                     cfg: ConfigMetricas | None = None) -> pd.DataFrame:
    tentativas = segmentar(serie, caminho, eventos)
    return pd.DataFrame([metricas_tentativa(serie, t, caminho, forca, cfg) for t in tentativas])


def resumo_sessao(por_tentativa: pd.DataFrame) -> dict:
    """Média e desvio padrão de cada métrica, só das tentativas completas."""
    if por_tentativa.empty:
        return {"n_tentativas": 0}
    ok = por_tentativa[por_tentativa["completa"]]
    resumo: dict = {"n_tentativas": int(len(por_tentativa)), "n_completas": int(len(ok))}
    for col in ok.columns:
        if col in ("tentativa", "completa", "t_inicio_s"):
            continue
        x = pd.to_numeric(ok[col], errors="coerce").dropna()
        if len(x):
            resumo[col] = {"media": float(x.mean()),
                           "dp": float(x.std(ddof=1)) if len(x) > 1 else 0.0}
    return resumo
