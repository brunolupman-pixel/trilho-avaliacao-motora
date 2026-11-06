import numpy as np
import pytest

from trilho.geometria import obter_caminho
from trilho.simulador import BancadaSimulada, debounce, simular_calibracao_ir
from trilho.sincronia import SincroniaAoVivo, ajustar_relogio
from trilho.transicoes import TabelaTransicoes, construir_tabela, tabela_nominal


def test_sincronia_abaixo_de_1ms():
    """Meta da arquitetura: erro de tempo ≤ 1 ms entre os relógios."""
    banc = BancadaSimulada(obter_caminho("C"), deriva_ppm=45.0)
    sinc = banc.trocas_sincronia(600.0, np.random.default_rng(1))   # sessão de 10 min
    aj = ajustar_relogio(sinc)
    t_joy = np.linspace(banc.t_joy(1.0), banc.t_joy(599.0), 500)
    erro = aj.para_bancada(t_joy * 1e6) / 1e6 - banc.t_banc(t_joy)
    assert np.max(np.abs(erro)) < 1e-3
    assert aj.deriva * 1e6 == pytest.approx(-45.0, abs=2.0)


def test_sincronia_ao_vivo():
    banc = BancadaSimulada(obter_caminho("C"))
    sinc = banc.trocas_sincronia(30.0, np.random.default_rng(2))
    s = SincroniaAoVivo()
    for r in sinc.itertuples(index=False):
        s.adicionar(*r)
    tj = banc.t_joy(25.0)
    assert abs(s.para_bancada(tj * 1e6) / 1e6 - 25.0) < 2e-3


def test_codigo_gray_da_espiral():
    cam = obter_caminho("B")
    tab = tabela_nominal(cam)
    ida = [l.codigo_depois for l in tab.linhas if l.sentido == "ida"]
    assert ida == ["001", "011", "010", "110", "111", "101", "100"]
    # só um sensor muda por divisa
    seq = ["000"] + ida
    for a, b in zip(seq, seq[1:]):
        assert sum(x != y for x, y in zip(a, b)) == 1


def test_debounce_filtra_trepidacao():
    # troca real em 1.000 s com trepidação de 1-2 ms e um pulso de 3 ms depois
    trocas = [(1.000, 1), (1.001, 0), (1.002, 1), (1.500, 0), (1.503, 1), (2.0, 0)]
    assert debounce(trocas, 0) == [(1.000, 1), (2.0, 0)]


def test_calibracao_recupera_atraso_e_posicoes(tmp_path):
    cam = obter_caminho("B")
    banc = BancadaSimulada(cam)
    tab = construir_tabela(cam, simular_calibracao_ir(banc), "BANC-007", "v1")
    assert tab.atraso_ir_s == pytest.approx(banc.atraso_ir_s, abs=0.0005)
    fisico = {(l.divisa, l.sentido): p for l, p in banc.disparos}
    for l in tab.linhas:
        assert l.posicao_mm == pytest.approx(fisico[(l.divisa, l.sentido)], abs=1.0)
    # histerese: a ida dispara depois da volta
    for d in range(1, 8):
        ida = next(l for l in tab.linhas if l.divisa == d and l.sentido == "ida")
        volta = next(l for l in tab.linhas if l.divisa == d and l.sentido == "volta")
        assert ida.posicao_mm > volta.posicao_mm
    tab.salvar(tmp_path / "t.json")
    assert TabelaTransicoes.carregar(tmp_path / "t.json") == tab
