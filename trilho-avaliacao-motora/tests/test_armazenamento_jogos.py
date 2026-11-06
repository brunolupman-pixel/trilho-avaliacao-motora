import os
import sqlite3

import pytest

from trilho.armazenamento import Banco, ContextoApp
from trilho.geometria import obter_caminho
from trilho.pipeline import processar_e_salvar
from trilho.relatorio import gerar_relatorio
from trilho.simulador import PerfilPaciente, simular_partida


@pytest.fixture()
def banco(tmp_path):
    b = Banco(tmp_path / "dados")
    yield b
    b.fechar()


def test_identificacao_criptografada_e_pseudonimizada(banco):
    ctx = ContextoApp(banco.operador_local())
    pid = banco.cadastrar_paciente(ctx, "Maria Teste", "1980-02-03", documento="123")
    blob = banco.con.execute("SELECT dados_cripto FROM identificacao_paciente").fetchone()[0]
    assert b"Maria" not in blob
    assert banco.paciente(ctx, pid, identificar=True)["nome"] == "Maria Teste"
    assert "nome" not in banco.paciente(ctx, pid)
    # nenhuma outra tabela guarda o nome
    for (tabela,) in banco.con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if tabela == "identificacao_paciente":
            continue
        for row in banco.con.execute(f"SELECT * FROM {tabela}"):
            assert "Maria" not in str(tuple(row))


def test_auditoria_imutavel(banco):
    ctx = ContextoApp(banco.operador_local())
    banco.cadastrar_paciente(ctx, "X")
    assert banco.auditoria()
    with pytest.raises(sqlite3.DatabaseError, match="imutável"):
        banco.con.execute("UPDATE auditoria SET acao = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="imutável"):
        banco.con.execute("DELETE FROM auditoria")


def test_fluxo_sessao_reprocessamento_e_relatorio(banco, tmp_path):
    ctx = ContextoApp(banco.operador_local())
    pid = banco.cadastrar_paciente(ctx, "Paciente")
    banco.registrar_dispositivo("JOY-C-001", "joystick C", ["MPU6050", "HX711"], "sim-1.0")
    cam = obter_caminho("C")
    for k in range(2):
        sid = banco.nova_sessao(ctx, pid, "JOY-C-001", "C", "carro",
                                data=f"2026-10-0{k + 1}T10:00:00+00:00")
        r = simular_partida(cam, 3, PerfilPaciente(forca_base_N=30 - 5 * k), semente=10 + k)
        part = banco.salvar_partida(ctx, sid, 1, r.dados)
        processar_e_salvar(banco, ctx, part, None)
    # reprocessar a partir do bruto guarda um novo resultado, sem perder o antigo
    processar_e_salvar(banco, ctx, part, None)
    n = banco.con.execute("SELECT COUNT(*) FROM resultado WHERE partida_id = ?", (part,)).fetchone()[0]
    assert n == 2
    res = banco.resultados_da_sessao(sid)
    assert len(res) == 1 and res[0]["metricas"][0]["forca_media_N"] > 0
    pdf = gerar_relatorio(banco, ctx, sid, tmp_path / "r.pdf")
    assert pdf.read_bytes()[:4] == b"%PDF"


@pytest.mark.parametrize("nome,cid", [("carro", "C"), ("vidro", "A"), ("bolo", "B")])
def test_jogos_rodam_com_fonte_simulada(nome, cid):
    pytest.importorskip("pygame")
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    from trilho.jogos.base import Dificuldade
    from trilho.jogos.executor import JOGOS, FonteSimulada, executar
    cam = obter_caminho(cid)
    r = simular_partida(cam, 2 if cid != "B" else 1, PerfilPaciente(velocidade_media_mm_s=120),
                        semente=1)
    fonte = FonteSimulada(r.dados, None)
    res = executar(JOGOS[nome](Dificuldade(duracao_s=8)), fonte, tamanho=(640, 360),
                   max_quadros=600, tempo_real=False)
    assert res["eventos"][0]["tipo"] == "inicio" and res["eventos"][-1]["tipo"] == "fim"
    assert isinstance(res["metricas"], dict)
