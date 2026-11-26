"""python -m trilho  [--simulador] [--porta COM5] [--http 8000] [--sem-navegador]"""
from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from . import banco, config, cripto
from .aquisicao import Aquisicao
from .portas import GerenciadorPortas
from .api import criar_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="trilho", description="Servidor local do Trilho")
    ap.add_argument("--simulador", action="store_true", help="usa a caixa simulada (sem hardware)")
    ap.add_argument("--simulador-bancada", action="store_true", help=argparse.SUPPRESS)   # hardware antigo, sem fio
    ap.add_argument("--porta", default=config.PORTA_SERIAL, help="porta serial da bancada (ex.: COM5)")
    ap.add_argument("--http", type=int, default=config.PORTA_HTTP, help="porta do site local")
    ap.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pasta = config.pasta_dados()
    cripto.iniciar(pasta)
    banco.iniciar(pasta / "trilho.db")
    logging.getLogger("trilho").info("dados em %s", pasta)

    porta_sim = None
    if a.simulador or a.simulador_bancada:
        from .simulador import PortaSimulada
        porta_sim = PortaSimulada(caixa=not a.simulador_bancada)
    # a porta da caixa: automática, a escolhida no site ("Conectar ESP32") ou a de --porta
    portas = GerenciadorPortas(None if porta_sim else pasta, a.porta, simulada=porta_sim)
    aq = Aquisicao(portas.abrir)
    aq.ao_fechar_porta = portas.fechou
    aq.iniciar()
    app = criar_app(aq, pasta, porta_sim, portas)

    url = f"http://127.0.0.1:{a.http}/"
    if not a.sem_navegador:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\n  Trilho rodando em {url}  (Ctrl+C para sair)\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.http, log_level="warning")
    finally:
        aq.parar()


if __name__ == "__main__":
    main()
