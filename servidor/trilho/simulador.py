"""Simulador da bancada + joysticks.

Gera exatamente os mesmos bytes que a bancada real manda pela USB (quadros
COBS + CRC), com: relógios diferentes e com deriva entre joystick e bancada,
latência e perda de pacotes no rádio, reenvio sob pedido, ruído e inclinação
da IMU, bordas dos LM393 a partir da tabela de transições e VL53L0X com ruído.
Serve para testar o servidor e o site sem hardware.

Dois tipos de hardware:
- caixa=True: a caixa real (uma ESP32, sensores por fio, linhas de texto com o
  relógio único da caixa; ver firmware/caixa/caixa.ino). É o padrão do
  "iniciar_simulador.bat".
- caixa=False: a bancada com joysticks sem fio (protocolo binário), mantida para
  os testes do caminho antigo.

Dois usos:
- PortaSimulada: imita uma porta serial em tempo real (python -m trilho --simulador);
  o mouse no site pode guiar o joystick virtual.
- GeradorSimulacao: tempo virtual, para os testes automáticos.
"""
from __future__ import annotations

import heapq
import itertools
import math
import threading
import time

import numpy as np

from . import protocolo as p
from . import protocolo_caixa as pc
from .caminhos import CAMINHOS, niveis_em

MASCARA = 0xFFFFFFFF


def min_jerk(s0: float, s1: float, T: float, t: float) -> tuple[float, float, float]:
    u = min(max(t / T, 0.0), 1.0)
    d = s1 - s0
    s = s0 + d * (10 * u ** 3 - 15 * u ** 4 + 6 * u ** 5)
    v = d / T * (30 * u ** 2 - 60 * u ** 3 + 30 * u ** 4) if 0 < t < T else 0.0
    a = d / T ** 2 * (60 * u - 180 * u ** 2 + 120 * u ** 3) if 0 < t < T else 0.0
    return s, v, a


class Programa:
    """Sequência de movimentos de jerk mínimo: [(alvo_mm, duracao_s, pausa_depois_s), ...]."""

    def __init__(self, s0: float, movimentos: list[tuple[float, float, float]], repetir: bool = True) -> None:
        self.s0 = s0
        self.movs = movimentos
        self.repetir = repetir
        self.ciclo = sum(T + pz for _, T, pz in movimentos)

    def __call__(self, t: float) -> tuple[float, float, float]:
        if t < 0:
            return self.s0, 0.0, 0.0
        if self.repetir and self.ciclo > 0:
            t = t % self.ciclo
        s_ini = self.s0
        for alvo, T, pz in self.movs:
            if t < T:
                return min_jerk(s_ini, alvo, T, t)
            t -= T
            if t < pz:
                return alvo, 0.0, 0.0
            t -= pz
            s_ini = alvo
        return s_ini, 0.0, 0.0


def programa_padrao(caminho: str) -> Programa:
    if caminho == "espiral":
        L = CAMINHOS["espiral"].comprimento
        return Programa(0.0, [(L, 14.0, 1.0), (0.0, 14.0, 1.5)])
    if caminho == "guarda_chuva":
        return Programa(0.0, [(170.0, 2.5, 0.6), (0.0, 2.5, 0.6)])
    return Programa(60.0, [(110.0, 1.5, 1.0), (60.0, 1.2, 1.2), (15.0, 1.5, 1.0), (60.0, 1.2, 1.5)])


class _Joystick:
    def __init__(self, jid: str, rng: np.random.Generator) -> None:
        self.id = jid
        self.caminho = CAMINHOS[{"A": "guarda_chuva", "B": "espiral", "C": "reto"}[jid]]
        self.seq = 0
        self.guardados: dict[int, bytes] = {}
        self.confirmado = 0
        self.estado = p.E_PRONTO
        self.offset_us = float(rng.uniform(1e6, 5e8))
        self.deriva = float(rng.uniform(-40e-6, 40e-6))
        self.ping_id = 0
        self.imu_buf: list[tuple] = []
        self.bateria_mv = int(rng.uniform(3800, 4100))

    def relogio(self, tv: float) -> float:
        return tv * 1e6 * (1 + self.deriva) + self.offset_us


class GeradorSimulacao:
    def __init__(self, semente: int = 1, perda: float = 0.01, ruido_acc: float = 25.0, ativo: str = "B",
                 joysticks: str = "ABC", programa: Programa | None = None, inclinacao_graus=(2.0, -1.0),
                 caixa: bool = False, giro_arco: bool | None = None, sinal_giro: float = -1.0,
                 fator_giro: float = 1.0, eixo_giro_graus: float = 0.0,
                 guinada_graus: float = 0.0, y_vertical: bool = False) -> None:
        self.rng = np.random.default_rng(semente)
        self.caixa = caixa
        # no guarda-chuva da caixa o IMU gira junto com o arco (o sinal depende da montagem)
        self.giro_arco = caixa if giro_arco is None else giro_arco
        self.sinal_giro = sinal_giro
        self.fator_giro = fator_giro   # quanto o IMU real gira a mais (ou a menos) que o desenho do arco
        # inclinação do eixo em que o IMU gira no arco: 0 = vertical (gravidade); 90 = deitado
        b = math.radians(eixo_giro_graus)
        self.eixo_giro = np.array([math.sin(b), 0.0, math.cos(b)])
        self.caixa_ativo = "0"
        self.prox_imu_cx = {j: 0.0 for j in joysticks}
        self.prox_status_cx = 0.05
        self.perda = perda
        self.ruido_acc = ruido_acc
        self.tv = 0.0
        self.offset_banc = float(self.rng.uniform(1e6, 4e9))   # relógio da bancada começa num valor qualquer
        self.joys = {j: _Joystick(j, self.rng) for j in joysticks}
        self.ativo = ativo
        self.programa = programa
        self.alvo_f: float | None = None
        self.t_alvo = -1e9
        self._t_prog0 = 0.0
        # estado verdadeiro do joystick ativo
        c = self.joys[ativo].caminho
        prog = programa or programa_padrao(c.id)
        self.verdade = {"s": prog(0)[0], "v": 0.0, "a": 0.0}
        self._seguidor = {"s": self.verdade["s"], "v": 0.0, "a": 0.0}
        self.niveis = {s: 0 for s in range(5)}
        self._atualizar_niveis(self.verdade["s"], self.tv, emitir=False)
        # tempos dos próximos eventos
        self.prox_imu = 0.0
        self.prox_vl53 = 0.0
        self.prox_ping = {j: 0.013 * i for i, j in enumerate(self.joys)}
        self.prox_status_joy = {j: 0.2 + 0.1 * i for i, j in enumerate(self.joys)}
        self.prox_status_banc = 0.05
        self.s_reto_parado = 60.0
        # saída
        self._fila: list = []
        self._ordem = itertools.count()
        self.registro_verdade: list[tuple] = []  # (t_bancada_us, s, v, a) do joystick ativo
        self.t_inicio_partida = -1e9
        # inclinação fixa da montagem (sensor em relação à placa)
        rx, ry = (math.radians(x) for x in inclinacao_graus)
        Rx = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
        Ry = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
        self.M = (Rx @ Ry).T   # mundo -> sensor
        # placa girada em torno da vertical (guinada) e/ou montada "em pé" (eixo y do sensor para
        # cima, como a MPU-92.65 da espiral na bancada)
        g = math.radians(guinada_graus)
        Rz = np.array([[math.cos(g), -math.sin(g), 0], [math.sin(g), math.cos(g), 0], [0, 0, 1]])
        self.M = self.M @ Rz.T
        if y_vertical:
            self.M = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]]) @ self.M
        self.vies_acc = self.rng.normal(0, 8.0, 3)
        self.vies_giro = self.rng.normal(0, 0.3, 3)
        if caixa:
            self._enviar(0.0, pc.montar_linha("L,caixa iniciada; fw 207; VL53L0X ok; IMU A ok, B ok, C ok (simulador)"))
        else:
            self._enviar(0.0, p.montar_quadro(p.Q_LOG, "bancada iniciada; MAC 24:6F:28:AA:BB:CC; VL53L0X ok; fw 100 (simulador)".encode()))

    # ------------------------------------------------------------------ util
    def relogio_banc(self, tv: float) -> float:
        return tv * 1e6 + self.offset_banc

    def _enviar(self, t_entrega: float, dados: bytes) -> None:
        heapq.heappush(self._fila, (t_entrega, next(self._ordem), dados))

    def _latencia_radio(self) -> float:
        if self.rng.random() < 0.05:
            return float(self.rng.uniform(0.010, 0.030))
        return float(self.rng.uniform(0.001, 0.004))

    def _enviar_pacote_joy(self, j: _Joystick, tipo: int, corpo: bytes, n: int, tv: float, guardar: bool = True) -> None:
        if guardar:
            j.seq += 1
            pac = p.pacote_joy(tipo, j.id, j.seq, corpo, n)
            j.guardados[j.seq] = pac
        else:
            pac = corpo
        self._transmitir(j, pac, tv)

    def _transmitir(self, j: _Joystick, pac: bytes, tv: float) -> None:
        if self.rng.random() < self.perda:
            return  # perdido no rádio (fica guardado para reenvio)
        chegada = tv + 0.0005 + self._latencia_radio()
        t_rx = int(self.relogio_banc(chegada)) & MASCARA
        self._enviar(chegada + 0.0008, p.quadro_pacote_joy(t_rx, -55, pac))

    # ------------------------------------------------------------- comandos
    def comando(self, dados: bytes) -> None:
        if self.caixa:
            self._comando_caixa(dados)
            return
        for tipo, corpo in p.LeitorQuadros().alimentar(dados):
            if tipo != p.Q_CMD_JOY or len(corpo) < p.CMD_JOY.size:
                continue
            jid, cmd, _, a, b = p.CMD_JOY.unpack_from(corpo)
            j = self.joys.get(chr(jid))
            if j is None:
                continue
            if cmd == p.C_INICIAR:
                if j.id != self.ativo:
                    self.definir_ativo(j.id)
                j.estado = p.E_GRAVANDO
                self.t_inicio_partida = self.tv
                self._t_prog0 = self.tv + 3.0   # o site faz contagem de 3 s
            elif cmd == p.C_PARAR:
                j.estado = p.E_PRONTO
            elif cmd == p.C_ACK_ATE:
                j.confirmado = max(j.confirmado, a)
                for s in [s for s in j.guardados if s <= a]:
                    del j.guardados[s]
            elif cmd == p.C_REENVIAR:
                for s in range(a, min(b, a + 63) + 1):
                    if s in j.guardados:
                        self._transmitir(j, j.guardados[s], self.tv + 0.002)

    def _comando_caixa(self, dados: bytes) -> None:
        for linha in dados.decode("ascii", "replace").splitlines():
            c = linha.strip().upper()
            if c == "STATUS":
                self._status_caixa(self.tv)
            elif c == "I2C":
                self._linha(self.tv + 0.03, "L,I2C D21/D22 (carro): 0x29 0x68")
                self._linha(self.tv + 0.05, "L,I2C D18/D19 (janela 0x68, espiral 0x69): 0x68 0x69")
            elif c.startswith("ATIVO"):
                a = c[5:].strip(" ,")[:1]
                if a in self.joys:
                    if a != self.ativo:
                        self.definir_ativo(a)
                    self.caixa_ativo = a
                    self.t_inicio_partida = self.tv
                    self._t_prog0 = self.tv + 3.0   # o site faz contagem de 3 s
                    self.prox_imu_cx = {j: self.tv for j in self.joys}
                elif a == "0":
                    self.caixa_ativo = "0"

    def _linha(self, t_entrega: float, corpo: str) -> None:
        self._enviar(t_entrega, pc.montar_linha(corpo))

    def _status_caixa(self, tv: float) -> None:
        mask = sum(int(self.niveis.get(i, 0)) << i for i in range(5))
        self._linha(tv + 0.0005, f"S,{int(self.relogio_banc(tv))},207,1,1,1,1,{self.caixa_ativo},{mask},0")

    def definir_ativo(self, jid: str) -> None:
        self.ativo = jid
        c = self.joys[jid].caminho
        s0 = (self.programa or programa_padrao(c.id))(0)[0]
        self.verdade = {"s": s0, "v": 0.0, "a": 0.0}
        self._seguidor = dict(self.verdade)
        self.alvo_f = None
        self.niveis = {s: 0 for s in range(5)}
        self._atualizar_niveis(s0, self.tv, emitir=False)

    def definir_alvo(self, f: float) -> None:
        """Mouse do site: posição desejada (0..1 ao longo do caminho)."""
        self.alvo_f = min(1.0, max(0.0, float(f)))
        self.t_alvo = self.tv

    # --------------------------------------------------------- movimento
    def _mover(self, tv: float, dt: float) -> None:
        c = self.joys[self.ativo].caminho
        if self.alvo_f is not None and tv - self.t_alvo < 3.0:
            seg = self._seguidor
            w = 2 * math.pi * 1.8
            a = w * w * (self.alvo_f * c.comprimento - seg["s"]) - 2 * w * seg["v"]
            seg["v"] = float(np.clip(seg["v"] + a * dt, -400, 400))
            seg["s"] = float(np.clip(seg["s"] + seg["v"] * dt, 0, c.comprimento))
            if seg["s"] in (0.0, c.comprimento):
                seg["v"] = 0.0
            seg["a"] = a
            self.verdade = dict(seg)
            self._t_prog0 = tv
        else:
            prog = self.programa or programa_padrao(c.id)
            s, v, a = prog(tv - self._t_prog0)
            self.verdade = {"s": s, "v": v, "a": a}
            self._seguidor = dict(self.verdade)

    def _atualizar_niveis(self, s: float, tv: float, emitir: bool = True) -> None:
        c = self.joys[self.ativo].caminho
        novos = dict(self.niveis)
        novos.update(niveis_em(c.id, s))
        for sid, nv in novos.items():
            if nv != self.niveis.get(sid) and emitir:
                t_borda = tv + 0.0003 + float(self.rng.normal(0, 0.0002))
                if self.caixa:     # a caixa só avisa depois de 5 ms estável, com o horário da 1ª troca
                    self._linha(t_borda + 0.0055, f"B,{int(self.relogio_banc(t_borda))},{sid},{nv}")
                    continue
                q = p.BORDA.pack(int(self.relogio_banc(t_borda)) & MASCARA, sid, nv, 0)
                self._enviar(t_borda + 0.0055, p.montar_quadro(p.Q_BORDA, q))
        self.niveis = novos

    # ------------------------------------------------------------ geração
    def _amostra_imu(self, j: _Joystick, tv: float, parado: bool = False) -> tuple:
        c = j.caminho
        s, v, a = (c.inicio, 0.0, 0.0) if parado else (self.verdade["s"], self.verdade["v"], self.verdade["a"])
        tg = c.tangente(s)
        nrm = np.array([-tg[1], tg[0]])
        k = c.curvatura(s)
        a_xy = a * tg + v * v * k * nrm
        w = 0.0
        mundo = np.array([a_xy[0], a_xy[1], p.G_MM_S2])
        if self.giro_arco and c.giro_acompanha:
            # o joystick gira com o arco em torno de self.eixo_giro: aceleração e gravidade
            # aparecem giradas no corpo do IMU; o giroscópio mede a velocidade em torno do eixo
            psi = self.sinal_giro * self.fator_giro * (c.angulo_tangente(s) - c.angulo_tangente(0.0))
            e = self.eixo_giro
            K = np.array([[0, -e[2], e[1]], [e[2], 0, -e[0]], [-e[1], e[0], 0]])
            R = np.eye(3) + math.sin(psi) * K + (1 - math.cos(psi)) * K @ K     # corpo -> mundo
            mundo = R.T @ mundo
            w = math.degrees(self.sinal_giro * self.fator_giro * v * k)
        acc = self.M @ mundo + self.vies_acc + self.rng.normal(0, self.ruido_acc, 3)
        giro = self.M @ (w * self.eixo_giro) + self.vies_giro + self.rng.normal(0, 0.05, 3)
        raw_a = np.clip(np.round(acc / p.G_MM_S2 * p.ACC_LSB_POR_G), -32768, 32767).astype(int)
        raw_g = np.clip(np.round(giro * p.GIRO_LSB_POR_GRAU_S), -32768, 32767).astype(int)
        t_j = int(j.relogio(tv) + self.rng.normal(0, 30)) & MASCARA
        return (t_j, *raw_a.tolist(), *raw_g.tolist())

    def _avancar_caixa(self, ate: float) -> None:
        """Caixa: tudo no relógio da caixa, linhas de texto, sem rádio nem sincronia."""
        dt = 0.0005
        while self.tv + dt <= ate:
            self.tv += dt
            tv = self.tv
            self._mover(tv, dt)
            self._atualizar_niveis(self.verdade["s"], tv)
            self.registro_verdade.append((self.relogio_banc(tv), self.verdade["s"], self.verdade["v"], self.verdade["a"]))
            if len(self.registro_verdade) > 2_000_000:
                del self.registro_verdade[:1_000_000]
            # IMU: o do jogo ativo a 250 Hz, os outros a 10 Hz
            for jid, jj in self.joys.items():
                if tv >= self.prox_imu_cx[jid]:
                    self.prox_imu_cx[jid] += 0.004 if jid == self.caixa_ativo else 0.1
                    am = self._amostra_imu(jj, tv, parado=jid != self.ativo)
                    tc = int(self.relogio_banc(tv) + self.rng.normal(0, 20))
                    self._linha(tv + 0.0006, f"I,{tc},{jid}," + ",".join(str(x) for x in am[1:]))
            # VL53L0X 50 Hz: a caixa marca o meio da medida
            if tv >= self.prox_vl53:
                self.prox_vl53 += 0.02
                s_reto = self.verdade["s"] if self.ativo == "C" else self.s_reto_parado
                d = int(round(50 + s_reto + self.rng.normal(0, 2.0)))
                self._linha(tv + 0.011, f"D,{int(self.relogio_banc(tv))},{d},0")
            if tv >= self.prox_status_cx:
                self.prox_status_cx += 0.5
                self._status_caixa(tv)

    def avancar(self, ate: float) -> list[bytes]:
        if self.caixa:
            self._avancar_caixa(ate)
            saida = []
            while self._fila and self._fila[0][0] <= ate:
                saida.append(heapq.heappop(self._fila)[2])
            return saida
        dt = 0.0005
        while self.tv + dt <= ate:
            self.tv += dt
            tv = self.tv
            self._mover(tv, dt)
            j = self.joys[self.ativo]
            self._atualizar_niveis(self.verdade["s"], tv)
            self.registro_verdade.append((self.relogio_banc(tv), self.verdade["s"], self.verdade["v"], self.verdade["a"]))
            if len(self.registro_verdade) > 2_000_000:
                del self.registro_verdade[:1_000_000]
            # IMU 500 Hz (só o joystick em uso, para poupar processamento)
            if tv >= self.prox_imu:
                self.prox_imu += 0.002
                j.imu_buf.append(self._amostra_imu(j, tv))
                if len(j.imu_buf) >= p.IMU_POR_PACOTE:
                    corpo = b"".join(p.IMU.pack(*x) for x in j.imu_buf)
                    self._enviar_pacote_joy(j, p.J_IMU, corpo, len(j.imu_buf), tv + 0.0008)
                    j.imu_buf = []
            # VL53L0X 50 Hz (sempre mede o trilho reto)
            if tv >= self.prox_vl53:
                self.prox_vl53 += 0.02
                s_reto = self.verdade["s"] if self.ativo == "C" else self.s_reto_parado
                d = int(round(50 + s_reto + self.rng.normal(0, 2.0)))
                q = p.VL53.pack(int(self.relogio_banc(tv)) & MASCARA, d, 0, 0)
                self._enviar(tv + 0.011, p.montar_quadro(p.Q_VL53, q))
            # sincronia de cada joystick (100 ms)
            for jid, jj in self.joys.items():
                if tv >= self.prox_ping[jid]:
                    self.prox_ping[jid] += 0.1
                    jj.ping_id += 1
                    l1, l2 = self._latencia_radio(), self._latencia_radio()
                    t1 = int(jj.relogio(tv)) & MASCARA
                    t2 = int(self.relogio_banc(tv + l1)) & MASCARA
                    t3 = int(self.relogio_banc(tv + l1 + 0.00004)) & MASCARA
                    t4 = int(jj.relogio(tv + l1 + 0.00004 + l2)) & MASCARA
                    corpo = p.SYNC.pack(t1, t2, t3, t4, jj.ping_id & 0xFFFF, 0)
                    self._enviar_pacote_joy(jj, p.J_SYNC, corpo, 1, tv + l1 + l2 + 0.0002)
                if tv >= self.prox_status_joy[jid]:
                    self.prox_status_joy[jid] += 1.0
                    corpo = p.STATUS_JOY.pack(int(jj.relogio(tv)) & MASCARA, jj.bateria_mv, 0, jj.estado, 0, 100,
                                              min(65535, len(jj.guardados)), 0, 0)
                    self._enviar_pacote_joy(jj, p.J_STATUS, corpo, 1, tv)
            if tv >= self.prox_status_banc:
                self.prox_status_banc += 0.5
                infos = b"".join(p.INFO_JOY.pack(ord(jid), -55, 20) for jid in self.joys)
                q = p.STATUS_BANC.pack(int(self.relogio_banc(tv)) & MASCARA, 100, 0, len(self.joys), 1) + infos
                self._enviar(tv + 0.001, p.montar_quadro(p.Q_STATUS_BANC, q))
        saida = []
        while self._fila and self._fila[0][0] <= ate:
            saida.append(heapq.heappop(self._fila)[2])
        return saida


class PortaSimulada:
    """Imita serial.Serial em tempo real (read/write/in_waiting/close)."""

    def __init__(self, **kw) -> None:
        self.gerador = GeradorSimulacao(**kw)
        self._buf = bytearray()
        self._trava = threading.Lock()
        self._t0 = time.monotonic()
        self._ativa = True
        self.is_open = True
        self._thread = threading.Thread(target=self._rodar, daemon=True, name="simulador")
        self._thread.start()

    def _rodar(self) -> None:
        while self._ativa:
            agora = time.monotonic() - self._t0
            with self._trava:
                for q in self.gerador.avancar(agora):
                    self._buf.extend(q)
            time.sleep(0.002)

    @property
    def in_waiting(self) -> int:
        return len(self._buf)

    def read(self, n: int = 1) -> bytes:
        fim = time.monotonic() + 0.02
        while not self._buf and time.monotonic() < fim:
            time.sleep(0.001)
        with self._trava:
            d = bytes(self._buf[:n])
            del self._buf[:n]
        return d

    def write(self, dados: bytes) -> int:
        with self._trava:
            self.gerador.comando(dados)
        return len(dados)

    def definir_alvo(self, f: float) -> None:
        with self._trava:
            self.gerador.definir_alvo(f)

    def close(self) -> None:
        self._ativa = False
        self.is_open = False
