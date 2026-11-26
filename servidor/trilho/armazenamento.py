"""Dados brutos em Parquet (arquitetura: "Dado bruto: arquivo Parquet, checksum, versão do formato")."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

VERSAO_FORMATO = 1


def salvar(pasta: Path, partida_id: int, nome: str, dados: np.ndarray, colunas: list[str], meta: dict) -> dict:
    destino = pasta / f"partida_{partida_id:06d}"
    destino.mkdir(parents=True, exist_ok=True)
    arq = destino / f"{nome}.parquet"
    dados = np.asarray(dados, dtype=float).reshape(-1, len(colunas))
    tabela = pa.table({c: dados[:, i] for i, c in enumerate(colunas)})
    meta_b = {b"trilho.versao_formato": str(VERSAO_FORMATO).encode()}
    meta_b.update({f"trilho.{k}".encode(): str(v).encode() for k, v in meta.items()})
    tabela = tabela.replace_schema_metadata(meta_b)
    pq.write_table(tabela, arq, compression="zstd")
    sha = hashlib.sha256(arq.read_bytes()).hexdigest()
    return {"tipo": nome, "arquivo": str(arq), "sha256": sha, "n": int(len(dados))}


def ler(arquivo: str) -> np.ndarray:
    t = pq.read_table(arquivo)
    return np.column_stack([t.column(i).to_numpy() for i in range(t.num_columns)]) if t.num_rows else np.zeros((0, t.num_columns))
