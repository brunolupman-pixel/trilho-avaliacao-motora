"""Fusão IMU + sensores da bancada ao longo do trilho.

Como o joystick está preso a um trilho plano, o problema tem uma só dimensão:
a posição ``s`` ao longo do caminho. A IMU dá o movimento fino e os sensores
da bancada (divisas dos LM393, VL53L0X, batentes) corrigem o erro acumulado.

``MotorFusao`` é incremental: o mesmo código roda ao vivo (núcleo de aquisição,
estado publicado para o jogo) e no pós-processamento (``processar_partida``),
que guarda o histórico e aplica o suavizador RTS ao fim da partida.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import VERSAO_ALGORITMO
from .dados import DadosBrutos
from .geometria import Caminho
from .kalman import A as IA, B as IB, J as IJ, N_ESTADOS, PSI as IPSI, S as IS, V as IV, FiltroTrilho
from .sincronia import AjusteRelogio, ajustar_relogio
from .transicoes import TabelaTransicoes


@dataclass
class ConfigFusao:
    q_snap: float = 200.0  # densidade do ruído de snap (m²/s⁷)
    q_vies: float = 1e-6
    q_psi: float = 1e-6                # deriva do giroscópio (rad²/s)
    sigma_acc: float = 0.05            # m/s² (ruído da IMU + erro de projeção)
    sigma_acc_normal: float = 0.2      # m/s², direção normal ao caminho
    sigma_tof_mm: float = 2.0
    sigma_batente_mm: float = 1.0
    distancia_batente_mm: float = 12.0
    distancia_batente_longo_mm: float = 60.0
    repouso_longo_s: float = 0.8
    janela_repouso_s: float = 0.6
    sigma_zupt_meio: float = 0.006     # m/s, velocidade zero longe dos batentes
    sigma_zupt_batente: float = 0.002
    cusum_k: float = 0.004             # m/s² (folga do CUSUM de saída do repouso)
    cusum_h: float = 1.5               # limiar (m/s² × amostras)
    bloqueio_apos_saida_s: float = 1.0
    fator_repouso: float = 2.0         # × desvio padrão medido no repouso inicial
    v_max_repouso: float = 0.006       # m/s (movimento lento constante ≈ repouso para a IMU)
    calibracao_inicial_s: float = 0.5  # repouso no início da partida
    zupt_a_cada: int = 5               # amostras entre atualizações de velocidade zero
    porta_ir_sigma: float = 3.0
    religar: bool = True               # divisa identificada fora da porta religa o filtro
    pulso_espurio_s: float = 0.04      # liga+desliga mais curto que isso = luz ambiente


@dataclass
class Qualidade:
    divisas_aceitas: int = 0
    divisas_rejeitadas: int = 0
    divergencias_gray: int = 0
    tof_validas: int = 0
    tof_falhas: int = 0
    tof_rejeitadas: int = 0
    correcoes_batente: int = 0
    religamentos: int = 0
    pulsos_espurios: int = 0
    restricoes: int = 0
    lacunas_imu: int = 0
    erro_sincronia_rms_us: float | None = None
    rejeicoes: list[dict] = field(default_factory=list)

    def como_dict(self) -> dict:
        d = dict(self.__dict__)
        d["rejeicoes"] = self.rejeicoes[:50]
        total = self.divisas_aceitas + self.divisas_rejeitadas
        d["fracao_divisas_aceitas"] = self.divisas_aceitas / total if total else None
        return d


def _trechos_por_codigo(caminho: Caminho) -> dict[str, tuple[float, float]]:
    """Código dos sensores → trecho contínuo [lo, hi] (m), quando é único."""
    if not caminho.sensores:
        return {}
    s = np.arange(0.0, caminho.comprimento_mm + 0.5, 0.5)
    cod = [caminho.codigo(x) for x in s]
    trechos: dict[str, list[tuple[float, float]]] = {}
    ini = 0
    for i in range(1, len(s) + 1):
        if i == len(s) or cod[i] != cod[ini]:
            trechos.setdefault(cod[ini], []).append((s[ini] / 1000, s[i - 1] / 1000))
            ini = i
    return {c: t[0] for c, t in trechos.items() if len(t) == 1}


class MotorFusao:
    def __init__(self, caminho: Caminho, tabela: TabelaTransicoes | None, calib_imu: dict,
                 config: ConfigFusao | None = None, guardar_historico: bool = False,
                 s0_mm: float = 0.0) -> None:
        self.caminho = caminho
        self.tabela = tabela
        self.cfg = config or ConfigFusao()
        self.cal = calib_imu
        self.L = caminho.comprimento_mm / 1000.0
        self.filtro = FiltroTrilho(self.cfg.q_snap, self.cfg.q_vies, self.cfg.q_psi,
                                   guardar_historico=guardar_historico)
        self.filtro.x[IS] = s0_mm / 1000.0
        self.qualidade = Qualidade()
        self.estado_ir = {sid: 0 for sid in caminho.sensores}
        self._calibrando = True
        self._buffer_cal: list[tuple] = []
        self._psi = float(calib_imu.get("psi0_rad", 0.0))
        self._t_ultimo: float | None = None
        self._vies_giro = 0.0
        self._f_repouso = np.zeros(2)
        self._limiar_acc = 0.06
        self._ruido_acc = 0.03
        self._limiar_giro = 0.01
        n_jan = max(5, int(self.cfg.janela_repouso_s * 500))
        self._jan_f: deque[np.ndarray] = deque(maxlen=n_jan)
        self._jan_g: deque[float] = deque(maxlen=n_jan)
        self._contador = 0
        self.em_repouso = False
        self.indices_imu: list[int] = []
        self.tempos_imu: list[float] = []
        self.filtrado_imu: list[np.ndarray] = []
        self.acc_tangencial: list[float] = []
        self.repouso_imu: list[bool] = []
        self._seq_anterior: int | None = None
        self._tof_seguidas_rejeitadas = 0
        self._inicio_repouso = 0.0
        self._saida_cusum = -1e9
        self._cusum = np.zeros(4)
        self._ultima_divisa: tuple[float, float] | None = None
        self._trechos = _trechos_por_codigo(caminho)

    # ---------------------------------------------------------- conversões
    def _corpo(self, ax: int, ay: int, gz: int) -> tuple[np.ndarray, float]:
        ea, oa = self.cal["escala_acc"], self.cal["offset_acc_lsb"]
        eg, og = self.cal["escala_giro"], self.cal["offset_giro_lsb"]
        f = np.array([(ax - oa[0]) * ea, (ay - oa[1]) * ea])
        return f, (gz - og[2]) * eg

    def _para_placa(self, f_corpo: np.ndarray) -> np.ndarray:
        c, s = np.cos(self._psi), np.sin(self._psi)
        return np.array([c * f_corpo[0] - s * f_corpo[1], s * f_corpo[0] + c * f_corpo[1]])

    # ------------------------------------------------------------------ IMU
    def imu(self, t: float, ax: int, ay: int, gz: int, seq: int | None = None) -> None:
        if seq is not None:
            if self._seq_anterior is not None and seq != self._seq_anterior + 1:
                self.qualidade.lacunas_imu += 1
            self._seq_anterior = seq
        if self._calibrando:
            self._buffer_cal.append((t, ax, ay, gz))
            if t - self._buffer_cal[0][0] >= self.cfg.calibracao_inicial_s:
                self._concluir_calibracao()
            return
        self._processar_imu(t, ax, ay, gz)

    def _concluir_calibracao(self) -> None:
        """Repouso inicial: viés do giroscópio, inclinação da base e ruído."""
        dados = [(self._corpo(ax, ay, gz)) for _, ax, ay, gz in self._buffer_cal]
        f = np.array([d[0] for d in dados])
        g = np.array([d[1] for d in dados])
        self._vies_giro = float(g.mean())
        self._f_repouso = self._para_placa(f.mean(axis=0))
        ruido = float(np.max(f.std(axis=0)))
        self._ruido_acc = max(ruido, 0.005)
        self._limiar_acc = max(0.01, self.cfg.fator_repouso * ruido)
        self._limiar_giro = max(0.004, self.cfg.fator_repouso * float(g.std()))
        self.filtro.t = self._buffer_cal[0][0]
        if self.filtro.guardar:
            self.filtro._t[0] = self.filtro.t
        self.filtro.P[IS, IS] = (0.5e-3) ** 2
        self.filtro.P[IV, IV] = (1e-3) ** 2
        self._calibrando = False
        buf, self._buffer_cal = self._buffer_cal, []
        for t, ax, ay, gz in buf:
            self._processar_imu(t, ax, ay, gz)

    def _processar_imu(self, t: float, ax: int, ay: int, gz: int) -> None:
        f_corpo, g = self._corpo(ax, ay, gz)
        if self._t_ultimo is not None:
            self._psi += (g - self._vies_giro) * (t - self._t_ultimo)
        self._t_ultimo = t
        f = self._para_placa(f_corpo) - self._f_repouso
        fl = self.filtro
        fl.prever(t)
        # medida 2D no plano da placa (EKF): f = R(-ψ)·[(a + b)·T(s) + v²·K(s)]
        # A componente normal (centrípeta) também é usada: projetar só na
        # tangente estimada realimenta o erro de posição nas curvas fechadas.
        # ψ (erro de orientação) evita que a deriva do giroscópio vire erro de s.
        dentro = 0.0 <= fl.x[IS] <= self.L
        s_mm = float(np.clip(fl.x[IS], 0, self.L) * 1000.0)
        T = self.caminho.tangente(s_mm)
        K = self.caminho.derivada_tangente(s_mm) * 1e3
        dK = self.caminho.derivada_curvatura(s_mm) * 1e6
        v, ab = fl.x[IV], fl.x[IA] + fl.x[IB]
        base = ab * T + v * v * K
        rot = np.array([-base[1], base[0]])          # J·base (giro de 90°)
        h_x = base - fl.x[IPSI] * rot
        H = np.zeros((2, N_ESTADOS))
        H[:, IPSI] = -rot
        if dentro:   # fora do caminho a geometria é constante (batente)
            H[:, IS] = ab * K + v * v * dK
        H[:, IV] = 2 * v * K
        H[:, IA] = T
        H[:, IB] = T
        # incerteza maior na direção normal: a parte centrípeta depende de v²
        # e, longe do ponto de linearização (v errado), engana o filtro
        N = np.array([-T[1], T[0]])
        R = (self.cfg.sigma_acc ** 2) * np.outer(T, T) + (self.cfg.sigma_acc_normal ** 2) * np.outer(N, N)
        fl.atualizar_vetor(f, h_x, H, R)
        a_t = float(f @ T)

        # saída do repouso: CUSUM nas duas componentes da aceleração detecta uma
        # aceleração pequena e sustentada (saída lenta do batente) bem antes
        # da janela de repouso perceber
        if self.em_repouso:
            k, h = self.cfg.cusum_k, self.cfg.cusum_h
            self._cusum = np.maximum(0.0, self._cusum + np.concatenate([f, -f]) - k)
            if np.any(self._cusum > h):
                self.em_repouso = False
                self._saida_cusum = t
                self._cusum[:] = 0.0

        # detecção de repouso: janela de aceleração e giro parados + velocidade baixa
        self._jan_f.append(f)
        self._jan_g.append(g)
        self._contador += 1
        if self._contador % self.cfg.zupt_a_cada == 0 and len(self._jan_f) == self._jan_f.maxlen:
            jf = np.array(self._jan_f)
            # repouso = média da janela compatível com zero (o teste da média é
            # bem mais sensível que o RMS em movimentos lentos), pouca vibração,
            # giro parado e velocidade estimada compatível com zero (porta 3σ)
            n = len(jf)
            media = float(np.linalg.norm(jf.mean(axis=0)))
            parado = (media < 2.5 * self._ruido_acc / np.sqrt(n) + 0.002
                      and float(np.max(jf.std(axis=0))) < self._limiar_acc
                      and float(np.std(self._jan_g)) < self._limiar_giro
                      and abs(fl.x[IV]) < self._v_max_repouso()
                      and t - self._saida_cusum > self.cfg.bloqueio_apos_saida_s)
            if parado and not self.em_repouso:
                self._inicio_repouso = t
                self._cusum[:] = 0.0
            self.em_repouso = parado
            # longe dos batentes o repouso é menos certo (movimento lento e
            # constante parece repouso para a IMU): velocidade zero mais fraca
            perto = self._perto_batente()
            sigma_zupt = self.cfg.sigma_zupt_batente if perto else self.cfg.sigma_zupt_meio
            if parado and fl.atualizar_velocidade_zero(sigma_zupt, porta_sigma=None if perto else 3.0):
                self._batente(t - self._inicio_repouso)
        if self._contador % self.cfg.zupt_a_cada == 0:
            self._restricoes()
        self.indices_imu.append(fl.indice_no)
        self.tempos_imu.append(t)
        self.filtrado_imu.append(fl.x.copy())
        self.acc_tangencial.append(a_t)
        self.repouso_imu.append(self.em_repouso)

    def _restricoes(self) -> None:
        """Restrições físicas como pseudo-medidas quando violadas:
        s ∈ [0, L] (batentes) e s dentro do trecho indicado pelo código Gray."""
        fl = self.filtro
        s = fl.x[IS]
        lim = None
        if s < 0.0:
            lim = 0.0
        elif s > self.L:
            lim = self.L
        else:
            trecho = self._trechos.get(self.codigo_atual())
            if trecho is not None:
                lo, hi = trecho
                margem = 0.005
                if s < lo - margem:
                    lim = lo
                elif s > hi + margem:
                    lim = hi
        if lim is not None:
            fl.atualizar_posicao(lim, 0.003, porta_sigma=None)
            self.qualidade.restricoes += 1

    def _perto_batente(self) -> bool:
        s_mm = self.filtro.x[IS] * 1000.0
        return min(abs(s_mm - b) for b in self.caminho.batentes_mm) < 2 * self.cfg.distancia_batente_mm

    def _v_max_repouso(self) -> float:
        """Perto de um batente o repouso é muito provável: não exige velocidade
        estimada baixa (ela pode ter derivado); a saída lenta do batente é
        detectada pelo CUSUM."""
        return 0.03 if self._perto_batente() else self.cfg.v_max_repouso

    def _batente(self, duracao_repouso: float) -> None:
        """Pontas do caminho são batentes mecânicos: repouso perto delas corrige s.

        Um repouso longo (≥ ``repouso_longo_s``) um pouco mais longe também é
        atribuído ao batente, desde que o código dos sensores confira com o
        trecho do batente (o joystick não passou por nenhuma divisa depois dele).
        """
        s_mm = self.filtro.x[IS] * 1000.0
        raio_longo = self.cfg.distancia_batente_longo_mm
        for b in self.caminho.batentes_mm:
            d = abs(s_mm - b)
            perto = d < self.cfg.distancia_batente_mm
            longo = (duracao_repouso >= self.cfg.repouso_longo_s and d < raio_longo
                     and self.caminho.codigo(b) == self.codigo_atual())
            # só no começo do repouso: uma saída muito lenta do batente parece
            # repouso para a IMU e não deve ficar presa na ponta
            if (perto or longo) and duracao_repouso <= self.cfg.repouso_longo_s + 0.5:
                if self.filtro.atualizar_posicao(b / 1000.0, self.cfg.sigma_batente_mm / 1000.0,
                                                 porta_sigma=None):
                    self.qualidade.correcoes_batente += 1

    # ------------------------------------------------------------ LM393
    def codigo_atual(self) -> str:
        return "".join(str(self.estado_ir[s]) for s in reversed(self.caminho.sensores))

    def ir(self, t: float, sensor: int, estado: int) -> None:
        """Evento de troca de um LM393 (t já descontado do atraso do sensor).

        1. as linhas candidatas são as do mesmo sensor e troca cujo código
           antes/depois confere com o estado atual dos sensores (código Gray);
        2. se elas apontam para um único lugar (≤ 10 mm de dispersão), a divisa
           está *identificada*; senão escolhe-se, entre as do sentido dado pela
           velocidade estimada, a mais próxima da posição prevista pela IMU;
        3. rejeita se a distância passar de 3× a incerteza combinada — exceto
           divisas identificadas, que religam o filtro (ele é que se perdeu);
        4. confere o código depois da troca com a sequência esperada.
        """
        codigo_antes = self.codigo_atual()
        self.estado_ir[sensor] = estado
        if self._calibrando or self.tabela is None:
            return
        codigo_depois = self.codigo_atual()
        fl = self.filtro
        fl.prever(max(t, fl.t))
        v = fl.x[IV]
        sentido = "ida" if v > 0.002 else "volta" if v < -0.002 else None
        troca = "liga" if estado else "desliga"
        s_prev = fl.x[IS] * 1000.0

        todas = self.tabela.candidatas(sensor, troca, None)
        coerentes = [l for l in todas if l.codigo_depois == codigo_depois
                     and self._codigo_antes(l) == codigo_antes]
        identificada = bool(coerentes) and (
            max(l.posicao_mm for l in coerentes) - min(l.posicao_mm for l in coerentes) <= 10.0)
        if identificada:
            por_sentido = [l for l in coerentes if l.sentido == sentido] or coerentes
            lin = min(por_sentido, key=lambda l: abs(l.posicao_mm - s_prev))
        else:
            lin = self.tabela.mais_proxima(sensor, troca, sentido, s_prev)
        if lin is None:
            self._rejeitar(t, sensor, troca, s_prev, None, "sem linha na tabela")
            return
        sigma = lin.incerteza_mm / 1000.0
        if fl.atualizar_posicao(lin.posicao_mm / 1000.0, sigma, porta_sigma=self.cfg.porta_ir_sigma):
            self.qualidade.divisas_aceitas += 1
            self._ultima_divisa = (t, lin.posicao_mm)
            if codigo_depois != lin.codigo_depois:
                self.qualidade.divergencias_gray += 1
            return
        if identificada and self.cfg.religar:
            erro = abs(lin.posicao_mm / 1000.0 - fl.x[IS])
            fl.P[IS, IS] += erro ** 2
            fl.P[IV, IV] += (erro / 2.0) ** 2
            fl.atualizar_posicao(lin.posicao_mm / 1000.0, sigma, porta_sigma=None)
            # velocidade média desde a última divisa identificada
            ant = self._ultima_divisa
            if ant is not None and abs(lin.posicao_mm - ant[1]) > 20 and t - ant[0] < 10:
                v_med = (lin.posicao_mm - ant[1]) / 1000.0 / (t - ant[0])
                fl.x[IV] = v_med
                fl.x[IA] = fl.x[IJ] = 0.0
                fl.P[IV, :] = fl.P[:, IV] = 0.0
                fl.P[IV, IV] = (0.3 * v_med) ** 2 + 1e-4
            self._ultima_divisa = (t, lin.posicao_mm)
            self.qualidade.religamentos += 1
            self.qualidade.divisas_aceitas += 1
            return
        self._rejeitar(t, sensor, troca, s_prev, lin.posicao_mm, "fora de 3σ")

    def _codigo_antes(self, lin) -> str:
        """Código antes da troca: o código depois com o bit do sensor invertido."""
        bits = list(lin.codigo_depois)
        i = len(bits) - lin.sensor
        bits[i] = "0" if bits[i] == "1" else "1"
        return "".join(bits)

    def _rejeitar(self, t, sensor, troca, s_prev, s_tab, motivo) -> None:
        self.qualidade.divisas_rejeitadas += 1
        self.qualidade.rejeicoes.append({"t": round(t, 4), "sensor": sensor, "troca": troca,
                                         "s_previsto_mm": round(s_prev, 1),
                                         "s_tabela_mm": s_tab, "motivo": motivo})

    # ---------------------------------------------------------- VL53L0X
    def tof(self, t: float, s_mm: float) -> None:
        if self._calibrando:
            return
        fl = self.filtro
        fl.prever(max(t, fl.t))
        # leituras isoladas fora de 5σ são descartadas; se várias seguidas
        # discordarem, o filtro é que se perdeu e a medida é aceita (religa)
        porta = None if self._tof_seguidas_rejeitadas >= 3 else 5.0
        if fl.atualizar_posicao(s_mm / 1000.0, self.cfg.sigma_tof_mm / 1000.0, porta_sigma=porta):
            self.qualidade.tof_validas += 1
            self._tof_seguidas_rejeitadas = 0
        else:
            self.qualidade.tof_rejeitadas += 1
            self._tof_seguidas_rejeitadas += 1

    # ----------------------------------------------------- estado ao vivo
    def estado(self) -> dict:
        x = self.filtro.x
        s = float(np.clip(x[IS], 0.0, self.L))
        return {"t": self.filtro.t, "s_mm": s * 1000, "fracao": s / self.L,
                "v": float(x[IV]), "a": float(x[IA]), "j": float(x[IJ]),
                "repouso": self.em_repouso, "calibrando": self._calibrando}


# ======================================================== pós-processamento
@dataclass
class ResultadoFusao:
    serie: pd.DataFrame          # t, s, v, a, j (RTS) + s_rt, v_rt (tempo real) + repouso
    forca: pd.DataFrame | None   # t, forca_N
    eventos: pd.DataFrame        # eventos do jogo no relógio da bancada (s)
    qualidade: dict
    ajuste_relogio: AjusteRelogio
    versao_algoritmo: str = VERSAO_ALGORITMO


def converter_tof(dados: DadosBrutos) -> pd.DataFrame:
    tof = dados.tof
    if tof.empty:
        return pd.DataFrame(columns=["t", "s_mm"])
    validos = tof[tof["status"] == 0]
    pts = np.array(dados.meta["calibracao_tof"]["pontos"])
    ordem = np.argsort(pts[:, 0])
    s_mm = np.interp(validos["distancia_mm"].to_numpy(float), pts[ordem, 0], pts[ordem, 1])
    return pd.DataFrame({"t": validos["t_banc_us"].to_numpy() / 1e6, "s_mm": s_mm})


def converter_carga(dados: DadosBrutos, ajuste: AjusteRelogio) -> pd.DataFrame | None:
    if dados.carga.empty or "calibracao_carga" not in dados.meta:
        return None
    cal = dados.meta["calibracao_carga"]
    pts = np.array(cal["pontos"])
    coef = np.polyfit(pts[:, 0], pts[:, 1], 2)      # curva, não só um fator
    rel = dados.carga["bruto"].to_numpy(float) - cal["tara_bruto"]
    t = ajuste.para_bancada(dados.carga["t_joy_us"].to_numpy()) / 1e6
    return pd.DataFrame({"t": t, "forca_N": np.polyval(coef, rel)})


def remover_pulsos_espurios(ir: pd.DataFrame, minimo_s: float) -> tuple[pd.DataFrame, int]:
    """Remove pares troca/destroca do mesmo sensor separados por menos de ``minimo_s``.

    Um disparo de luz ambiente liga e desliga o LM393 em poucos ms; uma passagem
    real deixa o sensor no novo estado bem mais tempo. Se for uma reversão
    real sobre a divisa, perde-se só uma correção (mesma posição).
    """
    if ir.empty:
        return ir, 0
    remover: set[int] = set()
    for _, g in ir.groupby("sensor"):
        idx = g.index.to_numpy()
        t = g["t_banc_us"].to_numpy() / 1e6
        i = 0
        while i < len(idx) - 1:
            if t[i + 1] - t[i] < minimo_s:
                remover.update((idx[i], idx[i + 1]))
                i += 2
            else:
                i += 1
    return ir.drop(index=list(remover)), len(remover) // 2


def processar_partida(dados: DadosBrutos, caminho: Caminho, tabela: TabelaTransicoes | None,
                      config: ConfigFusao | None = None) -> ResultadoFusao:
    ajuste = ajustar_relogio(dados.sinc)
    t_imu = ajuste.para_bancada(dados.imu["t_joy_us"].to_numpy()) / 1e6
    motor = MotorFusao(caminho, tabela, dados.meta["calibracao_imu"], config,
                       guardar_historico=True)
    motor.estado_ir = {sid: int(v) for sid, v in caminho.estado_sensores(0.0).items()}
    atraso_ir = tabela.atraso_ir_s if tabela is not None else 0.0

    # linha do tempo única com todas as medidas, no relógio da bancada
    ordem = []   # (t, prioridade, tipo, índice)
    imu = dados.imu[["seq", "ax", "ay", "gz"]].to_numpy()
    ordem += [(t_imu[i], 1, 0, i) for i in range(len(imu))]
    cfg = config or ConfigFusao()
    ir_df, n_espurios = remover_pulsos_espurios(dados.ir, cfg.pulso_espurio_s)
    ir = ir_df[["t_banc_us", "sensor", "estado"]].to_numpy()
    ordem += [(ir[i, 0] / 1e6 - atraso_ir, 0, 1, i) for i in range(len(ir))]
    tof = converter_tof(dados)
    tof_arr = tof.to_numpy()
    ordem += [(tof_arr[i, 0], 0, 2, i) for i in range(len(tof_arr))]
    ordem.sort()
    for t, _, tipo, i in ordem:
        if tipo == 0:
            motor.imu(t, int(imu[i, 1]), int(imu[i, 2]), int(imu[i, 3]), seq=int(imu[i, 0]))
        elif tipo == 1:
            motor.ir(t, int(ir[i, 1]), int(ir[i, 2]))
        else:
            motor.tof(t, float(tof_arr[i, 1]))

    _, xs, _ = motor.filtro.suavizar()
    idx = np.array(motor.indices_imu)
    xsi = xs[idx]
    xf = np.array(motor.filtrado_imu)
    serie = pd.DataFrame({
        "t": np.array(motor.tempos_imu),
        "s": xsi[:, IS], "v": xsi[:, IV], "a": xsi[:, IA], "j": xsi[:, IJ],
        "vies": xsi[:, IB],
        "s_rt": xf[:, IS], "v_rt": xf[:, IV],
        "a_medida": np.array(motor.acc_tangencial),
        "repouso": np.array(motor.repouso_imu),
    })
    q = motor.qualidade
    q.erro_sincronia_rms_us = ajuste.residuo_rms_us
    q.pulsos_espurios = n_espurios
    q.tof_falhas = int((dados.tof["status"] != 0).sum()) if not dados.tof.empty else 0
    eventos = dados.eventos.copy()
    eventos["t"] = eventos["t_banc_us"] / 1e6
    return ResultadoFusao(serie, converter_carga(dados, ajuste), eventos, q.como_dict(), ajuste)
