"""Processamento depois da partida (arquitetura: processo "Pós-processamento").

Refaz a sincronia com a partida inteira, roda a fusão de novo sobre os dados
brutos com o suavizador RTS e calcula as métricas de movimento e a qualidade.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .aquisicao import Gravacao
from .fusao import VERSAO_ALGORITMO, ConfigFusao, reprocessar
from .metricas import VERSAO_METRICAS, metricas_movimento
from .sincronia import ModeloRelogio, ajustar_partida


@dataclass
class ResultadoPartida:
    series: dict
    metricas: dict
    qualidade: dict
    tabelas_brutas: dict            # nome -> np.ndarray (para Parquet)
    colunas: dict                   # nome -> lista de colunas
    versao: str = f"{VERSAO_ALGORITMO}+{VERSAO_METRICAS}"


def _arr(lista, ncol):
    return np.array(lista, dtype=float).reshape(-1, ncol) if lista else np.zeros((0, ncol))


def processar(g: Gravacao, tabela: list[dict], calib_vl53: dict | None,
              config: ConfigFusao | None = None) -> ResultadoPartida:
    imu = _arr(sorted(set(g.imu), key=lambda r: r[1]), 8)        # seq, t_joy, ax..gz
    sync = _arr(g.sync, 4)
    bordas = _arr(sorted(g.bordas), 3)
    vl53 = _arr(sorted(g.vl53), 3)

    # caixa: todos os sensores no mesmo relógio -> nada a converter
    modelo = ModeloRelogio(valido=True, residuo_us=0.0) if g.relogio_unico else ajustar_partida(sync)
    t_imu_b = modelo.para_bancada(imu[:, 1]) if len(imu) else np.zeros(0)
    imu_b = np.column_stack([t_imu_b, imu[:, 2:8]]) if len(imu) else np.zeros((0, 7))
    # só o que está dentro da partida (do INICIAR ao PARAR, com folga)
    if g.t_inicio_banc and g.t_fim_banc and len(imu_b):
        m = (imu_b[:, 0] >= g.t_inicio_banc - 2e5) & (imu_b[:, 0] <= g.t_fim_banc + 2e5)
        imu_b = imu_b[m]

    series, cont = reprocessar(g.caminho, tabela, calib_vl53, imu_b, bordas, vl53, config=config,
                               escala_giro=g.escala_giro)

    # a análise começa no fim da contagem (3 s) do jogo
    if g.t_inicio_banc and len(series["t_us"]):
        m = series["t_us"] >= g.t_inicio_banc + 3e6
        if m.sum() > 10:
            series = {k: v[m] for k, v in series.items()}

    metricas = metricas_movimento(series, g.caminho.comprimento, imu_b if len(imu_b) else None)

    # qualidade dos dados (arquitetura: "Indicadores de qualidade por sessão")
    faltando = 0
    if g.seqs:
        faixa = range(g.seq_inicio, max(g.seqs) + 1)
        faltando = len(faixa) - len(g.seqs & set(faixa))
    ovf = [s.get("overflow_fifo", 0) for _, s in g.status_joy]
    qualidade = {
        "sincronia": modelo.resumo(),
        "amostras_imu": int(len(imu)),
        "pacotes_nao_recuperados": int(faltando),
        "transbordos_fifo_imu": int(max(ovf) - min(ovf)) if len(ovf) > 1 else 0,
        "fusao": cont.como_dict(),
        "incerteza_posicao_mediana_mm": round(float(np.median(series["sigma_s"])), 2) if len(series["sigma_s"]) else None,
    }
    tabelas = {"imu": imu, "sync": sync, "bordas": bordas, "vl53": vl53,
               "series": np.column_stack([series[k] for k in ("t_us", "s", "v", "a", "j", "sigma_s")])
               if len(series["t_us"]) else np.zeros((0, 6))}
    colunas = {"imu": ["seq", "t_joy_us", "ax", "ay", "az", "gx", "gy", "gz"],
               "sync": ["t1", "t2", "t3", "t4"],
               "bordas": ["t_bancada_us", "sensor", "nivel"], "vl53": ["t_bancada_us", "dist_mm", "estado"],
               "series": ["t_bancada_us", "s_mm", "v_mm_s", "a_mm_s2", "j_mm_s3", "sigma_s_mm"]}
    return ResultadoPartida(series, metricas, qualidade, tabelas, colunas)
