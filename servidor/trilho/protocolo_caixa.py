"""Protocolo de texto da caixa (uma ESP32 com todos os sensores ligados por fio).

Cada linha: TIPO,campo,campo,...*XX  — XX = "ou exclusivo" (XOR) de todos os
caracteres antes do '*', em hexadecimal. Linhas sem verificação válida são
descartadas; linhas que começam com '#' são só para pessoas (Monitor Serial).
Detalhes em docs/PROTOCOLO_CAIXA.md e no cabeçalho de firmware/caixa/caixa.ino.

Todos os horários vêm do mesmo relógio (microssegundos desde que a placa ligou,
64 bits): não há sincronia de relógios a fazer.
"""
from __future__ import annotations

from dataclasses import dataclass

JOYSTICKS = ("A", "B", "C")          # A janela (guarda-chuva), B espiral (bolo), C carro (reto)
MAX_LINHA = 200


@dataclass
class StatusCaixa:
    t_us: int
    versao_fw: int
    vl53_ok: bool
    imu_ok: dict          # "A"/"B"/"C" -> bool
    ativo: str            # "A", "B", "C" ou "0"
    niveis_ir: dict       # sensor -> 0/1
    descartadas: int


@dataclass
class AmostraImu:
    t_us: int
    joy: str
    acc: tuple            # bruto (8192 por g)
    giro: tuple           # bruto (65,5 por °/s)


@dataclass
class BordaIr:
    t_us: int
    sensor: int
    nivel: int


@dataclass
class Distancia:
    t_us: int
    dist_mm: int
    estado: int


@dataclass
class Mensagem:
    texto: str


def checksum(corpo: str) -> int:
    ck = 0
    for ch in corpo.encode("ascii", "replace"):
        ck ^= ch
    return ck


def montar_linha(corpo: str) -> bytes:
    """Para testes e para o simulador: a mesma linha que a caixa manda."""
    return f"{corpo}*{checksum(corpo):02X}\n".encode("ascii")


def decodificar_linha(linha: str):
    """Devolve a mensagem ou None (linha humana, incompleta ou com erro)."""
    linha = linha.strip()
    if not linha or linha.startswith("#") or "*" not in linha:
        return None
    corpo, _, ck = linha.rpartition("*")
    try:
        if int(ck[:2], 16) != checksum(corpo):
            return None
    except ValueError:
        return None
    tipo, _, resto = corpo.partition(",")
    try:
        if tipo == "L":
            return Mensagem(resto)
        c = resto.split(",")
        if tipo == "I" and len(c) == 8 and c[1] in JOYSTICKS:
            v = [int(x) for x in c[2:]]
            return AmostraImu(int(c[0]), c[1], tuple(v[:3]), tuple(v[3:]))
        if tipo == "B" and len(c) == 3:
            return BordaIr(int(c[0]), int(c[1]), int(c[2]))
        if tipo == "D" and len(c) == 3:
            return Distancia(int(c[0]), int(c[1]), int(c[2]))
        if tipo == "S" and len(c) in (9, 10):
            if len(c) == 10:
                del c[3]          # firmware até o 206: campo da célula de carga (retirada do projeto)
            mask = int(c[7])
            return StatusCaixa(int(c[0]), int(c[1]), c[2] == "1",
                               {"A": c[3] == "1", "B": c[4] == "1", "C": c[5] == "1"}, c[6],
                               {i: (mask >> i) & 1 for i in range(5)}, int(c[8]))
    except ValueError:
        return None
    return None


class LeitorLinhas:
    """Junta os bytes da USB em linhas e devolve as mensagens válidas."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.validas = 0
        self.invalidas = 0

    def alimentar(self, dados: bytes) -> list:
        self._buf.extend(dados)
        out = []
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                if len(self._buf) > MAX_LINHA:          # lixo sem fim de linha (ex.: outro protocolo)
                    self._buf.clear()
                break
            bruto = bytes(self._buf[:i])
            del self._buf[:i + 1]
            if len(bruto) > MAX_LINHA:
                self.invalidas += 1
                continue
            texto = bruto.decode("ascii", "replace")
            msg = decodificar_linha(texto)
            if msg is not None:
                self.validas += 1
                out.append(msg)
            elif texto.strip() and not texto.lstrip().startswith("#"):
                self.invalidas += 1
        return out


def comando(texto: str) -> bytes:
    return (texto.strip() + "\n").encode("ascii")
