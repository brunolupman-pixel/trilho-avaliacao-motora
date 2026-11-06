"""Linha de comando do aplicativo da clínica.

    trilho demo        fluxo completo com dados simulados (paciente, sessões, relatório)
    trilho validar     erro da fusão contra a verdade do simulador (sinais sintéticos)
    trilho calibrar-ir monta a tabela de transições a partir de passagens de calibração
    trilho simular     gera uma partida simulada (arquivo de sessão em Parquet)
    trilho processar   processa uma partida e mostra as métricas
    trilho reprocessar reprocessa todas as partidas do banco com o algoritmo atual
    trilho jogar       abre um mini-game (entrada simulada ou mouse)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import VERSAO_ALGORITMO
from .dados import DadosBrutos
from .geometria import obter_caminho
from .simulador import (BancadaSimulada, ConfigIMU, PerfilPaciente, simular_calibracao_ir,
                        simular_partida)
from .transicoes import TabelaTransicoes, construir_tabela

NOMES = {"A": "guarda-chuva", "B": "espiral", "C": "retilíneo"}


def _tabela_simulada(cid: str, versao: str = "sim-v1") -> TabelaTransicoes | None:
    cam = obter_caminho(cid)
    if not cam.sensores:
        return None
    banc = BancadaSimulada(cam)
    return construir_tabela(cam, simular_calibracao_ir(banc), f"BANC-{banc.semente:03d}", versao)


# --------------------------------------------------------------------- comandos
def cmd_calibrar_ir(a) -> None:
    tab = _tabela_simulada(a.caminho, a.versao)
    if tab is None:
        print("O caminho C não tem LM393 (VL53L0X cobre todo o percurso).")
        return
    tab.salvar(a.saida)
    print(f"Tabela de transições do caminho {a.caminho} ({len(tab.linhas)} linhas, "
          f"atraso do LM393 = {tab.atraso_ir_s * 1000:.2f} ms) salva em {a.saida}")
    for l in tab.linhas:
        print(f"  divisa {l.divisa} S{l.sensor} {l.sentido:5s} {l.troca:7s} → {l.codigo_depois}"
              f"  {l.posicao_mm:7.1f} mm ± {l.incerteza_mm:.1f}")


def cmd_simular(a) -> None:
    cam = obter_caminho(a.caminho)
    perfil = PerfilPaciente(velocidade_media_mm_s=a.velocidade)
    r = simular_partida(cam, a.tentativas, perfil, semente=a.semente)
    r.dados.salvar(a.saida)
    r.verdade.to_parquet(Path(a.saida) / "verdade.parquet", index=False)
    print(f"Partida simulada ({cam.nome}, {a.tentativas} tentativas, "
          f"{r.trajetoria.duracao_s:.0f} s) salva em {a.saida}")


def cmd_processar(a) -> None:
    from .pipeline import analisar
    dados = DadosBrutos.carregar(a.pasta)
    tab = TabelaTransicoes.carregar(a.tabela) if a.tabela else _tabela_simulada(dados.meta["caminho"])
    t0 = time.time()
    res = analisar(dados, tab)
    print(f"Processado em {time.time() - t0:.1f} s ({VERSAO_ALGORITMO})")
    q = res.fusao.qualidade
    print(f"Qualidade: sincronia {q['erro_sincronia_rms_us']:.0f} µs RMS, divisas "
          f"{q['divisas_aceitas']} aceitas / {q['divisas_rejeitadas']} rejeitadas, "
          f"{q['pulsos_espurios']} pulsos espúrios, {q['religamentos']} religamentos")
    cols = [c for c in ("tentativa", "completa", "tempo_movimento_s", "tempo_reacao_s",
                        "velocidade_pico_m_s", "sparc", "ldlj", "submovimentos", "pausas",
                        "retrocessos", "forca_media_N") if c in res.por_tentativa.columns]
    with pd.option_context("display.width", 160, "display.precision", 3):
        print(res.por_tentativa[cols].to_string(index=False))
    verdade = Path(a.pasta) / "verdade.parquet"
    if verdade.exists():
        v = pd.read_parquet(verdade)
        e = res.fusao.serie
        n = min(len(v), len(e))
        rmse = np.sqrt(np.mean((e["s"].to_numpy()[:n] - v["s"].to_numpy()[:n]) ** 2)) * 1000
        print(f"Erro de posição contra a verdade simulada: {rmse:.2f} mm RMS")


def cmd_validar(a) -> None:
    """Testes de validação com sinais sintéticos de resposta conhecida."""
    linhas = []
    imus = {"MPU6050": ConfigIMU()}
    if a.comparar_imu:
        imus["ICM-42688-P"] = ConfigIMU(ruido_acc=0.005, vies_residual_acc=(0.004, -0.003),
                                        ruido_giro=0.0008)
    for nome_imu, imu in imus.items():
        for cid in a.caminhos:
            cam = obter_caminho(cid)
            banc = BancadaSimulada(cam)
            tab = _tabela_simulada(cid)
            for vel in a.velocidades:
                for semente in range(1, a.repeticoes + 1):
                    r = simular_partida(cam, 1 if cam.vai_e_volta else 4,
                                        PerfilPaciente(velocidade_media_mm_s=vel), banc, imu,
                                        semente=semente)
                    from .fusao import processar_partida
                    f = processar_partida(r.dados, cam, tab)
                    e, v = f.serie, r.verdade
                    em_mov = np.abs(v["v"]) > 0.01
                    def rm(c):
                        return float(np.sqrt(np.mean((e[c] - v[c]) ** 2)))
                    linhas.append({
                        "imu": nome_imu, "caminho": cid, "vel_mm_s": vel, "semente": semente,
                        "s_rmse_mm": rm("s") * 1000,
                        "s_tempo_real_rmse_mm": float(np.sqrt(np.mean((e["s_rt"] - v["s"]) ** 2))) * 1000,
                        "v_rmse_mm_s": rm("v") * 1000, "a_rmse_m_s2": rm("a"),
                        "j_rmse_m_s3": float(np.sqrt(np.mean((e["j"][em_mov] - v["j"][em_mov]) ** 2))),
                        "j_pico_real_m_s3": float(np.abs(v["j"]).max()),
                        "sincronia_us": f.qualidade["erro_sincronia_rms_us"],
                        "divisas_rejeitadas": f.qualidade["divisas_rejeitadas"],
                        "religamentos": f.qualidade["religamentos"],
                    })
                    print(".", end="", flush=True)
    print()
    df = pd.DataFrame(linhas)
    resumo = df.groupby(["imu", "caminho", "vel_mm_s"]).agg(
        s_mm=("s_rmse_mm", "median"), s_max_mm=("s_rmse_mm", "max"),
        s_tempo_real_mm=("s_tempo_real_rmse_mm", "median"), v_mm_s=("v_rmse_mm_s", "median"),
        a_m_s2=("a_rmse_m_s2", "median"), j_m_s3=("j_rmse_m_s3", "median"),
        sincronia_us=("sincronia_us", "median"), religamentos=("religamentos", "sum"),
    ).reset_index()
    with pd.option_context("display.width", 200, "display.precision", 3):
        print(resumo.to_string(index=False))
    if a.saida:
        df.to_csv(a.saida, index=False)
        print(f"Detalhes em {a.saida}")


def cmd_demo(a) -> None:
    from .armazenamento import Banco, ContextoApp
    from .pipeline import processar_e_salvar
    from .relatorio import gerar_relatorio

    pasta = Path(a.pasta)
    banco = Banco(pasta)
    ctx = ContextoApp(banco.operador_local())
    print(f"Banco local em {pasta.resolve()}")
    tabelas = {}
    for cid in "ABC":
        jid = f"JOY-{cid}-001"
        sensores = ["MPU6050"] + (["HX711 4×50 kg"] if cid == "C" else [])
        banco.registrar_dispositivo(jid, f"joystick {cid}", sensores, "sim-1.0")
        tab = _tabela_simulada(cid)
        if tab is not None:
            banco.registrar_dispositivo("BANC-007", "bancada", ["LM393 ×5", "VL53L0X"], "sim-1.0")
            banco.registrar_calibracao(ctx, "BANC-007", f"transicoes_{cid}", tab.versao,
                                       tab.como_dict())
        tabelas[cid] = tab
    pid = banco.cadastrar_paciente(ctx, "Paciente Exemplo", "1970-01-01", lado_acometido="direito",
                                   dominancia="direita")
    ctx.paciente_id = pid
    print(f"Paciente cadastrado: {banco.paciente(ctx, pid)['codigo']} (identificação criptografada)")

    caminhos = a.caminhos
    hoje = datetime.now(timezone.utc).replace(microsecond=0)
    ultimas = {}
    semente = 100
    for semana in range(a.semanas):
        prog = semana / max(1, a.semanas - 1)          # 0 → 1: o paciente melhora
        perfil = PerfilPaciente(
            velocidade_media_mm_s=45 + 30 * prog, submovimentos_max=4 - round(2 * prog),
            prob_pausa=0.35 - 0.3 * prog, prob_retrocesso=0.25 - 0.2 * prog,
            tempo_reacao_s=0.6 - 0.2 * prog, forca_base_N=34 - 12 * prog)
        data = (hoje - timedelta(weeks=a.semanas - 1 - semana)).isoformat()
        for cid in caminhos:
            cam = obter_caminho(cid)
            jogo = {"A": "vidro", "B": "bolo", "C": "carro"}[cid]
            sid = banco.nova_sessao(ctx, pid, f"JOY-{cid}-001", cid, jogo,
                                    {"duracao_s": 90}, data=data)
            for numero in range(1, a.partidas + 1):
                semente += 1
                r = simular_partida(cam, 1 if cam.vai_e_volta else 4, perfil, semente=semente,
                                    tabela_id=tabelas[cid].versao if tabelas[cid] else "-")
                part = banco.salvar_partida(ctx, sid, numero, r.dados)
                res = processar_e_salvar(banco, ctx, part, tabelas[cid])
                rs = res.resumo
                sp = rs.get("sparc", {}).get("media", float("nan"))
                tm = rs.get("tempo_movimento_s", {}).get("media", float("nan"))
                extra = ""
                if "forca_media_N" in rs:
                    extra = f", força {rs['forca_media_N']['media']:.1f} N"
                print(f"  semana {semana + 1} · {NOMES[cid]:12s} partida {numero}: "
                      f"{rs.get('n_completas', 0)} tentativas, SPARC {sp:.2f}, "
                      f"tempo {tm:.1f} s{extra}")
            ultimas[cid] = sid
    for cid, sid in ultimas.items():
        arq = pasta / f"relatorio_{NOMES[cid].replace('í', 'i')}.pdf"
        gerar_relatorio(banco, ctx, sid, arq)
        print(f"Relatório: {arq}")
    print(f"Auditoria: {len(banco.auditoria())} registros imutáveis")
    banco.fechar()


def cmd_reprocessar(a) -> None:
    from .armazenamento import Banco, ContextoApp
    from .pipeline import processar_e_salvar
    banco = Banco(a.pasta)
    ctx = ContextoApp(banco.operador_local())
    rows = banco.con.execute(
        "SELECT p.id, s.caminho FROM partida p JOIN sessao s ON s.id = p.sessao_id").fetchall()
    for r in rows:
        cal = banco.calibracao_vigente("BANC-007", f"transicoes_{r['caminho']}")
        tab = TabelaTransicoes.de_dict(cal["dados"]) if cal else None
        processar_e_salvar(banco, ctx, r["id"], tab)
        print(".", end="", flush=True)
    print(f"\n{len(rows)} partidas reprocessadas com {VERSAO_ALGORITMO}")


def cmd_jogar(a) -> None:
    from .jogos.base import Dificuldade
    from .jogos.executor import JOGO_DO_CAMINHO, JOGOS, FonteMouse, FonteSimulada, executar
    caminho_do_jogo = {v: k for k, v in JOGO_DO_CAMINHO.items()}
    cid = caminho_do_jogo[a.jogo]
    cam = obter_caminho(cid)
    dif = Dificuldade(duracao_s=a.duracao, limite_forca_N=a.limite_forca, repeticoes=a.repeticoes)
    if a.entrada == "mouse":
        fonte = FonteMouse(cam)
    else:
        r = simular_partida(cam, 8 if not cam.vai_e_volta else 2,
                            PerfilPaciente(velocidade_media_mm_s=80), semente=a.semente)
        fonte = FonteSimulada(r.dados, _tabela_simulada(cid))
    res = executar(JOGOS[a.jogo](dif), fonte, tela_cheia=a.tela_cheia)
    print(json.dumps(res["metricas"], indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="trilho", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("demo", help="fluxo completo com dados simulados")
    s.add_argument("--pasta", default="dados_demo")
    s.add_argument("--semanas", type=int, default=4)
    s.add_argument("--partidas", type=int, default=2)
    s.add_argument("--caminhos", default="ABC")
    s.set_defaults(f=cmd_demo)

    s = sub.add_parser("validar", help="erro da fusão contra a verdade simulada")
    s.add_argument("--caminhos", default="CAB")
    s.add_argument("--velocidades", type=float, nargs="+", default=[60.0, 30.0])
    s.add_argument("--repeticoes", type=int, default=3)
    s.add_argument("--comparar-imu", action="store_true",
                   help="repete com uma IMU de baixo ruído (ICM-42688-P)")
    s.add_argument("--saida", help="CSV com os resultados de cada partida")
    s.set_defaults(f=cmd_validar)

    s = sub.add_parser("calibrar-ir", help="tabela de transições dos LM393 (simulada)")
    s.add_argument("--caminho", default="B", choices=list("ABC"))
    s.add_argument("--versao", default="sim-v1")
    s.add_argument("--saida", default="tabela_transicoes.json")
    s.set_defaults(f=cmd_calibrar_ir)

    s = sub.add_parser("simular", help="gera uma partida simulada")
    s.add_argument("--caminho", default="C", choices=list("ABC"))
    s.add_argument("--tentativas", type=int, default=4)
    s.add_argument("--velocidade", type=float, default=60.0, help="mm/s")
    s.add_argument("--semente", type=int, default=1)
    s.add_argument("--saida", default="partida_simulada")
    s.set_defaults(f=cmd_simular)

    s = sub.add_parser("processar", help="processa uma partida gravada")
    s.add_argument("pasta")
    s.add_argument("--tabela", help="JSON da tabela de transições")
    s.set_defaults(f=cmd_processar)

    s = sub.add_parser("reprocessar", help="reprocessa o banco com o algoritmo atual")
    s.add_argument("--pasta", default="dados_demo")
    s.set_defaults(f=cmd_reprocessar)

    s = sub.add_parser("jogar", help="abre um mini-game")
    s.add_argument("--jogo", default="carro", choices=["carro", "vidro", "bolo"])
    s.add_argument("--entrada", default="simulada", choices=["simulada", "mouse"])
    s.add_argument("--duracao", type=float, default=90.0)
    s.add_argument("--limite-forca", type=float, default=30.0)
    s.add_argument("--repeticoes", type=int, default=3)
    s.add_argument("--semente", type=int, default=1)
    s.add_argument("--tela-cheia", action="store_true")
    s.set_defaults(f=cmd_jogar)

    a = p.parse_args(argv)
    if hasattr(a, "caminhos") and isinstance(a.caminhos, str):
        a.caminhos = [c for c in a.caminhos.upper() if c in "ABC"]
    a.f(a)


if __name__ == "__main__":
    sys.exit(main())
