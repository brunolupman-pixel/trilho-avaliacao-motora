"""Camada de repositórios: o único lugar que lê e escreve no banco.

Nenhuma tela ou jogo acessa o banco direto (arquitetura: "Princípios do modelo
inicial"). Toda função recebe o Contexto do usuário corrente, chama
autorizar() e registra a ação na auditoria.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..autorizacao import Contexto, PermissaoNegada, autorizar
from ..caminhos import JOGO_CAMINHO, CAMINHOS
from .modelos import (Auditoria, AvaliacaoClinica, Calibracao, MetaPaciente, Consentimento, DadoBruto, Dispositivo,
                      IdentificacaoPaciente, Paciente, Partida, Perfil, Resultado, Sessao, Usuario, VinculoPaciente, agora)

VERSAO_TERMO = "v1"
NOMES_JOGOS = {"bolo": "Mexer a massa de bolo", "carro": "Pilotar o carro", "vidro": "Limpar a janela"}

def _auditar(s: Session, ctx: Contexto, acao: str, recurso: str, paciente_id: int | None = None, **detalhes) -> None:
    s.add(Auditoria(usuario_id=ctx.usuario_id, acao=acao, recurso=recurso, paciente_id=paciente_id, detalhes=detalhes))


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------- acesso a pacientes
def _ids_visiveis(s: Session, ctx: Contexto) -> set[int] | None:
    """None = todos (administrador); senão, os pacientes vinculados ao profissional."""
    if ctx.ve_todos_pacientes:
        return None
    return set(s.scalars(select(VinculoPaciente.paciente_id).where(VinculoPaciente.usuario_id == ctx.usuario_id)).all())


def _exigir_acesso_paciente(s: Session, ctx: Contexto, pid: int) -> Paciente:
    p = s.get(Paciente, pid)
    if p is None or not p.ativo:
        raise LookupError("paciente não encontrado")
    ids = _ids_visiveis(s, ctx)
    if ids is not None and pid not in ids:
        raise PermissaoNegada("este paciente não está vinculado a você; peça ao administrador")
    return p


# ---------------------------------------------------------------- ficha do paciente
CONDICOES = {
    "avc_isquemico": "AVC isquêmico", "avc_hemorragico": "AVC hemorrágico", "paralisia_cerebral": "Paralisia cerebral",
    "tce": "Traumatismo cranioencefálico", "lesao_medular": "Lesão medular", "esclerose_multipla": "Esclerose múltipla",
    "outra": "Outra",
}
ASHWORTH = ["0", "1", "1+", "2", "3", "4"]


def _data(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ValueError(f"data inválida: {v}")


def _texto(v, limite: int = 2000) -> str | None:
    v = (v or "").strip() if isinstance(v, str) else v
    return v[:limite] if v else None


def _numero(v, nome: str, minimo: float, maximo: float, inteiro: bool = False):
    if v in (None, ""):
        return None
    try:
        n = int(v) if inteiro else float(str(v).replace(",", "."))
    except ValueError:
        raise ValueError(f"{nome}: valor inválido")
    if not minimo <= n <= maximo:
        raise ValueError(f"{nome}: deve ficar entre {minimo:g} e {maximo:g}")
    return n


def _aplicar_ficha(p: Paciente, d: dict) -> None:
    if "lado" in d:
        if d["lado"] not in ("direito", "esquerdo", "bilateral"):
            raise ValueError("lado acometido inválido")
        p.lado_acometido = d["lado"]
    if "dominancia" in d:
        if d["dominancia"] not in (None, "", "direita", "esquerda", "ambidestro"):
            raise ValueError("mão dominante inválida")
        p.dominancia = d["dominancia"] or None
    if "condicao" in d:
        if d["condicao"] not in (None, "", *CONDICOES):
            raise ValueError("condição inválida")
        p.condicao = d["condicao"] or None
    if "condicao_detalhe" in d:
        p.condicao_detalhe = _texto(d["condicao_detalhe"], 300)
    if "data_lesao" in d:
        dl = _data(d["data_lesao"])
        if dl and dl > date.today():
            raise ValueError("a data da lesão não pode estar no futuro")
        p.data_lesao = dl
    if "toxina_botulinica" in d:
        p.toxina_botulinica = None if d["toxina_botulinica"] is None else bool(d["toxina_botulinica"])
    if "toxina_ultima_data" in d:
        p.toxina_ultima_data = _data(d["toxina_ultima_data"])
    for campo in ("toxina_musculos", "medicacoes", "objetivos", "observacoes"):
        if campo in d:
            setattr(p, campo, _texto(d[campo]))
    p.atualizado_em = agora()


def _ficha_dict(p: Paciente) -> dict:
    return {"lado": p.lado_acometido, "dominancia": p.dominancia, "condicao": p.condicao,
            "condicao_nome": CONDICOES.get(p.condicao or "", None), "condicao_detalhe": p.condicao_detalhe,
            "data_lesao": p.data_lesao.isoformat() if p.data_lesao else None,
            "toxina_botulinica": p.toxina_botulinica,
            "toxina_ultima_data": p.toxina_ultima_data.isoformat() if p.toxina_ultima_data else None,
            "toxina_musculos": p.toxina_musculos, "medicacoes": p.medicacoes, "objetivos": p.objetivos,
            "observacoes": p.observacoes, "inicio_tratamento": p.inicio_tratamento.isoformat() if p.inicio_tratamento else None,
            "atualizado_em": _iso(p.atualizado_em)}


def _paciente_dict(s: Session, p: Paciente) -> dict:
    n = s.scalar(select(func.count(Partida.id)).join(Sessao).where(Sessao.paciente_id == p.id,
                                                                   Partida.status == "valida")) or 0
    return {"id": p.id, "nome": p.identificacao.nome if p.identificacao else "?", "lado": p.lado_acometido,
            "preferencias": p.preferencias or {}, "n_partidas": n, "criado_em": _iso(p.criado_em)}


def listar_pacientes(s: Session, ctx: Contexto) -> list[dict]:
    autorizar(ctx, "paciente.ler", "paciente")
    q = select(Paciente).where(Paciente.ativo).order_by(Paciente.id)
    ids = _ids_visiveis(s, ctx)
    if ids is not None:
        q = q.where(Paciente.id.in_(ids))
    ps = s.scalars(q).all()
    _auditar(s, ctx, "listar", "paciente")
    return sorted((_paciente_dict(s, p) for p in ps), key=lambda x: x["nome"].lower())


def criar_paciente(s: Session, ctx: Contexto, dados: dict) -> dict:
    """Cria o paciente com a ficha e (se veio) a avaliação clínica inicial.
    Quem cadastra fica vinculado ao paciente."""
    autorizar(ctx, "paciente.criar", "paciente")
    nome = (dados.get("nome") or "").strip()
    if not nome:
        raise ValueError("nome obrigatório")
    if not dados.get("consentimento"):
        raise ValueError("é preciso registrar o consentimento (LGPD) antes de cadastrar")
    p = Paciente(lado_acometido="direito")
    _aplicar_ficha(p, {**dados, "lado": dados.get("lado", "direito")})
    p.identificacao = IdentificacaoPaciente(nome=nome[:120])
    s.add(p)
    s.flush()
    s.add(Consentimento(paciente_id=p.id, tipo="uso_clinico_dos_dados", versao_termo=VERSAO_TERMO,
                        registrado_por=ctx.usuario_id))
    s.add(VinculoPaciente(paciente_id=p.id, usuario_id=ctx.usuario_id))
    _auditar(s, ctx, "criar", "paciente", p.id)
    aval = dados.get("avaliacao_inicial")
    if aval and any(v not in (None, "") for k, v in aval.items() if k != "data"):
        criar_avaliacao(s, ctx, p.id, {**aval, "inicial": True})
    return _paciente_dict(s, p)


def obter_paciente(s: Session, ctx: Contexto, pid: int) -> dict:
    autorizar(ctx, "paciente.ler", "paciente", pid)
    p = _exigir_acesso_paciente(s, ctx, pid)
    resp = s.scalars(select(Usuario).join(VinculoPaciente, VinculoPaciente.usuario_id == Usuario.id)
                     .where(VinculoPaciente.paciente_id == pid)).all()
    _auditar(s, ctx, "ler_ficha", "paciente", pid)
    return {**_paciente_dict(s, p), "ficha": _ficha_dict(p),
            "responsaveis": [{"id": u.id, "nome": u.nome} for u in resp]}


def atualizar_ficha(s: Session, ctx: Contexto, pid: int, dados: dict) -> dict:
    autorizar(ctx, "paciente.editar", "paciente", pid)
    p = _exigir_acesso_paciente(s, ctx, pid)
    if "nome" in dados and (dados["nome"] or "").strip():
        p.identificacao.nome = dados["nome"].strip()[:120]
    _aplicar_ficha(p, dados)
    _auditar(s, ctx, "editar_ficha", "paciente", pid, campos=sorted(k for k in dados if k != "nome"))
    return obter_paciente(s, ctx, pid)


def atualizar_preferencias(s: Session, ctx: Contexto, pid: int, prefs: dict) -> dict:
    autorizar(ctx, "paciente.editar", "paciente", pid)
    p = _exigir_acesso_paciente(s, ctx, pid)
    novo = dict(p.preferencias or {})
    novo.update(prefs)
    p.preferencias = novo
    _auditar(s, ctx, "editar_preferencias", "paciente", pid, **prefs)
    return _paciente_dict(s, p)


# ---------------------------------------------------------------- avaliações clínicas
def _avaliacao_dict(s: Session, a: AvaliacaoClinica) -> dict:
    prof = s.get(Usuario, a.profissional_id)
    return {"id": a.id, "data": a.data.isoformat(), "inicial": a.inicial, "profissional": prof.nome if prof else "?",
            "ashworth_punho": a.ashworth_punho, "ashworth_dedos": a.ashworth_dedos,
            "ashworth_cotovelo": a.ashworth_cotovelo, "preensao_acometido_kgf": a.preensao_acometido_kgf,
            "preensao_nao_acometido_kgf": a.preensao_nao_acometido_kgf, "fugl_meyer_ms": a.fugl_meyer_ms,
            "outras_escalas": a.outras_escalas, "observacoes": a.observacoes}


def criar_avaliacao(s: Session, ctx: Contexto, pid: int, d: dict) -> dict:
    autorizar(ctx, "avaliacao.criar", "avaliacao", pid)
    _exigir_acesso_paciente(s, ctx, pid)
    for campo in ("ashworth_punho", "ashworth_dedos", "ashworth_cotovelo"):
        if d.get(campo) not in (None, "", *ASHWORTH):
            raise ValueError("Ashworth modificada: use 0, 1, 1+, 2, 3 ou 4")
    data_av = _data(d.get("data")) or date.today()
    if data_av > date.today():
        raise ValueError("a data da avaliação não pode estar no futuro")
    a = AvaliacaoClinica(
        paciente_id=pid, data=data_av, profissional_id=ctx.usuario_id, inicial=bool(d.get("inicial")),
        ashworth_punho=d.get("ashworth_punho") or None, ashworth_dedos=d.get("ashworth_dedos") or None,
        ashworth_cotovelo=d.get("ashworth_cotovelo") or None,
        preensao_acometido_kgf=_numero(d.get("preensao_acometido_kgf"), "Força de preensão (lado acometido)", 0, 100),
        preensao_nao_acometido_kgf=_numero(d.get("preensao_nao_acometido_kgf"), "Força de preensão (outro lado)", 0, 100),
        fugl_meyer_ms=_numero(d.get("fugl_meyer_ms"), "Fugl-Meyer (membro superior)", 0, 66, inteiro=True),
        outras_escalas=_texto(d.get("outras_escalas")), observacoes=_texto(d.get("observacoes")))
    s.add(a)
    s.flush()
    _auditar(s, ctx, "criar", "avaliacao", pid, avaliacao_id=a.id)
    return _avaliacao_dict(s, a)


def listar_avaliacoes(s: Session, ctx: Contexto, pid: int) -> list[dict]:
    autorizar(ctx, "avaliacao.ler", "avaliacao", pid)
    _exigir_acesso_paciente(s, ctx, pid)
    rows = s.scalars(select(AvaliacaoClinica).where(AvaliacaoClinica.paciente_id == pid)
                     .order_by(AvaliacaoClinica.data, AvaliacaoClinica.id)).all()
    return [_avaliacao_dict(s, a) for a in rows]


# ---------------------------------------------------------------- principais metas
MAX_METAS = 5


def _meta_dict(s: Session, m: MetaPaciente) -> dict:
    prof = s.get(Usuario, m.profissional_id)
    return {"id": m.id, "ordem": m.ordem, "jogo": m.jogo, "metrica": m.metrica, "alvo": m.alvo,
            "alvo_braco_sadio": bool(m.alvo_braco_sadio), "observacao": m.observacao or "",
            "criada_em": _iso(m.criada_em), "profissional": prof.nome if prof else "?"}


def listar_metas(s: Session, ctx: Contexto, pid: int) -> list[dict]:
    autorizar(ctx, "avaliacao.ler", "avaliacao", pid)
    _exigir_acesso_paciente(s, ctx, pid)
    from ..guia_metricas import GUIA
    rows = s.scalars(select(MetaPaciente).where(MetaPaciente.paciente_id == pid, MetaPaciente.ativa)
                     .order_by(MetaPaciente.ordem)).all()
    # metas de métricas que deixaram de existir (ex.: força, com a célula de carga retirada) não aparecem
    return [_meta_dict(s, m) for m in rows if m.metrica in GUIA]


def salvar_metas(s: Session, ctx: Contexto, pid: int, metas: list[dict]) -> list[dict]:
    """Substitui as metas do paciente. Metas que continuam iguais (mesmo jogo e métrica) mantêm a
    data de criação (é dela que sai a linha de base); as retiradas ficam inativas no histórico."""
    from ..guia_metricas import GUIA
    autorizar(ctx, "avaliacao.criar", "avaliacao", pid)
    p = _exigir_acesso_paciente(s, ctx, pid)
    if not isinstance(metas, list) or len(metas) > MAX_METAS:
        raise ValueError(f"escolha no máximo {MAX_METAS} metas")
    vistos, limpas = set(), []
    for d in metas:
        jogo, metrica = str(d.get("jogo") or ""), str(d.get("metrica") or "")
        if jogo not in JOGO_CAMINHO:
            raise ValueError("jogo desconhecido")
        if metrica not in GUIA:
            raise ValueError("métrica desconhecida")
        if (jogo, metrica) in vistos:
            raise ValueError(f"meta repetida: {GUIA[metrica]['rotulo']} em {NOMES_JOGOS[jogo]}")
        vistos.add((jogo, metrica))
        sadio = bool(d.get("alvo_braco_sadio"))
        if sadio and p.lado_acometido not in ("direito", "esquerdo"):
            raise ValueError("paciente com acometimento bilateral: não há braço sadio para usar como meta")
        alvo = None if sadio else _numero(d.get("alvo"), "Valor-alvo", -1e6, 1e6)
        limpas.append((jogo, metrica, alvo, sadio, str(d.get("observacao") or "")[:300]))
    atuais = {(m.jogo, m.metrica): m for m in
              s.scalars(select(MetaPaciente).where(MetaPaciente.paciente_id == pid, MetaPaciente.ativa)).all()}
    for chave, m in atuais.items():
        if chave not in vistos:
            m.ativa = False
    for ordem, (jogo, metrica, alvo, sadio, obs) in enumerate(limpas, start=1):
        m = atuais.get((jogo, metrica))
        if m is None:
            m = MetaPaciente(paciente_id=pid, jogo=jogo, metrica=metrica, profissional_id=ctx.usuario_id)
            s.add(m)
        m.ordem, m.alvo, m.alvo_braco_sadio, m.observacao = ordem, alvo, sadio, obs or None
    s.flush()
    _auditar(s, ctx, "definir_metas", "paciente", pid, metas=[f"{j}:{m}" for j, m, *_ in limpas])
    return listar_metas(s, ctx, pid)


# ---------------------------------------------------------------- braço da partida (acometido ou sadio)
MEMBROS = ("acometido", "sadio")
OPOSTO = {"direito": "esquerdo", "esquerdo": "direito"}


def lado_do_braco(p: Paciente, membro: str) -> str | None:
    """Lado (direito/esquerdo) do braço que joga. Paciente bilateral não tem lado sadio."""
    if membro not in MEMBROS:
        raise ValueError("braço inválido: use acometido ou sadio")
    if membro == "acometido":
        return p.lado_acometido
    if p.lado_acometido not in OPOSTO:
        raise ValueError("paciente com acometimento bilateral: não há braço sadio para comparar")
    return OPOSTO[p.lado_acometido]


def _membro(x) -> str:
    return x.membro or "acometido"


# ---------------------------------------------------------------- equipe e vínculos (administrador)
def _usuario_dict(u: Usuario) -> dict:
    return {"id": u.id, "nome": u.nome, "email": u.email, "perfil": u.perfil.nome,
            "registro_profissional": u.registro_profissional, "ativo": u.ativo,
            "tem_senha": bool(u.senha_hash), "deve_trocar_senha": bool(u.deve_trocar_senha),
            "ultimo_login": _iso(u.ultimo_login), "bloqueado": bool(u.bloqueado_ate and _iso(u.bloqueado_ate) > _iso(agora()))}


def listar_usuarios(s: Session, ctx: Contexto) -> list[dict]:
    autorizar(ctx, "usuarios.gerenciar", "usuario")
    return [_usuario_dict(u) for u in s.scalars(select(Usuario).where(Usuario.email.is_not(None)).order_by(Usuario.nome))]


def criar_usuario(s: Session, ctx: Contexto, d: dict) -> dict:
    from ..autenticacao import gerar_hash, normalizar_email
    autorizar(ctx, "usuarios.gerenciar", "usuario")
    if not (d.get("nome") or "").strip():
        raise ValueError("informe o nome")
    email = normalizar_email(d.get("email", ""))
    if s.scalar(select(Usuario).where(Usuario.email == email)):
        raise ValueError("já existe um usuário com este e-mail")
    perfil = s.scalar(select(Perfil).where(Perfil.nome == d.get("perfil")))
    if perfil is None or perfil.nome == "paciente":
        raise ValueError("perfil inválido")
    u = Usuario(nome=d["nome"].strip()[:120], email=email, perfil_id=perfil.id,
                senha_hash=gerar_hash(d.get("senha_provisoria", "")), deve_trocar_senha=True, tentativas_falhas=0,
                registro_profissional=(d.get("registro_profissional") or "").strip() or None)
    s.add(u)
    s.flush()
    _auditar(s, ctx, "criar", "usuario", None, usuario=u.id, perfil=perfil.nome)
    return _usuario_dict(u)


def atualizar_usuario(s: Session, ctx: Contexto, uid: int, d: dict) -> dict:
    from ..autenticacao import encerrar_sessoes, gerar_hash
    autorizar(ctx, "usuarios.gerenciar", "usuario")
    u = s.get(Usuario, uid)
    if u is None or u.email is None:
        raise LookupError("usuário não encontrado")
    if "nome" in d and d["nome"].strip():
        u.nome = d["nome"].strip()[:120]
    if "registro_profissional" in d:
        u.registro_profissional = (d["registro_profissional"] or "").strip() or None
    if "perfil" in d:
        perfil = s.scalar(select(Perfil).where(Perfil.nome == d["perfil"]))
        if perfil is None or perfil.nome == "paciente":
            raise ValueError("perfil inválido")
        if u.id == ctx.usuario_id and perfil.nome != "administrador":
            raise ValueError("você não pode tirar o seu próprio perfil de administrador")
        u.perfil_id = perfil.id
    if "ativo" in d:
        if u.id == ctx.usuario_id and not d["ativo"]:
            raise ValueError("você não pode desativar a sua própria conta")
        u.ativo = bool(d["ativo"])
        if not u.ativo:
            encerrar_sessoes(s, u.id)
    if d.get("senha_provisoria"):
        u.senha_hash = gerar_hash(d["senha_provisoria"])
        u.deve_trocar_senha = True
        u.tentativas_falhas = 0
        u.bloqueado_ate = None
        encerrar_sessoes(s, u.id)
    _auditar(s, ctx, "editar", "usuario", None, usuario=u.id,
             campos=sorted(k if k != "senha_provisoria" else "senha_redefinida" for k in d))
    s.flush()
    return _usuario_dict(u)


def listar_vinculos(s: Session, ctx: Contexto) -> list[dict]:
    """Todos os pacientes com seus responsáveis (tela de equipe)."""
    autorizar(ctx, "usuarios.gerenciar", "vinculo")
    saida = []
    for p in s.scalars(select(Paciente).where(Paciente.ativo)).all():
        ids = s.scalars(select(VinculoPaciente.usuario_id).where(VinculoPaciente.paciente_id == p.id)).all()
        saida.append({"paciente_id": p.id, "nome": p.identificacao.nome, "usuarios": sorted(ids)})
    return sorted(saida, key=lambda x: x["nome"].lower())


def definir_vinculos(s: Session, ctx: Contexto, pid: int, usuario_ids: list[int]) -> list[int]:
    autorizar(ctx, "usuarios.gerenciar", "vinculo")
    if s.get(Paciente, pid) is None:
        raise LookupError("paciente não encontrado")
    novos = {int(u) for u in usuario_ids}
    for uid in novos:
        if s.get(Usuario, uid) is None:
            raise ValueError("profissional inexistente")
    atuais = {v.usuario_id: v for v in s.scalars(select(VinculoPaciente).where(VinculoPaciente.paciente_id == pid))}
    for uid, v in atuais.items():
        if uid not in novos:
            s.delete(v)
    for uid in novos - set(atuais):
        s.add(VinculoPaciente(paciente_id=pid, usuario_id=uid))
    _auditar(s, ctx, "vincular", "paciente", pid, profissionais=sorted(novos))
    return sorted(novos)


# ---------------------------------------------------------------- calibrações
def calibracao_vigente(s: Session, tipo: str, caminho: str = "", simulador: bool | None = None) -> Calibracao | None:
    """Versão mais nova. Com `simulador` informado, as calibrações feitas no simulador e no
    equipamento real não se misturam (as de fábrica valem para os dois)."""
    q = select(Calibracao).where(Calibracao.tipo == tipo, Calibracao.caminho == caminho).order_by(Calibracao.versao.desc())
    if simulador is None:
        return s.scalar(q.limit(1))
    for c in s.scalars(q):
        feita_no_sim = bool((c.dados or {}).get("simulador"))
        if c.padrao or feita_no_sim == simulador:
            return c
    return None


def salvar_calibracao(s: Session, ctx: Contexto, tipo: str, caminho: str, dados: dict, dispositivo: str) -> Calibracao:
    # a calibração do caminho reto (VL53L0X) é rotina do atendimento: fisioterapeutas também podem
    autorizar(ctx, "calibracao.reto" if tipo == "vl53_reto" else "calibracao.criar", tipo)
    atual = calibracao_vigente(s, tipo, caminho)
    disp = s.scalar(select(Dispositivo).where(Dispositivo.identificador == dispositivo))
    c = Calibracao(dispositivo_id=disp.id, tipo=tipo, caminho=caminho, versao=(atual.versao + 1) if atual else 1,
                   dados=dados, responsavel_id=ctx.usuario_id)
    s.add(c)
    s.flush()
    _auditar(s, ctx, "criar", "calibracao", None, tipo=tipo, caminho=caminho, versao=c.versao)
    return c


def calibracao_dict(c: Calibracao | None) -> dict | None:
    if c is None:
        return None
    return {"id": c.id, "tipo": c.tipo, "caminho": c.caminho, "versao": c.versao, "dados": c.dados,
            "data": _iso(c.data), "padrao": c.padrao}


def atualizar_dispositivo(s: Session, identificador: str, versao_fw: int | None = None, mac: str | None = None) -> None:
    d = s.scalar(select(Dispositivo).where(Dispositivo.identificador == identificador))
    if d is None:
        return
    mudou = False
    if versao_fw is not None and d.versao_firmware != str(versao_fw):
        d.versao_firmware, mudou = str(versao_fw), True
    if mac and d.mac != mac:
        d.mac, mudou = mac, True
    if mudou:
        d.atualizado_em = agora()


# ---------------------------------------------------------------- partidas
def iniciar_partida(s: Session, ctx: Contexto, pid: int, jogo: str, dificuldade: dict, fonte: str,
                    calibracoes: dict, membro: str = "acometido") -> Partida:
    autorizar(ctx, "partida.criar", "partida", pid)
    pac = _exigir_acesso_paciente(s, ctx, pid)
    lado_braco = lado_do_braco(pac, membro)
    if jogo not in JOGO_CAMINHO:
        raise ValueError("jogo desconhecido")
    joy = CAMINHOS[JOGO_CAMINHO[jogo]].joystick
    disp = s.scalar(select(Dispositivo).where(Dispositivo.identificador == joy))
    t = agora()
    # Partidas em andamento que ficaram para trás (página fechada, queda de energia).
    for velha in s.scalars(select(Partida).where(Partida.status == "em_andamento")).all():
        velha.status, velha.motivo, velha.fim = "descartada", "interrompida", t
    # Sessão = mesma pessoa, mesmo jogo e mesma dificuldade, com atividade nos últimos 30 min.
    sess = s.scalar(select(Sessao).where(Sessao.paciente_id == pid, Sessao.jogo == jogo,
                                          Sessao.encerrada_em.is_(None)).order_by(Sessao.id.desc()).limit(1))
    if sess is not None:
        ultima = sess.ultima_atividade if sess.ultima_atividade.tzinfo else sess.ultima_atividade.replace(tzinfo=timezone.utc)
        # trocar de braço também abre outra sessão (cada braço tem sua série no gráfico)
        mesmo_braco = not sess.partidas or _membro(sess.partidas[-1]) == membro
        if t - ultima > timedelta(minutes=30) or sess.dificuldade != dificuldade or not mesmo_braco:
            sess.encerrada_em = t
            sess = None
    if sess is None:
        for aberta in s.scalars(select(Sessao).where(Sessao.paciente_id == pid, Sessao.encerrada_em.is_(None))).all():
            aberta.encerrada_em = t
        sess = Sessao(paciente_id=pid, dispositivo_id=disp.id, jogo=jogo, dificuldade=dificuldade,
                      profissional_id=ctx.usuario_id, calibracoes=calibracoes)
        s.add(sess)
        s.flush()
    sess.ultima_atividade = t
    n = s.scalar(select(func.count(Partida.id)).where(Partida.sessao_id == sess.id)) or 0
    part = Partida(sessao_id=sess.id, numero=n + 1, fonte=fonte, inicio=t, membro=membro, lado_braco=lado_braco)
    s.add(part)
    s.flush()
    _auditar(s, ctx, "iniciar", "partida", pid, partida_id=part.id, jogo=jogo, fonte=fonte, membro=membro)
    return part


def finalizar_partida(s: Session, ctx: Contexto, partida_id: int, jogo_dados: dict, metricas: dict | None,
                      qualidade: dict | None, versao: str | None, brutos: list[dict]) -> Partida:
    part = s.get(Partida, partida_id)
    if part is None:
        raise LookupError("partida não encontrada")
    pid = part.sessao.paciente_id
    autorizar(ctx, "partida.finalizar", "partida", pid)
    _exigir_acesso_paciente(s, ctx, pid)
    part.fim = agora()
    part.status = "valida"
    part.motivo = str(jogo_dados.get("motivo") or "")[:200]
    part.pontos = int(jogo_dados.get("pontos") or 0)
    part.estrelas = int(jogo_dados.get("estrelas") or 0)
    part.destaque = str(jogo_dados.get("destaque") or "")[:300]
    part.sessao.ultima_atividade = part.fim
    s.add(Resultado(partida_id=part.id, versao_algoritmo=versao or "jogo-sem-sensores",
                    metricas=metricas or {}, metricas_jogo=jogo_dados.get("metricas_jogo") or {},
                    qualidade=qualidade or {}, eventos_jogo=list(jogo_dados.get("eventos") or [])[:5000]))
    for b in brutos:
        s.add(DadoBruto(partida_id=part.id, tipo=b["tipo"], arquivo=b["arquivo"], checksum_sha256=b["sha256"],
                        versao_formato=1, n_registros=b["n"]))
    _auditar(s, ctx, "finalizar", "partida", pid, partida_id=part.id, pontos=part.pontos)
    return part


def descartar_partida(s: Session, ctx: Contexto, partida_id: int, motivo: str) -> None:
    part = s.get(Partida, partida_id)
    if part is None or part.status != "em_andamento":
        return
    autorizar(ctx, "partida.finalizar", "partida", part.sessao.paciente_id)
    part.status, part.motivo, part.fim = "descartada", motivo[:200], agora()
    _auditar(s, ctx, "descartar", "partida", part.sessao.paciente_id, partida_id=part.id, motivo=motivo)


def _fonte_rotulo(p: Partida) -> str:
    return "Sensores" if p.fonte == "sensores" else "Mouse (demonstração)"


def historico(s: Session, ctx: Contexto, pid: int, jogo: str) -> list[dict]:
    autorizar(ctx, "historico.ler", "partida", pid)
    _exigir_acesso_paciente(s, ctx, pid)
    pac = s.get(Paciente, pid)
    q = (select(Partida).join(Sessao).where(Sessao.paciente_id == pid, Sessao.jogo == jogo, Partida.status == "valida")
         .order_by(Partida.inicio))
    _auditar(s, ctx, "ler", "historico", pid, jogo=jogo)
    saida = []
    for p in s.scalars(q).all():
        r = p.resultado
        saida.append({"id": p.id, "data": _iso(p.inicio), "pontos": p.pontos, "estrelas": p.estrelas,
                      "fonte": _fonte_rotulo(p), "destaque": p.destaque, "sessao": p.sessao_id, "numero": p.numero,
                      "membro": _membro(p), "lado_braco": p.lado_braco or pac.lado_acometido,
                      "metricas": (r.metricas if r else {}), "qualidade": (r.qualidade if r else {})})
    return saida


def exportar_csv(s: Session, ctx: Contexto) -> str:
    """Uma linha por partida válida e uma coluna por métrica de movimento.

    As métricas do jogo (que variam de jogo para jogo) ficam numa coluna só.
    Datas no horário de Brasília. Separador ";" e vírgula decimal (Excel em português).
    """
    from ..guia_metricas import GUIA, cabecalho_csv
    autorizar(ctx, "relatorio.exportar", "partida")
    chaves = list(GUIA)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow(["Paciente", "Lado acometido", "Braço registrado", "Lado do braço", "Jogo", "Data", "Hora", "Sessão", "Partida", "Entrada", "Pontos",
                "Estrelas"] + [cabecalho_csv(k) for k in chaves] +
               ["Métricas do jogo", "Qualidade: erro da sincronia (ms)", "Qualidade: pacotes não recuperados",
                "Qualidade: marcos rejeitados", "Qualidade: leituras fora da sequência (espiral)"])
    brt = timezone(timedelta(hours=-3))

    def num(v):
        return "" if v is None else str(v).replace(".", ",") if isinstance(v, float) else v

    q = select(Partida).join(Sessao).where(Partida.status == "valida").order_by(Partida.inicio)
    ids = _ids_visiveis(s, ctx)
    if ids is not None:   # só os pacientes vinculados ao profissional
        q = q.where(Sessao.paciente_id.in_(ids))
    for p in s.scalars(q).all():
        pac = s.get(Paciente, p.sessao.paciente_id)
        r = p.resultado
        m = (r.metricas if r else {}) or {}
        qa = (r.qualidade if r else {}) or {}
        fu = qa.get("fusao") or {}
        ini = p.inicio if p.inicio.tzinfo else p.inicio.replace(tzinfo=timezone.utc)
        ini = ini.astimezone(brt)
        mj = "; ".join(f"{k}={v}" for k, v in ((r.metricas_jogo if r else {}) or {}).items())
        w.writerow([pac.identificacao.nome, pac.lado_acometido, _membro(p), p.lado_braco or pac.lado_acometido, NOMES_JOGOS.get(p.sessao.jogo, p.sessao.jogo),
                    ini.strftime("%d/%m/%Y"), ini.strftime("%H:%M"), p.sessao_id, p.numero, _fonte_rotulo(p),
                    p.pontos, p.estrelas] + [num(m.get(k)) for k in chaves] +
                   [mj, num((qa.get("sincronia") or {}).get("residuo_ms")), num(qa.get("pacotes_nao_recuperados")),
                    num(fu.get("bordas_rejeitadas")), num(fu.get("inconsistencias_gray"))])
    _auditar(s, ctx, "exportar", "partida", None, formato="csv")
    return buf.getvalue()


def listar_auditoria(s: Session, ctx: Contexto, limite: int = 200) -> list[dict]:
    autorizar(ctx, "auditoria.ler", "auditoria")
    rows = s.scalars(select(Auditoria).order_by(Auditoria.id.desc()).limit(limite)).all()
    return [{"id": a.id, "quando": _iso(a.quando), "usuario_id": a.usuario_id, "acao": a.acao, "recurso": a.recurso,
             "paciente_id": a.paciente_id, "detalhes": a.detalhes} for a in rows]
