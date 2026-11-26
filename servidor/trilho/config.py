"""Configuração: onde ficam os dados e como achar a bancada.

Variáveis de ambiente (todas opcionais):
  TRILHO_DADOS   pasta dos dados (padrão: %LOCALAPPDATA%\\Trilho no Windows, ~/.trilho no resto)
  TRILHO_PORTA   porta serial da bancada (ex.: COM5); vazio = detectar sozinho
  TRILHO_HTTP    porta do site local (padrão 8000)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def pasta_dados() -> Path:
    if os.environ.get("TRILHO_DADOS"):
        p = Path(os.environ["TRILHO_DADOS"])
    elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        p = Path(os.environ["LOCALAPPDATA"]) / "Trilho"
    else:
        p = Path.home() / ".trilho"
    p.mkdir(parents=True, exist_ok=True)
    (p / "brutos").mkdir(exist_ok=True)
    return p


PORTA_SERIAL = os.environ.get("TRILHO_PORTA") or None
PORTA_HTTP = int(os.environ.get("TRILHO_HTTP", "8000"))
PASTA_WEB = Path(__file__).resolve().parent.parent.parent / "web"
