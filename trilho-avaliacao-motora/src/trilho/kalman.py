"""Filtro de Kalman 1D ao longo do trilho + suavizador RTS.

Estado (SI):  x = [s, v, a, j, b, ψ]ᵀ
    s posição ao longo do caminho (m), v velocidade (m/s), a aceleração (m/s²),
    j jerk (m/s³), b viés residual do acelerômetro tangencial (m/s²),
    ψ erro de orientação da IMU no plano (rad, deriva do giroscópio).

Modelo de jerk constante com ruído branco no snap (ṡ=v, v̇=a, ȧ=j, j̇=ruído) e
viés e ψ em passeio aleatório. Viés e ψ são acréscimos ao estado do documento:
absorvem o resíduo da calibração da IMU e a deriva do giroscópio, que nas
curvas fechadas viraria erro de posição.

O mesmo filtro dá duas saídas: o estado filtrado (só passado, para o jogo em
tempo real) e o estado suavizado pelo RTS (passado e futuro, para as métricas).
"""

from __future__ import annotations

from math import factorial

import numpy as np

N_ESTADOS = 6
S, V, A, J, B, PSI = range(6)


def matriz_transicao(dt: float) -> np.ndarray:
    F = np.eye(N_ESTADOS)
    F[0, 1], F[0, 2], F[0, 3] = dt, dt * dt / 2, dt ** 3 / 6
    F[1, 2], F[1, 3] = dt, dt * dt / 2
    F[2, 3] = dt
    return F


_EXP = np.array([[7 - i - k for k in range(4)] for i in range(4)], dtype=float)
_DEN = np.array([[(7 - i - k) * factorial(3 - i) * factorial(3 - k) for k in range(4)]
                 for i in range(4)], dtype=float)


def ruido_processo(dt: float, q_snap: float, q_vies: float, q_psi: float = 1e-6) -> np.ndarray:
    """Q discreto do modelo de snap branco (densidade q_snap) + passeio do viés."""
    Q = np.zeros((N_ESTADOS, N_ESTADOS))
    Q[:4, :4] = q_snap * dt ** _EXP / _DEN
    Q[B, B] = q_vies * dt
    Q[PSI, PSI] = q_psi * dt
    return Q


class FiltroTrilho:
    def __init__(self, q_snap: float = 20.0, q_vies: float = 1e-6, q_psi: float = 1e-6,
                 x0: np.ndarray | None = None, P0: np.ndarray | None = None,
                 t0: float = 0.0, guardar_historico: bool = False) -> None:
        self.q_snap, self.q_vies, self.q_psi = q_snap, q_vies, q_psi
        self.x = np.zeros(N_ESTADOS) if x0 is None else np.asarray(x0, float).copy()
        self.P = np.diag([1e-4, 1e-4, 1e-2, 1.0, 1e-3, 4e-4]) if P0 is None else P0.copy()
        self.t = t0
        self.guardar = guardar_historico
        # histórico por nó de tempo: previsão (x⁻, P⁻), filtrado (x⁺, P⁺) e F
        self._t: list[float] = []
        self._xp: list[np.ndarray] = []
        self._Pp: list[np.ndarray] = []
        self._xf: list[np.ndarray] = []
        self._Pf: list[np.ndarray] = []
        self._F: list[np.ndarray] = []
        if self.guardar:
            self._abrir_no(np.eye(N_ESTADOS))

    # ------------------------------------------------------------ histórico
    def _abrir_no(self, F: np.ndarray) -> None:
        self._t.append(self.t)
        self._xp.append(self.x.copy())
        self._Pp.append(self.P.copy())
        self._F.append(F)
        self._xf.append(self.x.copy())
        self._Pf.append(self.P.copy())

    def _fechar_no(self) -> None:
        if self.guardar:
            self._xf[-1] = self.x.copy()
            self._Pf[-1] = self.P.copy()

    @property
    def indice_no(self) -> int:
        """Índice do nó atual no histórico (para amostrar a saída)."""
        return len(self._t) - 1

    # ------------------------------------------------------------- previsão
    def prever(self, t: float) -> None:
        dt = t - self.t
        if dt <= 0.0:
            return
        self._fechar_no()
        F = matriz_transicao(dt)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + ruido_processo(dt, self.q_snap, self.q_vies, self.q_psi)
        self.t = t
        if self.guardar:
            self._abrir_no(F)

    # ----------------------------------------------------------- atualização
    def _atualizar_h(self, h: np.ndarray, z: float, r: float, porta_sigma: float | None) -> bool:
        y = z - h @ self.x
        Ph = self.P @ h
        s = float(h @ Ph) + r
        if porta_sigma is not None and y * y > (porta_sigma ** 2) * s:
            return False
        K = Ph / s
        self.x = self.x + K * y
        self.P = self.P - np.outer(K, Ph)
        self.P = 0.5 * (self.P + self.P.T)
        return True

    def atualizar_vetor(self, z: np.ndarray, h_x: np.ndarray, H: np.ndarray,
                        R: np.ndarray) -> None:
        """Atualização EKF com medida vetorial (z previsto = h_x, jacobiano H)."""
        y = z - h_x
        PHt = self.P @ H.T
        Sm = H @ PHt + R
        K = np.linalg.solve(Sm.T, PHt.T).T
        self.x = self.x + K @ y
        self.P = self.P - K @ PHt.T
        self.P = 0.5 * (self.P + self.P.T)

    def atualizar_aceleracao(self, a_medida: float, sigma: float) -> bool:
        """A IMU mede a + b (aceleração tangencial + viés residual)."""
        h = np.zeros(N_ESTADOS)
        h[A] = 1.0
        h[B] = 1.0
        return self._atualizar_h(h, a_medida, sigma * sigma, porta_sigma=None)

    def atualizar_posicao(self, s_medida: float, sigma: float,
                          porta_sigma: float | None = 3.0) -> bool:
        """Correção de posição (divisa dos LM393, VL53L0X ou batente).

        Rejeita a medida quando a distância ao previsto passa de ``porta_sigma``
        vezes a incerteza combinada (disparo espúrio).
        """
        h = np.zeros(N_ESTADOS)
        h[S] = 1.0
        return self._atualizar_h(h, s_medida, sigma * sigma, porta_sigma)

    def atualizar_velocidade_zero(self, sigma: float = 0.002,
                                  porta_sigma: float | None = None) -> bool:
        h = np.zeros(N_ESTADOS)
        h[V] = 1.0
        return self._atualizar_h(h, 0.0, sigma * sigma, porta_sigma=porta_sigma)

    def incerteza_posicao(self) -> float:
        return float(np.sqrt(self.P[S, S]))

    # ---------------------------------------------------------------- RTS
    def suavizar(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Suavizador Rauch–Tung–Striebel. Devolve (t, x_suav, x_filtrado)."""
        if not self.guardar:
            raise RuntimeError("filtro criado sem histórico")
        self._fechar_no()
        n = len(self._t)
        xf = np.array(self._xf)
        xs = xf.copy()
        for k in range(n - 2, -1, -1):
            F = self._F[k + 1]
            Pp = self._Pp[k + 1]
            C = np.linalg.solve(Pp.T, (self._Pf[k] @ F.T).T).T
            xs[k] = xf[k] + C @ (xs[k + 1] - self._xp[k + 1])
        return np.array(self._t), xs, xf
