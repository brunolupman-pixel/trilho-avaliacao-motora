"""Ponto único de autorização.

Toda ação sobre dados passa por autorizar(ctx, acao, ...). Duas camadas:
1. perfil: o que cada tipo de profissional pode fazer (PERMISSOES);
2. paciente: fisioterapeutas só acessam os pacientes vinculados a eles
   (verificado em banco/repositorios.py, função _exigir_acesso_paciente).
"""
from __future__ import annotations

from dataclasses import dataclass

LOGIN_ATIVO = True


class PermissaoNegada(Exception):
    pass


@dataclass(frozen=True)
class Contexto:
    """Usuário corrente da requisição."""
    usuario_id: int
    perfil: str
    nome: str = ""

    @property
    def ve_todos_pacientes(self) -> bool:
        return self.perfil == "administrador"


PERMISSOES: dict[str, set[str]] = {
    "administrador": {"*"},
    "fisioterapeuta": {"paciente.ler", "paciente.criar", "paciente.editar", "avaliacao.ler", "avaliacao.criar",
                       "partida.criar", "partida.finalizar", "historico.ler", "relatorio.exportar",
                       "calibracao.ler", "calibracao.reto", "hardware.ler", "hardware.reconectar"},
    "tecnico": {"calibracao.ler", "calibracao.criar", "calibracao.reto", "hardware.ler", "hardware.reconectar"},
    "paciente": {"historico.proprio.ler"},   # portal do paciente (futuro, na nuvem)
}
NOMES_PERFIS = {"administrador": "Administrador", "fisioterapeuta": "Fisioterapeuta", "tecnico": "Técnico"}

# Nome antigo, mantido para a carga inicial.
PERMISSOES_PREVISTAS = PERMISSOES


def pode(ctx: Contexto, acao: str) -> bool:
    regras = PERMISSOES.get(ctx.perfil, set())
    return "*" in regras or acao in regras


def autorizar(ctx: Contexto, acao: str, recurso: str = "", paciente_id: int | None = None) -> None:
    if not LOGIN_ATIVO:
        return
    if not pode(ctx, acao):
        raise PermissaoNegada(f"o perfil {NOMES_PERFIS.get(ctx.perfil, ctx.perfil)} não tem permissão para esta ação")
