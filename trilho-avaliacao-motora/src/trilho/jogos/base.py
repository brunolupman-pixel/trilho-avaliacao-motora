"""Interface comum dos mini-games.

Cada mini-game é um módulo com as mesmas funções (iniciar, atualizar com o
estado do joystick, desenhar, terminou) e devolve seus eventos no relógio da
sessão. Novos jogos entram sem mexer no resto do sistema.

Entrada do jogo: posição ao longo do caminho (0 a 100 % do percurso), a
velocidade e, no joystick C, a força de preensão.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pygame

# paleta de alto contraste; nenhuma informação depende só da cor
FUNDO = (18, 22, 30)
TEXTO = (240, 244, 248)
SUAVE = (148, 160, 178)
DESTAQUE = (56, 189, 248)
BOM = (74, 222, 128)
ALERTA = (251, 191, 36)
RUIM = (248, 113, 113)


@dataclass
class EstadoJoystick:
    t: float                    # relógio da sessão (s)
    fracao: float               # 0..1 ao longo do caminho
    s_mm: float
    v_mm_s: float
    forca_N: float | None = None
    repouso: bool = False


@dataclass
class EventoJogo:
    t: float
    tipo: str
    valor: float | str | None = None


@dataclass
class Dificuldade:
    """Definida pelo profissional; o jogo nunca exige mais do que isto."""

    duracao_s: float = 90.0
    trecho: tuple[float, float] = (0.0, 1.0)     # parte do percurso usada
    velocidade_alvo: float = 0.5                 # 0..1 (carro) ou limite (bolo)
    limite_forca_N: float = 30.0                 # joystick C
    repeticoes: int = 3                          # passadas/idas e voltas
    extra: dict = field(default_factory=dict)


class Jogo(ABC):
    nome: str = "jogo"
    caminho: str = "?"

    def __init__(self, dificuldade: Dificuldade | None = None) -> None:
        self.dif = dificuldade or Dificuldade()
        self.eventos: list[EventoJogo] = []
        self.t0: float | None = None
        self.t = 0.0
        self._fim = False

    def evento(self, tipo: str, valor=None) -> None:
        self.eventos.append(EventoJogo(self.t, tipo, valor))

    def iniciar(self, tela: pygame.Surface) -> None:
        self.tela = tela
        self.W, self.H = tela.get_size()
        self.fonte = pygame.font.Font(None, max(28, self.H // 22))
        self.fonte_grande = pygame.font.Font(None, max(56, self.H // 9))
        self.preparar()

    def atualizar(self, estado: EstadoJoystick, dt: float) -> None:
        if self.t0 is None:
            self.t0 = estado.t
            self.evento("inicio")
        self.t = estado.t
        self.passo(estado, dt)
        if estado.t - self.t0 >= self.dif.duracao_s:
            self.encerrar("tempo")

    def encerrar(self, motivo: str) -> None:
        if not self._fim:
            self.evento("fim", motivo)
            self._fim = True

    def terminou(self) -> bool:
        return self._fim

    def escrever(self, texto: str, pos: tuple[int, int], cor=TEXTO, grande: bool = False,
                 centro: bool = False) -> None:
        f = self.fonte_grande if grande else self.fonte
        img = f.render(texto, True, cor)
        r = img.get_rect()
        if centro:
            r.center = pos
        else:
            r.topleft = pos
        self.tela.blit(img, r)

    def barra(self, x: int, y: int, w: int, h: int, valor: float, cor, rotulo: str,
              marca: float | None = None) -> None:
        pygame.draw.rect(self.tela, SUAVE, (x, y, w, h), 2, border_radius=6)
        pygame.draw.rect(self.tela, cor, (x + 3, y + 3, int((w - 6) * max(0.0, min(1.0, valor))),
                                          h - 6), border_radius=4)
        if marca is not None:
            mx = x + int(w * marca)
            pygame.draw.line(self.tela, TEXTO, (mx, y - 6), (mx, y + h + 6), 3)
        self.escrever(rotulo, (x, y - self.fonte.get_height() - 2), SUAVE)

    @abstractmethod
    def preparar(self) -> None: ...

    @abstractmethod
    def passo(self, estado: EstadoJoystick, dt: float) -> None: ...

    @abstractmethod
    def desenhar(self) -> None: ...

    @abstractmethod
    def metricas(self) -> dict: ...
