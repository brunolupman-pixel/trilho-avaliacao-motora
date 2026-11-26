import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "servidor"))

from trilho.aquisicao import Aquisicao  # noqa: E402
from trilho.caminhos import tabela_padrao  # noqa: E402
from trilho.simulador import GeradorSimulacao  # noqa: E402

CALIB_RETO = {"tras": 50.0, "frente": 170.0}


class PipelineVirtual:
    """Simulador -> núcleo de aquisição em tempo virtual (sem threads, rápido e repetível)."""

    def __init__(self, jid: str, perda: float = 0.01, semente: int = 3, caixa: bool = False, **kw):
        self.gen = GeradorSimulacao(semente=semente, perda=perda, ativo=jid, caixa=caixa, **kw)
        gen = self.gen

        class Porta:
            def write(self, d):
                gen.comando(d)
                return len(d)

        self.aq = Aquisicao(lambda: None, relogio=lambda: gen.tv)
        self.aq.porta = Porta()

    def avancar(self, dt: float) -> None:
        alvo = self.gen.tv + dt
        while self.gen.tv < alvo - 1e-9:
            for q in self.gen.avancar(self.gen.tv + 0.002):
                self.aq.passo(q)
            self.aq.passo(b"")

    def partida(self, caminho: str, dur: float):
        self.avancar(1.0)
        tab = tabela_padrao(caminho)
        self.aq.iniciar_partida(1, caminho, tab, CALIB_RETO)
        erros = []
        fim = self.gen.tv + dur
        while self.gen.tv < fim:
            self.avancar(0.01)
            e = self.aq.estado_publico()["partida"]
            if e and "s_mm" in e and self.gen.tv - self.aq.gravacao.inicio_host > 3.5:
                erros.append(e["s_mm"] - self.gen.registro_verdade[-1][1])
        g = self.aq.finalizar_partida(dormir=self.avancar)
        return g, tab, np.array(erros)


@pytest.fixture
def pipeline():
    return PipelineVirtual
