"""Login, perfis e acesso por vínculo paciente ↔ profissional."""
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app_e_pasta(tmp_path_factory):
    import os
    pasta = tmp_path_factory.mktemp("dados_login")
    os.environ["TRILHO_DADOS"] = str(pasta)
    from trilho import banco, config, cripto
    from trilho.aquisicao import Aquisicao
    from trilho.api import criar_app
    p = config.pasta_dados()
    cripto.iniciar(p)
    banco.iniciar(p / "trilho.db")
    aq = Aquisicao(lambda: None)
    return criar_app(aq, p), p


def cliente(app):
    return TestClient(app)


def entrar(app, email, senha):
    c = cliente(app)
    r = c.post("/api/auth/login", json={"email": email, "senha": senha})
    assert r.status_code == 200, r.text
    return c


def test_tudo_exige_login(app_e_pasta):
    app, _ = app_e_pasta
    c = cliente(app)
    for url in ("/api/pacientes", "/api/estado", "/api/exportar.csv", "/api/usuarios", "/api/calibracoes"):
        assert c.get(url).status_code == 401, url
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/login").status_code == 200
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws") as w:
            w.receive_text()


def test_primeiro_acesso_e_senha_fraca(app_e_pasta):
    app, _ = app_e_pasta
    c = cliente(app)
    assert c.get("/api/auth/estado").json()["primeiro_acesso"] is True
    r = c.post("/api/auth/primeiro-acesso", json={"nome": "Ana Admin", "email": "ana@clinica.br", "senha": "curta"})
    assert r.status_code == 400 and "8 caracteres" in r.json()["detail"]
    r = c.post("/api/auth/primeiro-acesso", json={"nome": "Ana Admin", "email": "Ana@Clinica.br", "senha": "Admin12345"})
    assert r.status_code == 200 and r.json()["perfil"] == "administrador"
    assert c.get("/api/pacientes").status_code == 200       # já entrou
    r = cliente(app).post("/api/auth/primeiro-acesso", json={"nome": "X", "email": "x@x.br", "senha": "Outra12345"})
    assert r.status_code == 400                              # só uma vez


def test_equipe_vinculos_e_perfis(app_e_pasta):
    app, pasta = app_e_pasta
    adm = entrar(app, "ana@clinica.br", "Admin12345")
    for nome, email, perfil in [("Bia Fisio", "bia@clinica.br", "fisioterapeuta"),
                                ("Caio Fisio", "caio@clinica.br", "fisioterapeuta"),
                                ("Davi Tec", "davi@clinica.br", "tecnico")]:
        r = adm.post("/api/usuarios", json={"nome": nome, "email": email, "perfil": perfil, "senha_provisoria": "Prov12345"})
        assert r.status_code == 200, r.text
    assert adm.post("/api/usuarios", json={"nome": "Dup", "email": "bia@clinica.br", "perfil": "tecnico",
                                           "senha_provisoria": "Prov12345"}).status_code == 400

    # senha provisória: precisa trocar antes de usar
    bia = entrar(app, "bia@clinica.br", "Prov12345")
    r = bia.get("/api/pacientes")
    assert r.status_code == 403 and "senha provisória" in r.json()["detail"]
    assert bia.get("/", follow_redirects=False).headers["location"] == "/conta"
    assert bia.post("/api/auth/trocar-senha", json={"atual": "errada1234", "nova": "Bia2025xyz"}).status_code == 400
    assert bia.post("/api/auth/trocar-senha", json={"atual": "Prov12345", "nova": "Bia2025xyz"}).status_code == 200

    # Bia cadastra um paciente com ficha completa e avaliação inicial
    ficha = {"nome": "Paciente Um", "lado": "esquerdo", "consentimento": True, "condicao": "avc_isquemico",
             "condicao_detalhe": "ACM direita", "data_lesao": "2025-03-10", "dominancia": "direita",
             "toxina_botulinica": True, "toxina_ultima_data": "2026-08-01", "toxina_musculos": "FDS, FCR",
             "medicacoes": "baclofeno 10 mg", "objetivos": "abrir a mão", "observacoes": "",
             "avaliacao_inicial": {"data": "2026-11-01", "ashworth_punho": "1+", "ashworth_dedos": "2",
                                   "preensao_acometido_kgf": "4,5", "fugl_meyer_ms": "31"}}
    p1 = bia.post("/api/pacientes", json=ficha).json()
    f = bia.get(f"/api/pacientes/{p1['id']}").json()
    assert f["ficha"]["condicao_nome"] == "AVC isquêmico" and f["responsaveis"][0]["nome"] == "Bia Fisio"
    av = bia.get(f"/api/pacientes/{p1['id']}/avaliacoes").json()
    assert av[0]["inicial"] and av[0]["ashworth_punho"] == "1+" and av[0]["preensao_acometido_kgf"] == 4.5
    assert bia.post(f"/api/pacientes/{p1['id']}/avaliacoes", json={"ashworth_punho": "5"}).status_code == 400
    assert bia.post(f"/api/pacientes/{p1['id']}/avaliacoes", json={"ashworth_punho": "1", "fugl_meyer_ms": 35}).status_code == 200
    assert bia.patch(f"/api/pacientes/{p1['id']}", json={"objetivos": "pegar copo"}).json()["ficha"]["objetivos"] == "pegar copo"
    # fisioterapeuta calibra o caminho reto (rotina antes do carro), mas não a tabela de transições
    assert bia.post("/api/calibracoes/reto", json={"tras": 50, "frente": 170}).status_code == 200
    assert bia.put("/api/calibracoes/transicoes/espiral", json={"linhas": []}).status_code == 403

    # Caio não vê o paciente da Bia
    caio = entrar(app, "caio@clinica.br", "Prov12345")
    caio.post("/api/auth/trocar-senha", json={"atual": "Prov12345", "nova": "Caio2025xyz"})
    assert caio.get("/api/pacientes").json() == []
    assert caio.get(f"/api/pacientes/{p1['id']}").status_code == 403
    assert caio.get(f"/api/pacientes/{p1['id']}/historico?jogo=bolo").status_code == 403
    assert caio.post("/api/partidas", json={"paciente_id": p1["id"], "jogo": "vidro", "fonte": "mouse"}).status_code == 403
    assert "Paciente Um" not in caio.get("/api/exportar.csv").text
    assert caio.get("/api/usuarios").status_code == 403

    # o administrador vincula o Caio
    usuarios = {u["email"]: u["id"] for u in adm.get("/api/usuarios").json()}
    adm.put(f"/api/vinculos/{p1['id']}", json={"usuario_ids": [usuarios["bia@clinica.br"], usuarios["caio@clinica.br"]]})
    assert [p["nome"] for p in caio.get("/api/pacientes").json()] == ["Paciente Um"]

    # técnico: equipamento sim, pacientes não
    davi = entrar(app, "davi@clinica.br", "Prov12345")
    davi.post("/api/auth/trocar-senha", json={"atual": "Prov12345", "nova": "Davi2025xyz"})
    assert davi.get("/api/pacientes").status_code == 403
    assert davi.get("/api/calibracoes").status_code == 200
    assert davi.get("/", follow_redirects=False).headers["location"] == "/calibracao"

    # desativar encerra a sessão
    adm.patch(f"/api/usuarios/{usuarios['caio@clinica.br']}", json={"ativo": False})
    assert caio.get("/api/pacientes").status_code == 401

    # dados de saúde em texto livre ficam cifrados no banco
    db = sqlite3.connect(pasta / "trilho.db")
    med, obj = db.execute("select medicacoes, objetivos from paciente").fetchone()
    assert "baclofeno" not in med and "copo" not in obj
    acoes = {r[0] for r in db.execute("select acao from auditoria")}
    assert {"login", "primeiro_acesso", "trocar_senha", "vincular", "criar"} <= acoes


def test_bloqueio_por_senhas_erradas(app_e_pasta):
    app, _ = app_e_pasta
    c = cliente(app)
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "bia@clinica.br", "senha": "errada999"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": "bia@clinica.br", "senha": "Bia2025xyz"})
    assert r.status_code == 401 and "bloqueada" in r.json()["detail"]
    r = c.post("/api/auth/login", json={"email": "ninguem@clinica.br", "senha": "x"})
    assert r.json()["detail"] == "e-mail ou senha incorretos"


def test_logout(app_e_pasta):
    app, _ = app_e_pasta
    adm = entrar(app, "ana@clinica.br", "Admin12345")
    assert adm.post("/api/auth/logout").status_code == 200
    assert adm.get("/api/pacientes").status_code == 401
