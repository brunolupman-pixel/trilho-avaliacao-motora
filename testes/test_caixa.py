"""Caixa (uma ESP32, sensores por fio, protocolo de texto): protocolo, fusão e API."""
import math
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from trilho import posprocesso
from trilho import protocolo_caixa as pc


# ---------------------------------------------------------------- protocolo
def test_linhas_do_firmware_sao_entendidas():
    """Saída do firmware/caixa/caixa.ino compilado com imitações das bibliotecas Arduino."""
    bruto = (Path(__file__).parent / "dados" / "saida_firmware_caixa.txt").read_bytes()
    leitor = pc.LeitorLinhas()
    msgs = leitor.alimentar(bruto)
    tipos = {type(m).__name__ for m in msgs}
    assert {"StatusCaixa", "AmostraImu", "Distancia", "Mensagem"} <= tipos
    assert leitor.invalidas == 0
    st = next(m for m in msgs if isinstance(m, pc.StatusCaixa))
    assert st.versao_fw == 207 and st.imu_ok == {"A": True, "B": True, "C": True} and st.ativo == "0"


def test_status_do_firmware_antigo_com_campo_da_celula():
    """Firmware até o 206 mandava o campo da célula de carga (retirada) na linha S: ainda é entendido."""
    st = pc.decodificar_linha(pc.montar_linha("S,1000,206,1,0,1,0,1,B,8,0").decode())
    assert st.versao_fw == 206 and st.vl53_ok and st.imu_ok == {"A": True, "B": False, "C": True}
    assert st.ativo == "B" and st.niveis_ir[3] == 1 and st.descartadas == 0


def test_linha_corrompida_e_descartada():
    boa = pc.montar_linha("B,123456,2,1")
    assert isinstance(pc.decodificar_linha(boa.decode()), pc.BordaIr)
    ruim = boa.replace(b"2,1", b"3,1")                       # um caractere trocado no caminho
    assert pc.decodificar_linha(ruim.decode()) is None
    assert pc.decodificar_linha("# resumo para pessoas") is None
    assert pc.decodificar_linha("P,1234,0,1,6") is None        # firmware antigo (sem verificação)
    leitor = pc.LeitorLinhas()
    assert leitor.alimentar(b"I,10,B,1,2,3,4,5") == []         # linha pela metade: espera o resto
    assert len(leitor.alimentar(b",6*" + f"{pc.checksum('I,10,B,1,2,3,4,5,6'):02X}".encode() + b"\n")) == 1


# ---------------------------------------------------------------- fusão com a caixa simulada
CASOS = [("B", "espiral", 34.0, 10.0), ("A", "guarda_chuva", 15.0, 5.0), ("C", "reto", 15.0, 5.0)]


@pytest.mark.parametrize("jid,caminho,dur,limite", CASOS)
def test_posicao_com_a_caixa(pipeline, jid, caminho, dur, limite):
    pl = pipeline(jid, caixa=True)
    g, tab, erros = pl.partida(caminho, dur)
    assert pl.aq.modo == "caixa" and g.relogio_unico
    rms_rt = float(np.sqrt(np.mean(erros ** 2)))
    lim_rt = 30.0 if caminho == "espiral" else limite        # espiral: tela só pelos sensores IR
    assert rms_rt < lim_rt, f"tempo real: RMS {rms_rt:.1f} mm"
    r = posprocesso.processar(g, tab, {"tras": 50.0, "frente": 170.0})
    ver = np.array(pl.gen.registro_verdade)
    s_true = np.interp(r.series["t_us"], ver[:, 0], ver[:, 1])
    rms = float(np.sqrt(np.mean((r.series["s"] - s_true) ** 2)))
    assert rms < limite, f"suavizado: RMS {rms:.1f} mm"
    assert r.qualidade["fusao"]["bordas_rejeitadas"] == 0
    assert r.qualidade["sincronia"]["valido"]
    # IMU do jogo a ~250 Hz
    assert 230 < len(g.imu) / dur < 270


@pytest.mark.parametrize("sinal,fator", [(1.0, 1.0), (-1.0, 1.0), (1.0, 1.6), (-1.0, 0.5)])
def test_giroscopio_do_arco_mede_a_escala(pipeline, sinal, fator):
    """O arco real pode girar mais ou menos que o desenho e o IMU pode estar montado ao contrário:
    a escala do giro é medida entre as pontas e, depois da primeira passada, o erro é pequeno."""
    pl = pipeline("A", caixa=True, sinal_giro=sinal, fator_giro=fator)
    g, tab, erros = pl.partida("guarda_chuva", 16.0)
    r = posprocesso.processar(g, tab, None)
    f = r.qualidade["fusao"]
    esperado = fator * 180.0 / 170.0                          # graus por mm
    assert abs(abs(f["giro_graus_por_mm"]) - esperado) < 0.05 * esperado
    depois = erros[len(erros) // 4:]                           # depois da primeira ida e volta
    assert float(np.sqrt(np.mean(depois ** 2))) < 2.0
    assert f["bordas_rejeitadas"] == 0


def test_escala_guardada_vale_desde_o_inicio(pipeline):
    """Com a escala medida antes (calibração automática), a primeira passada já sai certa."""
    from trilho import fusao
    pl = pipeline("A", caixa=True, fator_giro=1.6)
    pl.avancar(1.0)
    tab = __import__("trilho.caminhos", fromlist=["tabela_padrao"]).tabela_padrao("guarda_chuva")
    pl.aq.iniciar_partida(1, "guarda_chuva", tab, None, escala_giro=np.radians(1.6 * 180 / 170))   # o simulador gira no sentido "-1": escala positiva
    erros = []
    fim = pl.gen.tv + 8.0
    while pl.gen.tv < fim:
        pl.avancar(0.01)
        e = pl.aq.estado_publico()["partida"]
        if e and "s_mm" in e and pl.gen.tv - pl.aq.gravacao.inicio_host > 3.5:
            erros.append(e["s_mm"] - pl.gen.registro_verdade[-1][1])
    pl.aq.finalizar_partida(dormir=pl.avancar)
    assert float(np.sqrt(np.mean(np.array(erros) ** 2))) < 2.0
    assert fusao  # módulo usado


def test_arco_sem_giro_continua_funcionando(pipeline):
    """Se o IMU não girar com o arco, o giroscópio é ignorado e a posição vem da aceleração."""
    pl = pipeline("A", caixa=True, giro_arco=False)
    g, tab, erros = pl.partida("guarda_chuva", 12.0)
    r = posprocesso.processar(g, tab, None)
    assert r.qualidade["fusao"]["giro_sinal"] == 0 and r.qualidade["fusao"]["giro_atualizacoes"] == 0
    assert float(np.sqrt(np.mean(erros ** 2))) < 8.0


# ---------------------------------------------------------------- API com a caixa simulada (tempo real)
@pytest.fixture(scope="module")
def cliente_caixa(tmp_path_factory):
    import os
    pasta = tmp_path_factory.mktemp("dados_caixa")
    os.environ["TRILHO_DADOS"] = str(pasta)
    from trilho import banco, cripto
    from trilho.aquisicao import Aquisicao
    from trilho.api import criar_app
    from trilho.portas import GerenciadorPortas
    from trilho.simulador import PortaSimulada
    cripto.iniciar(pasta)
    banco.iniciar(pasta / "trilho.db")
    ps = PortaSimulada(caixa=True)
    portas = GerenciadorPortas(None, simulada=ps)
    aq = Aquisicao(portas.abrir)
    aq.ao_fechar_porta = portas.fechou
    aq.iniciar()
    with TestClient(criar_app(aq, pasta, ps, portas)) as c:
        assert c.post("/api/auth/primeiro-acesso", json={"nome": "Adm", "email": "adm@clinica.br",
                                                          "senha": "Senha12345"}).status_code == 200
        t0 = time.time()
        while time.time() - t0 < 5 and not c.get("/api/estado").json()["bancada"]["conectada"]:
            time.sleep(0.1)
        yield c
    aq.parar()
    ps.close()


def test_api_com_a_caixa(cliente_caixa):
    c = cliente_caixa
    time.sleep(1.2)                                           # 1 s de dados para medir a taxa
    e = c.get("/api/estado").json()
    assert e["hardware"] == "caixa" and e["bancada"]["conectada"]
    assert all(e["joysticks"][j]["conectado"] and e["joysticks"][j]["sincronia"]["valido"] for j in "ABC")
    imu = e["imu"]["B"]
    assert imu["ok"] and 5 <= imu["taxa_hz"] <= 15 and abs(imu["acc_g"][2] - 1.0) < 0.1     # parado: 1 g em z

    p = c.post("/api/pacientes", json={"nome": "Paciente Caixa", "lado": "direito", "consentimento": True}).json()
    st = c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "bolo", "fonte": "sensores"})
    assert st.status_code == 200, st.text
    time.sleep(2.5)
    e = c.get("/api/estado").json()
    assert e["imu"]["B"]["taxa_hz"] > 200                     # a caixa passou a mandar o IMU B a 250 Hz
    assert e["partida"]["caminho"] == "espiral" and "s_mm" in e["partida"]
    time.sleep(4.5)
    fim = c.post(f"/api/partidas/{st.json()['partida_id']}/finalizar", json={"pontos": 50, "estrelas": 1})
    assert fim.status_code == 200 and fim.json()["salva"]
    h = c.get(f"/api/pacientes/{p['id']}/historico?jogo=bolo").json()[-1]
    assert h["fonte"] == "Sensores" and h["metricas"]["distancia_percorrida_mm"] > 50
    time.sleep(1.5)                                           # a taxa é medida numa janela de 1 s
    assert c.get("/api/estado").json()["imu"]["B"]["taxa_hz"] < 30     # voltou para 10 Hz


def test_carro_so_com_o_sensor_de_distancia(pipeline):
    """Sem célula de carga (retirada do projeto): o carro funciona só com o VL53L0X."""
    pl = pipeline("C", caixa=True)
    g, tab, erros = pl.partida("reto", 10.0)
    assert float(np.sqrt(np.mean(erros ** 2))) < 5.0
    r = posprocesso.processar(g, tab, {"tras": 50.0, "frente": 170.0})
    assert not any("forca" in k or "carga" in k for k in r.metricas) and r.metricas["velocidade_pico_mm_s"] > 0
    assert "forca" not in r.tabelas_brutas


def test_janela_guarda_a_escala_do_giro(cliente_caixa):
    c = cliente_caixa
    p = c.post("/api/pacientes", json={"nome": "Paciente Janela", "lado": "direito", "consentimento": True}).json()
    st = c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "vidro", "fonte": "sensores"}).json()
    assert c.app.state.partida["escala_giro"] is None           # primeira vez: ainda não medida
    time.sleep(10)                                              # 3 s de contagem + idas e voltas entre as pontas
    assert c.post(f"/api/partidas/{st['partida_id']}/finalizar", json={"pontos": 1, "estrelas": 1}).status_code == 200
    st2 = c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "vidro", "fonte": "sensores"}).json()
    escala = c.app.state.partida["escala_giro"]
    assert escala is not None and abs(abs(np.degrees(escala)) - 180 / 170) < 0.06
    c.post(f"/api/partidas/{st2['partida_id']}/cancelar")


def test_barra_do_carro_nao_treme_com_vl53_ruidoso():
    """VL53L0X real: ~5 mm de ruído e picos isolados de 15-40 mm. Com o joystick parado,
    a posição mostrada no jogo fica firme; num movimento, acompanha com pouco atraso."""
    from trilho.caminhos import CAMINHOS
    from trilho.fusao import FiltroCaminho, FusaoTempoReal
    rng = np.random.default_rng(7)
    fr = FusaoTempoReal(FiltroCaminho(CAMINHOS["reto"], None, {"tras": 50.0, "frente": 170.0}), atraso_us=10_000)

    def verdade(t):                                  # parado em 40 mm, vai a 100 mm em 0,6 s, para
        u = min(1.0, max(0.0, (t - 3.0) / 0.6))
        return 40 + 60 * (10 * u ** 3 - 15 * u ** 4 + 6 * u ** 5)
    tela, prox, tv = [], 0.0, 0.0
    while tv < 6.0:
        while prox <= tv:
            d = 50 + verdade(prox - 0.01) + rng.normal(0, 5.0)
            if rng.random() < 0.04:
                d += rng.choice([-1, 1]) * rng.uniform(15, 40)
            fr.adicionar((prox - 0.01) * 1e6, "vl53", (int(round(d)), 0))
            prox += 0.02
        fr.processar(tv * 1e6)
        tela.append((tv, fr.estado(tv * 1e6)["f"] * 120.0))
        tv += 0.01
    t, s = np.array(tela).T
    parado = (t > 1.0) & (t < 3.0)
    assert np.ptp(s[parado]) < 15.0                  # antes: ~45 mm de vai e vem
    assert np.sqrt(np.mean(np.diff(s[parado], 2) ** 2)) < 0.6     # quadro a quadro, sem "glitch"
    assert abs(np.mean(s[(t > 4.5)]) - 100) < 5      # chegou aonde a mão foi


def test_preferencias_do_carro(cliente_caixa):
    c = cliente_caixa
    p = c.post("/api/pacientes", json={"nome": "Paciente Prefs", "lado": "direito", "consentimento": True}).json()
    r = c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"tolerancia_combo_s": 3, "margem_velocidade_kmh": 15})
    assert r.status_code == 200, r.text
    assert c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"margem_velocidade_kmh": 25}).status_code == 400
    assert c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"tolerancia_combo_s": 4}).status_code == 400
    pac = next(x for x in c.get("/api/pacientes").json() if x["id"] == p["id"])
    assert pac["preferencias"]["tolerancia_combo_s"] == 3 and pac["preferencias"]["margem_velocidade_kmh"] == 15


def test_sensibilidades_do_bolo_e_da_janela(cliente_caixa):
    c = cliente_caixa
    p = c.post("/api/pacientes", json={"nome": "Paciente Sens", "lado": "esquerdo", "consentimento": True}).json()
    r = c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"sensibilidade_respingo": "alta", "sensibilidade_limpeza": "baixa"})
    assert r.status_code == 200, r.text
    assert c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"sensibilidade_respingo": "maxima"}).status_code == 400
    pac = next(x for x in c.get("/api/pacientes").json() if x["id"] == p["id"])
    assert pac["preferencias"]["sensibilidade_respingo"] == "alta" and pac["preferencias"]["sensibilidade_limpeza"] == "baixa"


def test_braco_sadio_para_comparar(cliente_caixa):
    c = cliente_caixa
    p = c.post("/api/pacientes", json={"nome": "Paciente Braços", "lado": "direito", "consentimento": True}).json()
    for membro in ("acometido", "sadio", "sadio"):
        st = c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "bolo", "fonte": "mouse", "membro": membro})
        assert st.status_code == 200, st.text
        assert st.json()["lado_braco"] == ("direito" if membro == "acometido" else "esquerdo")
        assert c.post(f"/api/partidas/{st.json()['partida_id']}/finalizar",
                      json={"pontos": 10, "estrelas": 1}).status_code == 200
    h = c.get(f"/api/pacientes/{p['id']}/historico?jogo=bolo").json()
    assert [x["membro"] for x in h] == ["acometido", "sadio", "sadio"]
    assert [x["lado_braco"] for x in h] == ["direito", "esquerdo", "esquerdo"]
    assert h[0]["sessao"] != h[1]["sessao"] and h[1]["sessao"] == h[2]["sessao"]   # trocar de braço abre outra sessão
    linhas = [l for l in c.get("/api/exportar.csv").text.splitlines() if "Paciente Braços" in l]
    assert len(linhas) == 3 and ";sadio;esquerdo;" in linhas[-1]
    # paciente bilateral: não há braço sadio
    b = c.post("/api/pacientes", json={"nome": "Paciente Bilateral", "lado": "bilateral", "consentimento": True}).json()
    r = c.post("/api/partidas", json={"paciente_id": b["id"], "jogo": "bolo", "fonte": "mouse", "membro": "sadio"})
    assert r.status_code == 400 and "bilateral" in r.json()["detail"]
    assert c.post("/api/partidas", json={"paciente_id": p["id"], "jogo": "bolo", "fonte": "mouse",
                                         "membro": "outro"}).status_code == 400


def test_tutorial_oculto_por_paciente(cliente_caixa):
    c = cliente_caixa
    p = c.post("/api/pacientes", json={"nome": "Paciente Tutorial", "lado": "direito", "consentimento": True}).json()
    r = c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"tutorial_oculto": {"vidro": True, "bolo": False}})
    assert r.status_code == 200, r.text
    assert c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"tutorial_oculto": {"xadrez": True}}).status_code == 400
    assert c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"tutorial_oculto": True}).status_code == 400
    pac = next(x for x in c.get("/api/pacientes").json() if x["id"] == p["id"])
    assert pac["preferencias"]["tutorial_oculto"] == {"vidro": True, "bolo": False}


def test_preferencia_de_som(cliente_caixa):
    c = cliente_caixa
    p = c.post("/api/pacientes", json={"nome": "Paciente Som", "lado": "esquerdo", "consentimento": True}).json()
    for modo in ("avisos", "nenhum", "todos"):
        assert c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"som": modo}).status_code == 200
    assert c.patch(f"/api/pacientes/{p['id']}/preferencias", json={"som": "alto"}).status_code == 400


@pytest.mark.parametrize("caminho", ["espiral", "guarda_chuva"])
def test_imu_mandando_zeros_nao_derruba_o_filtro(caminho):
    """MPU6050 que reiniciou por mau contato fica "dormindo" e manda só zeros. Antes, isso no
    nivelamento virava divisão por zero, NaN e 'Singular matrix' (aquisição e pós-processamento)."""
    import warnings
    from trilho.caminhos import CAMINHOS, tabela_padrao
    from trilho.fusao import FiltroCaminho, reprocessar
    um_g = (0, 0, 8192)
    imu = []
    for i in range(1500):                                         # 6 s a 250 Hz
        zeros = i < 150 or 700 <= i < 760                         # no nivelamento e no meio da partida
        acc = (0, 0, 0) if zeros else um_g
        imu.append((1_000_000 + i * 4000, *acc, 0, 0, 0))
    with warnings.catch_warnings():
        warnings.simplefilter("error")                            # nenhuma divisão por zero
        f = FiltroCaminho(CAMINHOS[caminho], tabela_padrao(caminho), None)
        for r in imu:
            f.imu(r[0], r[1:4], r[4:7])
        assert np.isfinite(f.x).all() and np.isfinite(f.P).all()
        assert f.cont.imu_invalidas == 210 and f.R_nivel is not None
        series, cont = reprocessar(CAMINHOS[caminho], tabela_padrao(caminho), None, np.array(imu, float),
                                   np.zeros((0, 3)), np.zeros((0, 3)))
    assert np.isfinite(series["s"]).all() and cont.imu_invalidas == 210


def test_filtro_recomeca_se_o_estado_estragar():
    from trilho.caminhos import CAMINHOS, tabela_padrao
    from trilho.fusao import FiltroCaminho
    f = FiltroCaminho(CAMINHOS["espiral"], tabela_padrao("espiral"), None)
    for i in range(120):
        f.imu(1e6 + i * 4000, (0, 0, 8192), (0, 0, 0))
    f.x[1] = np.nan                                               # estado estragado por qualquer motivo
    f.imu(1e6 + 121 * 4000, (0, 0, 8192), (0, 0, 0))
    assert np.isfinite(f.x).all() and f.cont.falhas_numericas >= 1


@pytest.mark.parametrize("eixo,sinal", [(0, -1.0), (45, 1.0), (70, -1.0), (90, -1.0), (90, 1.0)])
def test_janela_com_eixo_de_giro_inclinado(pipeline, eixo, sinal):
    """O arco pode girar o IMU em torno de um eixo deitado ou inclinado (montagem ou desenho do
    arco). O eixo é descoberto pelos dados; antes, só o giro em torno da vertical era usado e o
    meio do vidro ficava sem posição (erro ~110 mm com o eixo deitado)."""
    pl = pipeline("A", caixa=True, eixo_giro_graus=eixo, sinal_giro=sinal)
    g, tab, erros = pl.partida("guarda_chuva", 16.0)
    depois = erros[len(erros) // 4:]
    assert float(np.sqrt(np.mean(depois ** 2))) < 3.0
    r = posprocesso.processar(g, tab, None)
    fu = r.qualidade["fusao"]
    assert fu["giro_eixo"] is not None and abs(fu["giro_eixo_vertical_graus"] - eixo) < 8
    assert fu["bordas_rejeitadas"] == 0


def test_escala_guardada_com_eixo_deitado(pipeline):
    """Escala salva de outra partida + eixo deitado: o sentido convencionado do eixo mantém a
    escala válida (e, se vier trocada, é corrigida ao sair da ponta A)."""
    from trilho.caminhos import tabela_padrao
    for escala in (np.radians(180 / 170), -np.radians(180 / 170)):
        pl = pipeline("A", caixa=True, eixo_giro_graus=90)
        pl.avancar(1.0)
        pl.aq.iniciar_partida(1, "guarda_chuva", tabela_padrao("guarda_chuva"), None, escala_giro=escala)
        erros, fim = [], pl.gen.tv + 8.0
        while pl.gen.tv < fim:
            pl.avancar(0.01)
            e = pl.aq.estado_publico()["partida"]
            if e and "s_mm" in e and pl.gen.tv - pl.aq.gravacao.inicio_host > 3.5:
                erros.append(e["s_mm"] - pl.gen.registro_verdade[-1][1])
        pl.aq.finalizar_partida(dormir=pl.avancar)
        assert float(np.sqrt(np.mean(np.array(erros) ** 2))) < 3.0


# ---------------------------------------------------------------- espiral: ordem dos sensores e "beco"
def test_espiral_ordem_s1_s2_s3_s1_s2_s3():
    from trilho.caminhos import MARCADORES_PADRAO
    marc = MARCADORES_PADRAO["espiral"]
    assert [s for s, _ in marc] == [2, 3, 4, 2, 3, 4]
    assert all(b > a for (_, a), (_, b) in zip(marc, marc[1:]))


def test_espiral_com_imu_ruidoso_usa_a_ordem_dos_sensores(pipeline):
    """Com o acelerômetro muito ruidoso a estimativa se perde entre os sensores. A ordem
    S1, S2, S3, S1, S2, S3 identifica cada cruzamento mesmo assim (antes eles eram rejeitados)
    e a regra do beco reconhece a chegada ao centro."""
    pl = pipeline("B", caixa=True, semente=3, ruido_acc=300.0)
    g, tab, erros = pl.partida("espiral", 34.0)
    r = posprocesso.processar(g, tab, None)
    fu = r.qualidade["fusao"]
    assert fu["bordas_rejeitadas"] == 0
    assert float(np.sqrt(np.mean(erros ** 2))) < 60.0
    # a tela (só sensores IR) não depende do IMU: o ruído não a afeta
    assert float(np.sqrt(np.mean(erros ** 2))) < 30.0


def _filtro_no_beco():
    from trilho.caminhos import CAMINHOS, tabela_padrao
    from trilho.fusao import FiltroCaminho
    f = FiltroCaminho(CAMINHOS["espiral"], tabela_padrao("espiral"), None)
    for i in range(120):
        f.imu(1e6 + i * 4000, (0, 0, 8192), (0, 0, 0))       # nivelado, parado
    inicio = f._cruz[-1][2] + 7.0
    f._passados = len(f._cruz)
    f._becos["fim"]["t0"] = f.t
    f._beco_t0 = f.t
    return f, inicio, CAMINHOS["espiral"].comprimento


def test_beco_pausa_no_meio_nao_conta_como_centro():
    f, inicio, L = _filtro_no_beco()
    f.x[0], f.x[1] = inicio + 0.3 * (L - inicio), 0.0           # parou com 30% do beco andado
    f._beco(f._beco_t0 + 3.0e6, quieto=True)                     # 3 s: nem 60% nem 5 s
    assert not f._becos["fim"]["fixado"] and f.cont.beco_centro == 0


def test_beco_parado_no_fim_conta_como_centro():
    f, inicio, L = _filtro_no_beco()
    f.x[0], f.x[1] = inicio + 0.8 * (L - inicio), 0.0            # estimativa adiantada no beco, parado
    f._beco(f._beco_t0 + 3.0e6, quieto=True)
    assert f._becos["fim"]["fixado"] and f.cont.beco_centro == 1 and f.x[0] > inicio + 0.8 * (L - inicio)


def test_beco_regra_dos_5_segundos():
    """Regra combinada com a equipe: 5 s no beco sem novo sensor, com o joystick parado = centro,
    mesmo que a estimativa tenha ficado para trás (deriva do IMU)."""
    f, inicio, L = _filtro_no_beco()
    f.x[0], f.x[1] = inicio + 0.2 * (L - inicio), 0.0
    f._beco(f._beco_t0 + 4.0e6, quieto=True)
    assert not f._becos["fim"]["fixado"]
    f._beco(f._beco_t0 + 5.5e6, quieto=True)
    assert f._becos["fim"]["fixado"] and f.x[0] > L - 40


# ---------------------------------------------------------------- espiral: sensores acionando à toa
def _filtro_espiral_parado(segundos: float = 0.5):
    from trilho.caminhos import CAMINHOS, tabela_padrao
    from trilho.fusao import FiltroCaminho
    f = FiltroCaminho(CAMINHOS["espiral"], tabela_padrao("espiral"), None)
    n = int(segundos / 0.004)
    for i in range(n):
        f.imu(1e6 + i * 4000, (0, 0, 8192), (0, 0, 0))       # nivelado, parado na entrada
    return f


def _ate(f, t_fim: float) -> float:
    """Leituras do IMU (sem aceleração: parado ou velocidade constante) até t_fim."""
    t = f.t
    while t + 4000 <= t_fim:
        t += 4000
        f.imu(t, (0, 0, 8192), (0, 0, 0))
    return t_fim


def _passar(f, i: int, t: float, dur: float = 0.08) -> float:
    """Entrada e saída do feixe do cruzamento i (na ordem do caminho)."""
    sensor = f._cruz[i][1]
    _ate(f, t)
    f.borda(t, sensor, 1)
    _ate(f, t + dur * 1e6)
    f.borda(t + dur * 1e6, sensor, 0)
    return t + dur * 1e6


def test_espiral_sensores_piscando_nao_levam_ao_centro():
    """Reflexo/mão: S2, S3, S1, S2, S3 piscando em 0,4 s com o joystick parado na entrada não
    podem levar a posição ao centro (antes a ordem avançava sozinha e o jogo acabava)."""
    f = _filtro_espiral_parado()
    t = f.t + 0.1e6
    for i in range(1, 6):
        t = _passar(f, i, t, 0.02) + 0.06e6
    assert f.zonas.k <= 1 and f.zonas.posicao(t) < f._cruz[1][2] and f.x[0] < f._cruz[1][2]
    assert f.cont.bordas_rejeitadas > 0


def test_espiral_sensor_distante_piscando_sozinho_e_ignorado():
    """S3 piscando várias vezes, devagar, com o joystick parado na entrada: o primeiro S3 do
    caminho é o 3º cruzamento (pularia dois), então não reposiciona."""
    f = _filtro_espiral_parado()
    t = f.t
    for _ in range(4):
        t = _passar(f, 2, t + 6e6)
    assert f.zonas.k == 0 and f._passados == 0 and f.x[0] < f._cruz[0][2]
    assert f.cont.bordas_rejeitadas >= 4


def test_espiral_passagem_real_lenta_continua_aceita():
    f = _filtro_espiral_parado()
    t, s_ant = f.t, 0.0
    for i, (_, _, c) in enumerate(f._cruz):
        t = _passar(f, i, t + (c - s_ant) / 60.0 * 1e6)          # 60 mm/s
        s_ant = c
    assert f._passados == len(f._cruz)
    assert f.cont.cruzamentos_implausiveis == 0 and f.cont.cruzamentos_saltados == 0


def _rastreador():
    from trilho.caminhos import CAMINHOS, tabela_padrao
    from trilho.fusao import FiltroCaminho
    f = FiltroCaminho(CAMINHOS["espiral"], tabela_padrao("espiral"), None)
    z = f.zonas
    z.iniciar(1e6)
    return f, z


def test_zonas_sensores_piscando_nao_movem_a_colher():
    f, z = _rastreador()
    t = 1.5e6
    for i in range(6):
        z.borda(t, f._cruz[i][1], 1)
        z.borda(t + 0.02e6, f._cruz[i][1], 0)
        t += 0.08e6
    # (o S1 meio segundo depois do início é indistinguível de uma passagem real: pode valer)
    assert z.k <= 1 and z.posicao(t + 1e6) < f._cruz[1][2] and z.ignorados > 0


def test_zonas_ida_e_volta_sem_passar_feixe_nao_cruzado():
    f, z = _rastreador()
    t, s_ant = 1e6, 0.0
    for i, (_, sen, c) in enumerate(f._cruz):
        t_chega = t + (c - s_ant) / 60.0 * 1e6
        # até o feixe ser cruzado, a colher não passa dele
        for tt in np.linspace(t, t_chega - 1, 20):
            assert z.posicao(tt) <= c + 1e-6
        t = t_chega
        assert z.borda(t, sen, 1)
        z.borda(t + 0.1e6, sen, 0)
        s_ant = c
    assert z.k == len(f._cruz)
    assert abs(z.posicao(t + 10e6) - z.L) < 1e-6             # anda até o centro (beco) e para
    # volta: entra de novo no último feixe e segue para trás
    t += 12e6
    assert z.borda(t, f._cruz[-1][1], 1) and z.k == len(f._cruz) - 1
    assert z.velocidade(t + 0.03e6) < 0


@pytest.mark.parametrize("guinada,em_pe", [(70.0, False), (180.0, False), (-120.0, True)])
def test_espiral_descobre_a_montagem_do_imu(pipeline, guinada, em_pe):
    """Placa do IMU girada em torno da vertical (ou montada em pé, como a MPU-92.65 na bancada):
    o ângulo é descoberto pelos dados e a fusão volta para a tela entre os feixes."""
    pl = pipeline("B", caixa=True, guinada_graus=guinada, y_vertical=em_pe)
    g, tab, erros = pl.partida("espiral", 34.0)
    r = posprocesso.processar(g, tab, None)
    q = r.qualidade["fusao"]
    assert q["montagem_confirmada"]
    if not em_pe:
        dif = (q["montagem_graus"] - guinada + 180) % 360 - 180
        assert abs(dif) < 12
    assert float(np.sqrt(np.mean(erros ** 2))) < 20.0


def test_zonas_hesitar_na_beira_do_feixe_nao_inverte_a_colher():
    f, z = _rastreador()
    c0 = f._cruz[0]
    t = 1e6 + c0[2] / 60.0 * 1e6
    z.borda(t, c0[1], 1)
    z.borda(t + 0.25e6, c0[1], 0)                          # saiu indo
    assert z.velocidade(t + 0.3e6) > 0
    z.borda(t + 0.45e6, c0[1], 1)                          # voltou ao mesmo feixe logo em seguida
    assert z.velocidade(t + 0.5e6) == 0.0                  # espera, não anda para trás
    z.borda(t + 0.7e6, c0[1], 0)
    assert z.velocidade(t + 0.8e6) == 0.0


def _filtro_arco_parado():
    from trilho.caminhos import CAMINHOS, tabela_padrao
    from trilho.fusao import FiltroCaminho
    f = FiltroCaminho(CAMINHOS["guarda_chuva"], tabela_padrao("guarda_chuva"), None)
    for i in range(150):
        f.imu(1e6 + i * 4000, (0, 0, 8192), (0, 0, 0))
    return f


def test_janela_ponta_b_falsa_logo_depois_da_a_e_ignorada():
    f = _filtro_arco_parado()
    lin_a = [l for l in f.tabela if l["sensor"] == 0 and l["troca"] == "desliga"][0]   # saiu da ponta A
    lin_b = [l for l in f.tabela if l["sensor"] == 1 and l["troca"] == "liga"][0]      # chegou na ponta B
    assert f.borda(f.t, lin_a["sensor"], 1 if lin_a["troca"] == "liga" else 0)
    t = f.t + 0.15e6                                       # 0,15 s depois: impossível chegar na B
    assert not f.borda(t, lin_b["sensor"], 1 if lin_b["troca"] == "liga" else 0)
    assert f.x[0] < 0.5 * f.c.comprimento and f.cont.pontas_implausiveis == 1


def test_janela_ponta_b_sem_giro_e_ignorada():
    """Com o giroscópio em uso: chegar à ponta B sem o IMU ter girado = sensor acionando à toa."""
    f = _filtro_arco_parado()
    lin_a = [l for l in f.tabela if l["sensor"] == 0 and l["troca"] == "desliga"][0]   # saiu da ponta A
    lin_b = [l for l in f.tabela if l["sensor"] == 1 and l["troca"] == "liga"][0]      # chegou na ponta B
    f.escala_giro, f.eixo = f._escala_nominal(), np.array([0.0, 0.0, 1.0])
    assert f.borda(f.t, lin_a["sensor"], 1 if lin_a["troca"] == "liga" else 0)
    t = f.t + 5e6
    assert not f.borda(t, lin_b["sensor"], 1 if lin_b["troca"] == "liga" else 0)
    assert f.cont.pontas_implausiveis == 1


def test_janela_sentido_invertido_e_corrigido():
    """Sentido do giro trocado (o joystick sai da ponta A, mas a estimativa "empurra" a ponta A sem o
    sensor ver o joystick): corrige em menos de meio segundo e a posição volta para dentro do arco."""
    f = _filtro_arco_parado()
    f.escala_giro, f.eixo, f._sentido_conferido = f._escala_nominal(), np.array([0.0, 0.0, 1.0]), True
    w_graus = -math.degrees(60.0 * f._escala_nominal())          # 60 mm/s no sentido trocado
    t = f.t
    for _ in range(int(1.0 / 0.004)):
        t += 4000
        f.imu(t, (0, 0, 8192), (0, 0, int(w_graus * 65.5)))
    assert f.cont.giro_sentido_corrigido == 1
    assert f.x[0] > 20 and f.x[1] > 20                          # dentro do arco, indo para a ponta B


def test_janela_escala_guardada_absurda_e_descartada(pipeline):
    """Escala do giro salva numa partida com o IMU com defeito (aqui 5% da real): é descartada e
    medida de novo; antes, o ruído do giroscópio virava velocidades enormes (pontas alternando)."""
    from trilho.caminhos import tabela_padrao
    pl = pipeline("A", caixa=True)
    pl.avancar(1.0)
    pl.aq.iniciar_partida(1, "guarda_chuva", tabela_padrao("guarda_chuva"), None,
                          escala_giro=0.05 * np.radians(180 / 170))
    erros, fim = [], pl.gen.tv + 10.0
    while pl.gen.tv < fim:
        pl.avancar(0.01)
        e = pl.aq.estado_publico()["partida"]
        if e and "s_mm" in e and pl.gen.tv - pl.aq.gravacao.inicio_host > 3.5:
            erros.append(e["s_mm"] - pl.gen.registro_verdade[-1][1])
    assert pl.aq.fusao.f.cont.escala_giro_descartada >= 1
    pl.aq.finalizar_partida(dormir=pl.avancar)
    assert float(np.sqrt(np.mean(np.array(erros) ** 2))) < 6.0


# ---------------------------------------------------------------- principais metas (área de avaliação)
def test_principais_metas(cliente_caixa):
    c = cliente_caixa
    p = c.post("/api/pacientes", json={"nome": "Paciente Metas", "lado": "direito", "consentimento": True}).json()
    url = f"/api/pacientes/{p['id']}/metas"
    assert c.get(url).json() == []
    metas = [{"jogo": "vidro", "metrica": "amplitude_pct", "alvo": "80"},
             {"jogo": "bolo", "metrica": "sparc", "alvo_braco_sadio": True},
             {"jogo": "carro", "metrica": "tempo_de_reacao_s", "observacao": "reavaliar em 4 semanas"}]
    r = c.put(url, json={"metas": metas})
    assert r.status_code == 200, r.text
    lista = c.get(url).json()
    assert [(m["ordem"], m["jogo"], m["metrica"]) for m in lista] == [(1, "vidro", "amplitude_pct"), (2, "bolo", "sparc"),
                                                                        (3, "carro", "tempo_de_reacao_s")]
    assert lista[0]["alvo"] == 80.0 and lista[1]["alvo_braco_sadio"] and lista[1]["alvo"] is None
    assert lista[2]["observacao"] == "reavaliar em 4 semanas"
    # reordenar e trocar uma: a que continua mantém a data de criação (linha de base)
    criada = lista[1]["criada_em"]
    r = c.put(url, json={"metas": [metas[1], {"jogo": "vidro", "metrica": "submovimentos", "alvo": 3}]}).json()
    assert [m["metrica"] for m in r] == ["sparc", "submovimentos"] and r[0]["criada_em"] == criada
    # validações
    seis = [{"jogo": "vidro", "metrica": k} for k in ("sparc", "ldlj", "pausas", "maior_pausa_s", "amplitude_pct", "submovimentos")]
    assert c.put(url, json={"metas": seis}).status_code == 400
    assert c.put(url, json={"metas": [metas[0], metas[0]]}).status_code == 400
    assert c.put(url, json={"metas": [{"jogo": "vidro", "metrica": "inventada"}]}).status_code == 400
    assert c.put(url, json={"metas": [{"jogo": "xadrez", "metrica": "sparc"}]}).status_code == 400
    b = c.post("/api/pacientes", json={"nome": "Paciente Bilateral 2", "lado": "bilateral", "consentimento": True}).json()
    r = c.put(f"/api/pacientes/{b['id']}/metas", json={"metas": [metas[1]]})
    assert r.status_code == 400 and "bilateral" in r.json()["detail"]


# ---------------------------------------------------------------- minha conta: dados e foto
def test_minha_conta_dados_e_foto(cliente_caixa):
    import base64
    c = cliente_caixa
    eu = c.get("/api/auth/estado").json()["usuario"]
    assert eu["foto_versao"] == 0 and "registro_profissional" in eu
    r = c.patch("/api/auth/eu", json={"nome": "  Ana   Souza ", "registro_profissional": "CREFITO-3 12345-F"})
    assert r.status_code == 200 and r.json()["nome"] == "Ana Souza" and r.json()["registro_profissional"] == "CREFITO-3 12345-F"
    assert c.patch("/api/auth/eu", json={"nome": "A"}).status_code == 400
    assert c.get("/api/auth/foto").status_code == 404
    # JPEG mínimo (só a assinatura importa para a validação)
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
    r = c.put("/api/auth/foto", json={"imagem": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()})
    assert r.status_code == 200 and r.json()["foto_versao"] >= 1
    f = c.get(f"/api/auth/foto?v={r.json()['foto_versao']}")
    assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg" and f.content == jpeg
    # não é imagem / grande demais
    assert c.put("/api/auth/foto", json={"imagem": "data:image/jpeg;base64," + base64.b64encode(b"<script>").decode()}).status_code == 400
    grande = b"\xff\xd8\xff" + b"\x00" * 500_000
    assert c.put("/api/auth/foto", json={"imagem": "data:image/jpeg;base64," + base64.b64encode(grande).decode()}).status_code == 400
    assert c.put("/api/auth/foto", json={"imagem": "nada"}).status_code == 400
    r = c.delete("/api/auth/foto")
    assert r.status_code == 200 and r.json()["foto_versao"] == 0
    assert c.get("/api/auth/foto").status_code == 404
    c.patch("/api/auth/eu", json={"nome": "Adm", "registro_profissional": ""})


def test_espiral_centro_devagar_segue_a_mao(pipeline):
    """No centro não há feixe: a colher segue a velocidade medida pelo IMU (centrípeta, v²·κ), mesmo
    com a mão entrando devagar, parando no caminho e voltando, e sem o ângulo da placa confirmado."""
    from trilho.caminhos import CAMINHOS, tabela_padrao
    from trilho.fusao import ConfigFusao
    from trilho.simulador import Programa
    L = CAMINHOS["espiral"].comprimento
    prog = Programa(0.0, [(600.0, 9.0, 0.0), (670.0, 4.0, 2.0), (L, 4.0, 1.5), (600.0, 5.0, 0.0), (0.0, 9.0, 1.0)])
    pl = pipeline("B", caixa=True, programa=prog)
    pl.aq.cfg_fusao = ConfigFusao(montagem_coerencia=1.01)          # ângulo nunca confirmado
    pl.avancar(1.0)
    pl.aq.iniciar_partida(1, "espiral", tabela_padrao("espiral"), None)
    centro, fim = [], pl.gen.tv + 30
    while pl.gen.tv < fim:
        pl.avancar(0.01)
        e = pl.aq.estado_publico()["partida"]
        real = pl.gen.registro_verdade[-1][1]
        if e and "s_mm" in e and real > 645:
            centro.append(e["s_mm"] - real)
    pl.aq.finalizar_partida(dormir=pl.avancar)
    centro = np.array(centro)
    assert len(centro) > 300 and float(np.sqrt(np.mean(centro ** 2))) < 8.0 and np.abs(centro).max() < 20
