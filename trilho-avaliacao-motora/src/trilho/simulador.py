"""Simulador da bancada, dos joysticks e do paciente.

Gera partidas com resposta conhecida para testar e validar todo o
processamento sem hardware (ver "Testes automáticos com sinais sintéticos").
O simulador reproduz os efeitos descritos na arquitetura:

* movimento do paciente como soma de submovimentos de jerk mínimo, com pausas,
  retrocessos e tempo de reação;
* IMU (MPU6050) no relógio do joystick, com deriva do cristal, ruído (~0,03
  m/s²), viés, inclinação da base, rotação do pino e aceleração centrípeta;
* LM393 com desalinhamento, histerese por sentido, atraso de resposta,
  trepidação perto da divisa e disparos espúrios de luz ambiente;
* VL53L0X com não linearidade e falhas de leitura; HX711 a 80 Hz;
* trocas de sincronia por ESP-NOW com atraso variável (rádio ocupado).

O filtro contra trepidação dos LM393 é o mesmo do firmware (``debounce``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import VERSAO_FORMATO
from .dados import DadosBrutos
from .geometria import Caminho
from .transicoes import ObservacaoCalibracao, tabela_nominal

G = 9.80665
ESCALA_ACC = G / 16384.0              # ±2 g
ESCALA_GIRO = np.deg2rad(1.0) / 131.0  # ±250 °/s
TAXA_IMU = 500.0
TAXA_CARGA = 80.0
TAXA_TOF = 40.0
TAXA_VERDADE = 2000.0


# ======================================================================= paciente
@dataclass
class PerfilPaciente:
    velocidade_media_mm_s: float = 60.0
    variabilidade: float = 0.15
    submovimentos_max: int = 3        # 1 = movimento suave, mais = mais "quebrado"
    prob_pausa: float = 0.2
    prob_retrocesso: float = 0.15
    tempo_reacao_s: float = 0.45
    forca_base_N: float = 25.0        # preensão média (joystick C)
    forca_variacao_N: float = 5.0
    tremor_mm: float = 0.0


@dataclass
class Segmento:
    t0: float
    T: float
    delta_m: float


def _mj(tau: np.ndarray, T: float):
    """Perfil de jerk mínimo normalizado e derivadas (por unidade de deslocamento)."""
    x = np.clip(tau, 0.0, 1.0)
    dentro = (tau > 0.0) & (tau < 1.0)
    p = 10 * x**3 - 15 * x**4 + 6 * x**5
    v = np.where(dentro, (30 * x**2 - 60 * x**3 + 30 * x**4) / T, 0.0)
    a = np.where(dentro, (60 * x - 180 * x**2 + 120 * x**3) / T**2, 0.0)
    j = np.where(dentro, (60 - 360 * x + 360 * x**2) / T**3, 0.0)
    return p, v, a, j


@dataclass
class Trajetoria:
    s0_m: float
    segmentos: list[Segmento]
    duracao_s: float
    tentativas: list[dict] = field(default_factory=list)
    tremor_m: float = 0.0
    tremor_hz: float = 6.0

    def avaliar(self, t: np.ndarray):
        t = np.asarray(t, dtype=float)
        s = np.full_like(t, self.s0_m)
        v = np.zeros_like(t)
        a = np.zeros_like(t)
        j = np.zeros_like(t)
        for sg in self.segmentos:
            m = t > sg.t0
            if not np.any(m):
                continue
            p_, v_, a_, j_ = _mj((t[m] - sg.t0) / sg.T, sg.T)
            s[m] += sg.delta_m * p_
            v[m] += sg.delta_m * v_
            a[m] += sg.delta_m * a_
            j[m] += sg.delta_m * j_
        if self.tremor_m > 0:
            w = 2 * np.pi * self.tremor_hz
            em_mov = np.abs(v) > 0.005
            s = s + em_mov * self.tremor_m * np.sin(w * t)
            v = v + em_mov * self.tremor_m * w * np.cos(w * t)
            a = a - em_mov * self.tremor_m * w**2 * np.sin(w * t)
            j = j - em_mov * self.tremor_m * w**3 * np.cos(w * t)
        return s, v, a, j


def _percurso(p0: float, p1: float, t_inicio: float, perfil: PerfilPaciente,
              rng: np.random.Generator) -> tuple[list[Segmento], float]:
    """Segmentos de um percurso de p0 a p1 (m). Devolve (segmentos, t_fim)."""
    D = p1 - p0
    dur = abs(D) * 1000 / perfil.velocidade_media_mm_s
    dur *= float(np.clip(rng.normal(1.0, perfil.variabilidade), 0.6, 1.5))
    n = int(rng.integers(1, max(1, perfil.submovimentos_max) + 1))
    pesos = rng.dirichlet(np.full(n, 3.0)) if n > 1 else np.array([1.0])
    segs: list[Segmento] = []
    t = t_inicio
    for i, w in enumerate(pesos):
        T = max(0.35, dur * float(w) * (1.25 if n > 1 else 1.0))
        segs.append(Segmento(t, T, D * float(w)))
        if i < n - 1:
            if rng.random() < perfil.prob_pausa:
                t += T + rng.uniform(0.35, 0.9)               # para e recomeça
            else:
                t += T * rng.uniform(0.55, 0.85)              # submovimentos sobrepostos
    t_fim = segs[-1].t0 + segs[-1].T
    if rng.random() < perfil.prob_retrocesso and abs(D) > 0.05:
        # recua alguns milímetros no meio do caminho e retoma
        back = -np.sign(D) * rng.uniform(0.005, 0.012)
        k = len(segs) // 2
        tb = segs[k].t0 + segs[k].T * 0.95 if n > 1 else t_inicio + dur * 0.5
        Tb = rng.uniform(0.4, 0.7)
        ajuste = [Segmento(tb, Tb, back), Segmento(tb + Tb * 0.9, Tb, -back)]
        # empurra os segmentos seguintes para depois do retrocesso
        atraso = Tb * 1.9
        for sg in segs:
            if sg.t0 >= tb:
                sg.t0 += atraso
        segs.extend(ajuste)
        t_fim = max(sg.t0 + sg.T for sg in segs)
    return segs, t_fim


def gerar_trajetoria(caminho: Caminho, n_tentativas: int, perfil: PerfilPaciente,
                     rng: np.random.Generator, repouso_inicial_s: float = 1.5) -> Trajetoria:
    L = caminho.comprimento_mm / 1000.0
    segs: list[Segmento] = []
    tentativas = []
    t = repouso_inicial_s
    pos = 0.0
    for k in range(n_tentativas):
        estimulo = t
        rt = max(0.15, rng.normal(perfil.tempo_reacao_s, 0.08))
        inicio = estimulo + rt
        if caminho.vai_e_volta:
            s1, t1 = _percurso(0.0, L, inicio, perfil, rng)
            s2, t2 = _percurso(L, 0.0, t1 + rng.uniform(0.1, 0.3), perfil, rng)
            segs += s1 + s2
            fim, destino = t2, 0.0
        else:
            destino = L if pos == 0.0 else 0.0
            s1, fim = _percurso(pos, destino, inicio, perfil, rng)
            segs += s1
        tentativas.append({"estimulo": estimulo, "inicio": inicio, "fim": fim,
                           "de": pos, "para": destino})
        pos = destino
        t = fim + rng.uniform(1.2, 2.0)
    return Trajetoria(0.0, segs, t + 0.5, tentativas, tremor_m=perfil.tremor_mm / 1000)


# ======================================================================= firmware
def debounce(trocas: list[tuple[float, int]], estado_inicial: int,
             minimo_s: float = 0.005) -> list[tuple[float, int]]:
    """Filtro contra trepidação dos LM393 (mesma lógica do firmware).

    Ignora pulsos menores que ``minimo_s``. Quando um novo estado fica estável,
    registra o instante da primeira troca da rajada que levou a ele (o mais
    próximo da passagem física pela divisa).
    """
    saida: list[tuple[float, int]] = []
    estavel = estado_inicial
    inicio_rajada: dict[int, float] = {}
    n = len(trocas)
    for i, (t, e) in enumerate(trocas):
        if e != estavel and e not in inicio_rajada:
            inicio_rajada[e] = t
        dur = (trocas[i + 1][0] - t) if i + 1 < n else np.inf
        if dur >= minimo_s:
            if e != estavel:
                saida.append((inicio_rajada.get(e, t), e))
                estavel = e
            inicio_rajada.clear()
    return saida


# ======================================================================= bancada
@dataclass
class BancadaSimulada:
    """Parâmetros físicos da bancada (fixos por bancada, como no hardware real)."""

    caminho: Caminho
    semente: int = 7
    atraso_ir_s: float = 0.004
    histerese_mm: float = 2.5
    jitter_ir_s: float = 0.0004
    prob_trepidacao: float = 0.4
    espurios_por_min: float = 1.0
    # relógio do joystick: t_joy = a·t_banc + c
    deriva_ppm: float = 35.0
    c_joy_s: float = 12.345678
    atraso_radio_s: float = 0.0009

    def __post_init__(self) -> None:
        rng = np.random.default_rng(self.semente)
        nominal = tabela_nominal(self.caminho)
        desalinh = {sid: rng.normal(0, 1.5) for sid in self.caminho.sensores}
        # posição física de disparo por divisa e sentido (o que a calibração mede)
        self.disparos = []
        for l in nominal.linhas:
            h = self.histerese_mm / 2 * (1 if l.sentido == "ida" else -1)
            self.disparos.append((l, l.posicao_mm + desalinh[l.sensor] + h))

    # -------------------------------------------------------------------- IR
    def eventos_ir(self, t: np.ndarray, s_mm: np.ndarray, rng: np.random.Generator,
                   espurios: bool = True) -> pd.DataFrame:
        """Eventos (já filtrados pelo firmware) de todos os LM393, no relógio da bancada."""
        brutos: dict[int, list[tuple[float, int]]] = {sid: [] for sid in self.caminho.sensores}
        for lin, p in self.disparos:
            if lin.sentido == "ida":
                idx = np.nonzero((s_mm[:-1] < p) & (s_mm[1:] >= p))[0]
            else:
                idx = np.nonzero((s_mm[:-1] > p) & (s_mm[1:] <= p))[0]
            for i in idx:
                f = (p - s_mm[i]) / (s_mm[i + 1] - s_mm[i])
                tc = t[i] + f * (t[i + 1] - t[i])
                brutos[lin.sensor].append((tc, 1 if lin.troca == "liga" else 0))
        linhas = []
        inicial = self.caminho.estado_sensores(float(s_mm[0]))
        for sid, trocas in brutos.items():
            trocas.sort()
            # mantém só trocas que mudam o estado (histerese)
            e0 = int(inicial[sid])
            estado, limpas = e0, []
            for tc, e in trocas:
                if e != estado:
                    limpas.append((tc, e))
                    estado = e
            # atraso de resposta, trepidação e disparos espúrios
            sinal: list[tuple[float, int]] = []
            for tc, e in limpas:
                te = tc + self.atraso_ir_s + rng.normal(0, self.jitter_ir_s)
                sinal.append((te, e))
                if rng.random() < self.prob_trepidacao:
                    tt = te
                    for _ in range(int(rng.integers(1, 4))):
                        tt += rng.uniform(0.0003, 0.002)
                        sinal.append((tt, 1 - e))
                        tt += rng.uniform(0.0003, 0.002)
                        sinal.append((tt, e))
            if espurios and self.espurios_por_min > 0:
                n_esp = rng.poisson(self.espurios_por_min * (t[-1] - t[0]) / 60)
                for te in rng.uniform(t[0] + 2, t[-1], n_esp):
                    est = self._estado_em(limpas, te, e0)
                    if est == 0:
                        d = rng.uniform(0.008, 0.03)
                        sinal += [(te, 1), (te + d, 0)]
            sinal.sort()
            for te, e in debounce(sinal, e0):
                linhas.append((te, sid, e))
        linhas.sort()
        df = pd.DataFrame(linhas, columns=["t", "sensor", "estado"])
        return pd.DataFrame({
            "seq": np.arange(len(df), dtype="int64"),
            "t_banc_us": np.round(df["t"].to_numpy() * 1e6).astype("int64"),
            "sensor": df["sensor"].astype("int64"),
            "estado": df["estado"].astype("int64"),
        })

    @staticmethod
    def _estado_em(trocas: list[tuple[float, int]], t: float, estado: int = 0) -> int:
        for tc, e in trocas:
            if tc > t:
                break
            estado = e
        return estado

    # ---------------------------------------------------------------- relógio
    def t_joy(self, t_banc: np.ndarray | float) -> np.ndarray:
        return (1 + self.deriva_ppm * 1e-6) * np.asarray(t_banc) + self.c_joy_s

    def t_banc(self, t_joy: np.ndarray | float) -> np.ndarray:
        return (np.asarray(t_joy) - self.c_joy_s) / (1 + self.deriva_ppm * 1e-6)

    def trocas_sincronia(self, duracao_s: float, rng: np.random.Generator) -> pd.DataFrame:
        t1 = self.t_joy(0.0) + np.arange(0.05, duracao_s, 0.1)

        def atraso(n):
            d = self.atraso_radio_s + rng.exponential(0.0004, n)
            ocupado = rng.random(n) < 0.1
            return d + ocupado * rng.uniform(0.003, 0.025, n)

        t2 = self.t_banc(t1) + atraso(len(t1))
        t3 = t2 + 0.00015
        t4 = self.t_joy(t3 + atraso(len(t1)))
        us = lambda x: np.round(np.asarray(x) * 1e6).astype("int64")  # noqa: E731
        return pd.DataFrame({"t1_us": us(t1), "t2_us": us(t2), "t3_us": us(t3), "t4_us": us(t4)})


# =================================================================== a partida
@dataclass
class ConfigIMU:
    ruido_acc: float = 0.03            # m/s² (MPU6050 com DLPF ~44 Hz)
    vies_residual_acc: tuple[float, float] = (0.015, -0.01)   # m/s², corpo
    inclinacao_plano: tuple[float, float] = (0.12, -0.08)     # m/s², placa (base inclinada)
    offset_lsb: tuple[int, int, int] = (180, -95, 240)
    ruido_giro: float = 0.003          # rad/s
    vies_giro: float = 0.012           # rad/s
    offset_giro_lsb: tuple[int, int, int] = (-21, 14, 37)
    psi0: float = 0.0                  # orientação do corpo em relação à placa
    oscilacao_psi: float = 0.05        # rad, giro lento do pino
    jitter_carimbo_s: float = 20e-6


@dataclass
class ResultadoSimulacao:
    dados: DadosBrutos
    verdade: pd.DataFrame            # t (bancada, s), s, v, a, j (SI)
    trajetoria: Trajetoria
    forca_verdadeira: pd.DataFrame | None = None


def simular_partida(caminho: Caminho, n_tentativas: int = 6,
                    perfil: PerfilPaciente | None = None,
                    bancada: BancadaSimulada | None = None,
                    imu: ConfigIMU | None = None, semente: int = 1,
                    joystick_id: str | None = None,
                    tabela_id: str = "nominal") -> ResultadoSimulacao:
    rng = np.random.default_rng(semente)
    perfil = perfil or PerfilPaciente()
    bancada = bancada or BancadaSimulada(caminho)
    imu = imu or ConfigIMU()
    traj = gerar_trajetoria(caminho, n_tentativas, perfil, rng)
    T = traj.duracao_s

    # ---------------------------------------------------- verdade densa (2 kHz)
    tv = np.arange(0.0, T, 1 / TAXA_VERDADE)
    sv, vv, _, _ = traj.avaliar(tv)
    ir = bancada.eventos_ir(tv, sv * 1000, rng)

    # --------------------------------------------------------- IMU (joystick)
    tj0 = float(bancada.t_joy(0.0))
    n_imu = int((T - 0.01) * TAXA_IMU)
    t_joy = tj0 + np.arange(n_imu) / TAXA_IMU + rng.normal(0, imu.jitter_carimbo_s, n_imu)
    t_b = bancada.t_banc(t_joy)
    s, v, a, j = traj.avaliar(t_b)
    s_mm = s * 1000
    Tvec = caminho.tangente(s_mm)
    dT = caminho.derivada_tangente(s_mm) * 1000.0          # 1/m
    a_placa = a[:, None] * Tvec + (v**2)[:, None] * dT
    f_placa = a_placa + np.asarray(imu.inclinacao_plano)
    # o pino gira devagar durante a partida (parado no repouso inicial)
    w_psi = 2 * np.pi * 0.07
    tr = np.maximum(t_b - 1.2, 0.0)
    psi = imu.psi0 + imu.oscilacao_psi * (1 - np.cos(w_psi * tr)) / 2
    dpsi = imu.oscilacao_psi * w_psi * np.sin(w_psi * tr) / 2
    c, si = np.cos(-psi), np.sin(-psi)
    fx = c * f_placa[:, 0] - si * f_placa[:, 1]
    fy = si * f_placa[:, 0] + c * f_placa[:, 1]
    fx += imu.vies_residual_acc[0] + rng.normal(0, imu.ruido_acc, n_imu)
    fy += imu.vies_residual_acc[1] + rng.normal(0, imu.ruido_acc, n_imu)
    fz = G + rng.normal(0, imu.ruido_acc, n_imu)
    gz = dpsi + imu.vies_giro + rng.normal(0, imu.ruido_giro, n_imu)
    gx = rng.normal(0, imu.ruido_giro, n_imu)
    gy = rng.normal(0, imu.ruido_giro, n_imu)

    def lsb(x, esc, off):
        return np.clip(np.round(x / esc) + off, -32768, 32767).astype("int64")

    imu_df = pd.DataFrame({
        "seq": np.arange(n_imu, dtype="int64"),
        "t_joy_us": np.round(t_joy * 1e6).astype("int64"),
        "ax": lsb(fx, ESCALA_ACC, imu.offset_lsb[0]),
        "ay": lsb(fy, ESCALA_ACC, imu.offset_lsb[1]),
        "az": lsb(fz, ESCALA_ACC, imu.offset_lsb[2]),
        "gx": lsb(gx, ESCALA_GIRO, imu.offset_giro_lsb[0]),
        "gy": lsb(gy, ESCALA_GIRO, imu.offset_giro_lsb[1]),
        "gz": lsb(gz, ESCALA_GIRO, imu.offset_giro_lsb[2]),
    })
    verdade = pd.DataFrame({"t": t_b, "s": s, "v": v, "a": a, "j": j})

    meta = {
        "versao_formato": VERSAO_FORMATO,
        "caminho": caminho.id,
        "joystick_id": joystick_id or f"JOY-{caminho.id}-001",
        "bancada_id": f"BANC-{bancada.semente:03d}",
        "versao_firmware": "sim-1.0",
        "taxas_hz": {"imu": TAXA_IMU, "carga": TAXA_CARGA, "tof": TAXA_TOF},
        "calibracao_imu": {
            "escala_acc": ESCALA_ACC, "offset_acc_lsb": list(imu.offset_lsb),
            "escala_giro": ESCALA_GIRO, "offset_giro_lsb": list(imu.offset_giro_lsb),
            "psi0_rad": imu.psi0,
        },
        "tabela_transicoes": tabela_id,
        "simulado": True,
    }

    # ------------------------------------------------ joystick C: carga + ToF
    forca = None
    carga_df = None
    tof_df = None
    if caminho.tem_tof:
        # VL53L0X na extremidade s = 0, 50 mm antes do início do curso
        t_tof = np.arange(0.0, T, 1 / TAXA_TOF)
        s_tof = traj.avaliar(t_tof)[0] * 1000
        def nao_linear(x):
            return 50.0 + x + 0.0006 * (x - 60.0) ** 2
        d = nao_linear(s_tof) + rng.normal(0, 1.5, len(t_tof))
        status = np.where(rng.random(len(t_tof)) < 0.01, 4, 0)
        d = np.where(status == 0, d, 8191)
        tof_df = pd.DataFrame({
            "seq": np.arange(len(t_tof), dtype="int64"),
            "t_banc_us": np.round(t_tof * 1e6).astype("int64"),
            "distancia_mm": np.round(d).astype("int64"),
            "status": status.astype("int64"),
        })
        ref = np.arange(0.0, caminho.comprimento_mm + 1, 10.0)
        meta["calibracao_tof"] = {"pontos": [[float(nao_linear(x)), float(x)] for x in ref]}

        # célula de carga: 4 meias-pontes em ponte completa, um HX711
        t_c_joy = tj0 + np.arange(int(T * TAXA_CARGA)) / TAXA_CARGA
        t_c = bancada.t_banc(t_c_joy)
        _, vc, ac, _ = traj.avaliar(t_c)
        F = (perfil.forca_base_N
             + perfil.forca_variacao_N * np.sin(2 * np.pi * 0.21 * t_c + 1.0)
             + 15.0 * np.abs(ac)                      # aperta mais ao acelerar
             + rng.normal(0, 0.4, len(t_c)))
        F = np.where(t_c < 0.8, 0.0, np.maximum(F, 0.0))  # sem contato no início
        k1, k2, tara = 4000.0, 2.0, 85_000
        bruto = tara + k1 * F + k2 * F**2 + rng.normal(0, 150, len(t_c))
        carga_df = pd.DataFrame({
            "seq": np.arange(len(t_c), dtype="int64"),
            "t_joy_us": np.round(t_c_joy * 1e6).astype("int64"),
            "bruto": np.round(bruto).astype("int64"),
        })
        pesos = np.array([0.0, 20.0, 50.0, 100.0, 200.0, 400.0])
        meta["calibracao_carga"] = {
            "pontos": [[float(k1 * p + k2 * p * p), float(p)] for p in pesos],
            "tara_bruto": tara + float(rng.normal(0, 30)),
        }
        forca = pd.DataFrame({"t": t_c, "forca_N": F})

    eventos = pd.DataFrame({
        "t_banc_us": np.array([round(x["estimulo"] * 1e6) for x in traj.tentativas], dtype="int64"),
        "tipo": ["estimulo"] * len(traj.tentativas),
        "valor": np.arange(1, len(traj.tentativas) + 1, dtype="int64"),
    })

    dados = DadosBrutos(meta=meta, imu=imu_df, ir=ir,
                        sinc=bancada.trocas_sincronia(T, rng), eventos=eventos)
    if carga_df is not None:
        dados.carga = carga_df
    if tof_df is not None:
        dados.tof = tof_df
    return ResultadoSimulacao(dados, verdade, traj, forca)


# ================================================================ calibração IR
def simular_calibracao_ir(bancada: BancadaSimulada, n_passagens: int = 12,
                          semente: int = 3) -> list[ObservacaoCalibracao]:
    """Passagens lentas nos dois sentidos com um encoder temporário de referência."""
    rng = np.random.default_rng(semente)
    L = bancada.caminho.comprimento_mm
    obs: list[ObservacaoCalibracao] = []
    for k in range(n_passagens):
        vel = rng.uniform(20, 250)            # mm/s, várias velocidades (separa o atraso)
        T = L / vel
        t = np.arange(0, T, 1 / TAXA_VERDADE)
        tau = t / T
        s_ida = L * (10 * tau**3 - 15 * tau**4 + 6 * tau**5)
        v_ida = L * (30 * tau**2 - 60 * tau**3 + 30 * tau**4) / T
        for s_mm, v_mm in ((s_ida, v_ida), (L - s_ida, -v_ida)):
            ev = bancada.eventos_ir(t, s_mm, rng, espurios=False)
            for r in ev.itertuples(index=False):
                te = r.t_banc_us / 1e6
                sr = float(np.interp(te, t, s_mm)) + rng.normal(0, 0.1)   # encoder
                vr = float(np.interp(te, t, v_mm))
                if abs(vr) < 2.0:
                    continue
                obs.append(ObservacaoCalibracao(int(r.sensor), "liga" if r.estado else "desliga",
                                                "ida" if vr > 0 else "volta", sr, vr))
    return obs
