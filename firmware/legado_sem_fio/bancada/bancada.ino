// bancada.ino — firmware da unidade de bancada (fonte).
//
// Placa: ESP32-S3 (Arduino-ESP32 3.x), ligada ao computador por USB
// (recomendado: "USB CDC On Boot: Enabled" e isolador galvânico no cabo).
//
// Funções (arquitetura, seção "Hardware e firmware"):
//   - relógio mestre da sessão: tudo que sai daqui leva o relógio da bancada;
//   - lê os 5 sensores LM393 por interrupção, com filtro contra trepidação
//     (ignora pulsos < 5 ms e registra o instante da primeira troca estável);
//   - lê o VL53L0X do caminho reto a ~50 Hz, sem bloquear;
//   - responde aos PINGs de sincronia dos joysticks (ESP-NOW);
//   - repassa ao computador tudo que vem dos joysticks e repassa aos
//     joysticks os comandos do computador (ACK, reenvio, iniciar, parar).
//
// Bibliotecas: TrilhoProtocolo (firmware/libraries) e "VL53L0X" da Pololu.

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <esp_idf_version.h>
#include <VL53L0X.h>
#include <TrilhoProtocolo.h>

using namespace trilho;

// ============================ CONFIGURAÇÃO =================================
// Índices dos sensores (iguais ao servidor, docs/PROTOCOLO.md):
//   0 = guarda-chuva, lado esquerdo   1 = guarda-chuva, lado direito
//   2 = espiral S1 (bit 0 do código Gray)
//   3 = espiral S2 (bit 1)            4 = espiral S3 (bit 2)
static const uint8_t PINOS_LM393[5] = {4, 5, 6, 7, 15};
#define N_SENSORES 5
#define LM393_ATIVO_EM_BAIXO 1   // módulos LM393 comuns: saída LOW = objeto detectado
#define DEBOUNCE_US 5000
#define PINO_SDA 8
#define PINO_SCL 9
#define CANAL_WIFI 1
#define BAUD 921600
// ===========================================================================

#define INTERVALO_STATUS_US 500000
#define MAX_JOYSTICKS 3

static VL53L0X vl53;
static bool vl53Ok = false;
static uint16_t filaPerdida = 0;

// ------------------- bordas dos LM393 (ISR -> loop) -----------------------
struct Borda { uint32_t t; uint8_t sensor; uint8_t nivel; };
static Borda filaBordas[64];
static volatile uint8_t bordaIni = 0, bordaFim = 0;
static portMUX_TYPE muxBordas = portMUX_INITIALIZER_UNLOCKED;

static void IRAM_ATTR isrBorda(void* arg) {
  uint8_t s = (uint8_t)(uintptr_t)arg;
  uint32_t t = (uint32_t)esp_timer_get_time();
  uint8_t bruto = (uint8_t)gpio_get_level((gpio_num_t)PINOS_LM393[s]);
  portENTER_CRITICAL_ISR(&muxBordas);
  uint8_t prox = (uint8_t)((bordaFim + 1) % 64);
  if (prox != bordaIni) { filaBordas[bordaFim] = {t, s, bruto}; bordaFim = prox; }
  else filaPerdida++;
  portEXIT_CRITICAL_ISR(&muxBordas);
}

static inline uint8_t detectando(uint8_t bruto) { return LM393_ATIVO_EM_BAIXO ? (bruto == 0) : (bruto != 0); }

struct EstadoSensor { uint8_t estavel; bool pendente; uint32_t tPendente; };
static EstadoSensor sensores[N_SENSORES];

// ------------------- pacotes dos joysticks (callback -> loop) -------------
struct PacoteRecebido { uint32_t t_rx; int8_t rssi; uint8_t mac[6]; uint8_t len; uint8_t dados[MAX_ESPNOW]; };
static QueueHandle_t filaPacotes;

struct Joystick { uint8_t id; uint8_t mac[6]; uint32_t ultimoMs; int8_t rssi; bool ativo; };
static Joystick joysticks[MAX_JOYSTICKS];
static portMUX_TYPE muxJoy = portMUX_INITIALIZER_UNLOCKED;

// Chamada com muxJoy travado: só mexe na tabela (nada de chamadas bloqueantes).
static void registrarJoystick(uint8_t id, const uint8_t* mac, int8_t rssi) {
  Joystick* livre = nullptr;
  for (auto& j : joysticks) {
    if (j.ativo && j.id == id) { memcpy(j.mac, mac, 6); j.ultimoMs = millis(); j.rssi = rssi; return; }
    if (!j.ativo && !livre) livre = &j;
  }
  if (!livre) return;
  livre->ativo = true; livre->id = id; memcpy(livre->mac, mac, 6);
  livre->ultimoMs = millis(); livre->rssi = rssi;
}

// Fora da seção crítica: cadastra o par ESP-NOW para poder responder.
static void garantirPar(const uint8_t* mac) {
  if (esp_now_is_peer_exist(mac)) return;
  esp_now_peer_info_t par{};
  memcpy(par.peer_addr, mac, 6);
  par.channel = CANAL_WIFI;
  par.encrypt = false;
  esp_now_add_peer(&par);
}

static Joystick* acharJoystick(uint8_t id) {
  for (auto& j : joysticks) if (j.ativo && j.id == id) return &j;
  return nullptr;
}

#if ESP_IDF_VERSION_MAJOR >= 5
static void aoReceber(const esp_now_recv_info_t* info, const uint8_t* d, int n) {
  const uint8_t* mac = info->src_addr;
  int8_t rssi = info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : 0;
#else
static void aoReceber(const uint8_t* mac, const uint8_t* d, int n) {
  int8_t rssi = 0;
#endif
  uint32_t t2 = (uint32_t)esp_timer_get_time();   // primeiro: instante da chegada
  if (n < (int)sizeof(JoyCab) || n > (int)MAX_ESPNOW) return;
  const JoyCab* cab = (const JoyCab*)d;
  if (cab->versao != VERSAO_PROTOCOLO) return;

  portENTER_CRITICAL(&muxJoy);
  registrarJoystick(cab->joy_id, mac, rssi);
  portEXIT_CRITICAL(&muxJoy);
  garantirPar(mac);

  if (cab->tipo == J_PING && n >= (int)(sizeof(JoyCab) + sizeof(Ping))) {
    // Responde já, para o atraso ficar simétrico (estilo NTP).
    Ping p; memcpy(&p, d + sizeof(JoyCab), sizeof(p));
    uint8_t buf[sizeof(JoyCab) + sizeof(Pong)];
    JoyCab* r = (JoyCab*)buf;
    r->tipo = J_PONG; r->versao = VERSAO_PROTOCOLO; r->joy_id = cab->joy_id; r->n = 1; r->seq = cab->seq;
    Pong po{(uint16_t)cab->seq, 0, p.t1, t2, 0};
    po.t3 = (uint32_t)esp_timer_get_time();
    memcpy(buf + sizeof(JoyCab), &po, sizeof(po));
    esp_now_send(mac, buf, sizeof(buf));
    return;
  }

  PacoteRecebido pr;
  pr.t_rx = t2; pr.rssi = rssi; memcpy(pr.mac, mac, 6);
  pr.len = (uint8_t)n; memcpy(pr.dados, d, n);
  if (xQueueSend(filaPacotes, &pr, 0) != pdTRUE) filaPerdida++;
}

// =============================== serial ====================================
static uint8_t bufSaida[MAX_QUADRO + 8];
static LeitorQuadros leitor;

static void enviarQuadro(uint8_t tipo, const void* d, size_t n) {
  size_t m = montar_quadro(tipo, d, n, bufSaida);
  if (m) Serial.write(bufSaida, m);
}

static void enviarLog(const char* txt) { enviarQuadro(Q_LOG, txt, strlen(txt)); }

static void enviarStatus() {
  uint8_t buf[sizeof(QuadroStatusBancada) + MAX_JOYSTICKS * sizeof(InfoJoy)];
  QuadroStatusBancada s{};
  s.t_us = (uint32_t)esp_timer_get_time();
  s.versao_fw = VERSAO_FIRMWARE;
  s.fila_perdida = filaPerdida;
  s.vl53_ok = vl53Ok ? 1 : 0;
  uint8_t n = 0;
  portENTER_CRITICAL(&muxJoy);
  for (auto& j : joysticks) {
    if (!j.ativo) continue;
    InfoJoy info;
    info.joy_id = j.id; info.rssi = j.rssi;
    uint32_t dt = millis() - j.ultimoMs;
    info.ms_desde_ultimo = dt > 65535 ? 65535 : (uint16_t)dt;
    memcpy(buf + sizeof(s) + n * sizeof(InfoJoy), &info, sizeof(info));
    n++;
  }
  portEXIT_CRITICAL(&muxJoy);
  s.n_joy = n;
  memcpy(buf, &s, sizeof(s));
  enviarQuadro(Q_STATUS_BANC, buf, sizeof(s) + n * sizeof(InfoJoy));
}

static void tratarQuadroDoPc() {
  if (leitor.tipo() == Q_PEDIR_STATUS) { enviarStatus(); return; }
  if (leitor.tipo() != Q_CMD_JOY || leitor.tamanho() < sizeof(QuadroCmdJoy)) return;
  QuadroCmdJoy q; memcpy(&q, leitor.dados(), sizeof(q));
  uint8_t mac[6];
  bool ok = false;
  portENTER_CRITICAL(&muxJoy);
  Joystick* j = acharJoystick(q.joy_id);
  if (j) { memcpy(mac, j->mac, 6); ok = true; }
  portEXIT_CRITICAL(&muxJoy);
  if (!ok) return;
  uint8_t buf[sizeof(JoyCab) + sizeof(ComandoJoy)];
  JoyCab* cab = (JoyCab*)buf;
  cab->tipo = J_CMD; cab->versao = VERSAO_PROTOCOLO; cab->joy_id = q.joy_id; cab->n = 1; cab->seq = 0;
  ComandoJoy c{}; c.cmd = q.cmd; c.a = q.a; c.b = q.b;
  memcpy(buf + sizeof(JoyCab), &c, sizeof(c));
  esp_now_send(mac, buf, sizeof(buf));
}

// =============================== LM393 =====================================
static void servirBordas() {
  // 1) consome as bordas registradas nas interrupções
  while (true) {
    Borda b;
    portENTER_CRITICAL(&muxBordas);
    bool tem = bordaIni != bordaFim;
    if (tem) { b = filaBordas[bordaIni]; bordaIni = (uint8_t)((bordaIni + 1) % 64); }
    portEXIT_CRITICAL(&muxBordas);
    if (!tem) break;
    EstadoSensor& s = sensores[b.sensor];
    uint8_t det = detectando(b.nivel);
    if (det != s.estavel && !s.pendente) { s.pendente = true; s.tPendente = b.t; }
    else if (det == s.estavel && s.pendente) s.pendente = false;   // pulso curto: descarta
  }
  // 2) aceita trocas que ficaram estáveis por DEBOUNCE_US
  uint32_t agora = (uint32_t)esp_timer_get_time();
  for (uint8_t i = 0; i < N_SENSORES; i++) {
    EstadoSensor& s = sensores[i];
    if (!s.pendente) continue;
    uint8_t det = detectando((uint8_t)gpio_get_level((gpio_num_t)PINOS_LM393[i]));
    if (det == s.estavel) { s.pendente = false; continue; }
    if ((uint32_t)(agora - s.tPendente) >= DEBOUNCE_US) {
      s.estavel = det; s.pendente = false;
      QuadroBorda q{s.tPendente, i, det, 0};
      enviarQuadro(Q_BORDA, &q, sizeof(q));
    }
  }
}

// =============================== VL53L0X ===================================
static void servirVl53() {
  if (!vl53Ok) return;
  // Leitura sem bloquear (equivale a readRangeContinuousMillimeters()).
  if ((vl53.readReg(VL53L0X::RESULT_INTERRUPT_STATUS) & 0x07) == 0) return;
  uint32_t t = (uint32_t)esp_timer_get_time();
  uint8_t st = vl53.readReg(VL53L0X::RESULT_RANGE_STATUS);
  uint16_t mm = vl53.readReg16Bit(VL53L0X::RESULT_RANGE_STATUS + 10);
  vl53.writeReg(VL53L0X::SYSTEM_INTERRUPT_CLEAR, 0x01);
  uint8_t codigo = (st & 0x78) >> 3;   // 11 = medida válida
  QuadroVl53 q{};
  q.t_us = t - 10000;                  // meio do orçamento de 20 ms
  q.dist_mm = mm;
  q.estado = (codigo == 11 && mm < 8190) ? 0 : (mm >= 8190 ? 1 : 2);
  enviarQuadro(Q_VL53, &q, sizeof(q));
}

// =============================== setup/loop ================================
void setup() {
  Serial.begin(BAUD);
  filaPacotes = xQueueCreate(48, sizeof(PacoteRecebido));

  for (uint8_t i = 0; i < N_SENSORES; i++) {
    pinMode(PINOS_LM393[i], INPUT_PULLUP);
    sensores[i].estavel = detectando((uint8_t)digitalRead(PINOS_LM393[i]));
    sensores[i].pendente = false;
    attachInterruptArg(digitalPinToInterrupt(PINOS_LM393[i]), isrBorda, (void*)(uintptr_t)i, CHANGE);
  }

  Wire.begin(PINO_SDA, PINO_SCL, 400000);
  vl53.setTimeout(50);
  vl53Ok = vl53.init();
  if (vl53Ok) {
    vl53.setMeasurementTimingBudget(20000);   // 20 ms -> ~50 Hz
    vl53.startContinuous();
  }

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(CANAL_WIFI, WIFI_SECOND_CHAN_NONE);
  esp_now_init();
  esp_now_register_recv_cb(aoReceber);

  delay(200);
  char txt[96];
  snprintf(txt, sizeof(txt), "bancada iniciada; MAC %s; VL53L0X %s; fw %u",
           WiFi.macAddress().c_str(), vl53Ok ? "ok" : "ausente", (unsigned)VERSAO_FIRMWARE);
  enviarLog(txt);
}

static uint64_t tUltimoStatus = 0;

void loop() {
  // comandos do computador
  while (Serial.available()) {
    if (leitor.alimentar((uint8_t)Serial.read())) tratarQuadroDoPc();
  }

  // pacotes dos joysticks -> computador
  PacoteRecebido pr;
  while (xQueueReceive(filaPacotes, &pr, 0) == pdTRUE) {
    uint8_t buf[sizeof(CabPacoteJoy) + MAX_ESPNOW];
    CabPacoteJoy c{pr.t_rx, pr.rssi, {0, 0, 0}};
    memcpy(buf, &c, sizeof(c));
    memcpy(buf + sizeof(c), pr.dados, pr.len);
    enviarQuadro(Q_PACOTE_JOY, buf, sizeof(c) + pr.len);
  }

  servirBordas();
  servirVl53();

  uint64_t agora = (uint64_t)esp_timer_get_time();
  if (agora - tUltimoStatus >= INTERVALO_STATUS_US) {
    tUltimoStatus = agora;
    enviarStatus();
  }
}
