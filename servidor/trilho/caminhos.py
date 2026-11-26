"""Geometria dos três caminhos e tabelas de transição padrão dos LM393.

Cada caminho é uma polilinha amostrada a cada 0,5 mm no plano da placa
(x = lado de 425 mm, y = lado de 250 mm), com a posição s (mm) medida ao longo
do caminho. A fusão usa a tangente em s para projetar a aceleração da IMU.

PREMISSAS (substituir pelas medidas do CAD quando estiverem prontas):
- reto: 120 mm, s = 0 na ponta de trás (onde fica o VL53L0X), +y para frente;
- guarda-chuva: arco de 170 mm (semicírculo de raio 170/π), s = 0 na ponta
  esquerda, junção com a haste no meio (s = 85);
- espiral: 1410 mm, começa na borda (raio 95 mm) e termina no centro (15 mm).

SENSORES IR (montagem da caixa, código da equipe de hardware):
- espiral: cada sensor vê o joystick quando ele PASSA na frente do feixe
  (cruzamento). Na ida a ordem é S1, S2, S3, S1, S3 e depois o trecho final até
  o centro. As posições foram estimadas pela foto da placa (ângulo de cada
  sensor em volta do centro) e são provisórias até a calibração;
- guarda-chuva: um sensor em cada ponta do arco (A = esquerda, B = direita).
  O IMU desse joystick gira junto com o arco: o giroscópio mede a velocidade.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from dataclasses import dataclass, field

import numpy as np

# Sensores IR (índices iguais aos do firmware da caixa)
SENSOR_ARCO_ESQ, SENSOR_ARCO_DIR, SENSOR_ESP_S1, SENSOR_ESP_S2, SENSOR_ESP_S3 = 0, 1, 2, 3, 4
SENSORES_ESPIRAL = [SENSOR_ESP_S1, SENSOR_ESP_S2, SENSOR_ESP_S3]


@dataclass
class Caminho:
    id: str
    nome: str
    joystick: str
    comprimento: float
    pontos: np.ndarray                  # (N, 2) mm
    s: np.ndarray                       # (N,) mm acumulado
    inicio: float                       # posição onde a partida começa
    batentes: list[float] = field(default_factory=list)  # fins de fenda (pontas físicas)
    sensores: list[int] = field(default_factory=list)
    usa_vl53: bool = False
    giro_acompanha: bool = False       # o IMU gira junto com o caminho (giroscópio mede a velocidade)
    suavizar_curvatura_mm: float = 4.5  # janela da média da curvatura (geometria digitalizada: maior)

    def __post_init__(self) -> None:
        # tangente, curvatura (com sinal) e sua derivada, pré-calculadas em cada ponto
        d = np.gradient(self.pontos, self.s, axis=0)
        n = np.hypot(d[:, 0], d[:, 1])
        n[n == 0] = 1.0
        self._tg = d / n[:, None]
        dd = np.gradient(self._tg, self.s, axis=0)
        k = self._tg[:, 0] * dd[:, 1] - self._tg[:, 1] * dd[:, 0]
        # suaviza (o pino tem tamanho; evita ruído numérico e o "degrau" de pixels da digitalização)
        passo = float(np.median(np.diff(self.s))) or 0.5
        w = max(3, int(round(self.suavizar_curvatura_mm / passo)) | 1)
        if len(k) > w:
            k = np.convolve(np.pad(k, w // 2, mode="edge"), np.ones(w) / w, mode="valid")
        self._k = k
        self._dk = np.gradient(k, self.s)
        # ângulo da tangente (desenrolado): quanto o joystick "gira" ao andar no caminho
        self._phi = np.unwrap(np.arctan2(self._tg[:, 1], self._tg[:, 0]))

    def angulo_tangente(self, s: float) -> float:
        return float(np.interp(s, self.s, self._phi))

    def geometria(self, s: float) -> tuple[np.ndarray, np.ndarray, float, float]:
        """Tangente T, normal à esquerda N, curvatura κ e dκ/ds em s."""
        s = min(max(s, self.s[0]), self.s[-1])
        i = int(np.clip(np.searchsorted(self.s, s), 1, len(self.s) - 1))
        u = (s - self.s[i - 1]) / (self.s[i] - self.s[i - 1])
        T = self._tg[i - 1] * (1 - u) + self._tg[i] * u
        T = T / math.hypot(T[0], T[1])
        N = np.array([-T[1], T[0]])
        k = float(self._k[i - 1] * (1 - u) + self._k[i] * u)
        dk = float(self._dk[i - 1] * (1 - u) + self._dk[i] * u)
        return T, N, k, dk

    def _indice(self, s: float) -> int:
        return int(np.clip(np.searchsorted(self.s, s), 1, len(self.s) - 1))

    def ponto(self, s: float) -> np.ndarray:
        i = self._indice(s)
        s0, s1 = self.s[i - 1], self.s[i]
        u = 0.0 if s1 == s0 else (np.clip(s, s0, s1) - s0) / (s1 - s0)
        return self.pontos[i - 1] * (1 - u) + self.pontos[i] * u

    def tangente(self, s: float) -> np.ndarray:
        return self.geometria(s)[0]

    def curvatura(self, s: float) -> float:
        """Curvatura com sinal (1/mm): positiva quando a curva vira à esquerda."""
        return self.geometria(s)[2]


def _polilinha(xy: np.ndarray, comprimento_alvo: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    d = np.hypot(*np.diff(xy, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(d)])
    if comprimento_alvo:
        s *= comprimento_alvo / s[-1]
    # reamostra a cada 0,5 mm
    sn = np.arange(0.0, s[-1] + 1e-9, 0.5)
    if sn[-1] < s[-1]:
        sn = np.append(sn, s[-1])
    x = np.interp(sn, s, xy[:, 0])
    y = np.interp(sn, s, xy[:, 1])
    return np.column_stack([x, y]), sn


def _reto() -> Caminho:
    xy = np.array([[0.0, 0.0], [0.0, 120.0]])
    p, s = _polilinha(xy)
    return Caminho("reto", "Reto", "C", float(s[-1]), p, s, inicio=60.0, batentes=[0.0, 120.0], usa_vl53=True)


def _guarda_chuva() -> Caminho:
    r = 170.0 / math.pi
    th = np.linspace(math.pi, 0.0, 2000)
    xy = np.column_stack([r * np.cos(th), r * np.sin(th)])
    p, s = _polilinha(xy, 170.0)
    return Caminho("guarda_chuva", "Guarda-chuva", "A", 170.0, p, s, inicio=0.0, batentes=[0.0, 170.0],
                   sensores=[SENSOR_ARCO_ESQ, SENSOR_ARCO_DIR], giro_acompanha=True)


ARQUIVO_ESPIRAL = Path(__file__).parent / "geometria" / "espiral.json"
RAIO_EXTERNO_ESPIRAL_MM = 66.489   # borda externa da peça (projeto no Onshape)
LARGURA_CANAL_ESPIRAL_MM = 10.0    # largura do canal da espiral (projeto)


def geometria_espiral() -> dict:
    """Linha central do canal da espiral, em mm, com o centro da peça na origem (x à direita,
    y para cima, vista de cima). Tirada do projeto no Onshape: começa na ponta da entrada (embaixo
    à esquerda), dá duas voltas no sentido anti-horário e termina no bolsão central, na ponta do
    gancho. O mesmo arquivo desenha a espiral no jogo."""
    return json.loads(ARQUIVO_ESPIRAL.read_text(encoding="utf-8"))


def _espiral() -> Caminho:
    xy = np.array(geometria_espiral()["pontos_mm"], dtype=float)
    p, s = _polilinha(xy)
    L = float(s[-1])
    return Caminho("espiral", "Espiral", "B", L, p, s, inicio=0.0, batentes=[0.0, L],
                   sensores=list(SENSORES_ESPIRAL), suavizar_curvatura_mm=15.0)


CAMINHOS: dict[str, Caminho] = {c.id: c for c in (_reto(), _guarda_chuva(), _espiral())}

# Jogos do site -> caminho
JOGO_CAMINHO = {"carro": "reto", "vidro": "guarda_chuva", "bolo": "espiral"}
JOYSTICK_CAMINHO = {c.joystick: c.id for c in CAMINHOS.values()}


# ---------------------------------------------------------------------------
# Tabelas de transição padrão (provisórias até a calibração)
# ---------------------------------------------------------------------------
# Cada sensor IR "vê" o joystick numa faixa de ±MEIA_LARGURA_FEIXE em volta do
# ponto em que o feixe cruza o caminho (largura do feixe + diâmetro do pino).
MEIA_LARGURA_FEIXE = 7.0

# Espiral: direção de cada sensor vista do centro da peça, em horas de relógio (foto da placa de
# 18/11): S1 embaixo à direita (~5 h), S2 em cima (~1 h), S3 embaixo à esquerda (~7 h 30). O feixe
# cruza o canal a cada volta, mas só alcança as DUAS voltas de fora: no bolsão central não chega.
# Resultado na ida: S1, S2, S3, S1, S2, S3; depois do último S3 vem o "beco" até o centro
# (a chegada é estimada: ver FiltroCaminho._beco).
HORAS_SENSORES_ESPIRAL = [(SENSOR_ESP_S1, 5.0), (SENSOR_ESP_S2, 1.0), (SENSOR_ESP_S3, 7.5)]
CRUZAMENTOS_POR_SENSOR = 2        # alcance do feixe: as duas voltas de fora
INCERTEZA_ESPIRAL_MM = 10.0       # direção dos sensores tirada da foto: incerteza maior até calibrar


def _cruzamentos_raio(c: Caminho, horas: float) -> list[float]:
    """Posições s (mm) em que a semirreta do centro na direção `horas` cruza o caminho."""
    th = math.radians(90.0 - 30.0 * horas)
    u = np.array([math.cos(th), math.sin(th)])
    proj, perp = c.pontos @ u, c.pontos @ np.array([-u[1], u[0]])
    out = []
    for i in range(len(c.s) - 1):
        if perp[i] * perp[i + 1] <= 0 and perp[i] != perp[i + 1] and proj[i] > 0:
            f = perp[i] / (perp[i] - perp[i + 1])
            out.append(float(c.s[i] + f * (c.s[i + 1] - c.s[i])))
    return out


def _marcadores_espiral() -> list[tuple[int, float]]:
    c = CAMINHOS["espiral"]
    marc = []
    for sensor, horas in HORAS_SENSORES_ESPIRAL:
        marc += [(sensor, round(x, 1)) for x in _cruzamentos_raio(c, horas)[:CRUZAMENTOS_POR_SENSOR]]
    return sorted(marc, key=lambda m: m[1])


# (sensor, centro_mm) dos cruzamentos de cada caminho
MARCADORES_PADRAO: dict[str, list[tuple[int, float]]] = {
    "espiral": _marcadores_espiral(),
    "guarda_chuva": [(SENSOR_ARCO_ESQ, 0.0), (SENSOR_ARCO_DIR, 170.0)],      # sensores nas pontas
}


def niveis_em(caminho: str, s: float, marcadores: list[tuple[int, float]] | None = None) -> dict[int, int]:
    """Nível de cada sensor IR com o joystick em s (1 = vendo)."""
    marc = MARCADORES_PADRAO.get(caminho, []) if marcadores is None else marcadores
    niveis = {sensor: 0 for sensor, _ in marc}
    for sensor, c in marc:
        if abs(s - c) < MEIA_LARGURA_FEIXE:
            niveis[sensor] = 1
    return niveis


def tabela_padrao(caminho: str) -> list[dict]:
    """Linhas: divisa, sensor, sentido, troca, codigo_depois, posicao_mm, incerteza_mm.

    Um cruzamento em c gera 4 linhas: na ida o sensor liga em c - w e desliga em
    c + w; na volta, ao contrário. Bordas fora do caminho (sensores das pontas)
    não acontecem e ficam de fora."""
    marc = MARCADORES_PADRAO.get(caminho)
    if not marc:
        return []
    L = CAMINHOS[caminho].comprimento
    inc = INCERTEZA_ESPIRAL_MM if caminho == "espiral" else 3.0
    w = MEIA_LARGURA_FEIXE
    linhas: list[dict] = []
    for k, (sensor, c) in enumerate(marc, start=1):
        for sentido, troca, pos in (("ida", "liga", c - w), ("ida", "desliga", c + w),
                                    ("volta", "liga", c + w), ("volta", "desliga", c - w)):
            if 0.0 <= pos <= L:
                linhas.append(dict(divisa=k, sensor=sensor, sentido=sentido, troca=troca, codigo_depois=None,
                                   posicao_mm=round(pos, 1), incerteza_mm=inc))
    return linhas
