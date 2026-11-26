"""Login dos profissionais.

- Senhas com Argon2 (argon2-cffi); mínimo de 8 caracteres, com letras e números.
- Sessão por cookie HttpOnly + SameSite=Strict; no banco fica só o SHA-256 do token.
- A sessão expira após 30 min sem uso e, de qualquer forma, após 12 h. Durante uma
  partida em andamento a sessão é mantida viva (a partida não pode se perder).
- 5 senhas erradas seguidas bloqueiam a conta por 15 min.
- Primeiro acesso: enquanto não houver nenhum usuário com senha, a tela de login
  pede para criar o administrador.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .autorizacao import Contexto
from .banco.modelos import Auditoria, Perfil, SessaoLogin, Usuario

COOKIE = "trilho_sessao"
INATIVIDADE = timedelta(minutes=30)
DURACAO_MAXIMA = timedelta(hours=12)
MAX_TENTATIVAS = 5
BLOQUEIO = timedelta(minutes=15)

_hasher = PasswordHasher()


class ErroLogin(Exception):
    pass


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validar_senha(senha: str) -> None:
    if len(senha or "") < 8 or not re.search(r"[A-Za-z]", senha) or not re.search(r"\d", senha):
        raise ErroLogin("a senha precisa ter pelo menos 8 caracteres, com letras e números")


def gerar_hash(senha: str) -> str:
    validar_senha(senha)
    return _hasher.hash(senha)


def normalizar_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise ErroLogin("e-mail inválido")
    return e


def _auditar(s: Session, usuario_id: int | None, acao: str, **det) -> None:
    s.add(Auditoria(usuario_id=usuario_id, acao=acao, recurso="login", detalhes=det))


def precisa_primeiro_acesso(s: Session) -> bool:
    return s.scalar(select(Usuario).where(Usuario.senha_hash.is_not(None), Usuario.ativo).limit(1)) is None


def criar_primeiro_admin(s: Session, nome: str, email: str, senha: str, registro: str | None) -> Usuario:
    if not precisa_primeiro_acesso(s):
        raise ErroLogin("o administrador já foi criado")
    if not (nome or "").strip():
        raise ErroLogin("informe o nome")
    adm = s.scalar(select(Perfil).where(Perfil.nome == "administrador"))
    u = Usuario(nome=nome.strip(), email=normalizar_email(email), perfil_id=adm.id, senha_hash=gerar_hash(senha),
                registro_profissional=(registro or "").strip() or None, deve_trocar_senha=False, tentativas_falhas=0)
    s.add(u)
    s.flush()
    _auditar(s, u.id, "primeiro_acesso")
    return u


def entrar(s: Session, email: str, senha: str) -> tuple[Usuario, str]:
    """Confere a senha e abre uma sessão. Devolve (usuário, token do cookie)."""
    agora = _agora()
    try:
        email = normalizar_email(email)
    except ErroLogin:
        raise ErroLogin("e-mail ou senha incorretos")
    u = s.scalar(select(Usuario).where(Usuario.email == email))
    if u is None or not u.ativo or not u.senha_hash:
        _hasher.hash("tempo-constante")   # não revela se o e-mail existe
        _auditar(s, None, "login_falhou", email=email)
        raise ErroLogin("e-mail ou senha incorretos")
    if u.bloqueado_ate and _utc(u.bloqueado_ate) > agora:
        minutos = int((_utc(u.bloqueado_ate) - agora).total_seconds() // 60) + 1
        _auditar(s, u.id, "login_bloqueado")
        raise ErroLogin(f"conta bloqueada por tentativas erradas; tente de novo em {minutos} min")
    try:
        _hasher.verify(u.senha_hash, senha or "")
    except (VerificationError, InvalidHashError):
        u.tentativas_falhas = (u.tentativas_falhas or 0) + 1
        if u.tentativas_falhas >= MAX_TENTATIVAS:
            u.bloqueado_ate = agora + BLOQUEIO
            u.tentativas_falhas = 0
            _auditar(s, u.id, "conta_bloqueada")
        _auditar(s, u.id, "login_falhou")
        raise ErroLogin("e-mail ou senha incorretos")
    if _hasher.check_needs_rehash(u.senha_hash):
        u.senha_hash = _hasher.hash(senha)
    u.tentativas_falhas = 0
    u.bloqueado_ate = None
    u.ultimo_login = agora
    token = secrets.token_urlsafe(32)
    s.add(SessaoLogin(usuario_id=u.id, token_hash=_hash_token(token), criada_em=agora, ultimo_uso=agora,
                      expira_em=agora + DURACAO_MAXIMA))
    _auditar(s, u.id, "login")
    return u, token


def sessao_do_token(s: Session, token: str | None, tocar: bool = True) -> SessaoLogin | None:
    if not token:
        return None
    sl = s.scalar(select(SessaoLogin).where(SessaoLogin.token_hash == _hash_token(token)))
    if sl is None or sl.encerrada:
        return None
    agora = _agora()
    if _utc(sl.expira_em) < agora or agora - _utc(sl.ultimo_uso) > INATIVIDADE or not sl.usuario.ativo:
        sl.encerrada = True
        return None
    if tocar:
        sl.ultimo_uso = agora
    return sl


def contexto(sl: SessaoLogin) -> Contexto:
    u = sl.usuario
    return Contexto(u.id, u.perfil.nome, u.nome)


def sair(s: Session, token: str | None) -> None:
    sl = sessao_do_token(s, token, tocar=False)
    if sl is not None:
        sl.encerrada = True
        _auditar(s, sl.usuario_id, "logout")


def trocar_senha(s: Session, usuario_id: int, atual: str, nova: str) -> None:
    u = s.get(Usuario, usuario_id)
    try:
        _hasher.verify(u.senha_hash, atual or "")
    except (VerificationError, InvalidHashError):
        raise ErroLogin("senha atual incorreta")
    if atual == nova:
        raise ErroLogin("a nova senha precisa ser diferente da atual")
    u.senha_hash = gerar_hash(nova)
    u.deve_trocar_senha = False
    _auditar(s, u.id, "trocar_senha")


def encerrar_sessoes(s: Session, usuario_id: int, exceto_token: str | None = None) -> None:
    manter = _hash_token(exceto_token) if exceto_token else None
    for sl in s.scalars(select(SessaoLogin).where(SessaoLogin.usuario_id == usuario_id,
                                                  SessaoLogin.encerrada.is_(False))).all():
        if sl.token_hash != manter:
            sl.encerrada = True
