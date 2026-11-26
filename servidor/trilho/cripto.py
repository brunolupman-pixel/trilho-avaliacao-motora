"""Criptografia dos dados identificáveis do paciente (arquitetura: LGPD).

Nome, data de nascimento, documento e contato ficam cifrados campo a campo
(Fernet: AES-128-CBC + HMAC-SHA256). A chave fica num arquivo na pasta de
dados; no Windows ela é protegida pelo DPAPI (só o usuário do Windows que a
criou consegue abri-la). Em outros sistemas o arquivo fica com permissão 600.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

log = logging.getLogger("trilho.cripto")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(dados: bytes, proteger: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32      # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32    # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(dados, len(dados))
    entrada = _Blob(len(dados), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    saida = _Blob()
    fn = crypt32.CryptProtectData if proteger else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(entrada), None, None, None, None, 0, ctypes.byref(saida))
    if not ok:
        raise OSError("DPAPI falhou")
    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        kernel32.LocalFree(saida.pbData)


class Cofre:
    def __init__(self, pasta: Path) -> None:
        # Arquivo: 1 byte de formato ("D" = protegida pelo DPAPI, "F" = simples) + chave.
        arq = pasta / "chave.bin"
        if arq.exists():
            bruto = arq.read_bytes()
            chave = _dpapi(bruto[1:], False) if bruto[:1] == b"D" else bruto[1:]
        else:
            chave = Fernet.generate_key()
            if sys.platform == "win32":
                arq.write_bytes(b"D" + _dpapi(chave, True))
            else:
                arq.write_bytes(b"F" + chave)
                os.chmod(arq, 0o600)
                log.warning("chave de criptografia guardada sem DPAPI (sistema não é Windows): %s", arq)
        self._f = Fernet(chave)

    def cifrar(self, texto: str | None) -> str | None:
        if texto is None:
            return None
        return self._f.encrypt(texto.encode("utf-8")).decode("ascii")

    def decifrar(self, cifrado: str | None) -> str | None:
        if cifrado is None:
            return None
        return self._f.decrypt(cifrado.encode("ascii")).decode("utf-8")


_cofre: Cofre | None = None


def iniciar(pasta: Path) -> Cofre:
    global _cofre
    _cofre = Cofre(pasta)
    return _cofre


def cofre() -> Cofre:
    if _cofre is None:
        raise RuntimeError("cofre não iniciado")
    return _cofre
