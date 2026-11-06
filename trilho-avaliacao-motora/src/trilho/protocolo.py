"""Protocolo serial bancada ↔ computador.

Quadros binários delimitados por COBS (byte 0x00 separa quadros) e protegidos
por CRC-16/CCITT-FALSE. Cada quadro tem cabeçalho com tipo, versão do formato
e número de sequência; as mesmas estruturas servem para o arquivo de sessão.

    quadro = COBS( tipo:u8 | versao:u8 | seq:u16 | payload | crc16:u16 ) + 0x00

Todos os inteiros são little-endian (nativo do ESP32-S3).
O mesmo formato está em ``firmware/comum/protocolo.hpp``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator

from . import VERSAO_FORMATO


# --------------------------------------------------------------------- CRC/COBS
def crc16_ccitt(dados: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF). crc16(b'123456789') = 0x29B1."""
    for b in dados:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_codificar(dados: bytes) -> bytes:
    saida = bytearray([0])
    idx_codigo, codigo = 0, 1
    for b in dados:
        if b == 0:
            saida[idx_codigo] = codigo
            idx_codigo, codigo = len(saida), 1
            saida.append(0)
        else:
            saida.append(b)
            codigo += 1
            if codigo == 0xFF:
                saida[idx_codigo] = codigo
                idx_codigo, codigo = len(saida), 1
                saida.append(0)
    saida[idx_codigo] = codigo
    return bytes(saida)


def cobs_decodificar(dados: bytes) -> bytes:
    saida = bytearray()
    i, n = 0, len(dados)
    while i < n:
        codigo = dados[i]
        if codigo == 0:
            raise ValueError("byte zero dentro de quadro COBS")
        i += 1
        fim = i + codigo - 1
        if fim > n:
            raise ValueError("quadro COBS truncado")
        saida.extend(dados[i:fim])
        i = fim
        if codigo < 0xFF and i < n:
            saida.append(0)
    return bytes(saida)


# ---------------------------------------------------------------- tipos e payloads
class Tipo(IntEnum):
    IMU = 0x01          # bloco de amostras da IMU (relógio do joystick)
    CARGA = 0x02        # amostra do HX711 (relógio do joystick)
    IR = 0x03           # troca de estado de um LM393 (relógio da bancada)
    TOF = 0x04          # leitura do VL53L0X (relógio da bancada)
    SINC = 0x05         # troca de sincronia t1..t4
    ESTADO = 0x10       # estado da máquina de sessão
    COMANDO = 0x11      # computador -> bancada
    FIM_PARTIDA = 0x12  # fim do arquivo da partida (total de amostras)
    CONFIRMA = 0x13     # computador confirma recebimento da partida


class Comando(IntEnum):
    INICIAR = 1
    PARAR = 2
    TARAR = 3
    CALIBRAR = 4


class EstadoSessao(IntEnum):
    OCIOSO = 0
    SINCRONIZANDO = 1
    PRONTO = 2
    GRAVANDO = 3
    ENVIANDO = 4


AMOSTRA_IMU = struct.Struct("<IQ6h")    # seq, t_us, ax, ay, az, gx, gy, gz (LSB)
CARGA = struct.Struct("<IQi")           # seq, t_us, bruto (contagens do HX711)
IR = struct.Struct("<IQBB")             # seq, t_us, sensor (1..5), estado (1 liga, 0 desliga)
TOF = struct.Struct("<IQHB")            # seq, t_us, distância (mm), status (0 = válido)
SINC = struct.Struct("<QQQQ")           # t1 (joy), t2 (banc), t3 (banc), t4 (joy) em µs
CABECALHO = struct.Struct("<BBH")       # tipo, versão, seq


@dataclass
class Quadro:
    tipo: Tipo
    versao: int
    seq: int
    payload: bytes

    def registros(self) -> list[tuple]:
        """Desempacota o payload em tuplas conforme o tipo."""
        estrutura = {
            Tipo.IMU: AMOSTRA_IMU, Tipo.CARGA: CARGA, Tipo.IR: IR,
            Tipo.TOF: TOF, Tipo.SINC: SINC,
        }.get(self.tipo)
        if estrutura is None:
            return [tuple(self.payload)]
        if len(self.payload) % estrutura.size:
            raise ValueError(f"payload de {self.tipo.name} com tamanho inválido")
        return list(estrutura.iter_unpack(self.payload))


def montar_quadro(tipo: Tipo, seq: int, payload: bytes, versao: int = VERSAO_FORMATO) -> bytes:
    corpo = CABECALHO.pack(int(tipo), versao, seq & 0xFFFF) + payload
    corpo += struct.pack("<H", crc16_ccitt(corpo))
    return cobs_codificar(corpo) + b"\x00"


def ler_quadro(bruto: bytes) -> Quadro:
    corpo = cobs_decodificar(bruto)
    if len(corpo) < CABECALHO.size + 2:
        raise ValueError("quadro curto demais")
    (crc_recebido,) = struct.unpack("<H", corpo[-2:])
    if crc16_ccitt(corpo[:-2]) != crc_recebido:
        raise ValueError("CRC inválido")
    tipo, versao, seq = CABECALHO.unpack_from(corpo)
    return Quadro(Tipo(tipo), versao, seq, corpo[CABECALHO.size:-2])


class Receptor:
    """Remonta quadros a partir de um fluxo de bytes (USB serial).

    Quadros com CRC inválido são descartados e contados; o arquivo da partida
    enviado ao fim (com confirmação e retransmissão) é a fonte oficial.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.quadros_ok = 0
        self.quadros_invalidos = 0

    def alimentar(self, dados: bytes) -> Iterator[Quadro]:
        self._buffer.extend(dados)
        while True:
            fim = self._buffer.find(0)
            if fim < 0:
                return
            bruto = bytes(self._buffer[:fim])
            del self._buffer[: fim + 1]
            if not bruto:
                continue
            try:
                q = ler_quadro(bruto)
            except ValueError:
                self.quadros_invalidos += 1
                continue
            self.quadros_ok += 1
            yield q


# Pacotes ESP-NOW têm até 250 bytes; os blocos de IMU seguem o mesmo limite
AMOSTRAS_IMU_POR_QUADRO = (250 - CABECALHO.size - 2) // AMOSTRA_IMU.size   # = 10
