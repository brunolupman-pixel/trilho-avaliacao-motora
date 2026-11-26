"""Banco antigo: tabela de transições provisória (código Gray) vira a padrão da caixa."""
from sqlalchemy import select


def test_tabela_provisoria_antiga_e_atualizada(tmp_path):
    from trilho import banco, cripto
    from trilho.banco.modelos import Calibracao
    from trilho.caminhos import tabela_padrao
    cripto.iniciar(tmp_path)
    banco.iniciar(tmp_path / "trilho.db")
    with banco.sessao() as s:      # simula o banco de antes: espiral com a tabela antiga, guarda-chuva medido à mão
        esp = s.scalar(select(Calibracao).where(Calibracao.caminho == "espiral", Calibracao.tipo == "tabela_transicoes"))
        esp.dados = {"linhas": [{"divisa": 1, "sensor": 2, "sentido": "ida", "troca": "liga", "codigo_depois": 1,
                                 "posicao_mm": 200.0, "incerteza_mm": 3.0}]}
        gc = s.scalar(select(Calibracao).where(Calibracao.caminho == "guarda_chuva", Calibracao.tipo == "tabela_transicoes"))
        gc.padrao = False
        gc.dados = {"linhas": [{"divisa": 1, "sensor": 0, "sentido": "ida", "troca": "liga",
                                "posicao_mm": 5.0, "incerteza_mm": 2.0}], "referencia": "medido"}
        s.commit()
    banco.iniciar(tmp_path / "trilho.db")
    banco.iniciar(tmp_path / "trilho.db")        # abrir de novo não cria outra versão
    with banco.sessao() as s:
        v = s.scalars(select(Calibracao).where(Calibracao.caminho == "espiral", Calibracao.tipo == "tabela_transicoes")
                      .order_by(Calibracao.versao)).all()
        assert [c.versao for c in v] == [1, 2] and v[-1].dados["linhas"] == tabela_padrao("espiral") and v[-1].padrao
        g = s.scalars(select(Calibracao).where(Calibracao.caminho == "guarda_chuva", Calibracao.tipo == "tabela_transicoes")).all()
        assert len(g) == 1 and g[0].dados["referencia"] == "medido"      # medida por alguém: não mexe


def test_calibracao_do_simulador_nao_vale_no_equipamento(tmp_path):
    from trilho import banco, cripto
    from trilho.autorizacao import Contexto
    from trilho.banco import repositorios as repo
    cripto.iniciar(tmp_path)
    banco.iniciar(tmp_path / "trilho.db")
    from trilho.banco.modelos import Usuario
    with banco.sessao() as s:
        adm = Contexto(s.scalar(select(Usuario).limit(1)).id, "administrador", "teste")
        repo.salvar_calibracao(s, adm, "vl53_reto", "reto", {"tras": 30, "frente": 150, "simulador": False}, "bancada")
        repo.salvar_calibracao(s, adm, "vl53_reto", "reto", {"tras": 50, "frente": 170, "simulador": True}, "bancada")
        s.commit()
        assert repo.calibracao_vigente(s, "vl53_reto", "reto", simulador=False).dados["tras"] == 30
        assert repo.calibracao_vigente(s, "vl53_reto", "reto", simulador=True).dados["tras"] == 50
