"""Conexão com o SQLite e carga inicial."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from ..caminhos import CAMINHOS, tabela_padrao
from .modelos import GATILHOS_AUDITORIA, Base, Calibracao, Dispositivo, Perfil, Usuario

_fabrica: sessionmaker | None = None


def iniciar(arquivo: Path) -> sessionmaker:
    global _fabrica
    motor = create_engine(f"sqlite:///{arquivo}", connect_args={"check_same_thread": False})

    @event.listens_for(motor, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    Base.metadata.create_all(motor)
    _migrar(motor)
    with motor.begin() as c:
        for g in GATILHOS_AUDITORIA:
            c.execute(text(g))
    _fabrica = sessionmaker(motor, expire_on_commit=False)
    with _fabrica() as s:
        _carga_inicial(s)
        s.commit()
    return _fabrica


def _migrar(motor) -> None:
    """Acrescenta colunas novas a tabelas que já existiam (bancos da versão 1).

    O SQLite aceita ADD COLUMN; colunas novas ficam vazias (NULL) nos registros antigos.
    """
    with motor.begin() as c:
        for tabela in Base.metadata.sorted_tables:
            existentes = {r[1] for r in c.execute(text(f"PRAGMA table_info({tabela.name})"))}
            for col in tabela.columns:
                if col.name not in existentes:
                    tipo = col.type.compile(dialect=motor.dialect)
                    c.execute(text(f'ALTER TABLE {tabela.name} ADD COLUMN "{col.name}" {tipo}'))


def sessao() -> Session:
    if _fabrica is None:
        raise RuntimeError("banco não iniciado")
    return _fabrica()


def _carga_inicial(s: Session) -> None:
    from ..autorizacao import PERMISSOES
    existentes = {p.nome: p for p in s.scalars(select(Perfil)).all()}
    for nome, regras in PERMISSOES.items():   # mantém os perfis em dia com o código
        if nome in existentes:
            existentes[nome].permissoes = {"acoes": sorted(regras)}
        else:
            s.add(Perfil(nome=nome, permissoes={"acoes": sorted(regras)}))
    s.flush()
    if s.scalar(select(Usuario).limit(1)) is None:
        adm = s.scalar(select(Perfil).where(Perfil.nome == "administrador"))
        s.add(Usuario(nome="Operador local", perfil_id=adm.id))
        s.flush()
    if s.scalar(select(Dispositivo).limit(1)) is None:
        for c in CAMINHOS.values():
            s.add(Dispositivo(tipo="joystick", identificador=c.joystick, caminho=c.id,
                              sensores={"imu": "MPU9250" if c.joystick == "B" else "MPU6050"}))
        s.add(Dispositivo(tipo="bancada", identificador="bancada",
                          sensores={"lm393": 5, "vl53l0x": 1}))
        s.flush()
    if s.scalar(select(Calibracao).limit(1)) is None:
        operador = s.scalar(select(Usuario).limit(1))
        banc = s.scalar(select(Dispositivo).where(Dispositivo.identificador == "bancada"))
        for cam in ("espiral", "guarda_chuva"):
            s.add(Calibracao(dispositivo_id=banc.id, tipo="tabela_transicoes", caminho=cam, versao=1,
                             dados={"linhas": tabela_padrao(cam)}, responsavel_id=operador.id, padrao=True))
        s.flush()
    # Tabela de transições ainda provisória (nunca medida) e diferente da padrão atual
    # (ex.: a do código Gray, de antes da montagem da caixa): grava a padrão nova como
    # versão seguinte. Tabelas medidas e salvas por alguém não são tocadas.
    for cam in ("espiral", "guarda_chuva"):
        atual = s.scalar(select(Calibracao).where(Calibracao.tipo == "tabela_transicoes", Calibracao.caminho == cam)
                         .order_by(Calibracao.versao.desc()).limit(1))
        nova = tabela_padrao(cam)
        if atual is not None and atual.padrao and (atual.dados or {}).get("linhas") != nova:
            s.add(Calibracao(dispositivo_id=atual.dispositivo_id, tipo="tabela_transicoes", caminho=cam,
                             versao=atual.versao + 1, responsavel_id=atual.responsavel_id, padrao=True,
                             dados={"linhas": nova, "referencia": "padrão atualizado: montagem da caixa "
                                                                  "(espiral por cruzamentos, sensores nas pontas do arco)"}))
