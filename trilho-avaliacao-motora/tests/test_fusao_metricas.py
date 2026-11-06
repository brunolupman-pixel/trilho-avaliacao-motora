"""Testes da fusão e das métricas com sinais sintéticos de resposta conhecida."""

import numpy as np
import pytest

from trilho.fusao import processar_partida, remover_pulsos_espurios
from trilho.geometria import obter_caminho
from trilho.metricas import ldlj, metricas_partida, segmentar, sparc
from trilho.simulador import (BancadaSimulada, PerfilPaciente, simular_calibracao_ir,
                              simular_partida)
from trilho.transicoes import construir_tabela


def _rmse(a, b):
    return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(b)) ** 2)))


@pytest.fixture(scope="module")
def tabelas():
    out = {}
    for cid in "AB":
        cam = obter_caminho(cid)
        out[cid] = construir_tabela(cam, simular_calibracao_ir(BancadaSimulada(cam)), "b", "v1")
    out["C"] = None
    return out


@pytest.mark.parametrize("cid,limite_mm", [("C", 1.0), ("A", 1.5), ("B", 3.0)])
def test_fusao_posicao(cid, limite_mm, tabelas):
    cam = obter_caminho(cid)
    r = simular_partida(cam, 1 if cam.vai_e_volta else 4, PerfilPaciente(velocidade_media_mm_s=70),
                        semente=4)
    f = processar_partida(r.dados, cam, tabelas[cid])
    assert _rmse(f.serie["s"], r.verdade["s"]) * 1000 < limite_mm
    # velocidade e aceleração (ao longo do trilho)
    assert _rmse(f.serie["v"], r.verdade["v"]) * 1000 < 5.0
    assert _rmse(f.serie["a"], r.verdade["a"]) < 0.03
    assert f.qualidade["erro_sincronia_rms_us"] < 1000


def test_suavizador_melhor_que_tempo_real(tabelas):
    cam = obter_caminho("A")
    r = simular_partida(cam, 4, semente=6)
    f = processar_partida(r.dados, cam, tabelas["A"])
    assert _rmse(f.serie["s"], r.verdade["s"]) < _rmse(f.serie["s_rt"], r.verdade["s"])


def test_disparos_espurios_nao_entram(tabelas):
    cam = obter_caminho("A")
    banc = BancadaSimulada(cam, espurios_por_min=20.0)
    r = simular_partida(cam, 4, bancada=banc, semente=8)
    _, n = remover_pulsos_espurios(r.dados.ir, 0.04)
    assert n > 0
    f = processar_partida(r.dados, cam, tabelas["A"])
    assert f.qualidade["pulsos_espurios"] == n
    assert _rmse(f.serie["s"], r.verdade["s"]) * 1000 < 2.0


def test_segmentacao_e_tempo_de_reacao(tabelas):
    cam = obter_caminho("C")
    r = simular_partida(cam, 5, PerfilPaciente(prob_pausa=0, prob_retrocesso=0), semente=9)
    f = processar_partida(r.dados, cam, None)
    tent = segmentar(f.serie, cam, f.eventos)
    assert len(tent) == 5 and all(t.completa for t in tent)
    for t, v in zip(tent, r.trajetoria.tentativas):
        assert t.t0 == pytest.approx(v["inicio"], abs=0.25)
        assert t.t1 == pytest.approx(v["fim"], abs=0.35)
        assert (t.t0 - t.t_estimulo) == pytest.approx(v["inicio"] - v["estimulo"], abs=0.25)


def test_sparc_e_ldlj_ordenam_suavidade():
    fs = 500.0
    t = np.arange(0, 2, 1 / fs)
    tau = t / 2
    v_suave = 30 * tau**2 - 60 * tau**3 + 30 * tau**4                # jerk mínimo
    v_quebrado = v_suave * (1 + 0.5 * np.sin(2 * np.pi * 3 * t)) ** 2  # vários picos
    assert sparc(v_suave, fs) > sparc(v_quebrado, fs)
    assert sparc(v_suave, fs) == pytest.approx(-1.42, abs=0.1)        # valor de referência
    j_s = np.gradient(np.gradient(v_suave, t), t)
    j_q = np.gradient(np.gradient(v_quebrado, t), t)
    assert ldlj(v_suave, j_s, 1 / fs) > ldlj(v_quebrado, j_q, 1 / fs)


def test_metricas_preensao_e_submovimentos():
    cam = obter_caminho("C")
    suave = simular_partida(cam, 4, PerfilPaciente(submovimentos_max=1, prob_pausa=0,
                                                   prob_retrocesso=0, forca_base_N=20), semente=2)
    quebrado = simular_partida(cam, 4, PerfilPaciente(submovimentos_max=4, prob_pausa=0.6,
                                                      prob_retrocesso=0, forca_base_N=40), semente=2)
    ms = metricas_partida(*_proc(suave, cam))
    mq = metricas_partida(*_proc(quebrado, cam))
    assert ms["submovimentos"].mean() < mq["submovimentos"].mean()
    assert ms["sparc"].mean() > mq["sparc"].mean()
    assert ms["forca_media_N"].mean() == pytest.approx(
        suave.forca_verdadeira.query("forca_N > 0")["forca_N"].mean(), rel=0.15)
    assert ms["forca_media_N"].mean() < mq["forca_media_N"].mean()


def _proc(r, cam):
    f = processar_partida(r.dados, cam, None)
    return f.serie, cam, f.forca, f.eventos


def test_espiral_tentativa_vai_e_volta(tabelas):
    """A virada no centro da espiral não encerra a tentativa."""
    cam = obter_caminho("B")
    r = simular_partida(cam, 1, PerfilPaciente(velocidade_media_mm_s=75), semente=3)
    f = processar_partida(r.dados, cam, tabelas["B"])
    m = metricas_partida(f.serie, cam, None, f.eventos)
    assert len(m) == 1 and bool(m["completa"].iloc[0])
    v = r.trajetoria.tentativas[0]
    assert m["tempo_movimento_s"].iloc[0] == pytest.approx(v["fim"] - v["inicio"], rel=0.15)
    assert m["tempo_ida_s"].iloc[0] > 0 and m["tempo_volta_s"].iloc[0] > 0
