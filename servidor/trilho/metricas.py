"""Métricas de movimento por partida (arquitetura: "Métricas clínicas").

Calculadas sobre as séries suavizadas (RTS). A suavidade usa SPARC como
métrica principal (depende só da velocidade) e o jerk adimensional em log
(LDLJ) como complementar. Unidades no resultado: mm, s, N, m/s².
"""
from __future__ import annotations

import math

import numpy as np

from .protocolo import ACC_LSB_POR_G, G_MM_S2

VERSAO_METRICAS = "metricas-1.0"


def reamostrar(series: dict, fs: float = 100.0) -> dict:
    t = series["t_us"] / 1e6
    if len(t) < 2:
        return {"t": np.zeros(0), **{k: np.zeros(0) for k in ("s", "v", "a", "j")}}
    tu, idx = np.unique(t, return_index=True)
    tn = np.arange(tu[0], tu[-1], 1 / fs)
    return {"t": tn, **{k: np.interp(tn, tu, np.asarray(series[k])[idx]) for k in ("s", "v", "a", "j")}}


def sparc(v: np.ndarray, fs: float, padnivel: int = 4, fc: float = 10.0, limiar: float = 0.05) -> float:
    """Spectral Arc Length (Balasubramanian et al., 2015). Mais perto de 0 = mais suave."""
    v = np.abs(np.asarray(v, dtype=float))
    if len(v) < 8 or v.max() <= 0:
        return float("nan")
    nfft = int(2 ** (math.ceil(math.log2(len(v))) + padnivel))
    f = np.arange(0, fs, fs / nfft)
    Mf = np.abs(np.fft.fft(v, nfft))
    Mf = Mf / Mf.max()
    sel = f <= fc
    f_sel, M_sel = f[sel], Mf[sel]
    acima = np.nonzero(M_sel >= limiar)[0]
    if len(acima) == 0:
        return float("nan")
    corte = acima[-1] + 1
    f_sel, M_sel = f_sel[:corte], M_sel[:corte]
    df = np.diff(f_sel) / (f_sel[-1] - f_sel[0]) if len(f_sel) > 1 else np.zeros(0)
    dM = np.diff(M_sel)
    return float(-np.sum(np.sqrt(df ** 2 + dM ** 2)))


def ldlj(v: np.ndarray, j: np.ndarray, fs: float) -> float:
    """Jerk adimensional em log, versão pela velocidade (LDLJ-V). Mais alto = mais suave."""
    if len(v) < 8:
        return float("nan")
    T = len(v) / fs
    vpico = float(np.max(np.abs(v)))
    if vpico <= 0:
        return float("nan")
    integral = float(np.sum(np.asarray(j) ** 2) / fs)
    if integral <= 0:
        return float("nan")
    return float(-math.log(T ** 3 / vpico ** 2 * integral))


def segmentar(v: np.ndarray, fs: float, v_min: float = 5.0, dur_min: float = 0.15, juntar: float = 0.1) -> list[tuple[int, int]]:
    """Trechos de movimento: |v| acima de max(5 mm/s, 5% do pico)."""
    if len(v) == 0:
        return []
    lim = max(v_min, 0.05 * float(np.max(np.abs(v))))
    mov = np.abs(v) > lim
    trechos, ini = [], None
    for i, m in enumerate(mov):
        if m and ini is None:
            ini = i
        elif not m and ini is not None:
            trechos.append([ini, i])
            ini = None
    if ini is not None:
        trechos.append([ini, len(v)])
    unidos: list[list[int]] = []
    for a, b in trechos:
        if unidos and (a - unidos[-1][1]) / fs < juntar:
            unidos[-1][1] = b
        else:
            unidos.append([a, b])
    return [(a, b) for a, b in unidos if (b - a) / fs >= dur_min]


def _arred(x, n=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), n)


def metricas_movimento(series: dict, comprimento: float, imu_bruto: np.ndarray | None = None) -> dict:
    fs = 100.0
    r = reamostrar(series, fs)
    t, s, v, a, j = r["t"], r["s"], r["v"], r["a"], r["j"]
    m: dict = {}
    if len(t) < 10:
        return {"observacao": "dados insuficientes"}
    trechos = segmentar(v, fs)
    m["duracao_s"] = _arred(t[-1] - t[0], 1)
    m["tempo_em_movimento_s"] = _arred(sum(b - a_ for a_, b in trechos) / fs, 1)
    m["tempo_de_reacao_s"] = _arred(trechos[0][0] / fs, 2) if trechos else None
    m["velocidade_media_mm_s"] = _arred(np.mean(np.abs(v[np.abs(v) > 5])) if np.any(np.abs(v) > 5) else 0.0, 1)
    ip = int(np.argmax(np.abs(v)))
    m["velocidade_pico_mm_s"] = _arred(abs(v[ip]), 1)
    if trechos:
        a0, b0 = next(((x, y) for x, y in trechos if x <= ip < y), trechos[0])
        m["pico_no_trecho_pct"] = _arred(100 * (ip - a0) / max(1, b0 - a0), 0)
    m["aceleracao_pico_mm_s2"] = _arred(np.max(a), 0)
    m["desaceleracao_pico_mm_s2"] = _arred(-np.min(a), 0)
    m["distancia_percorrida_mm"] = _arred(np.sum(np.abs(np.diff(s))), 0)
    m["amplitude_pct"] = _arred(100 * (np.max(s) - np.min(s)) / comprimento, 0)
    # suavidade por trecho de movimento (média)
    sp = [sparc(v[x:y], fs) for x, y in trechos if y - x >= 20]
    lj = [ldlj(v[x:y], j[x:y], fs) for x, y in trechos if y - x >= 20]
    sp = [x for x in sp if not math.isnan(x)]
    lj = [x for x in lj if not math.isnan(x)]
    m["sparc"] = _arred(np.mean(sp), 3) if sp else None
    m["ldlj"] = _arred(np.mean(lj), 2) if lj else None
    m["submovimentos"] = int(sum(_picos(np.abs(v[x:y])) for x, y in trechos))
    # pausas: |v| < 5 mm/s por pelo menos 0,5 s, entre o primeiro e o último movimento
    if trechos:
        ini, fim = trechos[0][0], trechos[-1][1]
        parado = np.abs(v[ini:fim]) < 5
        pausas, atual, maior = 0, 0, 0
        for p in parado:
            atual = atual + 1 if p else 0
            if atual == int(0.5 * fs):
                pausas += 1
            maior = max(maior, atual)
        m["pausas"] = pausas
        m["maior_pausa_s"] = _arred(maior / fs, 1)
    sinal = np.sign(v[np.abs(v) > 10])
    m["mudancas_de_sentido"] = int(np.sum(np.diff(sinal) != 0)) if len(sinal) > 1 else 0
    # tremor: RMS da aceleração entre 4 e 12 Hz (m/s²)
    if imu_bruto is not None and len(imu_bruto) > 500:
        m["tremor_rms_m_s2"] = _arred(_tremor(imu_bruto), 3)
    return m


def _picos(x: np.ndarray) -> int:
    if len(x) < 3:
        return 0
    lim = 0.1 * x.max()
    return int(np.sum((x[1:-1] > x[:-2]) & (x[1:-1] >= x[2:]) & (x[1:-1] > lim)))


def _tremor(imu: np.ndarray, fs: float = 500.0) -> float:
    from scipy.signal import butter, sosfiltfilt
    acc = imu[:, 1:4] * (G_MM_S2 / ACC_LSB_POR_G) / 1000.0
    sos = butter(4, [4.0, 12.0], btype="band", fs=fs, output="sos")
    filt = sosfiltfilt(sos, acc - acc.mean(axis=0), axis=0)
    return float(np.sqrt(np.mean(np.sum(filt ** 2, axis=1))))

