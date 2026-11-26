"""Protocolo entre bancada e computador — espelho de TrilhoProtocolo.h.

Quadros da serial: COBS([tipo u8][dados][CRC-16/CCITT-FALSE u16 LE]) + 0x00.
Pacotes ESP-NOW dos joysticks chegam embrulhados em quadros Q_PACOTE_JOY.
Tudo little-endian. Qualquer mudança aqui precisa ser feita também no .h
(o teste testes/test_protocolo_cpp.py compara as duas implementações).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

VERSAO_PROTOCOLO = 1

# ---- enlace ESP-NOW (joystick <-> bancada) ----
J_IMU, J_FORCA, J_SYNC, J_STATUS, J_PING, J_PONG, J_CMD = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
TIPOS_DADOS = (J_IMU, J_FORCA, J_SYNC, J_STATUS)  # numerados e guardados até o ACK

C_INICIAR, C_PARAR, C_ACK_ATE, C_REENVIAR = 1, 2, 3, 4
E_OCIOSO, E_PRONTO, E_GRAVANDO, E_CARREGANDO = 0, 1, 2, 3
NOMES_ESTADO_JOY = {0: "ocioso", 1: "pronto", 2: "gravando", 3: "carregando"}

# ---- enlace serial (bancada <-> computador) ----
Q_PACOTE_JOY, Q_BORDA, Q_VL53, Q_STATUS_BANC, Q_LOG = 0x10, 0x11, 0x12, 0x13, 0x14
Q_CMD_JOY, Q_PEDIR_STATUS = 0x20, 0x21

IMU_POR_PACOTE = 7
FORCA_POR_PACOTE = 10

CAB = struct.Struct("<BBBBI")            # JoyCab, 8
IMU = struct.Struct("<Ihhhhhh")          # AmostraImu, 16
FORCA = struct.Struct("<Ii")             # AmostraForca, 8
SYNC = struct.Struct("<IIIIHH")          # RegistroSync, 20
STATUS_JOY = struct.Struct("<IHBBHHHHB3x")  # StatusJoy, 20
PING = struct.Struct("<I")               # Ping, 4
PONG = struct.Struct("<HHIII")           # Pong, 16
CMD = struct.Struct("<B3xII")            # ComandoJoy, 12
CAB_PACOTE = struct.Struct("<Ib3x")      # CabPacoteJoy, 8
BORDA = struct.Struct("<IBBH")           # QuadroBorda, 8
VL53 = struct.Struct("<IHBB")            # QuadroVl53, 8
STATUS_BANC = struct.Struct("<IHHBB2x")  # QuadroStatusBancada, 12
INFO_JOY = struct.Struct("<BbH")         # InfoJoy, 4
CMD_JOY = struct.Struct("<BBHII")        # QuadroCmdJoy, 12

for _s, _n in [(CAB, 8), (IMU, 16), (FORCA, 8), (SYNC, 20), (STATUS_JOY, 20), (PONG, 16), (CMD, 12),
               (CAB_PACOTE, 8), (BORDA, 8), (VL53, 8), (STATUS_BANC, 12), (INFO_JOY, 4), (CMD_JOY, 12)]:
    assert _s.size == _n, (_s.format, _s.size, _n)

# Escalas do MPU6050 / MPU-9250 configurados pelo firmware (±4 g, ±500 °/s; iguais nos dois).
G_MM_S2 = 9806.65
ACC_LSB_POR_G = 8192.0
GIRO_LSB_POR_GRAU_S = 65.5

MAX_QUADRO = 300


# ---------------------------------------------------------------------------
# CRC e COBS
# ---------------------------------------------------------------------------
def crc16(dados: bytes) -> int:
    c = 0xFFFF
    for b in dados:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def cobs_codificar(dados: bytes) -> bytes:
    saida = bytearray([0])
    pos_cod, cod = 0, 1
    for b in dados:
        if b == 0:
            saida[pos_cod] = cod
            pos_cod = len(saida)
            saida.append(0)
            cod = 1
        else:
            saida.append(b)
            cod += 1
            if cod == 0xFF:
                saida[pos_cod] = cod
                pos_cod = len(saida)
                saida.append(0)
                cod = 1
    saida[pos_cod] = cod
    return bytes(saida)


def cobs_decodificar(dados: bytes) -> bytes | None:
    saida = bytearray()
    i, n = 0, len(dados)
    while i < n:
        cod = dados[i]
        if cod == 0 or i + cod > n + 1:
            return None
        i += 1
        for _ in range(cod - 1):
            if i >= n:
                return None
            saida.append(dados[i])
            i += 1
        if cod != 0xFF and i < n:
            saida.append(0)
    return bytes(saida)


def montar_quadro(tipo: int, dados: bytes = b"") -> bytes:
    bruto = bytes([tipo]) + dados
    c = crc16(bruto)
    return cobs_codificar(bruto + bytes([c & 0xFF, c >> 8])) + b"\x00"


class LeitorQuadros:
    """Acumula bytes da serial e devolve (tipo, dados) dos quadros válidos."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.invalidos = 0

    def alimentar(self, dados: bytes) -> list[tuple[int, bytes]]:
        saida = []
        for b in dados:
            if b != 0:
                if len(self._buf) < MAX_QUADRO + 8:
                    self._buf.append(b)
                else:
                    self._buf = bytearray(b"\xff" * (MAX_QUADRO + 9))  # marca como inválido
                continue
            if self._buf:
                dec = None if len(self._buf) > MAX_QUADRO + 8 else cobs_decodificar(bytes(self._buf))
                if dec is not None and len(dec) >= 3 and crc16(dec[:-2]) == (dec[-2] | dec[-1] << 8):
                    saida.append((dec[0], dec[1:-2]))
                else:
                    self.invalidos += 1
            self._buf = bytearray()
        return saida


# ---------------------------------------------------------------------------
# Mensagens decodificadas
# ---------------------------------------------------------------------------
@dataclass
class PacoteJoy:
    t_rx: int            # relógio da bancada (u32)
    rssi: int
    tipo: int
    joy_id: str
    seq: int
    imu: list[tuple] = field(default_factory=list)      # (t_us, ax, ay, az, gx, gy, gz)
    forca: list[tuple] = field(default_factory=list)    # (t_us, bruto)
    sync: tuple | None = None                           # (t1, t2, t3, t4, ping_id)
    status: dict | None = None
    bruto: bytes = b""


@dataclass
class Borda:
    t_us: int
    sensor: int
    nivel: int


@dataclass
class LeituraVl53:
    t_us: int
    dist_mm: int
    estado: int  # 0 válido, 1 fora do alcance, 2 erro


@dataclass
class StatusBancada:
    t_us: int
    versao_fw: int
    fila_perdida: int
    vl53_ok: bool
    joysticks: list[dict]


@dataclass
class Log:
    texto: str


def decodificar_pacote_joy(t_rx: int, rssi: int, p: bytes) -> PacoteJoy | None:
    if len(p) < CAB.size:
        return None
    tipo, versao, joy, n, seq = CAB.unpack_from(p)
    if versao != VERSAO_PROTOCOLO:
        return None
    pac = PacoteJoy(t_rx, rssi, tipo, chr(joy), seq, bruto=bytes(p))
    corpo = p[CAB.size:]
    if tipo == J_IMU:
        if len(corpo) < n * IMU.size:
            return None
        pac.imu = [IMU.unpack_from(corpo, i * IMU.size) for i in range(n)]
    elif tipo == J_FORCA:
        if len(corpo) < n * FORCA.size:
            return None
        pac.forca = [FORCA.unpack_from(corpo, i * FORCA.size) for i in range(n)]
    elif tipo == J_SYNC:
        if len(corpo) < SYNC.size:
            return None
        t1, t2, t3, t4, pid, _ = SYNC.unpack_from(corpo)
        pac.sync = (t1, t2, t3, t4, pid)
    elif tipo == J_STATUS:
        if len(corpo) < STATUS_JOY.size:
            return None
        t, bat, carr, est, ovf, ver, pend, perd, tf = STATUS_JOY.unpack_from(corpo)
        pac.status = {"t_us": t, "bateria_mv": bat, "carregando": bool(carr), "estado": NOMES_ESTADO_JOY.get(est, "?"),
                      "overflow_fifo": ovf, "versao_fw": ver, "pacotes_pendentes": pend,
                      "pacotes_perdidos": perd, "tem_forca": bool(tf)}
    else:
        return None
    return pac


def decodificar_quadro(tipo: int, dados: bytes):
    """Converte um quadro da bancada em objeto; devolve None se desconhecido."""
    if tipo == Q_PACOTE_JOY and len(dados) >= CAB_PACOTE.size:
        t_rx, rssi = CAB_PACOTE.unpack_from(dados)
        return decodificar_pacote_joy(t_rx, rssi, dados[CAB_PACOTE.size:])
    if tipo == Q_BORDA and len(dados) >= BORDA.size:
        t, s, nivel, _ = BORDA.unpack_from(dados)
        return Borda(t, s, nivel)
    if tipo == Q_VL53 and len(dados) >= VL53.size:
        t, mm, est, _ = VL53.unpack_from(dados)
        return LeituraVl53(t, mm, est)
    if tipo == Q_STATUS_BANC and len(dados) >= STATUS_BANC.size:
        t, ver, perd, n, vok = STATUS_BANC.unpack_from(dados)
        joys = []
        for i in range(n):
            off = STATUS_BANC.size + i * INFO_JOY.size
            if off + INFO_JOY.size > len(dados):
                break
            jid, rssi, ms = INFO_JOY.unpack_from(dados, off)
            joys.append({"id": chr(jid), "rssi": rssi, "ms_desde_ultimo": ms})
        return StatusBancada(t, ver, perd, bool(vok), joys)
    if tipo == Q_LOG:
        return Log(dados.decode("utf-8", "replace"))
    return None


# ---------------------------------------------------------------------------
# Montagem (computador -> bancada; e simulador)
# ---------------------------------------------------------------------------
def quadro_cmd_joy(joy_id: str, cmd: int, a: int = 0, b: int = 0) -> bytes:
    return montar_quadro(Q_CMD_JOY, CMD_JOY.pack(ord(joy_id), cmd, 0, a & 0xFFFFFFFF, b & 0xFFFFFFFF))


def pacote_joy(tipo: int, joy_id: str, seq: int, corpo: bytes, n: int = 1) -> bytes:
    return CAB.pack(tipo, VERSAO_PROTOCOLO, ord(joy_id), n, seq & 0xFFFFFFFF) + corpo


def quadro_pacote_joy(t_rx: int, rssi: int, pacote: bytes) -> bytes:
    return montar_quadro(Q_PACOTE_JOY, CAB_PACOTE.pack(t_rx & 0xFFFFFFFF, rssi) + pacote)


class Desenrolador:
    """Converte relógios de 32 bits (dão a volta a cada ~71,6 min) em contínuos."""

    def __init__(self) -> None:
        self.ultimo: int | None = None

    def __call__(self, t32: int) -> int:
        if self.ultimo is None:
            self.ultimo = t32
            return t32
        d = (t32 - (self.ultimo & 0xFFFFFFFF)) & 0xFFFFFFFF
        if d >= 0x80000000:
            d -= 1 << 32
        t = self.ultimo + d
        if d > 0:
            self.ultimo = t
        return t
