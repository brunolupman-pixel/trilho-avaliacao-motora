"""Escolha da porta USB da caixa (botão "Conectar ESP32" do site).

Quem abre a porta é sempre este programa, não o navegador: assim a aquisição, os
reenvios (ACK/NACK) e a gravação continuam funcionando quando o profissional troca
de página no site. O site só escolhe QUAL porta usar.

Modos:
- "auto": a primeira porta com chip USB típico de placas ESP32 (padrão);
- "COM5" (ou /dev/ttyUSB0...): a porta escolhida no site;
- desligado: nenhuma porta aberta (libera a porta para gravar o firmware na Arduino IDE).

A escolha fica salva em <pasta de dados>/porta.json e vale para as próximas aberturas.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Fabricantes de chip USB-serial comuns em placas ESP32 (VID USB)
CHIPS_ESP32 = {0x303A: "Espressif (USB nativo)", 0x10C4: "Silicon Labs CP210x", 0x1A86: "WCH CH340/CH343",
               0x0403: "FTDI"}
BAUD = 921600
SIMULADA = "SIMULADOR"


def _descrever_erro(e: Exception) -> str:
    t = str(e)
    baixo = t.lower()
    if "access is denied" in baixo or "acesso negado" in baixo or "permission" in baixo or "busy" in baixo:
        return "porta ocupada: feche o Monitor Serial da Arduino IDE (ou outro programa usando a porta)"
    if "could not open" in baixo or "no such file" in baixo or "filenotfound" in baixo:
        return "a porta sumiu: confira o cabo USB"
    return t or e.__class__.__name__


def abrir_serial(nome: str, baud: int, vid: int | None = None):
    """Abre a porta sem segurar a ESP32 em reset.

    Em placas com conversor USB-serial (CP210x, CH340, FTDI), as linhas DTR/RTS ligam o
    circuito de reset automático: abrir com RTS ligado pode deixar o chip parado em reset
    ou em modo de gravação. Por isso abrimos com as duas desligadas. No USB nativo do
    ESP32-S3 (Espressif) não existe esse circuito, e o DTR ligado indica "computador
    conectado" para a placa começar a mandar dados."""
    import serial
    s = serial.Serial()
    s.port, s.baudrate, s.timeout, s.write_timeout = nome, baud, 0.01, 0.2
    nativo = vid == 0x303A
    s.dtr = nativo
    s.rts = False
    s.open()
    return s


def analisar_amostra(dados: bytes) -> dict:
    """Classifica o que a placa mandou: nada, texto (qual) ou bytes sem sentido."""
    if not dados:
        return {"tipo": "nada"}
    imprimiveis = sum(1 for b in dados if 32 <= b < 127 or b in (9, 10, 13))
    if imprimiveis / len(dados) > 0.85:
        texto = dados.decode("ascii", "replace")
        linhas = [l.strip() for l in texto.replace("\r", "\n").split("\n") if l.strip()]
        antigo = sum(1 for l in linhas if l[:2] in ("P,", "C,", "M,", "W,", "J,", "I,") and "*" not in l)
        return {"tipo": "texto", "linhas": linhas[:4], "firmware_antigo": antigo >= max(1, len(linhas) // 3)}
    return {"tipo": "ilegivel"}


def explicar_sem_resposta(porta: str, amostra_921k: bytes, amostra_115k: bytes | None) -> str:
    """Mensagem para o profissional quando a porta abre, mas a caixa não responde."""
    gravar = "Grave nela o firmware/caixa/caixa.ino pela Arduino IDE (veja o README, \"Gravar o firmware da caixa\")."
    a = analisar_amostra(amostra_921k)
    b = analisar_amostra(amostra_115k) if amostra_115k is not None else {"tipo": "nada"}
    for x in (a, b):
        if x["tipo"] == "texto":
            exemplo = " | ".join(x["linhas"][:2])
            if x["firmware_antigo"]:
                return (f"A placa em {porta} está com o programa antigo (o usado com o HTML anterior: manda linhas de "
                        f"texto como \"{exemplo}\"). O Trilho precisa do firmware novo da caixa. {gravar}")
            return (f"A placa em {porta} está mandando texto (\"{exemplo}\"), não os dados do Trilho: está com outro "
                    f"programa gravado. {gravar}")
    if a["tipo"] == "nada" and b["tipo"] == "nada":
        return (f"A placa em {porta} não mandou nada. Aperte o botão RESET (ou EN) da placa e clique em Conectar de "
                f"novo. Se continuar, a placa está sem programa ou com o firmware travando ao iniciar: {gravar} "
                f"Depois, abra o Monitor Serial da Arduino IDE em 921600 baud por alguns segundos (devem aparecer "
                f"\"# TRILHO caixa\" e linhas como \"S,...\" e \"I,...\"), feche o monitor e conecte aqui.")
    return (f"Chegam dados da placa em {porta}, mas não no formato do Trilho (velocidade 921600 baud). Provavelmente "
            f"outro programa está gravado nela. {gravar}")


class _ProxySimulada:
    """Deixa a bancada simulada ser "desconectada" e "conectada" pelo site sem parar o simulador."""

    def __init__(self, sim) -> None:
        self.sim = sim
        self.is_open = True
        with sim._trava:                 # descarta o que acumulou enquanto estava desconectada
            sim._buf.clear()

    @property
    def in_waiting(self) -> int:
        return self.sim.in_waiting if self.is_open else 0

    def read(self, n: int = 1) -> bytes:
        if not self.is_open:
            raise OSError("porta fechada")
        return self.sim.read(n)

    def write(self, dados: bytes) -> int:
        if not self.is_open:
            raise OSError("porta fechada")
        return self.sim.write(dados)

    def close(self) -> None:
        self.is_open = False


class GerenciadorPortas:
    def __init__(self, pasta_dados: Path | None = None, porta_fixa: str | None = None, simulada=None) -> None:
        self.arquivo = (pasta_dados / "porta.json") if pasta_dados else None
        self.simulada = simulada
        self.trava = threading.Lock()
        self.modo = "auto"               # "auto" | nome da porta
        self.ligada = True               # False = desconectada pelo site
        self.aberta: str | None = None   # porta aberta agora
        self.erro: str | None = None     # último problema ao abrir
        self._pausada = False            # sondagem de diagnóstico em andamento
        self._carregar()
        if porta_fixa:                   # --porta COM5 na linha de comando tem prioridade
            self.modo = porta_fixa

    # ------------------------------------------------------------ persistência
    def _carregar(self) -> None:
        if self.arquivo and self.arquivo.exists():
            try:
                d = json.loads(self.arquivo.read_text(encoding="utf-8"))
                self.modo = d.get("modo") or "auto"
                self.ligada = bool(d.get("ligada", True))
            except Exception:
                log.warning("porta.json ilegível; usando detecção automática")

    def _salvar(self) -> None:
        if self.arquivo:
            try:
                self.arquivo.write_text(json.dumps({"modo": self.modo, "ligada": self.ligada}), encoding="utf-8")
            except OSError as e:
                log.warning("não salvou porta.json: %s", e)

    # ------------------------------------------------------------ consulta
    def listar(self) -> list[dict]:
        """Portas seriais que o computador vê agora, com as que parecem ESP32 primeiro."""
        if self.simulada is not None:
            return [{"porta": SIMULADA, "descricao": "Bancada simulada (sem hardware)", "chip": "simulador",
                     "parece_esp32": True}]
        try:
            from serial.tools import list_ports
            itens = list_ports.comports()
        except Exception as e:      # pyserial ausente ou erro do sistema
            log.warning("não listou as portas: %s", e)
            return []
        out = []
        for x in itens:
            chip = CHIPS_ESP32.get(x.vid) if x.vid is not None else None
            desc = x.description if x.description and x.description != "n/a" else x.device
            out.append({"porta": x.device, "descricao": desc, "chip": chip or (x.manufacturer or "desconhecido"),
                        "parece_esp32": chip is not None})
        out.sort(key=lambda d: (not d["parece_esp32"], d["porta"]))
        return out

    def _vid(self, nome: str) -> int | None:
        try:
            from serial.tools import list_ports
            return next((x.vid for x in list_ports.comports() if x.device == nome), None)
        except Exception:
            return None

    # ------------------------------------------------------------ diagnóstico
    def pausar(self) -> None:
        """Faz o laço de aquisição soltar a porta sem gravar a escolha (para a sondagem)."""
        with self.trava:
            self._pausada = True

    def retomar(self) -> None:
        with self.trava:
            self._pausada = False

    def sondar(self, nome: str, baud: int, segundos: float = 2.0) -> bytes:
        """Abre a porta em outra velocidade e lê o que a placa manda (porta já liberada)."""
        import time
        s = abrir_serial(nome, baud, self._vid(nome))
        try:
            fim, dados = time.monotonic() + segundos, bytearray()
            while time.monotonic() < fim and len(dados) < 4000:
                dados.extend(s.read(512))
            return bytes(dados)
        finally:
            s.close()

    def estado(self) -> dict:
        with self.trava:
            return {"modo": self.modo, "ligada": self.ligada, "aberta": self.aberta, "erro": self.erro,
                    "simulador": self.simulada is not None}

    # ------------------------------------------------------------ escolha (site)
    def escolher(self, porta: str | None) -> None:
        with self.trava:
            self.modo = porta or "auto"
            self.ligada = True
            self.erro = None
            self._salvar()

    def desligar(self) -> None:
        with self.trava:
            self.ligada = False
            self.erro = None
            self._salvar()

    def fechou(self) -> None:
        with self.trava:
            self.aberta = None

    # ------------------------------------------------------------ usado pela Aquisicao
    def abrir(self):
        """Chamado pelo laço de aquisição a cada ~2 s enquanto não houver porta aberta."""
        with self.trava:
            if not self.ligada or self._pausada:
                self.aberta, self.erro = None, None
                return None
            modo = self.modo
        if self.simulada is not None:
            with self.trava:
                self.aberta, self.erro = SIMULADA, None
            return _ProxySimulada(self.simulada)
        nome = None if modo == "auto" else modo
        if nome is None:
            candidatas = [d for d in self.listar() if d["parece_esp32"]]
            if not candidatas:
                with self.trava:
                    self.aberta, self.erro = None, "nenhuma ESP32 encontrada nas portas USB"
                return None
            nome = candidatas[0]["porta"]
        try:
            s = abrir_serial(nome, BAUD, self._vid(nome))
        except Exception as e:
            with self.trava:
                self.aberta, self.erro = None, f"{nome}: {_descrever_erro(e)}"
            raise
        with self.trava:
            self.aberta, self.erro = nome, None
        return s
