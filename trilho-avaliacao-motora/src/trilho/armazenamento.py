"""Armazenamento local: SQLite (cadastros e métricas) + Parquet (dados brutos).

Princípios do modelo (preparação para login, ver arquitetura):

1. Tudo é feito em nome de um usuário. Na versão 1 existe só o "operador
   local", criado na instalação.
2. Um ponto único de autorização: ``autorizar(usuario, acao, recurso)``. Na
   versão 1 ele sempre permite; no futuro aplica as regras por perfil.
3. Acesso ao banco só por esta camada de repositórios (nenhuma tela ou jogo lê
   o banco diretamente).
4. Identificação do paciente separada e criptografada; medições e resultados
   usam só o id interno (pseudonimização, LGPD).
5. Auditoria ligada desde a versão 1 e imutável (gatilhos impedem alteração).
6. Contexto de sessão do aplicativo com usuário e paciente correntes.

O SQLite local pode ser trocado por um servidor sem mudar o resto do código.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from cryptography.fernet import Fernet

from . import VERSAO_ALGORITMO
from .dados import DadosBrutos

ESQUEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS perfil (
    id TEXT PRIMARY KEY, nome TEXT UNIQUE NOT NULL, permissoes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usuario (
    id TEXT PRIMARY KEY, nome TEXT NOT NULL, email TEXT, perfil_id TEXT REFERENCES perfil(id),
    ativo INTEGER NOT NULL DEFAULT 1, senha_hash TEXT, criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paciente (
    id TEXT PRIMARY KEY, codigo TEXT UNIQUE NOT NULL, lado_acometido TEXT, dominancia TEXT,
    inicio_tratamento TEXT, criado_em TEXT NOT NULL
);
-- dados identificáveis: tabela separada, campos criptografados
CREATE TABLE IF NOT EXISTS identificacao_paciente (
    paciente_id TEXT PRIMARY KEY REFERENCES paciente(id), dados_cripto BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS conta_paciente (
    paciente_id TEXT PRIMARY KEY REFERENCES paciente(id), credencial_hash TEXT,
    tipo TEXT, ativo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS consentimento (
    id TEXT PRIMARY KEY, paciente_id TEXT NOT NULL REFERENCES paciente(id), tipo TEXT NOT NULL,
    versao_termo TEXT NOT NULL, data TEXT NOT NULL, registrado_por TEXT REFERENCES usuario(id)
);
CREATE TABLE IF NOT EXISTS dispositivo (
    id TEXT PRIMARY KEY, tipo TEXT NOT NULL, sensores TEXT, versao_firmware TEXT
);
CREATE TABLE IF NOT EXISTS calibracao (
    id TEXT PRIMARY KEY, dispositivo_id TEXT NOT NULL REFERENCES dispositivo(id),
    tipo TEXT NOT NULL, data TEXT NOT NULL, versao TEXT NOT NULL, dados TEXT NOT NULL,
    responsavel TEXT REFERENCES usuario(id)
);
CREATE TABLE IF NOT EXISTS sessao (
    id TEXT PRIMARY KEY, paciente_id TEXT NOT NULL REFERENCES paciente(id),
    joystick_id TEXT REFERENCES dispositivo(id), caminho TEXT NOT NULL, jogo TEXT,
    dificuldade TEXT, data TEXT NOT NULL, profissional_id TEXT REFERENCES usuario(id),
    calibracao_id TEXT REFERENCES calibracao(id), observacoes TEXT
);
CREATE TABLE IF NOT EXISTS partida (
    id TEXT PRIMARY KEY, sessao_id TEXT NOT NULL REFERENCES sessao(id), numero INTEGER NOT NULL,
    inicio TEXT, fim TEXT, status TEXT NOT NULL DEFAULT 'valida', motivo TEXT
);
CREATE TABLE IF NOT EXISTS dado_bruto (
    partida_id TEXT PRIMARY KEY REFERENCES partida(id), pasta TEXT NOT NULL,
    checksum TEXT NOT NULL, versao_formato INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resultado (
    id TEXT PRIMARY KEY, partida_id TEXT NOT NULL REFERENCES partida(id),
    versao_algoritmo TEXT NOT NULL, arquivo_series TEXT, metricas TEXT NOT NULL,
    qualidade TEXT NOT NULL, criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auditoria (
    id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id TEXT, acao TEXT NOT NULL,
    recurso TEXT NOT NULL, paciente_id TEXT, quando TEXT NOT NULL, detalhe TEXT
);
CREATE TRIGGER IF NOT EXISTS auditoria_sem_update BEFORE UPDATE ON auditoria
BEGIN SELECT RAISE(ABORT, 'auditoria é imutável'); END;
CREATE TRIGGER IF NOT EXISTS auditoria_sem_delete BEFORE DELETE ON auditoria
BEGIN SELECT RAISE(ABORT, 'auditoria é imutável'); END;
"""

PERFIS = {
    "administrador": ["usuarios", "perfis", "equipamentos", "backup"],
    "fisioterapeuta": ["pacientes", "sessoes", "historico", "relatorios"],
    "tecnico": ["calibracao", "qualidade"],
    "paciente": ["proprio_progresso"],
}


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id() -> str:
    return uuid.uuid4().hex


class AcessoNegado(PermissionError):
    pass


def autorizar(usuario: dict, acao: str, recurso: str) -> bool:
    """Ponto único de autorização. Versão 1: sempre permite.

    No futuro: consultar PERFIS[usuario['perfil']] e o vínculo com o paciente.
    """
    return True


@dataclass
class ContextoApp:
    """Usuário e paciente correntes, usados por todas as telas."""

    usuario: dict
    paciente_id: str | None = None


class Banco:
    """Repositórios sobre o SQLite local + pasta de dados brutos."""

    def __init__(self, pasta: str | Path) -> None:
        self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)
        (self.pasta / "brutos").mkdir(exist_ok=True)
        (self.pasta / "series").mkdir(exist_ok=True)
        self.con = sqlite3.connect(self.pasta / "clinica.sqlite3")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(ESQUEMA)
        self._fernet = Fernet(self._chave())
        self._instalar()

    # ------------------------------------------------------------ infraestrutura
    def _chave(self) -> bytes:
        # Em produção a chave fica protegida pelo Windows (DPAPI) ou o banco
        # inteiro usa SQLCipher; aqui fica num arquivo ao lado do banco.
        arq = self.pasta / "chave.fernet"
        if not arq.exists():
            arq.write_bytes(Fernet.generate_key())
            arq.chmod(0o600)
        return arq.read_bytes()

    def _instalar(self) -> None:
        with self.con:
            for nome, perm in PERFIS.items():
                self.con.execute("INSERT OR IGNORE INTO perfil VALUES (?, ?, ?)",
                                 (nome, nome, json.dumps(perm)))
            if not self.con.execute("SELECT 1 FROM usuario").fetchone():
                self.con.execute(
                    "INSERT INTO usuario (id, nome, perfil_id, criado_em) VALUES (?, ?, ?, ?)",
                    ("operador-local", "Operador local", "administrador", _agora()))

    def operador_local(self) -> dict:
        r = self.con.execute("SELECT * FROM usuario WHERE id = 'operador-local'").fetchone()
        return {**dict(r), "perfil": r["perfil_id"]}

    @contextmanager
    def _acao(self, ctx: ContextoApp, acao: str, recurso: str, paciente_id: str | None = None,
              detalhe: dict | None = None):
        if not autorizar(ctx.usuario, acao, recurso):
            raise AcessoNegado(f"{ctx.usuario['id']} não pode {acao} {recurso}")
        with self.con:
            yield
            self.con.execute(
                "INSERT INTO auditoria (usuario_id, acao, recurso, paciente_id, quando, detalhe)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ctx.usuario["id"], acao, recurso, paciente_id, _agora(),
                 json.dumps(detalhe, ensure_ascii=False) if detalhe else None))

    # ---------------------------------------------------------------- pacientes
    def cadastrar_paciente(self, ctx: ContextoApp, nome: str, nascimento: str | None = None,
                           documento: str | None = None, contato: str | None = None,
                           lado_acometido: str | None = None, dominancia: str | None = None,
                           consentimento_versao: str = "1.0") -> str:
        pid = _id()
        n = self.con.execute("SELECT COUNT(*) FROM paciente").fetchone()[0] + 1
        codigo = f"P{n:04d}"
        ident = json.dumps({"nome": nome, "nascimento": nascimento, "documento": documento,
                            "contato": contato}, ensure_ascii=False).encode()
        with self._acao(ctx, "criar", "paciente", pid):
            self.con.execute(
                "INSERT INTO paciente VALUES (?, ?, ?, ?, ?, ?)",
                (pid, codigo, lado_acometido, dominancia, date.today().isoformat(), _agora()))
            self.con.execute("INSERT INTO identificacao_paciente VALUES (?, ?)",
                             (pid, self._fernet.encrypt(ident)))
            self.con.execute("INSERT INTO conta_paciente (paciente_id) VALUES (?)", (pid,))
            self.con.execute("INSERT INTO consentimento VALUES (?, ?, ?, ?, ?, ?)",
                             (_id(), pid, "tratamento", consentimento_versao,
                              date.today().isoformat(), ctx.usuario["id"]))
        return pid

    def paciente(self, ctx: ContextoApp, pid: str, identificar: bool = False) -> dict:
        r = self.con.execute("SELECT * FROM paciente WHERE id = ?", (pid,)).fetchone()
        if r is None:
            raise KeyError(pid)
        d = dict(r)
        if identificar:
            with self._acao(ctx, "ler_identificacao", "paciente", pid):
                blob = self.con.execute(
                    "SELECT dados_cripto FROM identificacao_paciente WHERE paciente_id = ?",
                    (pid,)).fetchone()[0]
            d.update(json.loads(self._fernet.decrypt(blob)))
        return d

    def listar_pacientes(self, ctx: ContextoApp) -> list[dict]:
        if not autorizar(ctx.usuario, "listar", "paciente"):
            raise AcessoNegado("listar pacientes")
        return [dict(r) for r in self.con.execute("SELECT * FROM paciente ORDER BY codigo")]

    # ------------------------------------------------- dispositivos/calibração
    def registrar_dispositivo(self, did: str, tipo: str, sensores: list[str],
                              versao_firmware: str) -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO dispositivo VALUES (?, ?, ?, ?)",
                             (did, tipo, json.dumps(sensores), versao_firmware))

    def registrar_calibracao(self, ctx: ContextoApp, dispositivo_id: str, tipo: str,
                             versao: str, dados: dict) -> str:
        cid = _id()
        with self._acao(ctx, "calibrar", f"dispositivo:{dispositivo_id}", detalhe={"tipo": tipo}):
            self.con.execute("INSERT INTO calibracao VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (cid, dispositivo_id, tipo, _agora(), versao,
                              json.dumps(dados, ensure_ascii=False), ctx.usuario["id"]))
        return cid

    def calibracao_vigente(self, dispositivo_id: str, tipo: str) -> dict | None:
        r = self.con.execute(
            "SELECT * FROM calibracao WHERE dispositivo_id = ? AND tipo = ? ORDER BY data DESC, rowid DESC",
            (dispositivo_id, tipo)).fetchone()
        return None if r is None else {**dict(r), "dados": json.loads(r["dados"])}

    # ------------------------------------------------------ sessões e partidas
    def nova_sessao(self, ctx: ContextoApp, paciente_id: str, joystick_id: str, caminho: str,
                    jogo: str | None = None, dificuldade: dict | None = None,
                    calibracao_id: str | None = None, data: str | None = None) -> str:
        sid = _id()
        with self._acao(ctx, "criar", "sessao", paciente_id):
            self.con.execute(
                "INSERT INTO sessao VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, paciente_id, joystick_id, caminho, jogo,
                 json.dumps(dificuldade or {}), data or _agora(), ctx.usuario["id"],
                 calibracao_id, None))
        return sid

    def salvar_partida(self, ctx: ContextoApp, sessao_id: str, numero: int,
                       dados: DadosBrutos, status: str = "valida",
                       motivo: str | None = None) -> str:
        pid = _id()
        pasta = self.pasta / "brutos" / sessao_id / f"partida_{numero:02d}"
        checksum = dados.salvar(pasta)
        sess = self.con.execute("SELECT paciente_id FROM sessao WHERE id = ?", (sessao_id,)).fetchone()
        with self._acao(ctx, "gravar", "partida", sess["paciente_id"]):
            self.con.execute("INSERT INTO partida VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (pid, sessao_id, numero, _agora(), _agora(), status, motivo))
            self.con.execute("INSERT INTO dado_bruto VALUES (?, ?, ?, ?)",
                             (pid, str(pasta.relative_to(self.pasta)), checksum,
                              dados.meta.get("versao_formato", 1)))
        return pid

    def carregar_bruto(self, partida_id: str) -> DadosBrutos:
        r = self.con.execute("SELECT pasta FROM dado_bruto WHERE partida_id = ?",
                             (partida_id,)).fetchone()
        return DadosBrutos.carregar(self.pasta / r["pasta"])

    def salvar_resultado(self, ctx: ContextoApp, partida_id: str, serie: pd.DataFrame,
                         por_tentativa: pd.DataFrame, qualidade: dict,
                         versao_algoritmo: str = VERSAO_ALGORITMO) -> str:
        rid = _id()
        arq = self.pasta / "series" / f"{partida_id}_{rid[:8]}.parquet"
        serie.to_parquet(arq, index=False)
        metricas = por_tentativa.to_dict(orient="records")
        with self._acao(ctx, "processar", "partida", detalhe={"versao": versao_algoritmo}):
            self.con.execute("INSERT INTO resultado VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (rid, partida_id, versao_algoritmo,
                              str(arq.relative_to(self.pasta)),
                              json.dumps(metricas, default=_json_padrao),
                              json.dumps(qualidade, default=_json_padrao), _agora()))
        return rid

    def sessoes_do_paciente(self, ctx: ContextoApp, paciente_id: str,
                            caminho: str | None = None) -> list[dict]:
        if not autorizar(ctx.usuario, "ler", "sessao"):
            raise AcessoNegado("ler sessões")
        q = "SELECT * FROM sessao WHERE paciente_id = ?"
        args: list = [paciente_id]
        if caminho:
            q += " AND caminho = ?"
            args.append(caminho)
        return [dict(r) for r in self.con.execute(q + " ORDER BY data", args)]

    def resultados_da_sessao(self, sessao_id: str) -> list[dict]:
        """Último resultado de cada partida válida da sessão."""
        rows = self.con.execute(
            """SELECT p.id AS partida_id, p.numero, r.metricas, r.qualidade, r.versao_algoritmo,
                      r.arquivo_series
               FROM partida p JOIN resultado r ON r.partida_id = p.id
               WHERE p.sessao_id = ? AND p.status = 'valida'
               AND r.criado_em = (SELECT MAX(criado_em) FROM resultado WHERE partida_id = p.id)
               ORDER BY p.numero""", (sessao_id,)).fetchall()
        return [{**dict(r), "metricas": json.loads(r["metricas"]),
                 "qualidade": json.loads(r["qualidade"])} for r in rows]

    def serie(self, arquivo: str) -> pd.DataFrame:
        return pd.read_parquet(self.pasta / arquivo)

    def auditoria(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM auditoria ORDER BY id")]

    def fechar(self) -> None:
        self.con.close()


def _json_padrao(o):
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, float) and o != o:
        return None
    return str(o)
