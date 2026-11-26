"""Fusão dos sensores ao longo do caminho (arquitetura: "Fusão de dados e cinemática").

Estado do filtro de Kalman: x = [s, v, a, j] ao longo do trilho (mm, mm/s,
mm/s², mm/s³), modelo de jerk constante por partes.

Medidas:
- IMU (500 Hz): aceleração projetada na tangente do caminho em s (a gravidade
  sai com a inclinação medida em repouso no início; o giroscópio vertical
  acompanha o giro da empunhadura);
- LM393: cada troca estável vira uma posição lida da tabela de transições
  (mesmo sensor, mesma troca e mesmo sentido; escolhe a divisa mais próxima
  do previsto; rejeita se a diferença passar de 3 desvios);
- VL53L0X (caminho reto): posição contínua;
- pontas da fenda (batentes mecânicos): se a estimativa passa de uma ponta,
  a ponta vira medida de posição.
Não há ZUPT (velocidade zero em repouso): ver o comentário em imu().

O mesmo filtro roda em tempo real (para o jogo) e, depois da partida, de novo
sobre os dados brutos, guardando o histórico para o suavizador RTS.
"""
from __future__ import annotations

import heapq
import itertools
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .caminhos import Caminho
from .protocolo import ACC_LSB_POR_G, G_MM_S2, GIRO_LSB_POR_GRAU_S

VERSAO_ALGORITMO = "fusao-1.0"


@dataclass
class ConfigFusao:
    q_jerk: float = 2.0e9         # densidade espectral do "snap" (mm²/s⁷)
    sigma_acc: float = 40.0       # mm/s² (ruído do MPU6050)
    sigma_vies0: float = 30.0     # mm/s²: viés inicial da aceleração horizontal (0,1° de inclinação ≈ 17 mm/s²)
    q_vies: float = 1.0           # mm²/s⁵: quanto o viés pode mudar com o tempo
    sigma_vl53: float = 6.0       # mm (VL53L0X real: ~5 mm de ruído e picos isolados)
    mediana_vl53: int = 3         # mediana das últimas leituras: some com picos isolados do sensor
    q_jerk_vl53: float = 5.0e6    # caminho reto só com o VL53: movimento mais "calmo" (menos ruído na barra)
    sigma_zupt_v: float = 3.0     # mm/s
    sigma_zupt_a: float = 30.0    # mm/s²
    sigma_batente: float = 1.5    # mm
    tol_batente: float = 25.0     # mm: distância máxima para usar a ponta
    # "Beco": trecho final sem sensor (espiral: do último cruzamento até o centro). Só há um
    # caminho, ir até o fim e voltar. A chegada ao fim conta quando o joystick para (ou inverte
    # o sentido) depois de avançar boa parte do beco pela estimativa, ou depois de beco_tempo_s
    # sem nenhum sensor (regra combinada com a equipe), nunca antes do tempo mínimo plausível.
    beco_fracao: float = 0.6      # parte do beco que a estimativa precisa ter andado
    beco_tempo_s: float = 5.0     # tempo no beco que basta (com o joystick parado ou voltando)
    beco_v_max: float = 300.0     # mm/s: velocidade máxima plausível (tempo mínimo = beco / v_max)
    beco_quieto_s: float = 1.5    # IMU quieto por este tempo no beco = parado (mesmo com a velocidade estimada viciada)
    beco_v_parado: float = 15.0   # mm/s: velocidade estimada abaixo disso (com o IMU quieto) = parado
    v_max_ponta: float = 40.0     # mm/s: velocidade estimada máxima para fixar na ponta
    janela_repouso: int = 60      # amostras (120 ms)
    limiar_repouso_acc: float = 60.0   # mm/s² (desvio padrão na janela)
    limiar_repouso_giro: float = 2.0   # °/s
    v_max_repouso: float = 10.0   # mm/s: só declara repouso se o filtro concorda
    amostras_nivelamento: int = 100    # 0,2 s parado no início para medir a gravidade
    rotacao_montagem_graus: float = 0.0  # giro do eixo x da IMU em relação ao eixo x da placa
    portao_sigmas: float = 3.0
    v_sentido: float = 3.0        # mm/s: abaixo disso, o sentido é incerto
    # Saída de um feixe: conta como "atravessou" (mesmo sentido em que entrou). A velocidade estimada
    # nesse instante não é confiável para dizer o contrário; se o joystick deu meia-volta dentro do
    # feixe, o próximo sensor corrige. Valor finito = aceita "voltou" acima dessa velocidade contrária.
    v_saida_feixe: float = float("inf")
    # Caminho reto: a posição vem só do sensor de distância (absoluto, ~50 medidas/s, milímetros).
    # A aceleração do IMU só atrapalha se ele estiver montado girado em relação à placa (no
    # equipamento real fazia a alavanca do carro oscilar); o IMU continua gravado para o tremor.
    imu_na_posicao_com_vl53: bool = False
    # Espiral: proteção contra acionamentos falsos dos sensores IR (reflexo, mão, luz). Um
    # cruzamento só vale se a distância desde o último cruzamento aceito (ou desde a entrada, no
    # começo) puder ter sido percorrida nesse tempo, e se não pular mais de um cruzamento da ordem.
    v_max_cruzamento: float = 300.0   # mm/s: acima disso o cruzamento é impossível (sensor falso; = beco_v_max)
    dist_min_plausibilidade: float = 30.0   # mm: abaixo disso não confere (entrada e saída do mesmo feixe)
    # Espiral: a posição mostrada no jogo vem SÓ dos sensores IR (RastreadorZonas): a colher anda
    # com a velocidade medida entre os dois últimos feixes e para no próximo feixe até ele ser
    # cruzado. O IMU continua gravado e entra no pós-processamento (métricas). Com o IMU na
    # posição, uma montagem diferente da esperada fazia a estimativa disparar de ponta a ponta.
    espiral_tela_por_sensores: bool = True
    # Espiral: o joystick não gira com o caminho, então o ângulo da placa do IMU em torno da vertical
    # é fixo. Ele é DESCOBERTO pelos dados (nas curvas a aceleração aponta para o centro da curva,
    # direção conhecida pela geometria). Com o ângulo descoberto, a fusão com o IMU volta para a
    # tela entre os feixes (sempre dentro do trecho que os sensores permitem).
    espiral_aprender_montagem: bool = True
    montagem_amostras: int = 200      # amostras confiáveis por rodada de estimativa
    montagem_coerencia: float = 0.5   # 0..1: quão bem os vetores concordam para aceitar a rodada
    montagem_convergiu_graus: float = 10.0   # correção menor que isso = ângulo confirmado
    montagem_acc_min: float = 20.0    # mm/s²: aceleração centrípeta prevista mínima para usar a amostra
    montagem_ja_conhecida: bool = False   # ângulo dado por rotacao_montagem_graus já confirmado (2ª passada)
    imu_tela_distancia_mm: float = 45.0   # fusão mais longe que isso dos sensores = a tela usa só os sensores
    zonas_acc_parado: float = 25.0    # mm/s²: aceleração horizontal média abaixo disso (e IMU quieto) = mão parada
    zonas_acc_ruido: float = 8.0      # mm/s²: ruído da aceleração (suavizada) descontado antes de virar velocidade
    sigma_giro: float = 0.06      # rad/s: ruído do giroscópio usado como medida de velocidade no arco
    giro_min_rad: float = 0.15    # giro mínimo (~9°) para concluir que o IMU gira com o arco
    # Arco (janela): proteções contra o sensor de ponta acionando à toa e contra uma escala do giro
    # absurda (ex.: medida numa partida com o IMU com defeito ou em conflito de endereço). Com a
    # escala muito pequena, o ruído do giroscópio virava velocidades enormes e a estimativa batia
    # de uma ponta na outra.
    escala_giro_fator_min: float = 0.33   # escala aceita: entre 1/3 e 3 vezes a do desenho do arco
    escala_giro_fator_max: float = 3.0
    v_max_ponta_a_ponta: float = 500.0    # mm/s: ir de uma ponta à outra mais rápido que isso = sensor falso
    giro_ponta_min: float = 0.35          # chegou à outra ponta com menos que 35% do giro esperado = sensor falso
    v_max_arco: float = 800.0             # mm/s: velocidade estimada acima disso é limitada
    # Sentido do giro invertido (ex.: o primeiro movimento foi para o lado errado): a estimativa
    # fica "empurrando" uma ponta sem o sensor daquela ponta ver o joystick. Por mais que isso, o
    # sentido é corrigido na hora (e a posição espelhada).
    inversao_tempo_s: float = 0.4
    inversao_v_min: float = 20.0          # mm/s empurrando para fora da ponta


N_ESTADO = 6   # s, v, a, j, viés x, viés y


def _F(dt: float) -> np.ndarray:
    F = np.eye(N_ESTADO)
    F[:4, :4] = [[1, dt, dt * dt / 2, dt ** 3 / 6],
                 [0, 1, dt, dt * dt / 2],
                 [0, 0, 1, dt],
                 [0, 0, 0, 1]]
    return F


def _H(*linhas) -> np.ndarray:
    """Matriz de medida com as colunas que faltam (vieses) em zero."""
    H = np.zeros((len(linhas), N_ESTADO))
    for i, l in enumerate(linhas):
        H[i, :len(l)] = l
    return H


def _Q(dt: float, q: float, q_vies: float) -> np.ndarray:
    d2, d3, d4, d5, d6, d7 = dt ** 2, dt ** 3, dt ** 4, dt ** 5, dt ** 6, dt ** 7
    Q = np.zeros((N_ESTADO, N_ESTADO))
    Q[:4, :4] = q * np.array([[d7 / 252, d6 / 72, d5 / 30, d4 / 24],
                              [d6 / 72, d5 / 20, d4 / 8, d3 / 6],
                              [d5 / 30, d4 / 8, d3 / 3, d2 / 2],
                              [d4 / 24, d3 / 6, d2 / 2, dt]], dtype=float)
    Q[4, 4] = Q[5, 5] = q_vies * dt
    return Q


def _rotacao_para_z(u: np.ndarray) -> np.ndarray:
    """Matriz que leva o vetor unitário u em (0, 0, 1)."""
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(u, z)
    c = float(np.dot(u, z))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1 / (1 + c))


@dataclass
class Contadores:
    bordas_aceitas: int = 0
    bordas_rejeitadas: int = 0
    giro_atualizacoes: int = 0
    giro_sinal: int = 0           # +1/-1 quando aprendido; 0 = ainda não (ou o IMU não gira com o arco)
    giro_graus_por_mm: float = 0.0  # escala medida entre as pontas (0 = ainda não medida)
    vl53_aceitas: int = 0
    vl53_rejeitadas: int = 0
    vl53_fora: int = 0
    vl53_reinicios: int = 0
    repousos: int = 0
    batentes: int = 0
    imu_invalidas: int = 0        # leituras impossíveis descartadas (ex.: só zeros = IMU "dormindo")
    falhas_numericas: int = 0     # atualizações descartadas por conta impossível (proteção)
    ordem_ressincronizada: int = 0   # cruzamentos fora da ordem esperada (passagem não detectada)
    beco_inicio: int = 0          # voltas à entrada estimadas no trecho sem sensor antes do 1º cruzamento
    limites_zona: int = 0         # vezes em que a estimativa foi trazida de volta ao trecho entre sensores
    beco_centro: int = 0          # chegadas ao fim estimadas no trecho sem sensor (centro da espiral)
    cruzamentos_implausiveis: int = 0   # espiral: ignorados por exigirem velocidade impossível
    cruzamentos_saltados: int = 0       # espiral: ignorados por pularem mais de um cruzamento da ordem
    montagem_graus: float | None = None   # espiral: ângulo da placa do IMU descoberto (em torno da vertical)
    montagem_rodadas: int = 0             # rodadas de estimativa aceitas
    montagem_confirmada: bool = False     # ângulo confirmado (a fusão com o IMU pode ir para a tela)
    zonas_paradas: int = 0                # espiral: vezes em que a colher parou porque a mão parou
    zonas_velocidades_feixe: int = 0      # espiral: velocidades medidas pelo tempo dentro do feixe
    pontas_implausiveis: int = 0          # arco: sensor de ponta ignorado (rápido demais ou sem giro)
    escala_giro_descartada: int = 0       # arco: escala do giro fora do plausível (guardada ou medida)
    velocidade_limitada: int = 0          # arco: vezes em que a velocidade estimada foi limitada
    giro_sentido_corrigido: int = 0       # arco: vezes em que o sentido do giro estava invertido e foi corrigido
    giro_eixo: list | None = None          # eixo do giro no arco, nos eixos do sensor
    giro_eixo_vertical_graus: float | None = None   # inclinação desse eixo em relação à vertical

    def como_dict(self) -> dict:
        return dict(self.__dict__)


class FiltroCaminho:
    def __init__(self, caminho: Caminho, tabela: list[dict] | None = None, calib_vl53: dict | None = None,
                 config: ConfigFusao | None = None, s0: float | None = None, sigma_s0: float = 3.0,
                 historico: bool = False, escala_giro: float | None = None) -> None:
        self.c = caminho
        self.cfg = config or ConfigFusao()
        self.tabela = list(tabela or [])
        self.calib_vl53 = calib_vl53 or {"tras": 50.0, "frente": 170.0}
        s0 = caminho.inicio if s0 is None else s0
        self.x = np.array([s0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # a partida começa com o joystick parado na posição inicial; os dois
        # últimos estados são o viés da aceleração horizontal (erro do
        # nivelamento/inclinação), estimado pelo próprio filtro
        self.P = np.diag([sigma_s0 ** 2, 2.0 ** 2, 30.0 ** 2, 300.0 ** 2,
                          self.cfg.sigma_vies0 ** 2, self.cfg.sigma_vies0 ** 2])
        self.t: float | None = None
        self._t_inicio: float | None = None   # primeira leitura da partida (referência da entrada)
        self.cont = Contadores()
        # IMU
        self._acum_g: list[np.ndarray] = []
        self.R_nivel: np.ndarray | None = None
        self.g_mod = G_MM_S2
        self.psi = math.radians(self.cfg.rotacao_montagem_graus)
        self.vies_giro = 0.0
        self._janela_acc: deque = deque(maxlen=self.cfg.janela_repouso)
        self._janela_giro: deque = deque(maxlen=self.cfg.janela_repouso)
        self._t_imu_ant: float | None = None
        self.em_repouso = False
        self._ponta_fixada: set[float] = set()
        # sensores
        self.nivel = {s: 0 for s in caminho.sensores}
        # cruzamentos na ordem do caminho (centro de cada um) e quantos já foram passados na ida
        self._cruz = self._centros_cruzamentos()
        self._passados = 0
        self._dentro: tuple[int, str] | None = None      # (índice do cruzamento, sentido em que entrou)
        self._saiu_de: int | None = None                 # saiu deste feixe e o lado ainda não se sabe
        self._lado_saida = "ida"
        self._ult_cruz: tuple[float, float] | None = None   # (t, posição) do último cruzamento aceito
        self._trilha: list[tuple[float, int, float]] = []    # cruzamentos fora da contagem, em sequência
        # "becos": trechos sem sensor nas duas pontas (entrada antes do 1º cruzamento; centro depois
        # do último). t0 = quando entrou no beco pela ordem dos sensores.
        self._becos = {"fim": {"t0": None, "fixado": False, "vmax": 0.0},
                       "inicio": {"t0": None, "fixado": False, "vmax": 0.0}}
        self._faixas = self._faixas_cruzamentos()
        self.zonas = (RastreadorZonas(self.tabela, self._cruz, caminho.comprimento, caminho.inicio,
                                      self.cfg.v_max_cruzamento)
                      if len(self._cruz) > 2 and self.cfg.espiral_tela_por_sensores else None)
        self._mont_Z, self._mont_W, self._mont_n = 0j, 0.0, 0
        self._acc_media = 0j                              # média lenta da aceleração horizontal (viés)
        if self.zonas is not None and self.cfg.montagem_ja_conhecida:
            self.cont.montagem_confirmada = True
            self.cont.montagem_graus = round(self.cfg.rotacao_montagem_graus, 1)
        self._vl53_seguidas_fora = 0
        self._vl53_janela: deque = deque(maxlen=max(1, self.cfg.mediana_vl53))   # (t, mm)
        self.so_vl53 = caminho.usa_vl53 and not self.cfg.imu_na_posicao_com_vl53
        self._q_jerk = self.cfg.q_jerk_vl53 if self.so_vl53 else self.cfg.q_jerk
        # giroscópio no arco: o sentido do giro (como o IMU foi montado) é aprendido
        # comparando o quanto o IMU girou entre as duas pontas com o quanto o caminho gira
        # Escala do giroscópio (radianos de giro do IMU por mm andado no arco), como no código de
        # teste da equipe de hardware: estimada assim que o joystick sai da ponta A e medida de
        # verdade entre as duas pontas. Não depende do raio do desenho nem de como o IMU foi montado.
        self.escala_giro: float | None = escala_giro      # a medida em partidas anteriores, se houver
        self._ult_ponta: tuple[float, float, np.ndarray] | None = None   # (t, posição, giro acumulado)
        self._giro_inutil = False
        self._sentido_conferido = False
        self._psi_inicio: float | None = None
        self._marcas_giro: list[tuple[float, np.ndarray]] = []  # (s, giro acumulado) nas bordas aceitas
        if self.escala_giro is not None and not self._escala_plausivel(self.escala_giro):
            self.escala_giro = None                       # escala guardada absurda: mede de novo
            self.cont.escala_giro_descartada += 1
        # Eixo do giro no arco, descoberto pelos dados: num giro em torno de um eixo fixo, o vetor do
        # giroscópio aponta sempre na mesma direção (o eixo), qualquer que seja a montagem do IMU ou a
        # inclinação do arco. A soma do vetor ao longo do tempo é exatamente ângulo × eixo, então o
        # ângulo pode ser lido mesmo antes de o eixo ser descoberto.
        self._giro_amostras_vies: list[np.ndarray] = []
        self.vies3 = np.zeros(3)                  # viés do giroscópio, °/s (eixos do sensor)
        self.theta3 = np.zeros(3)                 # giro acumulado, rad (eixos do sensor)
        self._theta_inicio: np.ndarray | None = None
        self._M_eixo = np.zeros((3, 3))
        self._n_eixo = 0
        self._n_eixo_calc = 0
        self.eixo: np.ndarray | None = None       # eixo do giro (unitário, eixos do sensor)
        # histórico para o RTS
        self.historico = historico
        self._h_t: list[float] = []
        self._h_F: list[np.ndarray] = []
        self._h_xp: list[np.ndarray] = []
        self._h_Pp: list[np.ndarray] = []
        self._h_xf: list[np.ndarray] = []
        self._h_Pf: list[np.ndarray] = []

    # ---------------- núcleo de Kalman ----------------
    def _predizer(self, t: float) -> None:
        if self.t is None:
            self.t = t
            self._t_inicio = t
            if self.zonas is not None:
                self.zonas.iniciar(t)              # a partida começa com o joystick na entrada
            if self.historico:
                self._registrar_passo(np.eye(N_ESTADO))
            return
        dt = (t - self.t) / 1e6
        if dt <= 0:
            return  # mesma hora (ou ligeiramente fora de ordem): atualiza no passo atual
        F = _F(dt)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + _Q(dt, self._q_jerk, self.cfg.q_vies)
        self.t = t
        if self.historico:
            self._registrar_passo(F)

    def _registrar_passo(self, F: np.ndarray) -> None:
        self._h_t.append(self.t)
        self._h_F.append(F)
        self._h_xp.append(self.x.copy())
        self._h_Pp.append(self.P.copy())
        self._h_xf.append(self.x.copy())
        self._h_Pf.append(self.P.copy())

    def _atualizar(self, H: np.ndarray, z: np.ndarray, R: np.ndarray) -> None:
        self._atualizar_inovacao(H, z - H @ self.x, R)

    def _atualizar_inovacao(self, H: np.ndarray, y: np.ndarray, R: np.ndarray) -> None:
        # Proteção: uma medida absurda (ou um estado já estragado) nunca pode derrubar a
        # aquisição. A medida é descartada e, se preciso, o filtro recomeça de um estado seguro.
        if not (np.isfinite(self.x).all() and np.isfinite(self.P).all()):
            self.reiniciar_seguro()
        if not np.isfinite(y).all():
            self.cont.falhas_numericas += 1
            return
        S = H @ self.P @ H.T + R
        try:
            S_inv = np.linalg.inv(S)
        except np.linalg.LinAlgError:
            self.cont.falhas_numericas += 1
            return
        K = self.P @ H.T @ S_inv
        x_novo = self.x + K @ y
        I_KH = np.eye(N_ESTADO) - K @ H
        P_novo = I_KH @ self.P @ I_KH.T + K @ R @ K.T   # forma de Joseph (estável)
        if not (np.isfinite(x_novo).all() and np.isfinite(P_novo).all()):
            self.cont.falhas_numericas += 1
            return
        self.x, self.P = x_novo, P_novo
        if self.historico and self._h_xf:
            self._h_xf[-1] = self.x.copy()
            self._h_Pf[-1] = self.P.copy()

    def reiniciar_seguro(self) -> None:
        """Volta a um estado válido: posição atual (ou o início do caminho), parado."""
        s = float(self.x[0]) if np.isfinite(self.x[0]) else self.c.inicio
        s = min(max(s, 0.0), self.c.comprimento)
        self.x = np.array([s, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.P = np.diag([30.0 ** 2, 50.0 ** 2, 500.0 ** 2, 3000.0 ** 2,
                          self.cfg.sigma_vies0 ** 2, self.cfg.sigma_vies0 ** 2])
        if self.R_nivel is not None and not np.isfinite(self.R_nivel).all():
            self.R_nivel, self._acum_g = None, []
        if not math.isfinite(self.psi):
            self.psi = 0.0
        self.cont.falhas_numericas += 1

    def _posicao(self, z: float, sigma: float) -> None:
        self._atualizar(_H([1.0]), np.array([z]), np.array([[sigma ** 2]]))

    def _limites(self) -> None:
        # As pontas da fenda são batentes mecânicos: o joystick não passa delas.
        if self.zonas is not None and not 0.0 <= self.x[0] <= self.c.comprimento:
            # espiral: restrição (projeção), não medida repetida (ver _limites_zona)
            fora = -1.0 if self.x[0] < 0 else 1.0
            self.x[0] = 0.0 if fora < 0 else self.c.comprimento
            if self.x[1] * fora > 0:
                self.x[1] = 0.0
            self.cont.batentes += 1
            return
        if self.x[0] < 0:
            self._posicao(0.0, self.cfg.sigma_batente)
            self.cont.batentes += 1
        elif self.x[0] > self.c.comprimento:
            self._posicao(self.c.comprimento, self.cfg.sigma_batente)
            self.cont.batentes += 1

    # ---------------- medidas ----------------
    def imu(self, t: float, acc_bruto, giro_bruto) -> None:
        if self.c.usa_vl53 and not self.cfg.imu_na_posicao_com_vl53:
            return
        if not (np.isfinite(self.x).all() and np.isfinite(self.P).all()):
            self.reiniciar_seguro()
        acc = np.asarray(acc_bruto, dtype=float) * (G_MM_S2 / ACC_LSB_POR_G)
        giro = np.asarray(giro_bruto, dtype=float) / GIRO_LSB_POR_GRAU_S   # °/s
        # Leitura impossível: com o joystick na mão a aceleração medida nunca é ~0 (a gravidade
        # está sempre lá). Só zeros = MPU6050 "dormindo" (reiniciou por mau contato) ou falha.
        norma_g = float(np.linalg.norm(acc)) / G_MM_S2 if np.isfinite(acc).all() else 0.0
        if not (0.2 < norma_g < 8.0) or not np.isfinite(giro).all():
            self.cont.imu_invalidas += 1
            return
        if self.R_nivel is None:
            # nivelamento: joystick parado no início da partida
            self._acum_g.append(acc)
            self._giro_amostras_vies.append(giro)
            self._predizer(t)
            if len(self._acum_g) >= self.cfg.amostras_nivelamento:
                g = np.mean(self._acum_g, axis=0)
                self.g_mod = float(np.linalg.norm(g))
                self.R_nivel = _rotacao_para_z(g / self.g_mod)
                # ruído do acelerômetro parado (no plano): base do limiar de "movimento" nas pontas da espiral
                h = (np.asarray(self._acum_g) @ self.R_nivel.T)[:, :2]
                self._ruido_h = float(np.mean(np.std(h, axis=0)))
                self.vies3 = np.mean(self._giro_amostras_vies, axis=0)   # parado: o que o giro mede é viés
            return
        a_n = self.R_nivel @ acc
        w_n = self.R_nivel @ giro
        dt = 0.0 if self._t_imu_ant is None else (t - self._t_imu_ant) / 1e6
        self._t_imu_ant = t
        if self.zonas is None:
            # (espiral: o joystick não gira com o caminho; o ângulo da placa é fixo e descoberto
            # pelos dados. Integrar o giroscópio só acumularia o erro dele.)
            self.psi += math.radians(w_n[2] - self.vies_giro) * dt
        cps, sps = math.cos(self.psi), math.sin(self.psi)
        ax = cps * a_n[0] - sps * a_n[1]
        ay = sps * a_n[0] + cps * a_n[1]

        self._predizer(t)
        if self.zonas is not None and self.cfg.espiral_aprender_montagem:
            self._aprender_montagem(t, ax, ay, dt)
            self._velocidade_nas_pontas(t, ax, ay, dt)
            cps, sps = math.cos(self.psi), math.sin(self.psi)
            ax = cps * a_n[0] - sps * a_n[1]
            ay = sps * a_n[0] + cps * a_n[1]
            if not self.cont.montagem_confirmada:
                # com o ângulo da placa ainda desconhecido a aceleração só atrapalha: o filtro anda
                # só com os sensores IR até o ângulo ser confirmado
                self._repouso_e_pontas(a_n, giro)
                return
        if self.c.giro_acompanha:
            wc = giro - self.vies3                                   # °/s, sem viés
            self.theta3 = self.theta3 + np.radians(wc) * dt
            self._aprender_eixo(wc)
            if self._theta_inicio is None:
                self._theta_inicio = self.theta3.copy()
            d = self.angulo_arco(self.theta3 - self._theta_inicio)
            saindo_de_a = (abs(d) > self.cfg.giro_min_rad and self.x[0] < 0.5 * self.c.comprimento
                           and self.eixo is not None and not self._sentido_conferido)
            if saindo_de_a and not self._giro_inutil:
                # saiu da ponta A (s só pode aumentar): o sentido do giro é o da ida
                self._sentido_conferido = True
                if self.escala_giro is None:
                    # a escala começa pela do desenho e é medida na chegada à outra ponta
                    nominal = abs(self.c.angulo_tangente(self.c.comprimento) - self.c.angulo_tangente(0.0)) / self.c.comprimento
                    self.escala_giro = math.copysign(nominal, d)
                elif math.copysign(1, d) != math.copysign(1, self.escala_giro):
                    self.escala_giro = -self.escala_giro            # escala guardada com o sentido trocado
                self.cont.giro_sinal = 1 if self.escala_giro > 0 else -1
            if self.escala_giro is not None and self.eixo is not None:
                # giro = escala · v: medida direta da velocidade. A aceleração não entra (com o IMU
                # girando junto, ela só atrapalha se a montagem não for a esperada).
                w = math.radians(float(wc @ self.eixo))           # rad/s em torno do eixo do arco
                self._atualizar(_H([0.0, self.escala_giro]), np.array([w]), np.array([[self.cfg.sigma_giro ** 2]]))
                self.cont.giro_atualizacoes += 1
                if abs(self.x[1]) > self.cfg.v_max_arco:
                    self.x[1] = math.copysign(self.cfg.v_max_arco, self.x[1])
                    self.cont.velocidade_limitada += 1
                self._checar_sentido_giro(t)
                self._repouso_e_pontas(a_n, giro)
                return
            if not self._giro_inutil and (self.escala_giro is not None or abs(d) > self.cfg.giro_min_rad):
                # o IMU gira com o arco, mas o eixo (ou a escala) ainda está sendo descoberto: alguns
                # décimos de segundo só com o modelo do movimento. A aceleração não serve aqui: com o
                # IMU girando, a gravidade "vaza" para ela quando o eixo não é vertical.
                self._repouso_e_pontas(a_n, giro)
                return
        # Modelo da aceleração no plano (Kalman estendido):
        #   a_plano = a·T(s) + v²·κ(s)·N(s) + viés   (tangencial + centrípeta)
        # Nas curvas fechadas a centrípeta é muito maior que a tangencial; medir
        # o vetor inteiro evita que um erro de posição a "vaze" para a
        # tangencial e ainda informa a velocidade.
        s, v, a = self.x[0], self.x[1], self.x[2]
        T, N, k, dk = self.c.geometria(s)
        h = a * T + v * v * k * N + self.x[4:6]
        Hs = a * k * N + v * v * (dk * N - k * k * T)
        Hv = 2 * v * k * N
        H = _H([Hs[0], Hv[0], T[0], 0.0, 1.0, 0.0], [Hs[1], Hv[1], T[1], 0.0, 0.0, 1.0])
        y = np.array([ax, ay]) - h
        self._atualizar_inovacao(H, y, np.eye(2) * self.cfg.sigma_acc ** 2)
        self._repouso_e_pontas(a_n, giro)

    def _velocidade_nas_pontas(self, t: float, ax: float, ay: float, dt: float) -> None:
        """Espiral, trechos sem sensor (centro e entrada): a velocidade vem da aceleração centrípeta
        medida pelo IMU, |a| = v²·κ, com κ (curvatura) conhecida pela geometria. Só o MÓDULO da
        aceleração é usado, então não depende de como a placa foi montada nem do ângulo descoberto.
        No centro a curva é fechada e a centrípeta é grande: a medida é boa justamente ali."""
        m = complex(ax, ay) - self._acc_media
        a = min(1.0, max(dt, 0.0) / 0.15)                   # média do VETOR em ~0,15 s (o ruído se cancela)
        self._acc_vet = getattr(self, "_acc_vet", 0j) + a * (m - getattr(self, "_acc_vet", 0j))
        self._acc_mag = abs(self._acc_vet)
        z = self.zonas
        if z.k not in (0, len(z.centros)) or z.dentro is not None:
            return
        _, _, k, _ = self.c.geometria(z.posicao(t))
        k = max(abs(k), 0.015)      # trechos quase retos (ponta do gancho): sem curva, a centrípeta não mede
        ruido = max(self.cfg.zonas_acc_ruido, 2.5 * getattr(self, "_ruido_h", 0.0) * math.sqrt(0.004 / 0.3))
        util = max(0.0, self._acc_mag - ruido)
        z.velocidade_imu(t, math.sqrt(util / k))

    def _aprender_montagem(self, t: float, ax: float, ay: float, dt: float) -> None:
        """Espiral: ângulo da placa do IMU em torno da vertical. Onde os sensores dão posição e
        velocidade com confiança (dentro de um feixe ou logo depois), a aceleração centrípeta
        prevista v²·κ·N tem direção conhecida; o ângulo entre ela e a medida, somado em muitas
        amostras, é a correção. Uma rodada pequena seguida confirma."""
        m = complex(ax, ay)
        a = min(1.0, max(dt, 0.0) / 4.0)                  # viés: média lenta (~4 s)
        self._acc_media += a * (m - self._acc_media)
        kin = self.zonas.cinematica_confiavel(t)
        if kin is None:
            return
        s, v = kin
        T, N, k, _ = self.c.geometria(s)
        p = complex(*(v * v * k * N))
        if abs(p) < self.cfg.montagem_acc_min:
            return
        mc = m - self._acc_media
        self._mont_Z += p * mc.conjugate()
        self._mont_W += abs(p) * abs(mc)
        self._mont_n += 1
        if self._mont_n < self.cfg.montagem_amostras:
            return
        coer = abs(self._mont_Z) / self._mont_W if self._mont_W > 0 else 0.0
        if coer >= self.cfg.montagem_coerencia:
            d = math.atan2(self._mont_Z.imag, self._mont_Z.real)
            self.psi += d
            rot = complex(math.cos(d), math.sin(d))
            self._acc_media *= rot
            if abs(d) > math.radians(20):
                # correção grande: o viés estimado com o ângulo errado não vale; recomeça
                self.x[4] = self.x[5] = 0.0
                self.P[4:6, :] = 0.0
                self.P[:, 4:6] = 0.0
                self.P[4, 4] = self.P[5, 5] = self.cfg.sigma_vies0 ** 2
            else:
                b = complex(self.x[4], self.x[5]) * rot  # o viés estimado gira junto
                self.x[4], self.x[5] = b.real, b.imag
            self.cont.montagem_rodadas += 1
            self.cont.montagem_graus = round(math.degrees(math.atan2(math.sin(self.psi), math.cos(self.psi))), 1)
            if abs(math.degrees(d)) < self.cfg.montagem_convergiu_graus and self.cont.montagem_rodadas >= 2:
                self.cont.montagem_confirmada = True
        self._mont_Z, self._mont_W, self._mont_n = 0j, 0.0, 0

    def angulo_arco(self, theta: np.ndarray) -> float:
        """Ângulo (rad) em torno do eixo do arco; antes de o eixo ser descoberto, o vertical."""
        if self.eixo is not None:
            return float(theta @ self.eixo)
        return float(theta @ self.R_nivel[2]) if self.R_nivel is not None else 0.0

    def _aprender_eixo(self, wc: np.ndarray) -> None:
        """Direção dominante do giroscópio nas amostras com giro claro (> 10 °/s)."""
        if float(np.linalg.norm(wc)) < 10.0:
            return
        self._M_eixo += np.outer(wc, wc)
        self._n_eixo += 1
        if self._n_eixo < 25 or self._n_eixo - self._n_eixo_calc < (25 if self._n_eixo < 500 else 250):
            return
        self._n_eixo_calc = self._n_eixo
        val, vec = np.linalg.eigh(self._M_eixo)
        if val[2] < 3 * val[1]:
            return                                    # ainda sem uma direção clara
        u = vec[:, 2]
        # sentido fixo pela montagem (a escala guardada de outras partidas continua valendo):
        # para cima quando o eixo tem componente vertical; senão, pela maior componente do sensor
        vert = float(u @ self.R_nivel[2])
        if (vert < 0) if abs(vert) > 0.2 else (u[int(np.argmax(np.abs(u)))] < 0):
            u = -u
        self.eixo = u
        self.cont.giro_eixo = [round(float(x), 3) for x in u]
        self.cont.giro_eixo_vertical_graus = round(math.degrees(math.acos(min(1.0, abs(vert)))), 1)

    def _repouso_e_pontas(self, a_n, giro) -> None:
        w_n = self.R_nivel @ giro
        # Repouso: só ajusta o viés do giroscópio. NÃO zera a velocidade
        # (ZUPT): nos movimentos lentos dos pacientes a aceleração fica no nível
        # do ruído do MPU6050, e o detector confundiria "devagar" com "parado"
        # (verificado no simulador). As pontas entram só como limite físico.
        self._janela_acc.append(a_n)
        self._janela_giro.append(float(np.linalg.norm(giro)))
        parado = (len(self._janela_acc) == self._janela_acc.maxlen
                  and float(np.linalg.norm(np.std(self._janela_acc, axis=0))) < self.cfg.limiar_repouso_acc
                  and float(np.mean(self._janela_giro)) < self.cfg.limiar_repouso_giro
                  and abs(self.x[1]) < self.cfg.v_max_repouso)
        if parado and not self.em_repouso:
            self.cont.repousos += 1
        self.em_repouso = bool(parado)
        if parado:
            self.vies_giro += 0.002 * (w_n[2] - self.vies_giro)
            self.vies3 = self.vies3 + 0.002 * (np.asarray(giro, float) - self.vies3)
        # Exceção segura: ao CHEGAR numa ponta da fenda (batente) e parar, fixa uma
        # vez posição = ponta e velocidade = 0 (sem isso a estimativa deriva da
        # ponta durante a pausa). Só uma vez por visita: aplicar continuamente
        # prenderia o joystick na ponta quando ele sai devagar (testado no simulador).
        perto = [b for b in self.c.batentes if abs(self.x[0] - b) < self.cfg.tol_batente]
        for b in self.c.batentes:
            if abs(self.x[0] - b) > self.cfg.tol_batente + 10:
                self._ponta_fixada.discard(b)
        quieto_agora = (len(self._janela_acc) == self._janela_acc.maxlen
                        and float(np.linalg.norm(np.std(self._janela_acc, axis=0))) < self.cfg.limiar_repouso_acc
                        and float(np.mean(self._janela_giro)) < self.cfg.limiar_repouso_giro)
        self._beco(self.t, quieto_agora)
        if self.zonas is not None and len(self._janela_acc) == self._janela_acc.maxlen:
            # (só com a janela cheia: no começo, poucas amostras pareciam movimento)
            # "Mão parada" para a colher: pouca agitação E aceleração horizontal média perto de zero.
            # Movimento suave numa curva agita pouco, mas tem aceleração centrípeta (v²/r) apontando
            # para o centro da curva; o módulo dela não depende de como a placa foi montada. Na
            # dúvida, conta como "mexendo" (a colher só segue até o próximo feixe).
            media_h = float(np.linalg.norm(np.mean(np.asarray(self._janela_acc)[:, :2], axis=0))) if self._janela_acc else 0.0
            self.zonas.imu(self.t, quieto_agora and media_h < self.cfg.zonas_acc_parado)
            self.cont.zonas_paradas = self.zonas.paradas
        self._limites_zona()
        if perto and perto[0] not in self._ponta_fixada:
            quieto = (len(self._janela_acc) == self._janela_acc.maxlen
                      and float(np.linalg.norm(np.std(self._janela_acc, axis=0))) < self.cfg.limiar_repouso_acc
                      and float(np.mean(self._janela_giro)) < self.cfg.limiar_repouso_giro
                      and abs(self.x[1]) < self.cfg.v_max_ponta)
            if quieto:
                b = perto[0]
                self._atualizar(_H([1.0], [0, 1.0], [0, 0, 1.0]), np.array([b, 0.0, 0.0]),
                                np.diag([self.cfg.sigma_batente ** 2, self.cfg.sigma_zupt_v ** 2,
                                         self.cfg.sigma_zupt_a ** 2]))
                self._ponta_fixada.add(b)
                self.cont.batentes += 1
        self._limites()

    def _centros_cruzamentos(self) -> list[tuple[int, int, float]]:
        """(divisa, sensor, centro_mm) de cada cruzamento, na ordem do caminho."""
        por_div: dict[int, list] = {}
        for lin in self.tabela:
            por_div.setdefault(lin["divisa"], []).append(lin)
        out = [(d, ls[0]["sensor"], float(np.mean([l["posicao_mm"] for l in ls]))) for d, ls in por_div.items()]
        return sorted(out, key=lambda x: x[2])

    def _faixas_cruzamentos(self) -> list[tuple[float, float]]:
        """(início, fim) em mm de cada feixe ao longo do caminho, na ordem dos cruzamentos."""
        out = []
        for d, _, _ in self._cruz:
            pos = [lin["posicao_mm"] for lin in self.tabela if lin["divisa"] == d]
            out.append((min(pos), max(pos)))
        return out

    def _limites_zona(self) -> None:
        """A ordem dos sensores diz entre quais cruzamentos o joystick está: a estimativa não pode
        sair desse trecho (sem isso ela "foge" entre sensores, principalmente nos becos)."""
        if len(self._cruz) <= 2:
            return
        n, L, f = len(self._cruz), self.c.comprimento, self._faixas
        if self._dentro is not None:
            lo, hi = f[self._dentro[0]]
        else:   # (logo depois de sair de um feixe vale o palpite "atravessou"; o próximo sensor corrige)
            k = self._passados
            lo = f[k - 1][1] if k >= 1 else 0.0
            hi = f[k][0] if k < n else L
        folga = 5.0
        if self.zonas is not None:
            # espiral: o limite é uma restrição, não uma medida. Repetido a cada amostra como medida,
            # ele deixava o filtro "confiante demais" (incerteza de 1 mm) e preso no limite.
            if self.x[0] < lo - folga or self.x[0] > hi + folga:
                fora = -1.0 if self.x[0] < lo - folga else 1.0
                self.x[0] = lo if fora < 0 else hi
                if self.x[2] * fora > 0:
                    self.x[2] = 0.0
                # A velocidade NÃO é zerada: quase sempre a estimativa só chegou ao feixe um pouco
                # antes do sensor (zerar fazia o filtro "esquecer" o movimento e travar). Ela fica
                # incerta, para a próxima borda (com a velocidade medida no feixe) corrigir.
                self.P[1, 1] = max(self.P[1, 1], 40.0 ** 2)
                self.cont.limites_zona += 1
            return
        if self.x[0] < lo - folga:
            self._posicao(lo, self.cfg.sigma_batente * 2)
            self.cont.limites_zona += 1
        elif self.x[0] > hi + folga:
            self._posicao(hi, self.cfg.sigma_batente * 2)
            self.cont.limites_zona += 1

    def _divisa_pela_ordem(self, sensor: int) -> int | None:
        """Índice (na ordem do caminho) do cruzamento que disparou. Sensores que aparecem várias
        vezes (espiral): entre dois cruzamentos vizinhos o sensor nunca é o mesmo, então o sensor
        diz qual foi: o próximo (indo) ou o último que ficou para trás (voltando)."""
        if self._dentro is not None:
            if self._cruz[self._dentro[0]][1] == sensor:   # está dentro de um feixe: é a saída dele
                return self._dentro[0]
            self._dentro = None                            # a saída daquele feixe se perdeu
        n = len(self._cruz)
        if self._saiu_de is not None:
            # acabou de sair do feixe i sem saber para que lado: o próximo sensor decide. Os
            # cruzamentos i-1, i e i+1 são sempre de sensores diferentes.
            ref = self._saiu_de
            candidatos = (ref - 1, ref, ref + 1)
        else:
            ref = self._passados - 0.5
            candidatos = (self._passados - 1, self._passados)
        achados = [i for i in candidatos if 0 <= i < n and self._cruz[i][1] == sensor]
        if len(achados) == 1:
            return achados[0]
        k = ref + 0.5
        # Nenhum vizinho tem este sensor: alguma passagem não foi detectada (ou a contagem se
        # adiantou). Fica com o cruzamento deste sensor mais perto da posição atual na sequência;
        # empate (raro) decide pela estimativa de posição.
        todos = [i for i, (_, sen, _) in enumerate(self._cruz) if sen == sensor]
        if not todos:
            return None
        i = min(todos, key=lambda i: (abs(i - ref), abs(self._cruz[i][2] - self.x[0])))
        # Pular UM cruzamento acontece (passagem rápida não detectada). Mais que isso não serve
        # para reposicionar pela ordem (a estimativa decide; ver _cruzamento_plausivel).
        if abs(i - ref) > (2.0 if self._saiu_de is not None else 1.5):
            return None
        self.cont.ordem_ressincronizada += 1
        return i

    def borda(self, t: float, sensor: int, nivel: int) -> bool:
        if sensor not in self.c.sensores:
            return False
        if self.zonas is not None:
            return self._borda_por_zonas(t, sensor, nivel)
        self._predizer(t)
        self.nivel[sensor] = int(nivel)
        troca = "liga" if nivel else "desliga"
        v = self.x[1]
        sentidos = {"ida"} if v > self.cfg.v_sentido else {"volta"} if v < -self.cfg.v_sentido else {"ida", "volta"}
        def mais_perto(filtro):
            m, mr = None, float("inf")
            for lin in self.tabela:
                if lin["sensor"] != sensor or lin["troca"] != troca or not filtro(lin):
                    continue
                r = abs(lin["posicao_mm"] - self.x[0]) / math.sqrt(self.P[0, 0] + lin["incerteza_mm"] ** 2)
                if r < mr:
                    m, mr = lin, r
            return m, mr

        melhor, melhor_r = mais_perto(lambda lin: lin["sentido"] in sentidos)     # pela estimativa
        i_ordem = self._divisa_pela_ordem(sensor) if self._cruz and len(self._cruz) > 2 else None
        pela_ordem = self._cruz[i_ordem][0] if i_ordem is not None else None
        pela_ordem_forcada = False
        sent_ordem = None
        if pela_ordem is not None:
            if troca == "liga":
                # entrou no feixe: o lado de onde veio diz o sentido (sem depender da estimativa)
                if self._saiu_de is None:
                    sent_ordem = "ida" if i_ordem >= self._passados else "volta"
                elif i_ordem == self._saiu_de:                # voltou para o feixe de onde saiu
                    sent_ordem = "volta" if self._lado_saida == "ida" else "ida"
                else:
                    sent_ordem = "ida" if i_ordem > self._saiu_de else "volta"
            else:
                # saiu do feixe: o mais comum é ter atravessado (mesmo sentido em que entrou); só
                # conta como "voltou" se a velocidade estimada apontar com clareza para o outro lado
                entrou = self._dentro[1] if self._dentro is not None else ("ida" if v >= 0 else "volta")
                contra = (v < -self.cfg.v_saida_feixe) if entrou == "ida" else (v > self.cfg.v_saida_feixe)
                sent_ordem = ({"ida": "volta", "volta": "ida"}[entrou]) if contra else entrou
            o, o_r = mais_perto(lambda lin: lin["divisa"] == pela_ordem and lin["sentido"] == sent_ordem)
            # A ordem dos cruzamentos é a informação mais confiável: vale mesmo com a estimativa longe
            # (é justamente quando ela precisa de correção). Só perde para a estimativa quando esta
            # aponta com clareza para outro cruzamento (sinal de que um sensor falhou antes).
            estimativa_clara = melhor is not None and melhor["divisa"] != pela_ordem and melhor_r <= self.cfg.portao_sigmas
            if o is not None and not estimativa_clara:
                pela_ordem_forcada = o_r > self.cfg.portao_sigmas
                melhor, melhor_r = o, 0.0
            else:
                sent_ordem = None
        unica = [lin for lin in self.tabela if lin["sensor"] == sensor and lin["troca"] == troca]
        inequivoca = len(unica) == 1 and len(self.c.sensores) <= 2
        if inequivoca:
            # sensor que só existe num lugar (pontas do arco): não há o que confundir. Aceita mesmo
            # longe da estimativa, que é justamente quando ela mais precisa ser corrigida, desde que
            # o joystick possa ter chegado lá (sensor acionando à toa não teleporta a estimativa).
            melhor = unica[0]
            if not self._ponta_plausivel(t, melhor):
                self.cont.bordas_rejeitadas += 1
                return False
        elif melhor is None or melhor_r > self.cfg.portao_sigmas:
            self.cont.bordas_rejeitadas += 1
            return False
        if len(self._cruz) > 2 and not self._cruzamento_plausivel(t, melhor):
            return False
        if (inequivoca or pela_ordem_forcada) and abs(melhor["posicao_mm"] - self.x[0]) > 3 * math.sqrt(self.P[0, 0] + melhor["incerteza_mm"] ** 2):
            # estimativa muito longe: recomeça daqui (posição certa; velocidade incerta)
            self.x[0] = melhor["posicao_mm"]
            self.P[0, :] = self.P[:, 0] = 0.0
            self.P[0, 0] = melhor["incerteza_mm"] ** 2
            self.P[1, 1] = max(self.P[1, 1], 40.0 ** 2)
        self._posicao(melhor["posicao_mm"], melhor["incerteza_mm"])
        if self.c.giro_acompanha and self.R_nivel is not None:
            self._medir_escala_giro(melhor["posicao_mm"])
        self.cont.bordas_aceitas += 1
        self._limites()
        self._atualizar_passados(t, melhor, sent_ordem, troca)
        return True

    def _borda_por_zonas(self, t: float, sensor: int, nivel: int) -> bool:
        """Espiral: quem decide qual cruzamento foi (e se vale) é o RastreadorZonas, só pela ordem
        e pelos tempos dos sensores. O filtro recebe a posição da borda como medida."""
        self._predizer(t)
        self.nivel[sensor] = int(nivel)
        r = self.zonas.borda(t, sensor, nivel)
        self.cont.zonas_velocidades_feixe = self.zonas.velocidades_feixe
        if r is None:
            self.cont.bordas_rejeitadas += 1
            return False
        i, troca, sentido = r
        d = self._cruz[i][0]
        lin = next((l for l in self.tabela if l["divisa"] == d and l["troca"] == troca and l["sentido"] == sentido), None)
        z = lin["posicao_mm"] if lin is not None else self._cruz[i][2]
        inc = lin["incerteza_mm"] if lin is not None else 10.0
        if abs(z - self.x[0]) > 3 * math.sqrt(self.P[0, 0] + inc ** 2):
            # estimativa longe (IMU derivou): recomeça daqui (posição certa; velocidade incerta)
            self.x[0] = z
            self.P[0, :] = self.P[:, 0] = 0.0
            self.P[0, 0] = inc ** 2
            self.P[1, 1] = max(self.P[1, 1], 40.0 ** 2)
        self._posicao(z, inc)
        # velocidade: na saída, medida pelo tempo dentro do feixe (com o sentido); na entrada, só o
        # sentido é certo (o módulo é o do feixe anterior). O IMU, nas curvas, mede bem o módulo
        # (centrípeta v²/r) mas mal o sentido: esta medida completa o que falta.
        v_med = float(self.zonas.v)
        sig_v = (0.3 * abs(v_med) + 10.0) if troca == "desliga" else (0.6 * abs(v_med) + 25.0)
        self._atualizar(_H([0.0, 1.0]), np.array([v_med]), np.array([[sig_v ** 2]]))
        self.cont.bordas_aceitas += 1
        self._limites()
        if lin is not None:
            self._atualizar_passados(t, lin, sentido, troca)
        return True

    def _cruzamento_plausivel(self, t: float, lin: dict) -> bool:
        """Espiral: o joystick precisa ter tido tempo de ir do último cruzamento aceito (ou da
        entrada, no começo da partida) até este. Também não pode pular mais de um cruzamento em
        relação à contagem atual. Sensor acionando à toa não reposiciona nem pontua."""
        z = lin["posicao_mm"]
        if self._ult_cruz is None:
            t_ref, s_ref = (self._t_inicio if self._t_inicio is not None else t), self.c.inicio
        else:
            t_ref, s_ref = self._ult_cruz
        dist = abs(z - s_ref)
        dt = max((t - t_ref) / 1e6, 1e-6)
        if dist > self.cfg.dist_min_plausibilidade and dist / dt > self.cfg.v_max_cruzamento:
            self.cont.cruzamentos_implausiveis += 1
            self.cont.bordas_rejeitadas += 1
            return False
        idx = next(i for i, (d, _, _) in enumerate(self._cruz) if d == lin["divisa"])
        ref = self._saiu_de if self._saiu_de is not None else self._passados - 0.5
        if abs(idx - ref) > (2.0 if self._saiu_de is not None else 1.5):
            # Longe da contagem atual. Só aceita se for uma passagem de verdade: dois cruzamentos
            # VIZINHOS seguidos (com tempo para andar entre eles), como quando a partida começa
            # com o joystick fora da entrada ou a contagem se perdeu. Um sensor piscando sozinho
            # não forma essa sequência.
            tr = self._trilha
            if tr:
                t0, i0, z0 = tr[-1]
                d0, dt0 = abs(z - z0), max((t - t0) / 1e6, 1e-6)
                if abs(idx - i0) > 1 or (d0 > self.cfg.dist_min_plausibilidade and d0 / dt0 > self.cfg.v_max_cruzamento):
                    tr.clear()
            if tr and tr[-1][1] == idx:
                tr[-1] = (t, idx, z)          # entrada e saída do mesmo feixe
            else:
                tr.append((t, idx, z))
            if len(tr) < 2:
                self.cont.cruzamentos_saltados += 1
                self.cont.bordas_rejeitadas += 1
                return False
            self.cont.ordem_ressincronizada += 1
        self._trilha = []
        self._ult_cruz = (t, z)
        return True

    def _atualizar_passados(self, t: float, lin: dict, sent_ordem: str | None, troca: str) -> None:
        """Quantos cruzamentos ficaram para trás e entrada/saída do beco. Pela ordem dos sensores
        quando ela foi usada; senão, pela posição corrigida."""
        if not self._cruz:
            return
        idx = next(i for i, (d, _, _) in enumerate(self._cruz) if d == lin["divisa"])
        if sent_ordem is not None:
            if troca == "liga":
                # entrou: agora se sabe de que lado estava (sem depender da estimativa)
                self._dentro, self._saiu_de = (idx, sent_ordem), None
                n = idx if sent_ordem == "ida" else idx + 1
            else:
                # saiu: o lado é um palpite (atravessou, salvo velocidade clara ao contrário); o
                # próximo sensor confirma ou corrige
                self._dentro, self._saiu_de, self._lado_saida = None, idx, sent_ordem
                n = idx + 1 if sent_ordem == "ida" else idx
        else:
            self._dentro = (idx, lin["sentido"]) if troca == "liga" else None
            self._saiu_de = None
            n = sum(1 for _, _, c in self._cruz if self.x[0] > c)
        fim, ini = self._becos["fim"], self._becos["inicio"]
        if n == len(self._cruz) and self._passados < n:
            fim.update(t0=t, fixado=False, vmax=0.0)          # entrou no beco do centro
        elif n < len(self._cruz):
            fim["t0"] = None
        if n == 0 and self._passados > 0:
            ini.update(t0=t, fixado=False, vmax=0.0)          # voltou para o trecho da entrada
        elif n > 0:
            ini["t0"] = None
        self._passados = n

    def _beco(self, t: float, quieto: bool) -> None:
        """Trechos sem sensor nas pontas (centro da espiral e entrada): estima a chegada à ponta.
        Só nos caminhos com sensores em sequência (espiral); no arco os sensores ficam nas pontas."""
        if len(self._cruz) <= 2:
            return
        L = self.c.comprimento
        for lado, alvo, entrada, sentido in (("fim", L, self._faixas[-1][1], 1.0),
                                              ("inicio", 0.0, self._faixas[0][0], -1.0)):
            b = self._becos[lado]
            comprimento = abs(alvo - entrada)
            if b["t0"] is None or comprimento < self.cfg.tol_batente:
                continue                                 # fora do beco, ou beco curtíssimo (o batente resolve)
            if quieto:
                b.setdefault("quieto_desde", t)
            else:
                b.pop("quieto_desde", None)
            quieto_s = (t - b["quieto_desde"]) / 1e6 if "quieto_desde" in b else 0.0
            if b["fixado"]:
                continue
            dt = (t - b["t0"]) / 1e6
            v = float(self.x[1]) * sentido                # positivo = indo para a ponta
            b["vmax"] = max(b["vmax"], v)
            voltando = b["vmax"] > 3 * self.cfg.v_sentido and v < -self.cfg.v_sentido
            andou = (self.x[0] - entrada) * sentido / comprimento
            # "quieto" só pela agitação do IMU confunde movimento lento e suave com parada: exige
            # também a velocidade estimada baixa, ou o IMU quieto por mais tempo (a velocidade
            # estimada pode ficar viciada num beco sem sensores)
            parado = quieto and (abs(v) < self.cfg.beco_v_parado or quieto_s >= self.cfg.beco_quieto_s)
            if dt < comprimento / self.cfg.beco_v_max or not (parado or voltando):
                continue
            if andou >= self.cfg.beco_fracao or dt >= self.cfg.beco_tempo_s:
                # chegou à ponta: entra como medida (ponta do caminho, parado), combinada com a
                # estimativa. Se ela já estava certa, quase nada muda; se derivou, é corrigida.
                if abs(self.x[0] - alvo) > 3 * math.sqrt(self.P[0, 0] + 10.0 ** 2) + 20:
                    self.P[0, 0] = max(self.P[0, 0], (self.x[0] - alvo) ** 2)
                if voltando:
                    self._posicao(alvo, 10.0)
                else:
                    self._atualizar(_H([1.0], [0, 1.0]), np.array([alvo, 0.0]),
                                    np.diag([10.0 ** 2, self.cfg.sigma_zupt_v ** 2 * 9]))
                self._limites()
                b["fixado"] = True
                if lado == "fim":
                    self.cont.beco_centro += 1
                else:
                    self.cont.beco_inicio += 1

    def _sensor_da_ponta(self, s_ponta: float) -> int | None:
        perto = [l["sensor"] for l in self.tabela if abs(l["posicao_mm"] - s_ponta) < 0.25 * self.c.comprimento]
        return perto[0] if perto else None

    def _checar_sentido_giro(self, t: float) -> None:
        """Arco: a estimativa empurrando uma ponta (velocidade para fora, já no batente) sem o sensor
        daquela ponta ver o joystick = o sentido do giro está trocado. Corrige e espelha."""
        L = self.c.comprimento
        lado = None
        if self.x[0] <= 5.0 and self.x[1] < -self.cfg.inversao_v_min:
            sa = self._sensor_da_ponta(0.0)
            if sa is not None and not self.nivel.get(sa):
                lado = "A"
        elif self.x[0] >= L - 5.0 and self.x[1] > self.cfg.inversao_v_min:
            sb = self._sensor_da_ponta(L)
            if sb is not None and not self.nivel.get(sb):
                lado = "B"
        if lado is None:
            self._inv_desde = None
            return
        ant = getattr(self, "_inv_desde", None)
        if ant is None or ant[0] != lado:
            self._inv_desde = (lado, t, t, 0.0)
            return
        _, t0, t_ult, andou = ant
        andou += abs(float(self.x[1])) * max(0.0, (t - t_ult) / 1e6)   # o que o giro "andou" contra a ponta
        self._inv_desde = (lado, t0, t, andou)
        if (t - t0) / 1e6 < self.cfg.inversao_tempo_s:
            return
        # inverte: escala com o outro sinal; o joystick andou para DENTRO o que a estimativa tentou andar
        # para fora; velocidade e derivadas trocadas
        self.escala_giro = -self.escala_giro
        self.cont.giro_sinal = 1 if self.escala_giro > 0 else -1
        andou = min(andou, 0.5 * L)
        self.x[0] = andou if lado == "A" else L - andou
        T = np.diag([-1.0, -1.0, -1.0, -1.0, 1.0, 1.0])
        self.x[1:4] = -self.x[1:4]
        self.P = T @ self.P @ T
        self.cont.giro_sentido_corrigido += 1
        self._inv_desde = None

    def _escala_nominal(self) -> float:
        L = self.c.comprimento
        return abs(self.c.angulo_tangente(L) - self.c.angulo_tangente(0.0)) / L if L > 0 else 0.0

    def _escala_plausivel(self, escala: float) -> bool:
        nom = self._escala_nominal()
        if nom <= 0 or not math.isfinite(escala):
            return True
        r = abs(escala) / nom
        return self.cfg.escala_giro_fator_min <= r <= self.cfg.escala_giro_fator_max

    def _ponta_plausivel(self, t: float, lin: dict) -> bool:
        """Arco: o sensor de uma ponta só vale se o joystick pode ter chegado lá: tempo suficiente
        desde a outra ponta e, com o giroscópio em uso, giro de pelo menos parte do esperado."""
        z = lin["posicao_mm"]
        if self._ult_ponta is not None:
            t0, z0, th0 = self._ult_ponta
            dist = abs(z - z0)
            if dist > 0.5 * self.c.comprimento:
                dt = max((t - t0) / 1e6, 1e-6)
                if dist / dt > self.cfg.v_max_ponta_a_ponta:
                    self.cont.pontas_implausiveis += 1
                    return False
                if (self.c.giro_acompanha and self.escala_giro is not None and self.eixo is not None
                        and not self._giro_inutil):
                    girou = abs(self.angulo_arco(self.theta3 - th0))
                    esperado = abs(self.escala_giro) * dist
                    if girou < self.cfg.giro_ponta_min * esperado:
                        self.cont.pontas_implausiveis += 1
                        return False
        self._ult_ponta = (t, z, self.theta3.copy())
        return True

    def _medir_escala_giro(self, s_borda: float) -> None:
        """Entre bordas longe uma da outra (as duas pontas): escala = giro do IMU / distância.
        Giro quase nulo: o IMU não gira com o arco e o giroscópio deixa de ser usado."""
        self._marcas_giro.append((s_borda, self.theta3.copy()))
        self._marcas_giro = self._marcas_giro[-6:]
        s2, p2 = self._marcas_giro[-1]
        for s1, p1 in self._marcas_giro[-2::-1]:
            if abs(s2 - s1) < 0.6 * self.c.comprimento:
                continue
            dpsi = self.angulo_arco(p2 - p1)
            if abs(dpsi) >= 2 * self.cfg.giro_min_rad:
                nova = dpsi / (s2 - s1)
                if not self._escala_plausivel(nova):
                    self.cont.escala_giro_descartada += 1
                    return
                self.escala_giro = nova
                self.cont.giro_sinal = 1 if self.escala_giro > 0 else -1
                self.cont.giro_graus_por_mm = round(math.degrees(self.escala_giro), 4)
            elif abs(dpsi) < 0.5 * self.cfg.giro_min_rad:
                self.escala_giro = None
                self._giro_inutil = True
                self.cont.giro_sinal = 0
            return

    def vl53_para_s(self, dist_mm: float) -> float:
        c = self.calib_vl53
        return (dist_mm - c["tras"]) / (c["frente"] - c["tras"]) * self.c.comprimento

    def vl53(self, t: float, dist_mm: int, estado: int) -> bool:
        if not self.c.usa_vl53:
            return False
        if estado != 0:
            self.cont.vl53_fora += 1
            return False
        # Mediana das últimas 3 leituras, com o horário da leitura do meio: um pico isolado do
        # sensor (comum no VL53L0X) não chega ao filtro e a barra do jogo não "pula".
        self._vl53_janela.append((t, dist_mm))
        if len(self._vl53_janela) == self._vl53_janela.maxlen > 1:
            ordem = sorted(self._vl53_janela, key=lambda x: x[1])
            dist_mm = ordem[len(ordem) // 2][1]
            t = sorted(x[0] for x in self._vl53_janela)[len(ordem) // 2]
        if self.t is not None and t < self.t:
            t = self.t
        self._predizer(t)
        z = self.vl53_para_s(dist_mm)
        S = self.P[0, 0] + self.cfg.sigma_vl53 ** 2
        if abs(z - self.x[0]) > 5 * math.sqrt(S) + 10:
            self.cont.vl53_rejeitadas += 1
            self._vl53_seguidas_fora += 1
            # Uma leitura estranha é descartada. Mas se o sensor de distância discorda por
            # ~0,2 s seguidos, quem errou foi a estimativa (IMU): recomeça da medida do sensor.
            # Sem isso, a posição podia ficar presa numa ponta (o carro acelerava sem parar).
            if self._vl53_seguidas_fora >= 10 and 0 <= z <= self.c.comprimento:
                self.x[0], self.x[1:4] = z, 0.0
                self.P[:4, :4] = np.diag([self.cfg.sigma_vl53 ** 2, 50.0 ** 2, 500.0 ** 2, 3000.0 ** 2])
                self.P[0, 1:4] = self.P[1:4, 0] = 0.0
                self.cont.vl53_reinicios += 1
                self._vl53_seguidas_fora = 0
                self._vl53_janela.clear()
                return True
            return False
        self._vl53_seguidas_fora = 0
        self._posicao(z, self.cfg.sigma_vl53)
        self.cont.vl53_aceitas += 1
        self._limites()
        return True

    # ---------------- saídas ----------------
    def estado_em(self, t: float) -> np.ndarray:
        if self.t is None:
            return self.x.copy()
        # até "agora" só com posição, velocidade e aceleração (o jerk estimado é ruidoso) e no
        # máximo 60 ms: extrapolar mais que isso inventa movimento
        dt = min(max(0.0, (t - self.t) / 1e6), 0.06)
        x = self.x.copy()
        if self.so_vl53:
            # só com o sensor de distância a aceleração estimada é ruído: extrapola só com a velocidade
            x[0] += x[1] * dt
            return x
        x[0] += x[1] * dt + x[2] * dt * dt / 2
        x[1] += x[2] * dt
        return x

    def suavizar(self) -> dict:
        """Suavizador RTS sobre o histórico. Devolve séries (t_us, s, v, a, j, sigma_s)."""
        n = len(self._h_t)
        if n == 0:
            return {k: np.zeros(0) for k in ("t_us", "s", "v", "a", "j", "sigma_s")}
        xf, Pf = np.array(self._h_xf), np.array(self._h_Pf)
        xp, Pp = np.array(self._h_xp), np.array(self._h_Pp)
        F = np.array(self._h_F)
        xs, Ps = xf.copy(), Pf.copy()
        for k in range(n - 2, -1, -1):
            C = Pf[k] @ F[k + 1].T @ np.linalg.inv(Pp[k + 1])
            xs[k] = xf[k] + C @ (xs[k + 1] - xp[k + 1])
            Ps[k] = Pf[k] + C @ (Ps[k + 1] - Pp[k + 1]) @ C.T
        return {"t_us": np.array(self._h_t), "s": xs[:, 0], "v": xs[:, 1], "a": xs[:, 2], "j": xs[:, 3],
                "sigma_s": np.sqrt(np.maximum(Ps[:, 0, 0], 0))}


class RastreadorZonas:
    """Posição na espiral pelos sensores IR (a base do que o jogo mostra).

    - Conta as ENTRADAS nos feixes na ordem S1, S2, S3, S1, S2, S3: entrar no feixe do próximo
      cruzamento = avançou; entrar de novo no feixe do cruzamento anterior = voltou. Pode pular um
      cruzamento (passagem rápida não detectada), mais que isso não. Só vale se houve tempo para
      chegar até ali (velocidade máxima plausível).
    - A SAÍDA do feixe é uma segunda posição conhecida, e o tempo dentro do feixe dá a velocidade
      naquele ponto (largura do feixe / tempo), em vez da média entre dois feixes.
    - Entre os feixes a posição anda com essa velocidade, sem passar do próximo feixe (nem voltar
      além do anterior) até ele ser cruzado.
    - O IMU diz só se a mão está parada (não depende de como a placa foi montada): parada por
      QUIETO_PARA_S, a colher para; voltou a mexer, segue com a mesma velocidade.
    - Quando a mão chega a um feixe antes (ou depois) da colher, a colher alcança em ~TAU, sem pular.
    """

    V_MIN = 10.0          # mm/s: piso da velocidade medida no feixe (mão quase parada dentro dele)
    V_PADRAO = 40.0       # mm/s: antes da primeira medida
    TAU = 0.35            # s
    QUIETO_PARA_S = 0.35  # s de IMU quieto para a colher parar
    JANELA_CONFIAVEL_S = 0.25   # depois de uma borda, posição e velocidade bem conhecidas

    def __init__(self, tabela: list[dict], cruz: list[tuple[int, int, float]], comprimento: float,
                 inicio: float, v_max: float) -> None:
        self.centros = [c for _, _, c in cruz]
        self.sensor = [sen for _, sen, _ in cruz]
        self.faixa = []
        for d, _, c in cruz:
            ida = {l["troca"]: l["posicao_mm"] for l in tabela if l["divisa"] == d and l["sentido"] == "ida"}
            lo, hi = ida.get("liga", c - 7.0), ida.get("desliga", c + 7.0)
            self.faixa.append((min(lo, hi), max(lo, hi)))
        self.L, self.v_max = comprimento, v_max
        self.k = 0                     # cruzamentos já passados (entradas na ida menos voltas)
        self.s, self.v = float(inicio), 0.0
        self.v_mem = self.V_PADRAO     # última velocidade medida num feixe (módulo)
        self.sentido = 1.0
        self.off = 0.0                 # diferença entre a tela e a posição confirmada (some aos poucos)
        self.t_ref: float | None = None
        self.t_evento: float | None = None
        self.dentro: tuple[int, str, float] | None = None   # (cruzamento, sentido, hora da entrada)
        # começa parada na entrada; quando o IMU mostra a mão mexendo, anda para a frente
        self.parado = True
        self._v_salvo = self.V_PADRAO
        self._quieto_desde: float | None = None
        self.aceitos = self.ignorados = self.velocidades_feixe = self.paradas = 0
        self._ult_saida: tuple[int, float] | None = None   # (cruzamento, hora) da última saída de feixe
        self._ambigua = False          # entrou de novo no feixe de onde acabou de sair: sentido incerto
        self.ambiguas = 0

    def iniciar(self, t: float) -> None:
        if self.t_ref is None:
            self.t_ref = t
            self._conf = (t, self.s)       # última posição confirmada por sensor (para a plausibilidade)

    def limites(self) -> tuple[float, float]:
        if self.dentro is not None:
            return self.faixa[self.dentro[0]]
        n = len(self.centros)
        lo = self.faixa[self.k - 1][1] if self.k > 0 else 0.0
        hi = self.faixa[self.k][0] if self.k < n else self.L
        return lo, hi

    def _reancorar(self, t: float, s_novo: float, v_novo: float) -> None:
        s_tela = self.posicao(t)
        self.s, self.v, self.t_ref = s_novo, v_novo, t
        self.off = float(np.clip(s_tela - s_novo, -150.0, 150.0))

    def borda(self, t: float, sensor: int, nivel: int) -> tuple[int, str, str] | None:
        """Devolve (cruzamento, "liga"/"desliga", "ida"/"volta") se a borda foi aceita."""
        self.iniciar(t)
        n, k = len(self.centros), self.k
        if not nivel:
            if self.dentro is None or self.sensor[self.dentro[0]] != sensor:
                return None                    # saída de um feixe cuja entrada não valeu
            i, sentido, t_in = self.dentro
            lo, hi = self.faixa[i]
            dwell = max((t - t_in) / 1e6, 1e-3)
            sinal = 1.0 if sentido == "ida" else -1.0
            if self._ambigua:
                # vai-e-volta na beira do feixe: não se sabe para que lado saiu. A colher espera no
                # feixe; o próximo sensor diz o lado (e ela alcança sem pular)
                v_saida = 0.0
                self._ambigua = False
            else:
                self.v_mem = float(np.clip((hi - lo) / dwell, self.V_MIN, self.v_max))
                self.velocidades_feixe += 1
                v_saida = sinal * self.v_mem
            # saiu: o mais comum é ter atravessado (o próximo sensor corrige se deu meia-volta)
            self._reancorar(t, hi if sentido == "ida" else lo, v_saida)
            self._conf = (t, hi if sentido == "ida" else lo)
            self.dentro = None
            self._ult_saida = (i, t)
            self.t_evento, self.parado = t, False
            self.aceitos += 1
            return i, "desliga", sentido
        if self.dentro is not None and self.sensor[self.dentro[0]] == sensor:
            return None                        # já está dentro deste feixe
        # próximo (indo), anterior (voltando); pulando um, primeiro no sentido do movimento
        pulos = [(k + 1, k + 2), (k - 2, k - 2)]
        if self.sentido < 0:
            pulos.reverse()
        for i, k_novo in [(k, k + 1), (k - 1, k - 1)] + pulos:
            if 0 <= i < n and self.sensor[i] == sensor:
                break
        else:
            self.ignorados += 1
            return None
        sentido = "ida" if k_novo > k else "volta"
        lo, hi = self.faixa[i]
        z = lo if sentido == "ida" else hi
        t_c, s_c = self._conf      # (a colher pode ter sido reancorada pelo IMU; vale o último sensor)
        dt = max((t - t_c) / 1e6, 1e-6)
        dist = abs(z - s_c)
        if dist > 30.0 and dist / dt > self.v_max:
            self.ignorados += 1
            return None
        self.sentido = 1.0 if sentido == "ida" else -1.0
        # entrou de novo, em menos de 1 s, no feixe de onde acabou de sair: costuma ser a mão
        # hesitando na beira do feixe. A colher não inverte o movimento por isso: espera no feixe.
        self._ambigua = (self._ult_saida is not None and self._ult_saida[0] == i
                         and (t - self._ult_saida[1]) / 1e6 < 1.0)
        if self._ambigua:
            self.ambiguas += 1
        v_ent = 0.0 if self._ambigua else self.sentido * self.v_mem
        self._reancorar(t, z, v_ent)     # (antes de mudar a zona: a tela de agora)
        self.k, self.dentro = k_novo, (i, sentido, t)
        self._conf = (t, z)
        self.t_evento, self.parado = t, False
        self.aceitos += 1
        return i, "liga", sentido

    def imu(self, t: float, quieto: bool) -> None:
        """Mão parada (IMU quieto) por QUIETO_PARA_S: a colher para; mexeu: continua."""
        if self.t_ref is None:
            return
        if self.k in (0, len(self.centros)) and self.dentro is None and self.t_evento is not None:
            # trechos sem sensor das pontas: quem manda é a velocidade medida pelo IMU (velocidade_imu)
            self._quieto_desde = None
            self.parado = False
            return
        if quieto:
            if self._quieto_desde is None:
                self._quieto_desde = t
            if (not self.parado and self.dentro is None
                    and (t - self._quieto_desde) / 1e6 >= self.QUIETO_PARA_S):
                self._v_salvo = self.v
                self._reancorar(t, self.posicao(t), 0.0)
                self.off = 0.0
                self.parado = True
                self.paradas += 1
        else:
            self._quieto_desde = None
            if self.parado:
                s = self.posicao(t)
                v = self._v_salvo
                # parou numa ponta (centro ou entrada) e voltou a mexer: só há um sentido possível
                if self.k == len(self.centros) and s >= self.L - 15.0:
                    v, self.sentido = -max(abs(v), self.V_PADRAO), -1.0
                elif self.k == 0 and s <= 15.0:
                    v, self.sentido = max(abs(v), self.V_PADRAO), 1.0
                self._reancorar(t, s, v)
                self.off = 0.0
                self.parado = False

    def posicao(self, t: float) -> float:
        if self.t_ref is None:
            return self.s
        lo, hi = self.limites()
        dt = max(0.0, (t - self.t_ref) / 1e6)
        off = self.off * math.exp(-dt / self.TAU)
        s = self.s + self.v * dt + off
        # a zona vale para a posição confirmada; o resto da diferença (que some aos poucos) pode estar fora
        return float(min(max(s, lo + min(off, 0.0), 0.0), hi + max(off, 0.0), self.L))

    def velocidade(self, t: float) -> float:
        if self.parado:
            return 0.0
        s = self.posicao(t)
        lo, hi = self.limites()
        if (self.v > 0 and s >= hi - 1e-6) or (self.v < 0 and s <= lo + 1e-6):
            return 0.0                         # esperando o próximo feixe
        return self.v

    def velocidade_imu(self, t: float, v_mod: float) -> None:
        """Nos trechos sem sensor das pontas: a colher anda com a velocidade medida pelo IMU (módulo)
        no sentido atual. Parou na ponta (centro ou entrada) = o próximo movimento é a volta."""
        if self.t_ref is None:
            return
        v_mod = float(min(v_mod, self.v_max))
        s = self.posicao(t)
        na_ponta = (self.k == len(self.centros) and s >= self.L - 25.0) or (self.k == 0 and s <= 25.0)
        if v_mod < 10.0:
            self._lento_desde = getattr(self, "_lento_desde", None) or t
            if na_ponta and (t - self._lento_desde) / 1e6 >= 0.3:
                self.sentido = -1.0 if self.k == len(self.centros) else 1.0
        else:
            self._lento_desde = None
        self._reancorar(t, s, self.sentido * v_mod)
        self.off = 0.0

    def cinematica_confiavel(self, t: float) -> tuple[float, float] | None:
        """(posição, velocidade) quando os dois são bem conhecidos: dentro de um feixe ou logo
        depois de uma borda, com a mão em movimento. Usado para descobrir a montagem do IMU."""
        if self.t_evento is None or self.parado:
            return None
        if self.dentro is None and (t - self.t_evento) / 1e6 > self.JANELA_CONFIAVEL_S:
            return None
        return self.posicao(t), self.velocidade(t) if self.dentro is None else self.sentido * self.v_mem


# ---------------------------------------------------------------------------
# Tempo real: reordena as medidas por tempo antes de entregar ao filtro
# ---------------------------------------------------------------------------
class SuavizadorTela:
    """Filtro "One Euro" (Casiez et al., 2012) só para a posição mostrada no jogo.

    Parado, faz média forte (a barra não treme); em movimento rápido, quase não segura
    (a barra acompanha a mão). As métricas clínicas não passam por aqui: vêm do
    pós-processamento sobre os dados brutos."""

    def __init__(self, corte_min_hz: float, beta: float, corte_velocidade_hz: float = 2.0) -> None:
        self.corte_min, self.beta, self.corte_v = corte_min_hz, beta, corte_velocidade_hz
        self.x: float | None = None
        self.rapidez = 0.0            # |velocidade| suavizada, mm/s
        self.t = 0.0

    @staticmethod
    def _alfa(corte_hz: float, dt: float) -> float:
        r = 2 * math.pi * corte_hz * dt
        return r / (r + 1)

    def __call__(self, t_s: float, x: float, v: float) -> float:
        """x: posição (mm); v: velocidade estimada pelo filtro de Kalman (mm/s). Derivar a
        própria posição ruidosa faria o ruído parecer movimento e a barra continuaria tremendo."""
        if self.x is None:
            self.x, self.t = x, t_s
            return x
        dt = t_s - self.t
        if dt <= 0:
            return self.x
        dt = min(dt, 0.2)
        self.t = t_s
        self.rapidez += self._alfa(self.corte_v, dt) * (abs(v) - self.rapidez)
        corte = self.corte_min + self.beta * self.rapidez
        self.x += self._alfa(corte, dt) * (x - self.x)
        return self.x


@dataclass(order=True)
class _Medida:
    t: float
    ordem: int
    tipo: str = field(compare=False)
    dados: tuple = field(compare=False)


class FusaoTempoReal:
    """As amostras da IMU chegam ~15–25 ms atrasadas (pacotes de 14 ms + rádio);
    as bordas e o VL53, quase na hora. As medidas esperam `atraso_us` numa fila
    ordenada por tempo e só então entram no filtro; o estado publicado é
    extrapolado até "agora" pelo próprio modelo."""

    def __init__(self, filtro: FiltroCaminho, atraso_us: float = 25_000) -> None:
        self.f = filtro
        self.atraso_us = atraso_us
        self._fila: list[_Medida] = []
        self._ordem = itertools.count()
        self.t_ultima_imu: float | None = None
        # reto (só VL53) precisa de mais suavização na tela; nos caminhos com IMU ela é leve
        self.tela = SuavizadorTela(0.5, 0.05) if filtro.so_vl53 else SuavizadorTela(1.5, 0.08)
        self._peso, self._t_peso, self._longe_desde = 0.0, None, None   # espiral: quanto a fusão entra na tela
        self._mono: tuple[float, float] | None = None   # espiral: (sentido, última posição na tela)

    def adicionar(self, t: float, tipo: str, dados: tuple) -> None:
        if tipo == "imu":
            self.t_ultima_imu = t if self.t_ultima_imu is None else max(self.t_ultima_imu, t)
        heapq.heappush(self._fila, _Medida(t, next(self._ordem), tipo, dados))

    def processar(self, agora_bancada: float) -> None:
        limite = agora_bancada - self.atraso_us
        if self.t_ultima_imu is not None and agora_bancada - self.t_ultima_imu < 100_000:
            limite = min(limite, self.t_ultima_imu)  # espera a IMU alcançar (se ela está chegando)
        while self._fila and self._fila[0].t <= limite:
            m = heapq.heappop(self._fila)
            if self.f.t is not None and m.t < self.f.t - 50_000:
                continue  # chegou tarde demais para o tempo real (fica só no pós-processamento)
            if m.tipo == "imu":
                self.f.imu(m.t, m.dados[0], m.dados[1])
            elif m.tipo == "borda":
                self.f.borda(m.t, *m.dados)
            elif m.tipo == "vl53":
                self.f.vl53(m.t, *m.dados)

    def estado(self, agora_bancada: float) -> dict:
        x = self.f.estado_em(agora_bancada)
        L = self.f.c.comprimento
        z = self.f.zonas
        fonte = None
        if z is not None:
            # espiral: a base são os sensores IR. Com o ângulo do IMU confirmado, a fusão entra
            # entre os feixes, na medida em que concorda com eles (e sempre dentro do trecho que
            # os sensores permitem).
            t_z = min(agora_bancada - self.atraso_us, self.f.t if self.f.t is not None else agora_bancada)
            z.iniciar(t_z)
            s_z, v_z = z.posicao(t_z), z.velocidade(t_z)
            alvo = 0.0
            nas_pontas = z.k in (0, len(z.centros)) and z.dentro is None
            if self.f.cont.montagem_confirmada and not nas_pontas:   # (nas pontas o próprio IMU já guia a colher)
                # a fusão entra na medida em que concorda com os sensores: inteira até 15 mm de
                # diferença, nada a partir de imu_tela_distancia_mm
                dif = abs(float(x[0]) - s_z)
                lim = self.f.cfg.imu_tela_distancia_mm
                alvo = float(np.clip((lim - dif) / max(lim - 15.0, 1.0), 0.0, 1.0))
            dt_p = 0.0 if self._t_peso is None else max(0.0, (agora_bancada - self._t_peso) / 1e6)
            self._t_peso = agora_bancada
            passo = 2.0 * dt_p                              # troca de fonte em ~0,5 s, sem pulo
            self._peso += float(np.clip(alvo - self._peso, -passo, passo))
            peso = self._peso
            lo, hi = z.limites()
            s_f = float(np.clip(x[0], lo - 5.0, hi + 5.0))
            x = x.copy()
            x[0] = peso * s_f + (1 - peso) * s_z
            x[1] = peso * float(x[1]) + (1 - peso) * v_z
            x[2] = 0.0
            # A colher não anda para trás enquanto os sensores dizem que o movimento é para a frente
            # (e vice-versa): o sentido só muda quando um feixe mostra a volta. Sem isso, o ruído
            # (e o sentido mal observado pelo IMU) fazia a colher ir e voltar.
            sent = z.sentido
            if z.k == 0 or z.k == len(z.centros):
                # nos trechos sem sensor das pontas (entrada e centro) a volta não passa por nenhum
                # feixe antes de acontecer: ali a colher pode mudar de sentido livremente
                self._mono = None
            elif self._mono is not None and self._mono[0] == sent:
                ant = self._mono[1]
                x[0] = max(float(x[0]), ant - 1.0) if sent > 0 else min(float(x[0]), ant + 1.0)
                if x[1] * sent < 0:
                    x[1] = 0.0
            self._mono = (sent, float(x[0]))
            fonte = round(peso, 2)
        s = float(np.clip(x[0], 0, L))
        s_tela = float(np.clip(self.tela(agora_bancada / 1e6, s, float(x[1])), 0, L))
        # "f" é o que o jogo desenha (suavizado); "s_mm" é a estimativa do filtro
        return {"s_mm": round(s, 2), "v_mm_s": round(float(x[1]), 2), "a_mm_s2": round(float(x[2]), 1),
                "f": round(s_tela / L, 5), "v_frac": round(float(x[1]) / L, 5),
                "sigma_mm": round(float(math.sqrt(max(self.f.P[0, 0], 0))), 2),
                "nivelado": self.f.R_nivel is not None, "repouso": bool(self.f.em_repouso),
                **({"peso_imu": fonte} if fonte is not None else {}),
                **({"giro_total_graus": round(math.degrees(abs(self.f.escala_giro or self.f._escala_nominal())
                                                            * self.f.c.comprimento), 1)}
                   if self.f.c.giro_acompanha else {})}


def reprocessar(caminho: Caminho, tabela: list[dict], calib_vl53: dict | None, imu: np.ndarray,
                bordas: np.ndarray, vl53: np.ndarray, s0: float | None = None,
                config: ConfigFusao | None = None, escala_giro: float | None = None) -> tuple[dict, Contadores]:
    """Roda o filtro sobre a partida inteira e aplica o RTS.

    imu: (N, 7) t_bancada_us, ax, ay, az, gx, gy, gz (brutos)
    bordas: (M, 3) t_us, sensor, nivel;  vl53: (K, 3) t_us, dist_mm, estado
    """
    f = FiltroCaminho(caminho, tabela, calib_vl53, config, s0=s0, historico=True, escala_giro=escala_giro)
    if f.zonas is not None and f.cfg.espiral_aprender_montagem and not f.cfg.montagem_ja_conhecida:
        # espiral: 1ª passada só para descobrir o ângulo da placa do IMU; a 2ª já começa com ele
        # (assim a partida inteira usa o IMU, não só depois da descoberta)
        f1 = _rodar(f, imu, bordas, vl53)
        if f1.cont.montagem_confirmada:
            from dataclasses import replace
            cfg2 = replace(f.cfg, rotacao_montagem_graus=math.degrees(f1.psi), montagem_ja_conhecida=True)
            f = FiltroCaminho(caminho, tabela, calib_vl53, cfg2, s0=s0, historico=True, escala_giro=escala_giro)
            f = _rodar(f, imu, bordas, vl53)
            f.cont.montagem_rodadas = f1.cont.montagem_rodadas
        else:
            # ângulo não descoberto (partida curta, movimento muito lento): as métricas usam o IMU
            # com a montagem padrão, como antes
            from dataclasses import replace
            cfg2 = replace(f.cfg, espiral_aprender_montagem=False)
            f = _rodar(FiltroCaminho(caminho, tabela, calib_vl53, cfg2, s0=s0, historico=True,
                                     escala_giro=escala_giro), imu, bordas, vl53)
        return f.suavizar(), f.cont
    f = _rodar(f, imu, bordas, vl53)
    return f.suavizar(), f.cont


def _rodar(f: "FiltroCaminho", imu: np.ndarray, bordas: np.ndarray, vl53: np.ndarray) -> "FiltroCaminho":
    eventos = []
    for r in imu:
        eventos.append((r[0], 0, "imu", (r[1:4], r[4:7])))
    for r in bordas:
        eventos.append((r[0], 1, "borda", (int(r[1]), int(r[2]))))
    for r in vl53:
        eventos.append((r[0], 2, "vl53", (int(r[1]), int(r[2]))))
    eventos.sort(key=lambda e: (e[0], e[1]))
    for t, _, tipo, d in eventos:
        if tipo == "imu":
            f.imu(t, d[0], d[1])
        elif tipo == "borda":
            f.borda(t, *d)
        else:
            f.vl53(t, *d)
    return f
