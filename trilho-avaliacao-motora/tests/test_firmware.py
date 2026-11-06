"""Compila os testes da lógica do firmware (C++17) e confere o formato com o Python."""

import shutil
import subprocess
from pathlib import Path

import pytest

from trilho import protocolo as P

RAIZ = Path(__file__).resolve().parents[1] / "firmware"


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ não disponível")
def test_firmware_cpp_e_formato_cruzado(tmp_path):
    exe = tmp_path / "testes"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{RAIZ / 'comum'}",
                    str(RAIZ / "testes" / "testes.cpp"), "-o", str(exe)], check=True)
    arq = tmp_path / "quadros.bin"
    r = subprocess.run([str(exe), str(arq)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    quadros = list(P.Receptor().alimentar(arq.read_bytes()))
    assert [q.tipo for q in quadros] == [P.Tipo.IMU, P.Tipo.IR, P.Tipo.SINC]
    imu = quadros[0].registros()
    assert len(imu) == P.AMOSTRAS_IMU_POR_QUADRO
    assert imu[1] == (1, 1_002_000, 101, -200, 16384, 1, -2, 3)
    assert quadros[1].registros() == [(0, 2_500_000, 3, 1)]
    assert quadros[2].registros() == [(1, 2, 3, 4)]
