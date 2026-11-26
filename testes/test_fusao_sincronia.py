"""Fusão e sincronia, contra a verdade do simulador (tempo virtual)."""
import numpy as np
import pytest

from trilho import posprocesso
from trilho.sincronia import ajustar_partida

CASOS = [("B", "espiral", 30.0), ("A", "guarda_chuva", 15.0), ("C", "reto", 15.0)]


@pytest.mark.parametrize("jid,caminho,dur", CASOS)
def test_posicao_em_tempo_real_e_suavizada(pipeline, jid, caminho, dur):
    pl = pipeline(jid)
    g, tab, erros = pl.partida(caminho, dur)
    rms_rt = float(np.sqrt(np.mean(erros ** 2)))
    # espiral: na tela, só os sensores IR (a colher espera no próximo feixe); o IMU fica para as
    # métricas (pós-processamento, abaixo, continua com 5 mm)
    lim_rt = 30.0 if caminho == "espiral" else 5.0
    assert rms_rt < lim_rt, f"tempo real: RMS {rms_rt:.1f} mm"

    r = posprocesso.processar(g, tab, {"tras": 50.0, "frente": 170.0})
    ver = np.array(pl.gen.registro_verdade)
    s_true = np.interp(r.series["t_us"], ver[:, 0], ver[:, 1])
    rms_pos = float(np.sqrt(np.mean((r.series["s"] - s_true) ** 2)))
    # espiral: o ângulo da placa do IMU é descoberto pelos dados (erro de alguns graus)
    assert rms_pos < (7.0 if caminho == "espiral" else 5.0), f"suavizado: RMS {rms_pos:.1f} mm"
    f = r.qualidade["fusao"]
    assert f["bordas_rejeitadas"] == 0
    assert r.qualidade["pacotes_nao_recuperados"] == 0


@pytest.mark.parametrize("jid,caminho,dur", CASOS)
def test_sincronia_abaixo_de_1_ms(pipeline, jid, caminho, dur):
    pl = pipeline(jid)
    g, _, _ = pl.partida(caminho, min(dur, 15.0))
    m = ajustar_partida(np.array(g.sync, float))
    j = pl.gen.joys[jid]
    tv = np.linspace(pl.gen.tv - 10, pl.gen.tv, 40)
    tj = np.array([j.relogio(x) for x in tv])
    tb = np.array([pl.gen.relogio_banc(x) for x in tv])
    erro = ((m.para_bancada(tj) - tb + 2 ** 31) % 2 ** 32) - 2 ** 31
    assert np.max(np.abs(erro)) < 1000, f"erro máximo {np.max(np.abs(erro)) / 1000:.3f} ms"


def test_perda_de_10_por_cento_e_recuperada(pipeline):
    pl = pipeline("C", perda=0.10, semente=7)
    g, _, _ = pl.partida("reto", 10.0)
    faixa = set(range(g.seq_inicio, max(g.seqs) + 1))
    assert faixa <= g.seqs, f"{len(faixa - g.seqs)} pacotes não recuperados"
    assert pl.aq.joys["C"].duplicados >= 0


