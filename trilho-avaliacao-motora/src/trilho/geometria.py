"""Geometria dos três caminhos da bancada (placa de 425 × 250 mm).

Todo caminho é plano e descrito por uma polilinha densa, parametrizada pelo
comprimento de arco ``s`` (mm). A fusão usa a tangente em ``s`` para projetar a
aceleração da IMU; os jogos usam os pontos para desenhar o caminho real.

Os sensores LM393 são modelados por intervalos de ``s`` em que cada um "vê" o
joystick. A partir deles sai a tabela nominal de transições (ver ``transicoes``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Caminho:
    id: str                       # "A", "B" ou "C"
    nome: str
    pontos_mm: np.ndarray         # (N, 2) polilinha densa no plano da placa
    # sensor (1..n) -> intervalos de s (mm) em que o feixe enxerga o joystick
    regioes_sensores: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    vai_e_volta: bool = False     # espiral: uma tentativa é borda -> centro -> borda
    tem_tof: bool = False         # caminho retilíneo: VL53L0X cobre todo o percurso

    def __post_init__(self) -> None:
        d = np.diff(self.pontos_mm, axis=0)
        self._s = np.concatenate([[0.0], np.cumsum(np.hypot(d[:, 0], d[:, 1]))])
        # tabela de tangentes a cada 0,5 mm, para consulta rápida no filtro
        L = self.comprimento_mm
        self._s_tab = np.linspace(0.0, L, int(np.ceil(L / 0.5)) + 1)
        s_mais = np.minimum(self._s_tab + 0.25, L)
        s_menos = np.maximum(s_mais - 0.5, 0.0)
        p_mais = self.ponto(s_mais)
        p_menos = self.ponto(s_menos)
        t = p_mais - p_menos
        self._t_tab = t / np.linalg.norm(t, axis=1, keepdims=True)
        # vetor curvatura K = dT/ds (1/mm) e sua derivada dK/ds (1/mm²),
        # levemente suavizados para tirar o serrilhado da polilinha
        self._k_tab = _bordas(_suavizar(np.gradient(self._t_tab, self._s_tab, axis=0)))
        self._dk_tab = _bordas(_suavizar(np.gradient(self._k_tab, self._s_tab, axis=0)))

    # ------------------------------------------------------------------ básico
    @property
    def comprimento_mm(self) -> float:
        return float(self._s[-1])

    @property
    def batentes_mm(self) -> tuple[float, float]:
        """As duas pontas do caminho são batentes mecânicos."""
        return (0.0, self.comprimento_mm)

    @property
    def sensores(self) -> list[int]:
        return sorted(self.regioes_sensores)

    def ponto(self, s_mm: float | np.ndarray) -> np.ndarray:
        s = np.clip(s_mm, 0.0, self.comprimento_mm)
        x = np.interp(s, self._s, self.pontos_mm[:, 0])
        y = np.interp(s, self._s, self.pontos_mm[:, 1])
        return np.stack([x, y], axis=-1)

    def tangente(self, s_mm: float | np.ndarray) -> np.ndarray:
        """Vetor unitário tangente (sentido de s crescente)."""
        s = np.clip(s_mm, 0.0, self.comprimento_mm)
        tx = np.interp(s, self._s_tab, self._t_tab[:, 0])
        ty = np.interp(s, self._s_tab, self._t_tab[:, 1])
        t = np.stack([tx, ty], axis=-1)
        return t / np.linalg.norm(t, axis=-1, keepdims=True)

    def _tab(self, tab: np.ndarray, s_mm) -> np.ndarray:
        s = np.clip(s_mm, 0.0, self.comprimento_mm)
        return np.stack([np.interp(s, self._s_tab, tab[:, 0]),
                         np.interp(s, self._s_tab, tab[:, 1])], axis=-1)

    def derivada_tangente(self, s_mm: float | np.ndarray) -> np.ndarray:
        """dT/ds em 1/mm (vetor curvatura = κ·N)."""
        return self._tab(self._k_tab, s_mm)

    def derivada_curvatura(self, s_mm: float | np.ndarray) -> np.ndarray:
        """d²T/ds² em 1/mm² (usado no jacobiano do filtro)."""
        return self._tab(self._dk_tab, s_mm)

    def estado_sensores(self, s_mm: float) -> dict[int, bool]:
        return {
            sid: any(a <= s_mm < b for a, b in regs)
            for sid, regs in self.regioes_sensores.items()
        }

    def codigo(self, s_mm: float) -> str:
        """Código dos sensores, do maior para o menor (ex.: S3 S2 S1 -> '011')."""
        est = self.estado_sensores(s_mm)
        return "".join("1" if est[sid] else "0" for sid in reversed(self.sensores))


def _suavizar(x: np.ndarray, n: int = 9) -> np.ndarray:
    k = np.ones(n) / n
    pad = np.pad(x, ((n // 2, n // 2), (0, 0)), mode="edge")
    return np.stack([np.convolve(pad[:, i], k, mode="valid") for i in range(x.shape[1])], axis=1)


def _bordas(x: np.ndarray, n: int = 8) -> np.ndarray:
    """Repete o valor interno nas pontas (as diferenças finitas ali são unilaterais)."""
    x = x.copy()
    x[:n] = x[n]
    x[-n:] = x[-n - 1]
    return x


# ---------------------------------------------------------------------- fábricas
def caminho_retilineo(comprimento_mm: float = 120.0) -> Caminho:
    """Caminho C: fenda reta no centro da placa, VL53L0X numa extremidade."""
    n = 241
    x = np.linspace(0.0, comprimento_mm, n)
    pts = np.stack([np.zeros(n), x], axis=1)   # fenda vertical na placa
    return Caminho("C", "retilíneo", pts, {}, vai_e_volta=False, tem_tof=True)


def caminho_guarda_chuva(arco_mm: float = 170.0, largura_feixe_mm: float = 8.0,
                         distancia_juncao_mm: float = 40.0) -> Caminho:
    """Caminho A: arco do guarda-chuva (~170 mm somando os dois lados).

    O paciente percorre o arco inteiro, passando pela junção com a haste no
    meio. Há um LM393 no meio de cada lado (~40 mm da junção).
    """
    raio = arco_mm / np.pi
    theta = np.linspace(np.pi, 0.0, 681)       # da esquerda para a direita, por cima
    pts = np.stack([raio * np.cos(theta), raio * np.sin(theta)], axis=1)
    juncao = arco_mm / 2
    w = largura_feixe_mm / 2
    regioes = {
        1: [(juncao - distancia_juncao_mm - w, juncao - distancia_juncao_mm + w)],
        2: [(juncao + distancia_juncao_mm - w, juncao + distancia_juncao_mm + w)],
    }
    return Caminho("A", "guarda-chuva", pts, regioes)


# divisas da espiral (mm ao longo do caminho, a partir da borda): igualmente
# espaçadas em comprimento de arco (1410 / 8 ≈ 176 mm), o que as deixa mais
# próximas em ângulo nas voltas internas, onde a curva é mais fechada
DIVISAS_ESPIRAL_MM = (176.0, 352.0, 529.0, 705.0, 881.0, 1058.0, 1234.0)


def _espiral_arquimedes(r_ext: float, r_int: float, voltas: float, n: int = 6001) -> np.ndarray:
    theta = np.linspace(0.0, 2 * np.pi * voltas, n)
    r = r_ext - (r_ext - r_int) * theta / theta[-1]
    return np.stack([r * np.cos(theta), r * np.sin(theta)], axis=1)


def caminho_espiral(comprimento_mm: float = 1410.0, r_ext: float = 95.0,
                    r_int: float = 12.0,
                    divisas_mm: tuple[float, ...] = DIVISAS_ESPIRAL_MM) -> Caminho:
    """Caminho B: espiral num rebaixo circular, da borda até o centro.

    Três LM393 com feixes sobrepostos formam o código Gray de 3 dígitos
    000 → 001 → 011 → 010 → 110 → 111 → 101 → 100 ao longo da ida.
    """
    # ajusta o número de voltas para atingir o comprimento desejado
    lo, hi = 0.5, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = _espiral_arquimedes(r_ext, r_int, mid)
        comp = np.sum(np.hypot(*np.diff(p, axis=0).T))
        lo, hi = (mid, hi) if comp < comprimento_mm else (lo, mid)
    pts = _espiral_arquimedes(r_ext, r_int, (lo + hi) / 2)
    d = divisas_mm
    regioes = {
        1: [(d[0], d[2]), (d[4], d[6])],
        2: [(d[1], d[5])],
        3: [(d[3], comprimento_mm + 1.0)],   # fica ligado até o centro
    }
    return Caminho("B", "espiral", pts, regioes, vai_e_volta=True)


def obter_caminho(cid: str) -> Caminho:
    cid = cid.upper()
    if cid == "A":
        return caminho_guarda_chuva()
    if cid == "B":
        return caminho_espiral()
    if cid == "C":
        return caminho_retilineo()
    raise ValueError(f"caminho desconhecido: {cid!r}")
