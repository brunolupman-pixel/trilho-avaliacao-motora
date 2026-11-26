"""Núcleo de aquisição (arquitetura: processo "Núcleo de aquisição").

Lê a USB da bancada, decodifica os quadros, confirma (ACK) e pede reenvio
(NACK) dos pacotes dos joysticks, mantém a sincronia de relógio, roda a fusão
em tempo real da partida em andamento e grava os dados brutos da partida.
Roda numa thread própria; o servidor web só lê o estado publicado.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import protocolo as p
from . import protocolo_caixa as pc
from .caminhos import CAMINHOS, Caminho
from .fusao import ConfigFusao, FiltroCaminho, FusaoTempoReal
from .sincronia import ModeloRelogio, SincroniaTempoReal

log = logging.getLogger("trilho.aquisicao")

TIMEOUT_BANCADA_S = 2.0
TIMEOUT_JOY_S = 2.5


@dataclass
class EstadoJoy:
    id: str
    desenrolar: p.Desenrolador = field(default_factory=p.Desenrolador)
    sync: SincroniaTempoReal = field(default_factory=SincroniaTempoReal)
    status: dict = field(default_factory=dict)
    rssi: int = 0
    ultimo_contato: float = 0.0
    maior_seq: int = 0
    contiguo: int = 0
    faltando: dict = field(default_factory=dict)       # seq -> [instante detectado, tentativas]
    vistos: set = field(default_factory=set)
    perdidos_definitivos: int = 0
    duplicados: int = 0

    def registrar_seq(self, seq: int, agora: float) -> bool:
        """Devolve False se o pacote é repetido."""
        if self.maior_seq and seq < self.maior_seq - 50_000:
            log.warning("joystick %s reiniciou (sequência voltou de %d para %d)", self.id, self.maior_seq, seq)
            self.maior_seq = self.contiguo = 0
            self.faltando.clear()
            self.vistos.clear()
        if self.maior_seq == 0:
            self.maior_seq = seq
            self.contiguo = seq
            self.vistos.add(seq)
            return True
        if seq <= self.contiguo or seq in self.vistos:
            self.duplicados += 1
            return False
        self.vistos.add(seq)
        if seq > self.maior_seq:
            for s in range(self.maior_seq + 1, seq):
                self.faltando[s] = [agora, 0]
            self.maior_seq = seq
        self.faltando.pop(seq, None)
        novo = min(self.faltando) - 1 if self.faltando else self.maior_seq
        if novo > self.contiguo:
            self.vistos = {s for s in self.vistos if s > novo}
            self.contiguo = novo
        return True


@dataclass
class Gravacao:
    partida_id: int
    caminho: Caminho
    joy_id: str
    inicio_host: float
    seq_inicio: int = 0
    imu: list = field(default_factory=list)       # (seq, t_joy, ax, ay, az, gx, gy, gz)
    sync: list = field(default_factory=list)      # (t1, t2, t3, t4) desenrolados
    bordas: list = field(default_factory=list)    # (t_banc, sensor, nivel)
    vl53: list = field(default_factory=list)      # (t_banc, dist_mm, estado)
    status_joy: list = field(default_factory=list)
    seqs: set = field(default_factory=set)
    t_inicio_banc: float | None = None
    t_fim_banc: float | None = None
    relogio_unico: bool = False          # caixa: todos os sensores no mesmo relógio (sem sincronia)
    escala_giro: float | None = None     # giro do IMU por mm no arco, medido em partidas anteriores


class Aquisicao:
    def __init__(self, abrir_porta: Callable[[], object | None], relogio: Callable[[], float] = time.monotonic,
                 config_fusao: ConfigFusao | None = None) -> None:
        self.abrir_porta = abrir_porta
        self.relogio = relogio
        self.cfg_fusao = config_fusao or ConfigFusao()
        self.porta = None
        self.trava = threading.RLock()
        self.leitor = p.LeitorQuadros()
        self.desenrolar_banc = p.Desenrolador()
        self.joys: dict[str, EstadoJoy] = {}
        self.bancada: dict = {"conectada": False, "mac": None, "versao_fw": None, "vl53_ok": None,
                              "fila_perdida": 0, "ultimo_contato": 0.0}
        self.logs: deque = deque(maxlen=50)
        self._erro_fusao_avisado = False
        self._imu_zeros: dict[str, int] = {}      # leituras seguidas só com zeros, por joystick
        self.vl53_ultimo: dict | None = None
        self.niveis_lm393: dict[int, int] = {}
        self._offsets: deque = deque()        # (host, host_us - t_banc) para estimar "agora" no relógio da bancada
        self._offset_min: float | None = None
        self.gravacao: Gravacao | None = None
        self.fusao: FusaoTempoReal | None = None
        self._ultimo_ack = 0.0
        self._rodando = False
        self._thread: threading.Thread | None = None
        self.bytes_recebidos = 0
        self.ao_fechar_porta: Callable[[], None] | None = None   # avisa o gerenciador de portas
        self.bytes_desde_abertura = 0
        self.quadros_desde_abertura = 0
        self.amostra_bruta = bytearray()
        # Hardware: "caixa" (uma ESP32, tudo por fio, protocolo de texto) ou "bancada"
        # (bancada + joysticks sem fio, protocolo binário). Descoberto pelo que chega.
        self.modo: str | None = None
        self.leitor_caixa = pc.LeitorLinhas()
        self.caixa_status: pc.StatusCaixa | None = None
        self.imu_ultimo: dict[str, dict] = {}
        self._imu_hosts: dict[str, deque] = {}
        self._n_offset = 0
        self._ultima_tentativa = -10.0

    # ------------------------------------------------------------ ciclo
    def iniciar(self) -> None:
        self._rodando = True
        self._thread = threading.Thread(target=self._laco, daemon=True, name="aquisicao")
        self._thread.start()

    def parar(self) -> None:
        self._rodando = False
        if self._thread:
            self._thread.join(timeout=2)
        self._fechar_porta()

    def _fechar_porta(self) -> None:
        if self.porta is not None:
            try:
                self.porta.close()
            except Exception:
                pass
        self.porta = None
        self.bancada["ultimo_contato"] = 0.0      # sem porta, a bancada aparece desconectada na hora
        if self.ao_fechar_porta:
            self.ao_fechar_porta()

    def reconectar(self) -> None:
        """Fecha a porta e tenta abrir de novo imediatamente (troca de porta pelo site)."""
        with self.trava:
            self._fechar_porta()
            self._ultima_tentativa = -10.0

    def porta_aberta(self) -> bool:
        return self.porta is not None

    def diagnostico_porta(self) -> dict:
        """O que chegou desde que a porta abriu (bytes, quadros válidos e uma amostra)."""
        with self.trava:
            return {"bytes": self.bytes_desde_abertura, "quadros": self.quadros_desde_abertura,
                    "amostra": bytes(self.amostra_bruta)}

    def ultimo_contato_bancada(self) -> float:
        with self.trava:
            return self.bancada["ultimo_contato"]

    def _laco(self) -> None:
        while self._rodando:
            if self.porta is None:
                agora = self.relogio()
                if agora - self._ultima_tentativa >= 2.0:
                    self._ultima_tentativa = agora
                    try:
                        porta = self.abrir_porta()
                        with self.trava:
                            self.porta = porta
                            if porta is not None:
                                log.info("porta da bancada aberta")
                                self.leitor = p.LeitorQuadros()      # descarta restos da porta anterior
                                self.leitor_caixa = pc.LeitorLinhas()
                                self.modo = None
                                self.bytes_desde_abertura = self.quadros_desde_abertura = 0
                                self.amostra_bruta = bytearray()
                                self._escrever(p.montar_quadro(p.Q_PEDIR_STATUS))   # bancada sem fio
                                self._escrever(pc.comando("STATUS"))               # caixa
                                self._escrever(pc.comando("I2C"))                  # caixa: quem responde no I2C
                    except Exception as e:   # porta ocupada, cabo solto...
                        log.debug("não abriu a porta: %s", e)
                        self.porta = None
                if self.porta is None:
                    time.sleep(0.05)
                    continue
            porta = self.porta
            if porta is None:
                continue
            try:
                n = max(1, getattr(porta, "in_waiting", 0) or 0)
                dados = porta.read(min(n, 4096))
            except Exception as e:
                with self.trava:
                    if self.porta is not porta:     # fechada de propósito (troca de porta pelo site)
                        continue
                    log.warning("bancada desconectada: %s", e)
                    self._fechar_porta()
                    self.bancada["conectada"] = False
                continue
            try:
                self.passo(dados)
            except Exception:
                log.exception("erro ao processar os dados da caixa (a leitura continua)")

    def passo(self, dados: bytes) -> None:
        """Processa bytes recebidos e faz as tarefas periódicas (também usado nos testes)."""
        agora = self.relogio()
        with self.trava:
            if dados:
                self.bytes_recebidos += len(dados)
                self.bytes_desde_abertura += len(dados)
                if len(self.amostra_bruta) < 600:          # para o diagnóstico "a porta abriu, mas..."
                    self.amostra_bruta.extend(dados[:600 - len(self.amostra_bruta)])
                if self.modo in (None, "caixa"):
                    for msg in self.leitor_caixa.alimentar(dados):
                        if self.modo is None:
                            self.modo = "caixa"
                            log.info("hardware: caixa (uma ESP32, sensores por fio)")
                        self.quadros_desde_abertura += 1
                        self._tratar_caixa(msg, agora)
                if self.modo in (None, "bancada"):
                    for tipo, corpo in self.leitor.alimentar(dados):
                        msg = p.decodificar_quadro(tipo, corpo)
                        if msg is not None:
                            if self.modo is None:
                                self.modo = "bancada"
                                log.info("hardware: bancada com joysticks sem fio")
                            self.quadros_desde_abertura += 1
                            self._tratar(msg, agora)
            if agora - self._ultimo_ack >= 0.25:
                self._ultimo_ack = agora
                self._ack_nack(agora)
            if self.fusao is not None and self._offset_min is not None:
                try:
                    self.fusao.processar(self.agora_bancada(agora))
                except Exception:
                    # um erro de conta no filtro nunca pode parar a leitura da caixa nem a gravação
                    if not self._erro_fusao_avisado:
                        log.exception("erro na fusão em tempo real; o filtro recomeça de um estado seguro")
                        self._erro_fusao_avisado = True
                    self.fusao.f.reiniciar_seguro()
            self.bancada["conectada"] = agora - self.bancada["ultimo_contato"] < TIMEOUT_BANCADA_S

    def _escrever(self, dados: bytes) -> None:
        if self.porta is None:
            return
        try:
            self.porta.write(dados)
        except Exception as e:
            log.warning("falha ao escrever na bancada: %s", e)

    # ------------------------------------------------------------ relógio
    def _marcar_offset(self, agora: float, t_banc: float) -> None:
        off = agora * 1e6 - t_banc
        self._offsets.append((agora, off))
        while self._offsets and agora - self._offsets[0][0] > 10.0:
            self._offsets.popleft()
        self._offset_min = min(o for _, o in self._offsets)

    def agora_bancada(self, agora: float | None = None) -> float:
        agora = self.relogio() if agora is None else agora
        if self._offset_min is None:
            return 0.0
        return agora * 1e6 - self._offset_min

    # ------------------------------------------------------------ mensagens
    def _joy(self, jid: str) -> EstadoJoy:
        if jid not in self.joys:
            self.joys[jid] = EstadoJoy(jid)
        return self.joys[jid]

    def _tratar(self, msg, agora: float) -> None:
        self.bancada["ultimo_contato"] = agora
        g = self.gravacao
        if isinstance(msg, p.PacoteJoy):
            t_rx = self.desenrolar_banc(msg.t_rx)
            self._marcar_offset(agora, t_rx)
            j = self._joy(msg.joy_id)
            j.ultimo_contato = agora
            j.rssi = msg.rssi
            if not j.registrar_seq(msg.seq, agora):
                return
            gravando = g is not None and g.joy_id == j.id and msg.seq >= g.seq_inicio
            if gravando:
                g.seqs.add(msg.seq)
            if msg.tipo == p.J_IMU:
                ativo = self.fusao is not None and g is not None and g.joy_id == j.id
                for a in msg.imu:
                    tj = j.desenrolar(a[0])
                    if gravando:
                        g.imu.append((msg.seq, tj, *a[1:]))
                    if ativo and j.sync.modelo.valido:
                        tb = float(j.sync.modelo.para_bancada(tj))
                        self.fusao.adicionar(tb, "imu", (np.array(a[1:4], float), np.array(a[4:7], float)))
            elif msg.tipo == p.J_SYNC:
                t1, t2, t3, t4, _ = msg.sync
                reg = (j.desenrolar(t1), self.desenrolar_banc(t2), self.desenrolar_banc(t3), j.desenrolar(t4))
                j.sync.adicionar(*reg)
                if gravando:
                    g.sync.append(reg)
            elif msg.tipo == p.J_STATUS:
                j.status = msg.status
                if gravando:
                    g.status_joy.append((agora, dict(msg.status)))
        elif isinstance(msg, p.Borda):
            t = self.desenrolar_banc(msg.t_us)
            self.niveis_lm393[msg.sensor] = msg.nivel
            if g is not None and msg.sensor in g.caminho.sensores:
                g.bordas.append((t, msg.sensor, msg.nivel))
            if self.fusao is not None:
                self.fusao.adicionar(t, "borda", (msg.sensor, msg.nivel))
        elif isinstance(msg, p.LeituraVl53):
            t = self.desenrolar_banc(msg.t_us)
            self.vl53_ultimo = {"dist_mm": msg.dist_mm if msg.estado == 0 else None, "estado": msg.estado,
                                "host": agora}
            if g is not None and g.caminho.usa_vl53:
                g.vl53.append((t, msg.dist_mm, msg.estado))
            if self.fusao is not None and self.fusao.f.c.usa_vl53:
                self.fusao.adicionar(t, "vl53", (msg.dist_mm, msg.estado))
        elif isinstance(msg, p.StatusBancada):
            t = self.desenrolar_banc(msg.t_us)
            self._marcar_offset(agora, t)
            self.bancada.update(versao_fw=msg.versao_fw, vl53_ok=msg.vl53_ok, fila_perdida=msg.fila_perdida)
        elif isinstance(msg, p.Log):
            self.logs.append((time.time(), msg.texto))
            log.info("bancada: %s", msg.texto)
            m = re.search(r"MAC ([0-9A-Fa-f:]{17})", msg.texto)
            if m:
                self.bancada["mac"] = m.group(1).upper()

    def _tratar_caixa(self, msg, agora: float) -> None:
        """Mensagens da caixa: todos os horários já estão no relógio da própria caixa."""
        self.bancada["ultimo_contato"] = agora
        g = self.gravacao
        if isinstance(msg, pc.AmostraImu):
            t = float(msg.t_us)
            self._n_offset += 1
            if self._n_offset % 8 == 0:
                self._marcar_offset(agora, t)
            self.imu_ultimo[msg.joy] = {"t_us": msg.t_us, "acc": msg.acc, "giro": msg.giro, "host": agora}
            # IMU "dormindo": responde no I2C, mas manda só zeros (o firmware 203 já o acorda sozinho)
            if not any(msg.acc) and not any(msg.giro):
                n = self._imu_zeros.get(msg.joy, 0) + 1
                self._imu_zeros[msg.joy] = n
                if n == 25:
                    self.logs.append((time.time(), f"IMU {msg.joy} mandando só zeros (dormindo ou mau contato): confira os fios"))
            else:
                self._imu_zeros[msg.joy] = 0
            self._imu_hosts.setdefault(msg.joy, deque(maxlen=600)).append(agora)
            j = self._joy(msg.joy)
            j.ultimo_contato = agora
            if g is not None and g.joy_id == msg.joy:
                g.imu.append((0, t, *msg.acc, *msg.giro))
                if self.fusao is not None:
                    self.fusao.adicionar(t, "imu", (np.array(msg.acc, float), np.array(msg.giro, float)))
        elif isinstance(msg, pc.BordaIr):
            t = float(msg.t_us)
            self.niveis_lm393[msg.sensor] = msg.nivel
            if g is not None and msg.sensor in g.caminho.sensores:
                g.bordas.append((t, msg.sensor, msg.nivel))
            if self.fusao is not None:
                self.fusao.adicionar(t, "borda", (msg.sensor, msg.nivel))
        elif isinstance(msg, pc.Distancia):
            t = float(msg.t_us)
            self._marcar_offset(agora, t + 10000)       # a caixa marca o meio da medida (10 ms antes)
            self.vl53_ultimo = {"dist_mm": msg.dist_mm if msg.estado == 0 else None, "estado": msg.estado,
                                "host": agora}
            if g is not None and g.caminho.usa_vl53:
                g.vl53.append((t, msg.dist_mm, msg.estado))
            if self.fusao is not None and self.fusao.f.c.usa_vl53:
                self.fusao.adicionar(t, "vl53", (msg.dist_mm, msg.estado))
        elif isinstance(msg, pc.StatusCaixa):
            self._marcar_offset(agora, float(msg.t_us))
            self.caixa_status = msg
            self.bancada.update(versao_fw=msg.versao_fw, vl53_ok=msg.vl53_ok,
                                fila_perdida=msg.descartadas, mac=None)
            self.niveis_lm393 = dict(msg.niveis_ir)
            for jid in pc.JOYSTICKS:
                j = self._joy(jid)
                j.sync.modelo = ModeloRelogio(valido=True, residuo_us=0.0)    # mesmo relógio: nada a sincronizar
                # o carro funciona só com o VL53L0X se o IMU dele faltar
                if msg.imu_ok[jid] or (jid == "C" and msg.vl53_ok):
                    j.ultimo_contato = agora
                j.status = {"imu_ok": msg.imu_ok[jid],
                            "estado": "gravando" if msg.ativo == jid else "pronto"}
        elif isinstance(msg, pc.Mensagem):
            self.logs.append((time.time(), msg.texto))
            log.info("caixa: %s", msg.texto)

    def _ack_nack(self, agora: float, urgente: bool = False) -> None:
        for j in self.joys.values():
            if agora - j.ultimo_contato > TIMEOUT_JOY_S or j.maior_seq == 0:
                continue
            self._escrever(p.quadro_cmd_joy(j.id, p.C_ACK_ATE, j.contiguo))
            espera = 0.05 if urgente else 0.15
            pedir = sorted(s for s, (t0, _) in j.faltando.items() if agora - t0 >= espera)
            desistir = [s for s in pedir if j.faltando[s][1] >= 12]
            for s in desistir:
                del j.faltando[s]
                j.perdidos_definitivos += 1
            pedir = [s for s in pedir if s in j.faltando]
            for ini, fim in _faixas(pedir)[:8]:
                self._escrever(p.quadro_cmd_joy(j.id, p.C_REENVIAR, ini, fim))
                for s in range(ini, fim + 1):
                    j.faltando[s][1] += 1
            if desistir and not j.faltando:
                j.contiguo = j.maior_seq

    # ------------------------------------------------------------ partidas
    def _comando_joy(self, jid: str, iniciar: bool) -> None:
        if self.modo == "caixa":
            # a caixa passa a mandar o IMU desse joystick a 250 Hz (os outros ficam a 10 Hz)
            self._escrever(pc.comando(f"ATIVO {jid if iniciar else '0'}"))
        else:
            self._escrever(p.quadro_cmd_joy(jid, p.C_INICIAR if iniciar else p.C_PARAR))

    def joystick_conectado(self, jid: str) -> bool:
        j = self.joys.get(jid)
        return bool(j and self.relogio() - j.ultimo_contato < TIMEOUT_JOY_S)

    def iniciar_partida(self, partida_id: int, caminho_id: str, tabela: list[dict], calib_vl53: dict | None,
                        escala_giro: float | None = None) -> None:
        caminho = CAMINHOS[caminho_id]
        self._erro_fusao_avisado = False
        with self.trava:
            agora = self.relogio()
            j = self._joy(caminho.joystick)
            g = Gravacao(partida_id, caminho, caminho.joystick, agora, seq_inicio=j.maior_seq + 1,
                         relogio_unico=self.modo == "caixa")
            g.t_inicio_banc = self.agora_bancada(agora)
            g.escala_giro = escala_giro
            self.gravacao = g
            filtro = FiltroCaminho(caminho, tabela, calib_vl53, self.cfg_fusao, escala_giro=escala_giro)
            # caixa: tudo chega pela USB em poucos ms (um relógio só); sem fio, a IMU chega ~25 ms atrasada
            self.fusao = FusaoTempoReal(filtro, atraso_us=10_000 if self.modo == "caixa" else 25_000)
            self._comando_joy(caminho.joystick, iniciar=True)

    def finalizar_partida(self, espera_max_s: float = 1.5, dormir: Callable[[float], None] = time.sleep) -> Gravacao | None:
        with self.trava:
            g = self.gravacao
            if g is None:
                return None
            self._comando_joy(g.joy_id, iniciar=False)
            g.t_fim_banc = self.agora_bancada()
            j = self.joys.get(g.joy_id)
        # espera os reenvios dos pacotes que faltam desta partida
        fim = self.relogio() + espera_max_s
        while self.relogio() < fim:
            with self.trava:
                faltam = [s for s in j.faltando if s >= g.seq_inicio] if j else []
                if not faltam:
                    break
                self._ack_nack(self.relogio(), urgente=True)
            dormir(0.1)
        with self.trava:
            self.gravacao = None
            self.fusao = None
        return g

    def cancelar_partida(self) -> None:
        with self.trava:
            if self.gravacao:
                self._comando_joy(self.gravacao.joy_id, iniciar=False)
            self.gravacao = None
            self.fusao = None

    # ------------------------------------------------------------ leituras
    def estado_publico(self) -> dict:
        with self.trava:
            agora = self.relogio()
            self.bancada["conectada"] = agora - self.bancada["ultimo_contato"] < TIMEOUT_BANCADA_S
            joys = {}
            for jid, j in sorted(self.joys.items()):
                con = agora - j.ultimo_contato < TIMEOUT_JOY_S
                st = j.status or {}
                joys[jid] = {"conectado": con, "bateria_mv": st.get("bateria_mv"),
                             "bateria_pct": _bateria_pct(st.get("bateria_mv")), "carregando": st.get("carregando"),
                             "estado": st.get("estado"), "rssi": j.rssi,
                             "sincronia": j.sync.modelo.resumo(), "pacotes_faltando": len(j.faltando),
                             "perdidos": j.perdidos_definitivos}
            vl = self.vl53_ultimo
            vl_ativo = bool(vl and agora - vl["host"] < 2.0)
            imus = {}
            if self.modo == "caixa":
                st = self.caixa_status
                for jid in pc.JOYSTICKS:
                    u = self.imu_ultimo.get(jid)
                    hosts = self._imu_hosts.get(jid, ())
                    taxa = sum(1 for h in hosts if agora - h <= 1.0)
                    imus[jid] = {"ok": bool(st and st.imu_ok[jid]) and self._imu_zeros.get(jid, 0) < 25, "taxa_hz": taxa,
                                 "so_zeros": self._imu_zeros.get(jid, 0) >= 25,
                                 "acc_g": [round(x / p.ACC_LSB_POR_G, 3) for x in u["acc"]] if u and agora - u["host"] < 1 else None,
                                 "giro_graus_s": [round(x / p.GIRO_LSB_POR_GRAU_S, 1) for x in u["giro"]] if u and agora - u["host"] < 1 else None}
            e = {"bancada": {k: v for k, v in self.bancada.items() if k != "ultimo_contato"},
                 "hardware": self.modo, "imu": imus,
                 "mensagens": [txt for _, txt in list(self.logs)[-8:]],
                 "joysticks": joys,
                 "vl53": {"ativo": vl_ativo, "dist_mm": vl["dist_mm"] if vl_ativo else None},
                 "lm393": self.niveis_lm393,
                 "partida": None}
            if self.gravacao is not None:
                g = self.gravacao
                e["partida"] = {"id": g.partida_id, "caminho": g.caminho.id, "joystick": g.joy_id}
                if self.fusao is not None and self._offset_min is not None:
                    est = self.fusao.estado(self.agora_bancada(agora))
                    est["t_bancada_us"] = round(self.agora_bancada(agora))
                    est["fora_do_alcance"] = bool(g.caminho.usa_vl53 and (not vl_ativo or vl["estado"] != 0))
                    e["partida"].update(est)
            return e


def _faixas(seqs: list[int]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s in seqs:
        if out and s == out[-1][1] + 1:
            out[-1][1] = s
        else:
            out.append([s, s])
    return [(a, b) for a, b in out]


def _bateria_pct(mv) -> int | None:
    if not mv:
        return None
    return int(np.clip((mv - 3300) / (4150 - 3300) * 100, 0, 100))
