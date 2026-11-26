"""Protocolo binário antigo (bancada sem fio, firmware/legado_sem_fio): Python e C++ geram os mesmos bytes."""
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from trilho import protocolo as p

CABECALHO = Path(__file__).resolve().parent.parent / "firmware" / "legado_sem_fio" / "libraries" / "TrilhoProtocolo" / "src"


def test_cobs_crc_ida_e_volta():
    rnd = random.Random(1)
    leitor = p.LeitorQuadros()
    fluxo, esperados = b"", []
    for _ in range(300):
        dados = bytes(rnd.choice([0, 0, 1, 255, rnd.randrange(256)]) for _ in range(rnd.randrange(0, 280)))
        tipo = rnd.choice([p.Q_BORDA, p.Q_VL53, p.Q_PACOTE_JOY])
        fluxo += p.montar_quadro(tipo, dados)
        esperados.append((tipo, dados))
    recebidos = []
    for i in range(0, len(fluxo), 37):          # chega em pedaços, como na serial
        recebidos += leitor.alimentar(fluxo[i:i + 37])
    assert recebidos == esperados and leitor.invalidos == 0


def test_quadro_corrompido_e_descartado():
    q = bytearray(p.montar_quadro(p.Q_BORDA, p.BORDA.pack(123, 2, 1, 0)))
    q[3] ^= 0x40
    leitor = p.LeitorQuadros()
    assert leitor.alimentar(bytes(q)) == [] and leitor.invalidos == 1
    # o quadro seguinte continua válido
    assert len(leitor.alimentar(p.montar_quadro(p.Q_VL53, p.VL53.pack(1, 2, 0, 0)))) == 1


def test_desenrolador_passa_a_volta_dos_32_bits():
    d = p.Desenrolador()
    assert d(0xFFFFFF00) == 0xFFFFFF00
    assert d(0x00000100) == 0x100000100
    assert d(0xFFFFFFF0) == 0xFFFFFFF0       # um pouco fora de ordem, antes da volta


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ indisponível")
def test_cpp_e_python_geram_os_mesmos_quadros(tmp_path):
    prog = tmp_path / "t.cpp"
    prog.write_text(r'''
#include <TrilhoProtocolo.h>
#include <cstdio>
using namespace trilho;
int main() {
  printf("%zu %zu %zu %zu %zu\n", sizeof(JoyCab), sizeof(AmostraImu), sizeof(StatusJoy), sizeof(QuadroCmdJoy), sizeof(QuadroStatusBancada));
  uint8_t dados[260]; uint8_t saida[MAX_QUADRO + 8];
  for (int n = 0; n < 260; n += 13) {
    for (int i = 0; i < n; i++) dados[i] = (uint8_t)((i * 37 + n) % 7 == 0 ? 0 : (i * 91 + n) & 0xFF);
    size_t m = montar_quadro(Q_PACOTE_JOY, dados, n, saida);
    for (size_t i = 0; i < m; i++) printf("%02x", saida[i]);
    printf("\n");
  }
  // o leitor C++ aceita um quadro montado pelo Python? (lido da entrada padrão)
  LeitorQuadros l; int c, ok = 0;
  while ((c = getchar()) != EOF) if (l.alimentar((uint8_t)c)) ok++;
  printf("%d\n", ok);
}''')
    exe = tmp_path / "t"
    subprocess.run(["g++", "-std=gnu++17", "-I", str(CABECALHO), str(prog), "-o", str(exe)], check=True)
    entrada = b"".join(p.quadro_cmd_joy("C", p.C_REENVIAR, 10, 20) for _ in range(5))
    saida = subprocess.run([str(exe)], input=entrada, capture_output=True, check=True).stdout.decode().split()
    assert [int(x) for x in saida[:5]] == [p.CAB.size, p.IMU.size, p.STATUS_JOY.size, p.CMD_JOY.size, p.STATUS_BANC.size]
    for k, n in enumerate(range(0, 260, 13)):
        dados = bytes(0 if (i * 37 + n) % 7 == 0 else (i * 91 + n) & 0xFF for i in range(n))
        assert saida[5 + k] == p.montar_quadro(p.Q_PACOTE_JOY, dados).hex()
    assert saida[-1] == "5"
