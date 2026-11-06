import random

import pytest

from trilho import protocolo as P
from trilho.dados import DadosBrutos
from trilho.geometria import obter_caminho
from trilho.simulador import simular_partida


def test_crc16_valor_conhecido():
    assert P.crc16_ccitt(b"123456789") == 0x29B1


@pytest.mark.parametrize("dados", [b"", b"\x00", b"\x00\x00", b"\x11\x22\x00\x33",
                                   bytes(range(1, 255)), bytes(range(256)) * 3, b"\xff" * 600])
def test_cobs_ida_e_volta(dados):
    cod = P.cobs_codificar(dados)
    assert 0 not in cod
    assert P.cobs_decodificar(cod) == dados


def test_cobs_aleatorio():
    rng = random.Random(1)
    for _ in range(300):
        d = bytes(rng.choice([0, 0, 1, 7, 255]) for _ in range(rng.randint(0, 700)))
        assert P.cobs_decodificar(P.cobs_codificar(d)) == d


def test_quadro_crc_detecta_corrupcao():
    q = P.montar_quadro(P.Tipo.IR, 7, P.IR.pack(1, 123456, 2, 1))
    lido = P.ler_quadro(q[:-1])
    assert lido.tipo == P.Tipo.IR and lido.seq == 7
    assert lido.registros() == [(1, 123456, 2, 1)]
    corrompido = bytearray(q)
    corrompido[5] ^= 0x10
    with pytest.raises(ValueError):
        P.ler_quadro(bytes(corrompido[:-1]))


def test_quadro_cabe_em_pacote_esp_now():
    payload = b"".join(P.AMOSTRA_IMU.pack(i, 10**9 + i, 1, 2, 3, 4, 5, 6)
                       for i in range(P.AMOSTRAS_IMU_POR_QUADRO))
    corpo = P.CABECALHO.size + len(payload) + 2
    assert corpo <= 250


def test_partida_inteira_pela_serial_com_ruido():
    r = simular_partida(obter_caminho("C"), 2, semente=3)
    fluxo = r.dados.para_quadros()
    # entrega em pedaços de tamanho aleatório, com um byte corrompido no meio
    rx = P.Receptor()
    fluxo = bytearray(fluxo)
    fluxo[len(fluxo) // 2] ^= 0x55
    quadros = []
    rng = random.Random(2)
    i = 0
    while i < len(fluxo):
        n = rng.randint(1, 300)
        quadros += list(rx.alimentar(bytes(fluxo[i:i + n])))
        i += n
    assert rx.quadros_invalidos == 1
    d = DadosBrutos.de_quadros(quadros, r.dados.meta)
    # um quadro perdido no máximo: o resto chega idêntico
    assert abs(len(d.imu) - len(r.dados.imu)) <= P.AMOSTRAS_IMU_POR_QUADRO
    assert d.sinc.equals(r.dados.sinc) or len(d.sinc) == len(r.dados.sinc) - 1


def test_partida_sem_ruido_identica():
    r = simular_partida(obter_caminho("C"), 1, semente=4)
    rx = P.Receptor()
    d = DadosBrutos.de_quadros(rx.alimentar(r.dados.para_quadros()), r.dados.meta)
    for nome in ("imu", "ir", "tof", "carga", "sinc"):
        assert d.fluxos()[nome].reset_index(drop=True).equals(
            r.dados.fluxos()[nome].reset_index(drop=True)), nome


def test_arquivo_sessao_checksum(tmp_path):
    r = simular_partida(obter_caminho("A"), 1, semente=5)
    r.dados.salvar(tmp_path / "p")
    d = DadosBrutos.carregar(tmp_path / "p")
    assert d.imu.equals(r.dados.imu)
    (tmp_path / "p" / "ir.parquet").write_bytes(b"corrompido")
    with pytest.raises(ValueError, match="checksum"):
        DadosBrutos.carregar(tmp_path / "p")
