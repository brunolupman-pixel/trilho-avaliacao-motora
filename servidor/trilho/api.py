"""Servidor web local: site, API REST e WebSocket do estado dos sensores.

O site (web/index.html) roda no navegador do computador da clínica e fala só
com este servidor, nunca direto com o hardware:
  - REST /api/...  : pacientes, partidas, histórico, calibrações, exportação;
  - WebSocket /ws  : estado do joystick já processado (fusão), ~100 Hz durante
                     a partida, e status de bancada/joysticks.
Tudo exige login (cookie de sessão), exceto a tela de login e o primeiro acesso.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import armazenamento, autenticacao as auth, banco, posprocesso
from .aquisicao import Aquisicao
from .autorizacao import NOMES_PERFIS, Contexto, PermissaoNegada, pode
from .banco import repositorios as repo
from .caminhos import CAMINHOS, JOGO_CAMINHO, tabela_padrao
from .config import PASTA_WEB
from .portas import GerenciadorPortas

log = logging.getLogger("trilho.api")


INSTRUCOES = {
    "espiral": "Coloque a colher (joystick) na BORDA da espiral",
    "guarda_chuva": "Coloque o joystick na PONTA ESQUERDA do arco",
    "reto": "Deixe o joystick no MEIO do trilho (neutro)",
}


def criar_app(aq: Aquisicao, pasta_dados: Path, porta_simulada=None,
              portas: GerenciadorPortas | None = None) -> FastAPI:
    @asynccontextmanager
    async def ciclo_de_vida(_app: FastAPI):
        await _inicio()
        yield

    app = FastAPI(title="Trilho", docs_url="/api/docs", redoc_url=None, lifespan=ciclo_de_vida)
    app.state.aq = aq
    app.state.partida = None   # partida em andamento: dict
    simulador = porta_simulada is not None
    portas = portas or GerenciadorPortas(simulada=porta_simulada)   # testes: só para listar

    def estado_hardware() -> dict:
        e = aq.estado_publico()
        e["simulador"] = simulador
        e["porta"] = portas.estado()
        return e

    SISTEMA = Contexto(None, "administrador", "sistema")   # tarefas internas (sem usuário)

    def ctx_e_sessao(request: Request, permitir_troca_pendente: bool = False):
        """Sessão do banco + contexto do profissional logado (401 se não estiver)."""
        s = banco.sessao()
        sl = auth.sessao_do_token(s, request.cookies.get(auth.COOKIE))
        if sl is None:
            s.commit()
            s.close()
            raise HTTPException(401, "faça login para continuar")
        if sl.usuario.deve_trocar_senha and not permitir_troca_pendente:
            s.commit()
            s.close()
            raise HTTPException(403, "troque a senha provisória antes de continuar")
        return s, auth.contexto(sl)

    def ctx_sistema():
        return banco.sessao(), SISTEMA

    @app.exception_handler(PermissaoNegada)
    async def _negado(_req: Request, exc: PermissaoNegada):
        return Response(json.dumps({"detail": str(exc)}), status_code=403, media_type="application/json")

    # ------------------------------------------------------------ páginas
    def _pagina(arquivo: str, request: Request | None = None, perfis: tuple[str, ...] | None = None):
        if request is not None:
            with banco.sessao() as s:
                sl = auth.sessao_do_token(s, request.cookies.get(auth.COOKIE))
                perfil = sl.usuario.perfil.nome if sl else None
                trocar = bool(sl and sl.usuario.deve_trocar_senha)
                s.commit()
            if perfil is None:
                return RedirectResponse(f"/login?volta={request.url.path}", status_code=303)
            if trocar and arquivo != "conta.html":
                return RedirectResponse("/conta", status_code=303)
            if perfis and perfil not in perfis:
                return RedirectResponse("/calibracao" if perfil == "tecnico" else "/", status_code=303)
        return FileResponse(PASTA_WEB / arquivo, headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    def pagina(request: Request):
        return _pagina("index.html", request, ("administrador", "fisioterapeuta"))

    @app.get("/avaliacao", include_in_schema=False)
    def pagina_avaliacao(request: Request):
        return _pagina("avaliacao.html", request, ("administrador", "fisioterapeuta"))

    @app.get("/calibracao", include_in_schema=False)
    def pagina_calibracao(request: Request):
        return _pagina("calibracao.html", request, ("administrador", "tecnico", "fisioterapeuta"))

    @app.get("/equipe", include_in_schema=False)
    def pagina_equipe(request: Request):
        return _pagina("equipe.html", request, ("administrador",))

    @app.get("/conta", include_in_schema=False)
    def pagina_conta(request: Request):
        return _pagina("conta.html", request)

    @app.get("/login", include_in_schema=False)
    def pagina_login():
        return _pagina("login.html")

    @app.get("/marca-trilho.png", include_in_schema=False)
    def marca_trilho():
        return FileResponse(PASTA_WEB / "marca-trilho.png", media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/comum.js", include_in_schema=False)
    def comum_js():
        return FileResponse(PASTA_WEB / "comum.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/geometria/espiral")
    def geometria_espiral(request: Request):
        """Linha central do canal da espiral (mm), a mesma usada na fusão: o jogo desenha a peça real."""
        s, _ = ctx_e_sessao(request)
        s.close()
        from .caminhos import LARGURA_CANAL_ESPIRAL_MM, RAIO_EXTERNO_ESPIRAL_MM, geometria_espiral as geo
        g = geo()
        return {"pontos_mm": g["pontos_mm"], "comprimento_mm": g["comprimento_mm"],
                "largura_canal_mm": LARGURA_CANAL_ESPIRAL_MM, "raio_externo_mm": RAIO_EXTERNO_ESPIRAL_MM}

    @app.get("/api/metricas/guia")
    def guia_metricas(request: Request):
        s, _ = ctx_e_sessao(request)
        s.close()
        from .guia_metricas import GUIA, QUALIDADE
        return {"metricas": GUIA, "qualidade": QUALIDADE}

    # ------------------------------------------------------------ login
    def _cookie(resp: Response, token: str) -> None:
        resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", path="/",
                        max_age=int(auth.DURACAO_MAXIMA.total_seconds()))

    def _eu(u) -> dict:
        return {"id": u.id, "nome": u.nome, "email": u.email, "perfil": u.perfil.nome,
                "perfil_nome": NOMES_PERFIS.get(u.perfil.nome, u.perfil.nome),
                "registro_profissional": u.registro_profissional or "",
                "foto_versao": (u.foto_versao or 1) if u.foto else 0,
                "deve_trocar_senha": bool(u.deve_trocar_senha)}

    @app.get("/api/auth/estado")
    def auth_estado(request: Request):
        with banco.sessao() as s:
            sl = auth.sessao_do_token(s, request.cookies.get(auth.COOKIE))
            r = {"primeiro_acesso": auth.precisa_primeiro_acesso(s), "usuario": _eu(sl.usuario) if sl else None}
            s.commit()
            return r

    @app.post("/api/auth/primeiro-acesso")
    def primeiro_acesso(response: Response, dados: dict = Body(...)):
        with banco.sessao() as s:
            try:
                auth.criar_primeiro_admin(s, dados.get("nome", ""), dados.get("email", ""), dados.get("senha", ""),
                                          dados.get("registro_profissional"))
                s.flush()
                u, token = auth.entrar(s, dados.get("email", ""), dados.get("senha", ""))
            except auth.ErroLogin as e:
                s.rollback()
                raise HTTPException(400, str(e))
            s.commit()
            _cookie(response, token)
            return _eu(u)

    @app.post("/api/auth/login")
    def login(response: Response, dados: dict = Body(...)):
        with banco.sessao() as s:
            try:
                u, token = auth.entrar(s, dados.get("email", ""), dados.get("senha", ""))
            except auth.ErroLogin as e:
                s.commit()   # guarda a tentativa errada e a auditoria
                raise HTTPException(401, str(e))
            s.commit()
            _cookie(response, token)
            return _eu(u)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        with banco.sessao() as s:
            auth.sair(s, request.cookies.get(auth.COOKIE))
            s.commit()
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/trocar-senha")
    def trocar_senha(request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request, permitir_troca_pendente=True)
        with s:
            try:
                auth.trocar_senha(s, ctx.usuario_id, dados.get("atual", ""), dados.get("nova", ""))
            except auth.ErroLogin as e:
                raise HTTPException(400, str(e))
            auth.encerrar_sessoes(s, ctx.usuario_id, exceto_token=request.cookies.get(auth.COOKIE))
            s.commit()
            return {"ok": True}

    # ------------------------------------------------------------ minha conta (dados e foto)
    FOTO_MAX_BYTES = 400_000
    TIPOS_FOTO = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"RIFF": "image/webp"}

    @app.patch("/api/auth/eu")
    def editar_eu(request: Request, dados: dict = Body(...)):
        """O próprio usuário muda o nome e o registro profissional (e-mail e perfil: só o administrador)."""
        s, ctx = ctx_e_sessao(request)
        with s:
            u = s.get(repo.Usuario, ctx.usuario_id)
            if "nome" in dados:
                nome = " ".join(str(dados["nome"] or "").split())
                if not 2 <= len(nome) <= 120:
                    raise HTTPException(400, "nome: entre 2 e 120 caracteres")
                u.nome = nome
            if "registro_profissional" in dados:
                reg = str(dados["registro_profissional"] or "").strip()
                if len(reg) > 40:
                    raise HTTPException(400, "registro profissional: no máximo 40 caracteres")
                u.registro_profissional = reg or None
            repo._auditar(s, ctx, "editar_conta", "usuario", None)
            s.commit()
            return _eu(u)

    @app.put("/api/auth/foto")
    def salvar_foto(request: Request, dados: dict = Body(...)):
        import base64
        txt = str(dados.get("imagem") or "")
        if not txt.startswith("data:image/") or ";base64," not in txt:
            raise HTTPException(400, "imagem inválida")
        try:
            bruto = base64.b64decode(txt.split(";base64,", 1)[1], validate=True)
        except Exception:
            raise HTTPException(400, "imagem inválida")
        if len(bruto) > FOTO_MAX_BYTES:
            raise HTTPException(400, "foto grande demais (máximo 400 KB depois de reduzida)")
        tipo = next((t for assinatura, t in TIPOS_FOTO.items() if bruto.startswith(assinatura)), None)
        if tipo is None or (tipo == "image/webp" and bruto[8:12] != b"WEBP"):
            raise HTTPException(400, "use uma foto JPG, PNG ou WebP")
        s, ctx = ctx_e_sessao(request)
        with s:
            u = s.get(repo.Usuario, ctx.usuario_id)
            u.foto, u.foto_tipo, u.foto_versao = bruto, tipo, (u.foto_versao or 0) + 1
            repo._auditar(s, ctx, "trocar_foto", "usuario", None)
            s.commit()
            return _eu(u)

    @app.delete("/api/auth/foto")
    def remover_foto(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            u = s.get(repo.Usuario, ctx.usuario_id)
            u.foto, u.foto_tipo = None, None
            u.foto_versao = (u.foto_versao or 0) + 1
            repo._auditar(s, ctx, "remover_foto", "usuario", None)
            s.commit()
            return _eu(u)

    @app.get("/api/auth/foto")
    def ver_foto(request: Request):
        s, ctx = ctx_e_sessao(request, permitir_troca_pendente=True)
        with s:
            u = s.get(repo.Usuario, ctx.usuario_id)
            if not u.foto:
                raise HTTPException(404, "sem foto")
            # a URL leva ?v=<versão>: o navegador guarda a foto até ela mudar
            return Response(content=u.foto, media_type=u.foto_tipo or "image/jpeg",
                            headers={"Cache-Control": "private, max-age=31536000"})

    # ------------------------------------------------------------ equipe (administrador)
    @app.get("/api/usuarios")
    def usuarios(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = repo.listar_usuarios(s, ctx)
            s.commit()
            return r

    @app.post("/api/usuarios")
    def novo_usuario(request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        with s:
            try:
                u = repo.criar_usuario(s, ctx, dados)
            except (ValueError, auth.ErroLogin) as e:
                raise HTTPException(400, str(e))
            s.commit()
            return u

    @app.patch("/api/usuarios/{uid}")
    def editar_usuario(uid: int, request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        with s:
            try:
                u = repo.atualizar_usuario(s, ctx, uid, dados)
            except (ValueError, auth.ErroLogin) as e:
                raise HTTPException(400, str(e))
            except LookupError as e:
                raise HTTPException(404, str(e))
            s.commit()
            return u

    @app.get("/api/vinculos")
    def vinculos(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = repo.listar_vinculos(s, ctx)
            s.commit()
            return r

    @app.put("/api/vinculos/{pid}")
    def definir_vinculos(pid: int, request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        with s:
            try:
                r = repo.definir_vinculos(s, ctx, pid, dados.get("usuario_ids") or [])
            except ValueError as e:
                raise HTTPException(400, str(e))
            except LookupError as e:
                raise HTTPException(404, str(e))
            s.commit()
            return {"usuario_ids": r}

    # ------------------------------------------------------------ estado
    @app.get("/api/estado")
    def estado(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "hardware.ler")
            s.commit()
        return estado_hardware()

    @app.post("/api/hardware/reconectar")
    def reconectar(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "hardware.reconectar")
            repo._auditar(s, ctx, "reconectar", "hardware")
            s.commit()
        aq.reconectar()
        return {"ok": True}

    # ---- botão "Conectar ESP32": o site escolhe a porta, este programa abre e lê
    def _exigir_sem_partida_com_sensores() -> None:
        pa = app.state.partida
        if pa is not None and pa["fonte"] == "sensores":
            raise HTTPException(409, "há uma partida com sensores em andamento; encerre-a antes de trocar a porta")

    @app.get("/api/hardware/portas")
    def listar_portas(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "hardware.ler")
            s.commit()
        return {"portas": portas.listar(), "estado": portas.estado(),
                "bancada_respondendo": estado_hardware()["bancada"]["conectada"]}

    @app.post("/api/hardware/conectar")
    async def conectar_porta(request: Request, dados: dict = Body(default={})):
        """{"porta": "COM5"} usa essa porta; {"porta": null} volta para a detecção automática."""
        porta = dados.get("porta") or None
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "hardware.reconectar")
            _exigir_sem_partida_com_sensores()
            repo._auditar(s, ctx, "conectar_porta", "hardware", porta=porta or "auto")
            s.commit()
        portas.escolher(porta)
        t_pedido = aq.relogio()
        aq.reconectar()
        # espera a bancada responder (o firmware manda o status logo que recebe o pedido)
        fim = time.monotonic() + 3.5
        while time.monotonic() < fim:
            await asyncio.sleep(0.1)
            if aq.porta_aberta() and aq.ultimo_contato_bancada() > t_pedido:
                break
        est = portas.estado()
        resp = aq.porta_aberta() and aq.ultimo_contato_bancada() > t_pedido
        diagnostico = None
        if est["aberta"] and not resp:
            aviso, diagnostico = await _diagnosticar(est["aberta"])
        elif not est["aberta"]:
            aviso = est["erro"] or "não foi possível abrir a porta"
        else:
            aviso = None
        return {"estado": portas.estado(), "bancada_respondendo": bool(resp), "aviso": aviso, "diagnostico": diagnostico}

    async def _diagnosticar(nome: str) -> tuple[str, dict]:
        """A porta abriu, mas não chegou nenhum quadro do Trilho: descobre o que a placa está mandando.
        Se não vier nada legível a 921600, solta a porta por ~2 s e escuta a 115200 (velocidade do
        programa antigo usado com o HTML anterior)."""
        from starlette.concurrency import run_in_threadpool
        from .portas import analisar_amostra, explicar_sem_resposta
        d = aq.diagnostico_porta()
        a921 = d["amostra"]
        a115 = None
        if analisar_amostra(a921)["tipo"] != "texto" and not simulador:
            portas.pausar()
            aq.reconectar()
            await asyncio.sleep(0.3)
            try:
                a115 = await run_in_threadpool(portas.sondar, nome, 115200, 2.0)
            except Exception as e:      # porta sumiu no meio
                log.info("sondagem a 115200 falhou: %s", e)
            finally:
                portas.retomar()
                aq.reconectar()
        info = {"bytes_921600": d["bytes"], "quadros_validos": d["quadros"],
                "amostra_921600": analisar_amostra(a921),
                "amostra_115200": analisar_amostra(a115) if a115 is not None else None}
        return explicar_sem_resposta(nome, a921, a115), info

    @app.post("/api/hardware/desconectar")
    def desconectar_porta(request: Request):
        """Fecha a porta e não tenta abrir de novo (libera para a Arduino IDE gravar o firmware)."""
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "hardware.reconectar")
            _exigir_sem_partida_com_sensores()
            repo._auditar(s, ctx, "desconectar_porta", "hardware")
            s.commit()
        portas.desligar()
        aq.reconectar()
        return {"estado": portas.estado()}

    # ------------------------------------------------------------ pacientes
    def _erros(fn):
        try:
            return fn()
        except ValueError as e:
            raise HTTPException(400, str(e))
        except LookupError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/pacientes")
    def pacientes(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = repo.listar_pacientes(s, ctx)
            s.commit()
            return r

    @app.post("/api/pacientes")
    def novo_paciente(request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        with s:
            p = _erros(lambda: repo.criar_paciente(s, ctx, dados))
            s.commit()
            return p

    @app.get("/api/pacientes/{pid}")
    def ficha(pid: int, request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = _erros(lambda: repo.obter_paciente(s, ctx, pid))
            s.commit()
            return r

    @app.patch("/api/pacientes/{pid}")
    def editar_ficha(pid: int, request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = _erros(lambda: repo.atualizar_ficha(s, ctx, pid, dados))
            s.commit()
            return r

    @app.get("/api/pacientes/{pid}/avaliacoes")
    def avaliacoes(pid: int, request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = _erros(lambda: repo.listar_avaliacoes(s, ctx, pid))
            s.commit()
            return r

    @app.get("/api/pacientes/{pid}/metas")
    def metas(pid: int, request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            return _erros(lambda: repo.listar_metas(s, ctx, pid))

    @app.put("/api/pacientes/{pid}/metas")
    def salvar_metas(pid: int, request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = _erros(lambda: repo.salvar_metas(s, ctx, pid, dados.get("metas")))
            s.commit()
            return r

    @app.post("/api/pacientes/{pid}/avaliacoes")
    def nova_avaliacao(pid: int, request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = _erros(lambda: repo.criar_avaliacao(s, ctx, pid, {**dados, "inicial": False}))
            s.commit()
            return r

    @app.patch("/api/pacientes/{pid}/preferencias")
    def preferencias(pid: int, request: Request, dados: dict = Body(...)):
        permitidas = {k: dados[k] for k in ("zona_cm", "tolerancia_combo_s", "margem_velocidade_kmh", "sensibilidade_respingo",
                                          "sensibilidade_limpeza", "giro_janela_graus", "baixa_visao", "tutorial_oculto", "som") if k in dados}
        if "baixa_visao" in permitidas:
            permitidas["baixa_visao"] = bool(permitidas["baixa_visao"])
        if "giro_janela_graus" in permitidas and float(permitidas["giro_janela_graus"]) not in (45.0, 60.0, 65.0, 70.0, 90.0, 180.0):
            raise HTTPException(400, "giro da janela inválido")
        if "tolerancia_combo_s" in permitidas and float(permitidas["tolerancia_combo_s"]) not in (1.0, 1.5, 2.0, 3.0):
            raise HTTPException(400, "tolerância inválida")
        if "margem_velocidade_kmh" in permitidas and float(permitidas["margem_velocidade_kmh"]) not in (5.0, 10.0, 15.0, 20.0):
            raise HTTPException(400, "margem de pontuação inválida")
        if "som" in permitidas and permitidas["som"] not in ("todos", "avisos", "nenhum"):
            raise HTTPException(400, "som inválido: use todos, avisos ou nenhum")
        if "tutorial_oculto" in permitidas:
            t = permitidas["tutorial_oculto"]
            if not isinstance(t, dict) or any(k not in JOGO_CAMINHO for k in t):
                raise HTTPException(400, "tutorial_oculto inválido")
            permitidas["tutorial_oculto"] = {k: bool(v) for k, v in t.items()}
        for k in ("sensibilidade_respingo", "sensibilidade_limpeza"):
            if k in permitidas and permitidas[k] not in ("baixa", "normal", "alta"):
                raise HTTPException(400, "sensibilidade inválida")
        s, ctx = ctx_e_sessao(request)
        with s:
            p = _erros(lambda: repo.atualizar_preferencias(s, ctx, pid, permitidas))
            s.commit()
            return p

    @app.get("/api/pacientes/{pid}/historico")
    def historico(pid: int, jogo: str, request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            r = _erros(lambda: repo.historico(s, ctx, pid, jogo))
            s.commit()
            return r

    @app.get("/api/exportar.csv")
    def exportar(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            texto = repo.exportar_csv(s, ctx)
            s.commit()
        return Response("﻿" + texto, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="trilho_partidas.csv"'})

    @app.get("/api/auditoria")
    def auditoria(request: Request, limite: int = 200):
        s, ctx = ctx_e_sessao(request)
        with s:
            return repo.listar_auditoria(s, ctx, min(limite, 2000))

    # ------------------------------------------------------------ calibrações
    def _calib(s, tipo, caminho=""):
        # reto (VL53L0X) e giro do arco: simulador e equipamento real têm calibrações separadas
        sep = simulador if tipo in ("vl53_reto", "giro_arco") else None
        return repo.calibracao_vigente(s, tipo, caminho, sep)

    @app.get("/api/calibracoes")
    def calibracoes(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "calibracao.ler")
            return {"reto": repo.calibracao_dict(_calib(s, "vl53_reto", "reto")),
                    "transicoes": {c: repo.calibracao_dict(_calib(s, "tabela_transicoes", c))
                                   for c in ("espiral", "guarda_chuva")}}

    @app.get("/api/calibracoes/reto")
    def calib_reto(request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "calibracao.ler")
            c = _calib(s, "vl53_reto", "reto")
            return None if c is None else {**c.dados, "data": repo._iso(c.data), "versao": c.versao}

    @app.post("/api/calibracoes/reto")
    def salvar_calib_reto(request: Request, dados: dict = Body(...)):
        tras, frente = float(dados["tras"]), float(dados["frente"])
        if abs(frente - tras) < 40:
            raise HTTPException(400, "diferença entre trás e frente menor que 40 mm")
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "calibracao.reto")
            c = repo.salvar_calibracao(s, ctx, "vl53_reto", "reto", {"tras": tras, "frente": frente, "simulador": simulador},
                                       "bancada")
            s.commit()
            return {**c.dados, "data": repo._iso(c.data), "versao": c.versao}

    @app.get("/api/calibracoes/transicoes/{caminho}")
    def ver_transicoes(caminho: str, request: Request):
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.autorizar(ctx, "calibracao.ler")
            return repo.calibracao_dict(_calib(s, "tabela_transicoes", caminho))

    @app.put("/api/calibracoes/transicoes/{caminho}")
    def salvar_transicoes(caminho: str, request: Request, dados: dict = Body(...)):
        s0, ctx0 = ctx_e_sessao(request)      # login e permissão antes de validar o conteúdo
        with s0:
            repo.autorizar(ctx0, "calibracao.criar")
        if caminho not in ("espiral", "guarda_chuva"):
            raise HTTPException(404, "caminho sem tabela de transições")
        linhas = dados.get("linhas")
        campos = {"divisa", "sensor", "sentido", "troca", "posicao_mm", "incerteza_mm"}
        if not isinstance(linhas, list) or not linhas or not all(isinstance(l, dict) and campos <= set(l) for l in linhas):
            raise HTTPException(400, f"cada linha precisa de: {', '.join(sorted(campos))}")
        for l in linhas:
            if l["sentido"] not in ("ida", "volta") or l["troca"] not in ("liga", "desliga"):
                raise HTTPException(400, "sentido = ida|volta; troca = liga|desliga")
            l.setdefault("codigo_depois", None)
        s, ctx = ctx_e_sessao(request)
        with s:
            c = repo.salvar_calibracao(s, ctx, "tabela_transicoes", caminho,
                                       {"linhas": linhas, "referencia": dados.get("referencia", "")}, "bancada")
            s.commit()
            return repo.calibracao_dict(c)

    # ------------------------------------------------------------ partidas
    @app.post("/api/partidas")
    def iniciar_partida(request: Request, dados: dict = Body(...)):
        pid, jogo = int(dados["paciente_id"]), dados["jogo"]
        fonte = "sensores" if dados.get("fonte") == "sensores" else "mouse"
        membro = dados.get("membro") or "acometido"
        if membro not in repo.MEMBROS:
            raise HTTPException(400, "braço inválido: use acometido ou sadio")
        if jogo not in JOGO_CAMINHO:
            raise HTTPException(400, "jogo desconhecido")
        caminho = CAMINHOS[JOGO_CAMINHO[jogo]]
        s, ctx = ctx_e_sessao(request)
        s.close()
        if app.state.partida is not None:   # só um joystick em uso por vez
            _encerrar_pendente("substituída por nova partida")
        s, ctx = ctx_e_sessao(request)
        with s:
            calib_ids = {}
            tabela, vl53 = [], None
            if fonte == "sensores":
                est = aq.estado_publico()
                if not est["bancada"]["conectada"]:
                    raise HTTPException(409, "caixa (ESP32) desconectada: clique em \"Conectar ESP32\"")
                j = est["joysticks"].get(caminho.joystick)
                if not j or not j["conectado"]:
                    raise HTTPException(409, f"joystick {caminho.joystick} desconectado")
                if j.get("carregando"):
                    raise HTTPException(409, f"joystick {caminho.joystick} no carregador: retire para jogar")
                if not j["sincronia"]["valido"]:
                    raise HTTPException(409, "aguardando a sincronia de relógio do joystick")
                ct = _calib(s, "tabela_transicoes", caminho.id)
                if ct is not None:
                    tabela, calib_ids["tabela_transicoes"] = ct.dados["linhas"], ct.id
                elif caminho.sensores:
                    tabela = tabela_padrao(caminho.id)
                if caminho.usa_vl53:
                    cv = _calib(s, "vl53_reto", "reto")
                    if cv is None:
                        raise HTTPException(409, "calibre o sensor do caminho reto antes de jogar")
                    vl53, calib_ids["vl53_reto"] = cv.dados, cv.id
            part = _erros(lambda: repo.iniciar_partida(s, ctx, pid, jogo, dados.get("dificuldade") or {}, fonte,
                                                       calib_ids, membro))
            s.commit()
            # janela: escala do giroscópio medida em partidas anteriores (o arco não muda)
            escala_giro = None
            if caminho.giro_acompanha:
                cg = _calib(s, "giro_arco", caminho.id)
                escala_giro = float(cg.dados["rad_por_mm"]) if cg is not None else None
            app.state.partida = {"id": part.id, "jogo": jogo, "fonte": fonte, "caminho": caminho.id, "paciente_id": pid,
                                 "membro": membro, "escala_giro": escala_giro,
                                 "tabela": tabela, "vl53": vl53,
                                 "inicio": time.time(), "usuario_id": ctx.usuario_id}
        if fonte == "sensores":
            aq.iniciar_partida(part.id, caminho.id, tabela, vl53, escala_giro=escala_giro)
        return {"partida_id": part.id, "sessao_id": part.sessao_id, "numero": part.numero, "caminho": caminho.id,
                "membro": membro, "lado_braco": part.lado_braco,
                "comprimento_mm": caminho.comprimento, "fonte": fonte,
                "instrucao": INSTRUCOES[caminho.id] if fonte == "sensores" else ""}

    def _encerrar_pendente(motivo: str) -> None:
        pa = app.state.partida
        app.state.partida = None
        if pa is None:
            return
        if pa["fonte"] == "sensores":
            aq.cancelar_partida()
        s, ctx = ctx_sistema()
        with s:
            repo.descartar_partida(s, ctx, pa["id"], motivo)
            s.commit()

    @app.post("/api/partidas/{partida_id}/cancelar")
    def cancelar(partida_id: int, request: Request):
        s, ctx = ctx_e_sessao(request)
        s.close()
        if app.state.partida and app.state.partida["id"] == partida_id:
            _encerrar_pendente("cancelada")
        return {"ok": True}

    @app.post("/api/partidas/{partida_id}/finalizar")
    def finalizar(partida_id: int, request: Request, dados: dict = Body(...)):
        s, ctx = ctx_e_sessao(request)
        s.close()
        pa = app.state.partida
        if pa is None or pa["id"] != partida_id:
            raise HTTPException(409, "esta partida não está em andamento")
        if pa["usuario_id"] != ctx.usuario_id:
            raise HTTPException(403, "a partida foi iniciada por outro profissional")
        app.state.partida = None
        metricas, qualidade, versao, brutos = None, None, None, []
        nova_escala_giro = None
        if pa["fonte"] == "sensores":
            g = aq.finalizar_partida()
            if g is not None:
                try:
                    r = posprocesso.processar(g, pa["tabela"], pa["vl53"])
                    metricas, qualidade, versao = r.metricas, r.qualidade, r.versao
                    gpm = (qualidade.get("fusao") or {}).get("giro_graus_por_mm") or 0.0
                    if gpm:
                        nova_escala_giro = math.radians(gpm)
                    meta = {"partida_id": partida_id, "caminho": pa["caminho"], "versao_algoritmo": r.versao}
                    for nome, arr in r.tabelas_brutas.items():
                        brutos.append(armazenamento.salvar(pasta_dados / "brutos", partida_id, nome, arr,
                                                           r.colunas[nome], meta))
                except Exception as e:   # os dados do jogo não podem se perder por erro no processamento
                    log.exception("falha no pós-processamento da partida %s", partida_id)
                    qualidade = {"erro_processamento": str(e)}
        s, ctx = ctx_e_sessao(request)
        with s:
            repo.finalizar_partida(s, ctx, partida_id, dados, metricas, qualidade, versao, brutos)
            antiga = pa.get("escala_giro")
            if nova_escala_giro and (not antiga or abs(nova_escala_giro - antiga) > 0.03 * abs(antiga)):
                # o programa mediu quanto o IMU da janela gira por mm: guarda como calibração automática
                auto = Contexto(ctx.usuario_id, "administrador", "calibração automática")
                repo.salvar_calibracao(s, auto, "giro_arco", pa["caminho"],
                                       {"rad_por_mm": nova_escala_giro, "simulador": simulador,
                                        "referencia": "medida automática entre as pontas do arco"}, "A")
            s.commit()
        # O paciente vê só o resultado do jogo; as métricas clínicas ficam na
        # área de avaliação (/avaliacao) e no CSV.
        return {"partida_id": partida_id, "salva": True}

    # ------------------------------------------------------------ WebSocket
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        token = sock.cookies.get(auth.COOKIE)
        with banco.sessao() as s:
            sl = auth.sessao_do_token(s, token)
            uid = sl.usuario_id if sl else None
            ok = sl is not None and not sl.usuario.deve_trocar_senha and pode(auth.contexto(sl), "hardware.ler")
            s.commit()
        if not ok:
            await sock.close(code=4401)
            return
        await sock.accept()
        await sock.send_text(json.dumps({"tipo": "config", "simulador": simulador}))

        async def receber():
            while True:
                msg = json.loads(await sock.receive_text())
                if msg.get("tipo") == "sim_alvo" and simulador:
                    porta_simulada.definir_alvo(float(msg["f"]))

        tarefa = asyncio.create_task(receber())
        ultimo_toque = time.monotonic()
        try:
            while not tarefa.done():
                # Partida em andamento deste profissional: mantém a sessão viva (sem cliques
                # durante o jogo, a sessão expiraria e a partida não poderia ser salva).
                pa = app.state.partida
                if pa and pa.get("usuario_id") == uid and time.monotonic() - ultimo_toque > 60:
                    ultimo_toque = time.monotonic()
                    with banco.sessao() as s:
                        if auth.sessao_do_token(s, token) is None:
                            break
                        s.commit()
                e = estado_hardware()
                e["tipo"] = "estado"
                await sock.send_text(json.dumps(e))
                await asyncio.sleep(0.01 if e["partida"] else 0.2)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            tarefa.cancel()

    # ------------------------------------------------------------ manutenção
    async def _inicio():
        s, ctx = ctx_sistema()
        with s:
            for p in s.scalars(repo.select(repo.Partida).where(repo.Partida.status == "em_andamento")).all():
                repo.descartar_partida(s, ctx, p.id, "interrompida (servidor reiniciado)")
            s.commit()

        async def registrar_dispositivos():
            while True:
                await asyncio.sleep(5)
                try:
                    e = aq.estado_publico()
                    s2 = banco.sessao()
                    with s2:
                        b = e["bancada"]
                        if b.get("versao_fw") or b.get("mac"):
                            repo.atualizar_dispositivo(s2, "bancada", b.get("versao_fw"), b.get("mac"))
                        for jid, j in e["joysticks"].items():
                            st = aq.joys[jid].status if jid in aq.joys else {}
                            if st.get("versao_fw"):
                                repo.atualizar_dispositivo(s2, jid, st["versao_fw"])
                        s2.commit()
                except Exception:
                    log.debug("falha ao registrar dispositivos", exc_info=True)

        asyncio.create_task(registrar_dispositivos())

    return app
