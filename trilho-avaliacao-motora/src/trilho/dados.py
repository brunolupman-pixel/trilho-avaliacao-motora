"""Dados brutos de uma partida e o arquivo de sessão.

Cada partida é guardada como um conjunto de arquivos Parquet (um por fluxo)
mais um ``manifesto.json`` com metadados, versão do formato e o checksum
SHA-256 de cada arquivo. Os dados brutos são sempre guardados, para que as
sessões possam ser reprocessadas quando o algoritmo melhorar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import VERSAO_FORMATO
from . import protocolo as P

COLUNAS = {
    "imu": ["seq", "t_joy_us", "ax", "ay", "az", "gx", "gy", "gz"],
    "carga": ["seq", "t_joy_us", "bruto"],
    "ir": ["seq", "t_banc_us", "sensor", "estado"],
    "tof": ["seq", "t_banc_us", "distancia_mm", "status"],
    "sinc": ["t1_us", "t2_us", "t3_us", "t4_us"],
    "eventos": ["t_banc_us", "tipo", "valor"],
}


def _vazio(nome: str) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="int64") for c in COLUNAS[nome]})


@dataclass
class DadosBrutos:
    """Tudo o que chega da bancada numa partida, sem processamento.

    ``meta`` leva: versao_formato, caminho, joystick_id, bancada_id,
    versao_firmware, taxas nominais, calibração vigente (IMU, carga, ToF) e o
    id da tabela de transições.
    """

    meta: dict
    imu: pd.DataFrame
    ir: pd.DataFrame
    sinc: pd.DataFrame
    carga: pd.DataFrame = field(default_factory=lambda: _vazio("carga"))
    tof: pd.DataFrame = field(default_factory=lambda: _vazio("tof"))
    eventos: pd.DataFrame = field(default_factory=lambda: _vazio("eventos"))

    def fluxos(self) -> dict[str, pd.DataFrame]:
        return {"imu": self.imu, "carga": self.carga, "ir": self.ir,
                "tof": self.tof, "sinc": self.sinc, "eventos": self.eventos}

    # ---------------------------------------------------------- arquivo Parquet
    def salvar(self, pasta: str | Path) -> str:
        """Salva a partida e devolve o checksum do manifesto."""
        pasta = Path(pasta)
        pasta.mkdir(parents=True, exist_ok=True)
        arquivos = {}
        for nome, df in self.fluxos().items():
            caminho = pasta / f"{nome}.parquet"
            df.to_parquet(caminho, index=False)
            arquivos[nome] = hashlib.sha256(caminho.read_bytes()).hexdigest()
        manifesto = {"versao_formato": VERSAO_FORMATO, "meta": self.meta, "arquivos": arquivos}
        texto = json.dumps(manifesto, indent=2, ensure_ascii=False, sort_keys=True)
        (pasta / "manifesto.json").write_text(texto, encoding="utf-8")
        return hashlib.sha256(texto.encode()).hexdigest()

    @classmethod
    def carregar(cls, pasta: str | Path, verificar: bool = True) -> "DadosBrutos":
        pasta = Path(pasta)
        manifesto = json.loads((pasta / "manifesto.json").read_text(encoding="utf-8"))
        if manifesto["versao_formato"] > VERSAO_FORMATO:
            raise ValueError("arquivo gravado por uma versão mais nova do formato")
        fluxos = {}
        for nome, sha in manifesto["arquivos"].items():
            caminho = pasta / f"{nome}.parquet"
            if verificar and hashlib.sha256(caminho.read_bytes()).hexdigest() != sha:
                raise ValueError(f"checksum inválido em {caminho}")
            fluxos[nome] = pd.read_parquet(caminho)
        return cls(meta=manifesto["meta"], **fluxos)

    # -------------------------------------------------- conversão para quadros
    def para_quadros(self) -> bytes:
        """Serializa a partida como o fluxo de quadros que a bancada envia."""
        saida = bytearray()
        seq = 0

        def emitir(tipo: P.Tipo, payload: bytes) -> None:
            nonlocal seq
            saida.extend(P.montar_quadro(tipo, seq, payload))
            seq += 1

        imu = self.imu[COLUNAS["imu"]].to_numpy(dtype=np.int64)
        n = P.AMOSTRAS_IMU_POR_QUADRO
        for i in range(0, len(imu), n):
            emitir(P.Tipo.IMU, b"".join(P.AMOSTRA_IMU.pack(*map(int, r)) for r in imu[i:i + n]))
        for nome, tipo, est in (("carga", P.Tipo.CARGA, P.CARGA), ("ir", P.Tipo.IR, P.IR),
                                ("tof", P.Tipo.TOF, P.TOF)):
            for r in getattr(self, nome)[COLUNAS[nome]].itertuples(index=False):
                emitir(tipo, est.pack(*map(int, r)))
        for r in self.sinc.itertuples(index=False):
            emitir(P.Tipo.SINC, P.SINC.pack(*map(int, r)))
        emitir(P.Tipo.FIM_PARTIDA, len(self.imu).to_bytes(4, "little"))
        return bytes(saida)

    @classmethod
    def de_quadros(cls, quadros, meta: dict) -> "DadosBrutos":
        """Remonta a partida a partir dos quadros recebidos pela USB."""
        acc: dict[str, list] = {k: [] for k in ("imu", "carga", "ir", "tof", "sinc")}
        for q in quadros:
            if q.tipo == P.Tipo.IMU:
                acc["imu"].extend(q.registros())
            elif q.tipo == P.Tipo.CARGA:
                acc["carga"].extend(q.registros())
            elif q.tipo == P.Tipo.IR:
                acc["ir"].extend(q.registros())
            elif q.tipo == P.Tipo.TOF:
                acc["tof"].extend(q.registros())
            elif q.tipo == P.Tipo.SINC:
                acc["sinc"].extend(q.registros())

        def df(nome: str, cols: list[str]) -> pd.DataFrame:
            if not acc[nome]:
                return _vazio(nome)
            return pd.DataFrame(acc[nome], columns=cols).astype("int64")

        return cls(meta=meta, **{n: df(n, COLUNAS[n]) for n in acc})
