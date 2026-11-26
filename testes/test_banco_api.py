"""Banco de dados e API com o simulador em tempo real."""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient


_PORTA = {}


def cliente_porta():
    return _PORTA["ps"]


@pytest.fixture(scope="module")
def cliente(tmp_path_factory):
    import os
    pasta = tmp_path_factory.mktemp("dados")
    os.environ["TRILHO_DADOS"] = str(pasta)
    from trilho import banco, config, cripto
    from trilho.aquisicao import Aquisicao
    from trilho.api import criar_app
    from trilho.simulador import PortaSimulada
    p = config.pasta_dados()
    cripto.iniciar(p)
    banco.iniciar(p / "trilho.db")
    from trilho.portas import GerenciadorPortas
    ps = PortaSimulada()
    _PORTA["ps"] = ps
    portas = GerenciadorPortas(None, simulada=ps)
    aq = Aquisicao(portas.abrir)
    aq.ao_fechar_porta = portas.fechou
    aq.iniciar()
    with TestClient(criar_app(aq, p, ps, portas)) as c:
        r = c.post("/api/auth/primeiro-acesso", json={"nome": "Admin Teste", "email": "admin@clinica.br",
                                                       "senha": "Senha12345"})
        assert r.status_code == 200, r.text
        t0 = time.time()
        while time.time() - t0 < 10 and not c.get("/api/estado").json()["joysticks"].get("C", {}).get("sincronia", {}).get("valido"):
            time.sleep(0.2)
        yield c, p
    aq.parar()
    ps.close()


def test_consentimento_obrigatorio(cliente):
    c, _ = cliente
    r = c.post("/api/pacientes", json={"nome": "Ana", "lado": "esquerdo"})
    assert r.status_code == 400


def test_fluxo_completo(cliente):
    c, pasta = cliente
    p = c.post("/api/pacientes", json={"nome": "Ana Teste", "lado": "esquerdo", "consentimento": True}).json()
    assert c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "carro", "fonte": "sensores"}).status_code == 409
    assert c.post("/api/calibracoes/reto", json={"tras": 50, "frente": 170}).status_code == 200
    st = c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "carro", "fonte": "sensores"}).json()
    time.sleep(6)
    e = c.get("/api/estado").json()
    assert e["partida"]["id"] == st["partida_id"] and 0 <= e["partida"]["f"] <= 1
    fim = c.post(f"/api/partidas/{st['partida_id']}/finalizar",
                 json={"pontos": 80, "estrelas": 2, "metricas_jogo": {"x": 1}, "motivo": "concluído"}).json()
    assert fim["salva"] and "movimento" not in fim          # o paciente não vê métricas clínicas
    h = c.get(f"/api/pacientes/{p['id']}/historico?jogo=carro").json()
    assert len(h) == 1 and h[0]["pontos"] == 80 and h[0]["fonte"] == "Sensores"
    assert h[0]["metricas"]["velocidade_pico_mm_s"] > 0
    assert not any("forca" in k or "carga" in k for k in h[0]["metricas"])   # célula de carga retirada
    guia = c.get("/api/metricas/guia").json()["metricas"]
    assert set(h[0]["metricas"]) <= set(guia)                # toda métrica calculada tem explicação
    assert c.get("/api/pacientes").json()[0]["n_partidas"] >= 1
    csv = c.get("/api/exportar.csv").text
    assert "Ana Teste" in csv and "Pilotar o carro" in csv
    cab = csv.lstrip("\ufeff").splitlines()[0].split(";")
    assert "Suavidade (SPARC)" in cab and not any("carga" in x.lower() or "Força de preensão m" in x for x in cab)

    db = sqlite3.connect(pasta / "trilho.db")
    nome_cifrado = db.execute("select nome from identificacao_paciente").fetchone()[0]
    assert "Ana" not in nome_cifrado
    tipos = {r[0] for r in db.execute("select tipo from dado_bruto")}
    assert {"imu", "sync", "vl53", "series"} <= tipos and "forca" not in tipos
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("delete from auditoria")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("update auditoria set acao = 'x'")


def test_partida_nao_finalizada_e_descartada(cliente):
    c, pasta = cliente
    pid = c.get("/api/pacientes").json()[0]["id"]
    a = c.post("/api/partidas", json={"paciente_id": pid, "jogo": "vidro", "fonte": "mouse"}).json()
    b = c.post("/api/partidas", json={"paciente_id": pid, "jogo": "vidro", "fonte": "mouse"}).json()
    db = sqlite3.connect(pasta / "trilho.db")
    assert db.execute("select status from partida where id=?", (a["partida_id"],)).fetchone()[0] == "descartada"
    c.post(f"/api/partidas/{b['partida_id']}/finalizar", json={"pontos": 1, "estrelas": 1})


def test_tabela_de_transicoes_versionada(cliente):
    c, _ = cliente
    atual = c.get("/api/calibracoes/transicoes/espiral").json()
    linhas = atual["dados"]["linhas"]
    linhas[0]["posicao_mm"] = 205.0
    nova = c.put("/api/calibracoes/transicoes/espiral", json={"linhas": linhas, "referencia": "teste"}).json()
    assert nova["versao"] == atual["versao"] + 1
    assert c.put("/api/calibracoes/transicoes/espiral", json={"linhas": [{"x": 1}]}).status_code == 400


def test_botao_conectar_esp32(cliente):
    """O site lista as portas, desconecta (libera para a Arduino IDE) e conecta de novo."""
    c, _ = cliente
    r = c.get("/api/hardware/portas").json()
    assert r["portas"][0]["porta"] == "SIMULADOR" and r["bancada_respondendo"]
    assert c.post("/api/hardware/desconectar").json()["estado"]["ligada"] is False
    time.sleep(0.5)
    e = c.get("/api/estado").json()
    assert not e["bancada"]["conectada"] and e["porta"]["aberta"] is None
    r = c.post("/api/hardware/conectar", json={"porta": "SIMULADOR"}).json()
    assert r["bancada_respondendo"] and r["estado"]["aberta"] == "SIMULADOR" and r["aviso"] is None
    t0 = time.time()      # espera os joysticks voltarem a sincronizar antes dos próximos testes
    while time.time() - t0 < 10 and not c.get("/api/estado").json()["joysticks"].get("C", {}).get("sincronia", {}).get("valido"):
        time.sleep(0.2)
    # não troca de porta no meio de uma partida com sensores
    p = c.post("/api/pacientes", json={"nome": "Caio Porta", "lado": "direito", "consentimento": True}).json()
    c.post("/api/calibracoes/reto", json={"tras": 50, "frente": 170})
    st = c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "vidro", "fonte": "sensores"})
    assert st.status_code == 200, st.text
    assert c.post("/api/hardware/desconectar").status_code == 409
    c.post(f"/api/partidas/{st.json()['partida_id']}/cancelar")


def test_portas_mensagens_de_erro(tmp_path):
    from trilho import portas as m
    assert "Monitor Serial" in m._descrever_erro(OSError("could not open port 'COM5': PermissionError(13, 'Access is denied.')"))
    g = m.GerenciadorPortas(tmp_path)
    g.escolher("COM7")
    assert m.GerenciadorPortas(tmp_path).estado()["modo"] == "COM7"      # escolha salva para a próxima vez
    g.desligar()
    assert g.abrir() is None and m.GerenciadorPortas(tmp_path).estado()["ligada"] is False
    assert m.GerenciadorPortas(tmp_path, porta_fixa="COM3").estado()["modo"] == "COM3"


def test_diagnostico_placa_com_programa_antigo():
    """Placa respondendo com o programa do HTML antigo (texto) é reconhecida e explicada."""
    import os
    import pty
    import threading
    from trilho import portas as m
    from trilho.aquisicao import Aquisicao
    mestre, escravo = pty.openpty()
    nome = os.ttyname(escravo)
    parar = threading.Event()

    def placa_antiga():
        while not parar.is_set():
            os.write(mestre, b"P,0,1,3,11\r\nC,0,123\r\nM,0,1,2,40,0.1,0.3,2\r\n")
            parar.wait(0.05)
    th = threading.Thread(target=placa_antiga, daemon=True)
    th.start()
    try:
        g = m.GerenciadorPortas(None, porta_fixa=nome)
        aq = Aquisicao(g.abrir)
        aq.ao_fechar_porta = g.fechou
        aq.iniciar()
        t0 = time.time()
        while time.time() - t0 < 5 and aq.diagnostico_porta()["bytes"] < 100:
            time.sleep(0.05)
        d = aq.diagnostico_porta()
        assert d["bytes"] >= 100 and d["quadros"] == 0 and not aq.estado_publico()["bancada"]["conectada"]
        msg = m.explicar_sem_resposta(nome, d["amostra"], None)
        assert "programa antigo" in msg and "caixa.ino" in msg
        aq.parar()
    finally:
        parar.set()
        os.close(mestre)


def test_mensagens_de_diagnostico():
    from trilho import portas as m
    assert "RESET" in m.explicar_sem_resposta("COM4", b"", b"")
    assert "programa antigo" in m.explicar_sem_resposta("COM4", bytes([0x80, 0xF8, 0x00] * 50), b"P,0,1,3,11\nC,0,98\n")
    assert "outro programa" in m.explicar_sem_resposta("COM4", b"Hello world\nHello world\n", None)
    assert "formato do Trilho" in m.explicar_sem_resposta("COM4", bytes([0x80, 0xF8, 0x00] * 50), bytes([0xFF, 0x00] * 9))


def test_celula_de_carga_retirada(cliente):
    """A célula de carga saiu do projeto: nada dela na API nem nas calibrações."""
    c, _ = cliente
    p = c.post("/api/pacientes", json={"nome": "Sem Carga", "lado": "direito", "consentimento": True}).json()
    assert "carga_base" not in p
    assert c.get(f"/api/pacientes/{p['id']}/cargas-base").status_code == 404
    assert c.post("/api/calibracoes/celula", json={"etapa": "zero"}).status_code in (404, 405)
    assert "celula" not in c.get("/api/calibracoes").json()
    assert "forca_n" not in c.get("/api/estado").json()
