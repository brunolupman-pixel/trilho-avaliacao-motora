"""Modelo de dados (arquitetura: "Nuvem, modelo de dados e histórico").

Versão 2: login de profissionais (usuário, sessão de login, vínculo
paciente ↔ profissional), ficha clínica do paciente e avaliações clínicas
repetíveis. Textos livres de saúde ficam cifrados (TextoCifrado).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, LargeBinary, Date, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .. import cripto


def agora() -> datetime:
    return datetime.now(timezone.utc)


class TextoCifrado(TypeDecorator):
    """Texto guardado cifrado no banco (ver cripto.py)."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, valor, dialeto):
        return cripto.cofre().cifrar(valor)

    def process_result_value(self, valor, dialeto):
        return cripto.cofre().decifrar(valor)


class Base(DeclarativeBase):
    pass


class Perfil(Base):
    __tablename__ = "perfil"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(40), unique=True)   # administrador, fisioterapeuta, tecnico, paciente
    permissoes: Mapped[dict] = mapped_column(JSON, default=dict)


class Usuario(Base):
    """Profissional da equipe. O e-mail é o login. O 'Operador local' da versão 1
    continua como autor dos registros antigos, mas não tem senha (não entra)."""
    __tablename__ = "usuario"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(200), unique=True)
    perfil_id: Mapped[int] = mapped_column(ForeignKey("perfil.id"))
    senha_hash: Mapped[str | None] = mapped_column(String(200))   # Argon2
    registro_profissional: Mapped[str | None] = mapped_column(String(40))   # ex.: CREFITO
    deve_trocar_senha: Mapped[bool | None] = mapped_column(Boolean, default=False)
    tentativas_falhas: Mapped[int | None] = mapped_column(Integer, default=0)
    bloqueado_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    # foto do perfil (já recortada em quadrado e reduzida no navegador; aparece redonda no topo)
    foto: Mapped[bytes | None] = mapped_column(LargeBinary)
    foto_tipo: Mapped[str | None] = mapped_column(String(20))
    foto_versao: Mapped[int | None] = mapped_column(Integer, default=0)
    perfil: Mapped[Perfil] = relationship()


class SessaoLogin(Base):
    """Sessão aberta no navegador. Guarda só o hash do token do cookie."""
    __tablename__ = "sessao_login"
    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    ultimo_uso: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    encerrada: Mapped[bool] = mapped_column(Boolean, default=False)
    usuario: Mapped[Usuario] = relationship()


class VinculoPaciente(Base):
    """Profissional responsável pelo paciente: só os vinculados (e o administrador) o veem."""
    __tablename__ = "vinculo_paciente"
    __table_args__ = (UniqueConstraint("paciente_id", "usuario_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"))
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class Paciente(Base):
    __tablename__ = "paciente"
    id: Mapped[int] = mapped_column(primary_key=True)
    lado_acometido: Mapped[str] = mapped_column(String(20))
    dominancia: Mapped[str | None] = mapped_column(String(20))
    inicio_tratamento: Mapped[date] = mapped_column(Date, default=lambda: date.today())
    preferencias: Mapped[dict] = mapped_column(JSON, default=dict)   # ex.: zona neutra do carro
    # diagnóstico e lesão
    condicao: Mapped[str | None] = mapped_column(String(40))       # avc_isquemico, paralisia_cerebral, ...
    condicao_detalhe: Mapped[str | None] = mapped_column(TextoCifrado)
    data_lesao: Mapped[date | None] = mapped_column(Date)
    # tratamento e objetivos
    toxina_botulinica: Mapped[bool | None] = mapped_column(Boolean)
    toxina_ultima_data: Mapped[date | None] = mapped_column(Date)
    toxina_musculos: Mapped[str | None] = mapped_column(TextoCifrado)
    medicacoes: Mapped[str | None] = mapped_column(TextoCifrado)
    objetivos: Mapped[str | None] = mapped_column(TextoCifrado)
    observacoes: Mapped[str | None] = mapped_column(TextoCifrado)
    atualizado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    identificacao: Mapped["IdentificacaoPaciente"] = relationship(back_populates="paciente", uselist=False,
                                                                  cascade="all, delete-orphan")


class IdentificacaoPaciente(Base):
    """Dados identificáveis, separados das medições e cifrados."""
    __tablename__ = "identificacao_paciente"
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"), primary_key=True)
    nome: Mapped[str] = mapped_column(TextoCifrado)
    data_nascimento: Mapped[str | None] = mapped_column(TextoCifrado)
    documento: Mapped[str | None] = mapped_column(TextoCifrado)
    contato: Mapped[str | None] = mapped_column(TextoCifrado)
    paciente: Mapped[Paciente] = relationship(back_populates="identificacao")


class ContaPaciente(Base):
    """Login futuro do paciente (portal na nuvem). Sem uso na versão 1."""
    __tablename__ = "conta_paciente"
    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"), unique=True)
    tipo_credencial: Mapped[str] = mapped_column(String(20))       # pin, qr, senha
    credencial_hash: Mapped[str] = mapped_column(String(200))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class Consentimento(Base):
    __tablename__ = "consentimento"
    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"))
    tipo: Mapped[str] = mapped_column(String(60))
    versao_termo: Mapped[str] = mapped_column(String(20))
    data: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    registrado_por: Mapped[int] = mapped_column(ForeignKey("usuario.id"))


class MetaPaciente(Base):
    """Principais metas do paciente (até 5): uma métrica de um jogo que o profissional quer
    acompanhar de perto, com valor-alvo opcional (ou "chegar ao braço sadio")."""
    __tablename__ = "meta_paciente"
    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"))
    ordem: Mapped[int] = mapped_column(Integer)
    jogo: Mapped[str] = mapped_column(String(20))
    metrica: Mapped[str] = mapped_column(String(40))
    alvo: Mapped[float | None] = mapped_column(Float)
    alvo_braco_sadio: Mapped[bool] = mapped_column(Boolean, default=False)
    observacao: Mapped[str | None] = mapped_column(TextoCifrado)
    profissional_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"))
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)


class AvaliacaoClinica(Base):
    """Avaliação clínica (inicial e reavaliações), para comparar com as métricas dos sensores."""
    __tablename__ = "avaliacao_clinica"
    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"))
    data: Mapped[date] = mapped_column(Date)
    profissional_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"))
    ashworth_punho: Mapped[str | None] = mapped_column(String(3))      # 0, 1, 1+, 2, 3, 4
    ashworth_dedos: Mapped[str | None] = mapped_column(String(3))
    ashworth_cotovelo: Mapped[str | None] = mapped_column(String(3))
    preensao_acometido_kgf: Mapped[float | None] = mapped_column(Float)
    preensao_nao_acometido_kgf: Mapped[float | None] = mapped_column(Float)
    fugl_meyer_ms: Mapped[int | None] = mapped_column(Integer)          # 0 a 66
    outras_escalas: Mapped[str | None] = mapped_column(TextoCifrado)
    observacoes: Mapped[str | None] = mapped_column(TextoCifrado)
    inicial: Mapped[bool] = mapped_column(Boolean, default=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class Dispositivo(Base):
    """Joysticks A, B, C e a unidade de bancada."""
    __tablename__ = "dispositivo"
    id: Mapped[int] = mapped_column(primary_key=True)
    tipo: Mapped[str] = mapped_column(String(20))            # joystick | bancada
    identificador: Mapped[str] = mapped_column(String(20), unique=True)   # A, B, C, bancada
    caminho: Mapped[str | None] = mapped_column(String(30))
    sensores: Mapped[dict] = mapped_column(JSON, default=dict)
    versao_firmware: Mapped[str | None] = mapped_column(String(20))
    mac: Mapped[str | None] = mapped_column(String(20))
    atualizado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class Calibracao(Base):
    """Versionada: uma calibração nova é uma linha nova (nunca editada no lugar)."""
    __tablename__ = "calibracao"
    __table_args__ = (UniqueConstraint("tipo", "caminho", "versao"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dispositivo_id: Mapped[int] = mapped_column(ForeignKey("dispositivo.id"))
    tipo: Mapped[str] = mapped_column(String(40))            # vl53_reto | tabela_transicoes | giro_arco | imu
    caminho: Mapped[str] = mapped_column(String(30), default="")
    versao: Mapped[int] = mapped_column(Integer)
    dados: Mapped[dict] = mapped_column(JSON)
    data: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    responsavel_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"))
    padrao: Mapped[bool] = mapped_column(Boolean, default=False)   # valores provisórios de fábrica


class Sessao(Base):
    __tablename__ = "sessao"
    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"))
    dispositivo_id: Mapped[int] = mapped_column(ForeignKey("dispositivo.id"))
    jogo: Mapped[str] = mapped_column(String(20))
    dificuldade: Mapped[dict] = mapped_column(JSON, default=dict)
    iniciada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    ultima_atividade: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    encerrada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profissional_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"))
    calibracoes: Mapped[dict] = mapped_column(JSON, default=dict)   # tipo -> id da calibração vigente
    partidas: Mapped[list["Partida"]] = relationship(back_populates="sessao", order_by="Partida.numero")


class Partida(Base):
    __tablename__ = "partida"
    id: Mapped[int] = mapped_column(primary_key=True)
    sessao_id: Mapped[int] = mapped_column(ForeignKey("sessao.id"))
    numero: Mapped[int] = mapped_column(Integer)
    inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    fim: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="em_andamento")   # em_andamento | valida | descartada
    motivo: Mapped[str | None] = mapped_column(String(200))
    fonte: Mapped[str] = mapped_column(String(20))           # sensores | mouse
    # Braço usado nesta partida, para comparar o lado acometido com o sadio do mesmo paciente.
    # Partidas antigas (vazio) foram todas com o braço acometido.
    membro: Mapped[str | None] = mapped_column(String(20))       # acometido | sadio
    lado_braco: Mapped[str | None] = mapped_column(String(20))   # direito | esquerdo (o braço que jogou)
    pontos: Mapped[int | None] = mapped_column(Integer)
    estrelas: Mapped[int | None] = mapped_column(Integer)
    destaque: Mapped[str | None] = mapped_column(String(300))
    sessao: Mapped[Sessao] = relationship(back_populates="partidas")
    resultado: Mapped["Resultado"] = relationship(uselist=False, back_populates="partida")
    dados_brutos: Mapped[list["DadoBruto"]] = relationship(back_populates="partida")


class DadoBruto(Base):
    __tablename__ = "dado_bruto"
    id: Mapped[int] = mapped_column(primary_key=True)
    partida_id: Mapped[int] = mapped_column(ForeignKey("partida.id"))
    tipo: Mapped[str] = mapped_column(String(20))            # imu | sync | bordas | vl53 | series
    arquivo: Mapped[str] = mapped_column(String(300))
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    versao_formato: Mapped[int] = mapped_column(Integer, default=1)
    n_registros: Mapped[int] = mapped_column(Integer)
    partida: Mapped[Partida] = relationship(back_populates="dados_brutos")


class Resultado(Base):
    __tablename__ = "resultado"
    id: Mapped[int] = mapped_column(primary_key=True)
    partida_id: Mapped[int] = mapped_column(ForeignKey("partida.id"), unique=True)
    versao_algoritmo: Mapped[str] = mapped_column(String(60))
    metricas: Mapped[dict] = mapped_column(JSON, default=dict)        # movimento (servidor)
    metricas_jogo: Mapped[dict] = mapped_column(JSON, default=dict)   # do mini-game
    qualidade: Mapped[dict] = mapped_column(JSON, default=dict)
    eventos_jogo: Mapped[list] = mapped_column(JSON, default=list)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    partida: Mapped[Partida] = relationship(back_populates="resultado")


class Auditoria(Base):
    """Somente inclusão: gatilhos no SQLite impedem UPDATE e DELETE."""
    __tablename__ = "auditoria"
    id: Mapped[int] = mapped_column(primary_key=True)
    quando: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    acao: Mapped[str] = mapped_column(String(60))
    recurso: Mapped[str] = mapped_column(String(80))
    paciente_id: Mapped[int | None] = mapped_column(Integer)
    detalhes: Mapped[dict] = mapped_column(JSON, default=dict)


GATILHOS_AUDITORIA = [
    """CREATE TRIGGER IF NOT EXISTS auditoria_sem_update BEFORE UPDATE ON auditoria
       BEGIN SELECT RAISE(ABORT, 'auditoria é somente inclusão'); END""",
    """CREATE TRIGGER IF NOT EXISTS auditoria_sem_delete BEFORE DELETE ON auditoria
       BEGIN SELECT RAISE(ABORT, 'auditoria é somente inclusão'); END""",
]
