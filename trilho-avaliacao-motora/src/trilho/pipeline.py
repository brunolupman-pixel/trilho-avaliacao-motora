"""Fluxo de pós-processamento de uma partida: bruto → fusão → métricas → banco."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .armazenamento import Banco, ContextoApp
from .dados import DadosBrutos
from .fusao import ConfigFusao, ResultadoFusao, processar_partida
from .geometria import obter_caminho
from .metricas import ConfigMetricas, metricas_partida, resumo_sessao
from .transicoes import TabelaTransicoes


@dataclass
class ResultadoPartida:
    fusao: ResultadoFusao
    por_tentativa: pd.DataFrame
    resumo: dict


def analisar(dados: DadosBrutos, tabela: TabelaTransicoes | None,
             cfg_fusao: ConfigFusao | None = None,
             cfg_metricas: ConfigMetricas | None = None) -> ResultadoPartida:
    caminho = obter_caminho(dados.meta["caminho"])
    fus = processar_partida(dados, caminho, tabela, cfg_fusao)
    por_tentativa = metricas_partida(fus.serie, caminho, fus.forca, fus.eventos, cfg_metricas)
    return ResultadoPartida(fus, por_tentativa, resumo_sessao(por_tentativa))


def processar_e_salvar(banco: Banco, ctx: ContextoApp, partida_id: str,
                       tabela: TabelaTransicoes | None,
                       cfg_metricas: ConfigMetricas | None = None) -> ResultadoPartida:
    """Processa (ou reprocessa) uma partida a partir dos dados brutos guardados."""
    dados = banco.carregar_bruto(partida_id)
    res = analisar(dados, tabela, cfg_metricas=cfg_metricas)
    q = dict(res.fusao.qualidade)
    q["ajuste_relogio"] = res.fusao.ajuste_relogio.como_dict()
    q["tabela_transicoes"] = None if tabela is None else tabela.versao
    serie = res.fusao.serie.copy()
    if res.fusao.forca is not None:
        serie = pd.merge_asof(serie, res.fusao.forca.sort_values("t"), on="t", direction="nearest")
    banco.salvar_resultado(ctx, partida_id, serie, res.por_tentativa, q)
    return res
