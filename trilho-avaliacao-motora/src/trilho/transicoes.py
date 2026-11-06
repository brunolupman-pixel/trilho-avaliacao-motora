"""Tabela de transições dos LM393 (código Gray na espiral, divisas no guarda-chuva).

O LM393 não dispara exatamente no ponto teórico e o ponto muda com o sentido
do movimento. Por isso a fusão não usa a posição teórica dos sensores: usa uma
tabela medida na calibração, com uma linha por divisa e por sentido.

Quem corrige a posição é a *divisa* (o instante da troca), não o código. O
código depois da troca é conferido com a sequência esperada como indicador de
qualidade (sensor falhando ou desalinhado).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from .geometria import Caminho


@dataclass(frozen=True)
class LinhaTransicao:
    divisa: int
    sensor: int
    sentido: str          # "ida" (s crescente) ou "volta"
    troca: str            # "liga" ou "desliga"
    codigo_depois: str    # ex.: "011" (S3 S2 S1)
    posicao_mm: float
    incerteza_mm: float


@dataclass
class TabelaTransicoes:
    caminho: str
    bancada_id: str
    versao: str
    data: str
    atraso_ir_s: float                    # atraso de resposta do LM393 (calibrado)
    linhas: list[LinhaTransicao] = field(default_factory=list)

    # ------------------------------------------------------------- consulta
    def candidatas(self, sensor: int, troca: str, sentido: str | None) -> list[LinhaTransicao]:
        return [l for l in self.linhas
                if l.sensor == sensor and l.troca == troca
                and (sentido is None or l.sentido == sentido)]

    def mais_proxima(self, sensor: int, troca: str, sentido: str | None,
                     s_previsto_mm: float) -> LinhaTransicao | None:
        cands = self.candidatas(sensor, troca, sentido)
        if not cands:
            return None
        return min(cands, key=lambda l: abs(l.posicao_mm - s_previsto_mm))

    # ---------------------------------------------------- arquivo versionado
    def salvar(self, caminho: str | Path) -> None:
        d = asdict(self)
        Path(caminho).write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def carregar(cls, caminho: str | Path) -> "TabelaTransicoes":
        d = json.loads(Path(caminho).read_text(encoding="utf-8"))
        d["linhas"] = [LinhaTransicao(**l) for l in d["linhas"]]
        return cls(**d)

    def como_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def de_dict(cls, d: dict) -> "TabelaTransicoes":
        d = dict(d)
        d["linhas"] = [LinhaTransicao(**l) for l in d["linhas"]]
        return cls(**d)


def _fronteiras(caminho: Caminho) -> list[tuple[float, int, str]]:
    """(posição, sensor, 'ini'|'fim') de todas as bordas internas ao caminho."""
    L = caminho.comprimento_mm
    out = []
    for sid, regs in caminho.regioes_sensores.items():
        for a, b in regs:
            if 0.0 < a < L:
                out.append((a, sid, "ini"))
            if 0.0 < b < L:
                out.append((b, sid, "fim"))
    return sorted(out)


def tabela_nominal(caminho: Caminho, incerteza_mm: float = 2.0,
                   bancada_id: str = "nominal", atraso_ir_s: float = 0.0) -> TabelaTransicoes:
    """Tabela teórica, a partir do projeto no CAD (antes da calibração)."""
    linhas = []
    eps = 1e-3
    for divisa, (pos, sid, borda) in enumerate(_fronteiras(caminho), start=1):
        # ida: entrar no intervalo liga, sair desliga; na volta é o contrário
        troca_ida = "liga" if borda == "ini" else "desliga"
        troca_volta = "desliga" if borda == "ini" else "liga"
        linhas.append(LinhaTransicao(divisa, sid, "ida", troca_ida,
                                     caminho.codigo(pos + eps), pos, incerteza_mm))
        linhas.append(LinhaTransicao(divisa, sid, "volta", troca_volta,
                                     caminho.codigo(pos - eps), pos, incerteza_mm))
    return TabelaTransicoes(caminho.id, bancada_id, "nominal", date.today().isoformat(),
                            atraso_ir_s, linhas)


@dataclass
class ObservacaoCalibracao:
    """Uma troca registrada durante a calibração, com a referência de posição."""

    sensor: int
    troca: str
    sentido: str
    posicao_ref_mm: float     # referência (encoder temporário) no instante do evento
    velocidade_mm_s: float    # velocidade da referência (com sinal) no instante


def construir_tabela(caminho: Caminho, observacoes: list[ObservacaoCalibracao],
                     bancada_id: str, versao: str,
                     incerteza_minima_mm: float = 0.5) -> TabelaTransicoes:
    """Monta a tabela a partir de passagens de calibração nos dois sentidos.

    Modelo: o evento é registrado τ segundos depois de o joystick passar pela
    posição de disparo p (própria de cada divisa e sentido). Então
        posicao_ref = p_linha + τ · velocidade
    Resolve-se por mínimos quadrados um p por linha e um τ comum (o "atraso e
    deslocamento do disparo IR" da calibração). A dispersão vira a incerteza.
    """
    nominal = tabela_nominal(caminho)
    chaves: list[tuple[int, str, str, int]] = []
    associacao = []
    for ob in observacoes:
        cands = nominal.candidatas(ob.sensor, ob.troca, ob.sentido)
        if not cands:
            continue
        lin = min(cands, key=lambda l: abs(l.posicao_mm - ob.posicao_ref_mm))
        chave = (lin.divisa, lin.sentido, lin.troca, lin.sensor)
        if chave not in chaves:
            chaves.append(chave)
        associacao.append((chaves.index(chave), ob))
    if not associacao:
        raise ValueError("nenhuma observação de calibração utilizável")
    n_lin = len(chaves)
    A = np.zeros((len(associacao), n_lin + 1))
    y = np.zeros(len(associacao))
    for i, (k, ob) in enumerate(associacao):
        A[i, k] = 1.0
        A[i, -1] = ob.velocidade_mm_s
        y[i] = ob.posicao_ref_mm
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    residuo = y - A @ coef
    tau = float(coef[-1])
    codigos = {(l.divisa, l.sentido): l.codigo_depois for l in nominal.linhas}
    linhas = []
    for k, (divisa, sentido, troca, sensor) in enumerate(chaves):
        r = residuo[[i for i, (kk, _) in enumerate(associacao) if kk == k]]
        inc = max(incerteza_minima_mm, float(np.std(r, ddof=1)) if len(r) > 1 else 2.0)
        linhas.append(LinhaTransicao(divisa, sensor, sentido, troca,
                                     codigos[(divisa, sentido)], round(float(coef[k]), 2),
                                     round(inc, 2)))
    linhas.sort(key=lambda l: (l.divisa, l.sentido != "ida"))
    return TabelaTransicoes(caminho.id, bancada_id, versao, date.today().isoformat(),
                            tau, linhas)
