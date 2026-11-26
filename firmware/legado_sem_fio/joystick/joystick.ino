// joystick.ino — firmware do joystick móvel (bateria).
//
// Placa: ESP32-S3 com PSRAM (Arduino-ESP32 3.x). Em "Ferramentas", ative
// "PSRAM: OPI PSRAM" (ou QSPI, conforme o módulo).
//
// O joystick só adquire e carimba o tempo (arquitetura, seção "Hardware e
// firmware"): lê o MPU6050 a 500 Hz pelo FIFO e, no joystick C, a célula de
// carga pelo HX711 a 80 Hz. Envia tudo à bancada por ESP-NOW, em pacotes
// numerados que ficam guardados até o computador confirmar (ACK). Também faz a
// troca de mensagens de sincronia de relógio com a bancada a cada 100 ms; o
// ajuste da reta (diferença + deriva) é feito no computador.
//
// Biblioteca necessária: TrilhoProtocolo (pasta firmware/libraries).

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <esp_idf_version.h>
#include <TrilhoProtocolo.h>

using namespace trilho;

// ============================ CONFIGURAÇÃO =================================
// Identificação: 'A' = guarda-chuva (vidro), 'B' = espiral (bolo), 'C' = reto (carro).
#define JOY_ID 'C'
// Célula de carga (quatro meias-pontes + HX711): só no joystick C.
#define TEM_FORCA (JOY_ID == 'C')
// Endereço MAC da bancada: aparece no painel do servidor ("Bancada: MAC ...")
// e no log do servidor quando a bancada liga. Copie aqui.
static const uint8_t MAC_BANCADA[6] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
#define CANAL_WIFI 1

#define PINO_SDA 8
#define PINO_SCL 9
#define PINO_HX_DOUT 4
#define PINO_HX_SCK 5
#define PINO_BATERIA 1        // ADC: tensão da bateria por um divisor resistivo
#define DIVISOR_BATERIA 2.0f  // 100k/100k -> metade da tensão
#define PINO_CARREGANDO 2     // HIGH = carregador conectado
// ===========================================================================

#define MPU_ENDERECO 0x68
#define PERIODO_NOMINAL_US 2000.0  // 500 Hz
#define INTERVALO_PING_US 100000   // sincronia a cada 100 ms
#define INTERVALO_STATUS_US 1000000

// --------------------------- buffer de reenvio ----------------------------
// Cada pacote de dados fica guardado até o ACK do computador. Com PSRAM:
// 4096 pacotes (~45 s de folga); sem PSRAM: 768.
struct Slot { uint32_t seq; uint8_t len; uint8_t dados[128]; };
static Slot* slots = nullptr;
static uint32_t nSlots = 0;
static uint32_t proxSeq = 1;
static uint32_t seqConfirmado = 0;
static uint16_t pacotesPerdidos = 0;

// --------------------------- estado geral ---------------------------------
static volatile EstadoJoy estado = E_OCIOSO;
static uint16_t overflowFifo = 0;
static uint64_t tUltimoPing = 0, tUltimoStatus = 0;
static uint16_t pingId = 0;

// PONG recebido na callback -> tratado no loop.
static volatile bool pongPendente = false;
static Pong pongRecebido;
static volatile uint32_t t4Pong = 0;

// Comandos recebidos na callback -> fila tratada no loop.
static QueueHandle_t filaComandos;

// Pacotes em montagem.
static uint8_t pacImu[sizeof(JoyCab) + IMU_POR_PACOTE * sizeof(AmostraImu)];
static uint8_t nImu = 0;
#if TEM_FORCA
static uint8_t pacForca[sizeof(JoyCab) + FORCA_POR_PACOTE * sizeof(AmostraForca)];
static uint8_t nForca = 0;
#endif

static inline uint64_t agoraUs() { return (uint64_t)esp_timer_get_time(); }

// ============================== ESP-NOW ====================================
static void enviarBruto(const uint8_t* d, size_t n) {
  esp_now_send(MAC_BANCADA, d, n);
}

// Numera, guarda e envia um pacote de dados (IMU, FORCA, SYNC, STATUS).
static void enviarDados(uint8_t* d, size_t n) {
  JoyCab* cab = (JoyCab*)d;
  cab->versao = VERSAO_PROTOCOLO;
  cab->joy_id = JOY_ID;
  cab->seq = proxSeq++;
  if (slots && n <= sizeof(slots[0].dados)) {
    Slot& s = slots[cab->seq % nSlots];
    // Sobrescrever um pacote ainda não confirmado = perda durante a gravação.
    if (s.seq > seqConfirmado && s.seq != 0 && estado == E_GRAVANDO) pacotesPerdidos++;
    s.seq = cab->seq; s.len = (uint8_t)n; memcpy(s.dados, d, n);
  }
  enviarBruto(d, n);
}

static void reenviar(uint32_t de, uint32_t ate) {
  if (!slots) return;
  uint32_t enviados = 0;
  for (uint32_t s = de; s <= ate && enviados < 64; s++) {
    if (s <= seqConfirmado) continue;
    const Slot& sl = slots[s % nSlots];
    if (sl.seq == s) { enviarBruto(sl.dados, sl.len); enviados++; delayMicroseconds(300); }
  }
}

#if ESP_IDF_VERSION_MAJOR >= 5
static void aoReceber(const esp_now_recv_info_t* info, const uint8_t* d, int n) {
#else
static void aoReceber(const uint8_t* mac, const uint8_t* d, int n) {
#endif
  uint32_t t4 = (uint32_t)esp_timer_get_time();
  if (n < (int)sizeof(JoyCab)) return;
  const JoyCab* cab = (const JoyCab*)d;
  if (cab->versao != VERSAO_PROTOCOLO || cab->joy_id != JOY_ID) return;
  if (cab->tipo == J_PONG && n >= (int)(sizeof(JoyCab) + sizeof(Pong))) {
    memcpy((void*)&pongRecebido, d + sizeof(JoyCab), sizeof(Pong));
    t4Pong = t4;
    pongPendente = true;
  } else if (cab->tipo == J_CMD && n >= (int)(sizeof(JoyCab) + sizeof(ComandoJoy))) {
    ComandoJoy c;
    memcpy(&c, d + sizeof(JoyCab), sizeof(c));
    xQueueSend(filaComandos, &c, 0);
  }
}

// ============================== MPU6050 ====================================
static void mpuEscrever(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(MPU_ENDERECO); Wire.write(reg); Wire.write(v); Wire.endTransmission();
}
static uint8_t mpuLer(uint8_t reg, uint8_t* buf, uint8_t n) {
  Wire.beginTransmission(MPU_ENDERECO); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return 0;
  uint8_t r = Wire.requestFrom((uint8_t)MPU_ENDERECO, n);
  for (uint8_t i = 0; i < r; i++) buf[i] = Wire.read();
  return r;
}

static bool mpuIniciar() {
  uint8_t who = 0;
  mpuLer(0x75, &who, 1);
  if (who != 0x68 && who != 0x70 && who != 0x72) return false;  // clones respondem 0x70/0x72
  mpuEscrever(0x6B, 0x80); delay(100);   // reset
  mpuEscrever(0x6B, 0x01); delay(10);    // relógio: PLL do giroscópio X
  mpuEscrever(0x1A, 0x04);               // DLPF ~20 Hz (arquitetura: movimento voluntário < 10 Hz)
  mpuEscrever(0x19, 0x01);               // 1 kHz / (1 + 1) = 500 Hz
  mpuEscrever(0x1B, 0x08);               // giroscópio ±500 °/s
  mpuEscrever(0x1C, 0x08);               // acelerômetro ±4 g
  mpuEscrever(0x38, 0x10);               // habilita o aviso de FIFO cheio
  mpuEscrever(0x6A, 0x04); delay(1);     // reseta o FIFO
  mpuEscrever(0x6A, 0x40);               // liga o FIFO
  mpuEscrever(0x23, 0x78);               // acelerômetro + giroscópio no FIFO (12 bytes/amostra)
  return true;
}

// Estimador do instante de cada amostra do FIFO: o MPU6050 tem relógio
// próprio; corrige continuamente a diferença e o período (arquitetura,
// "Cuidados de tempo").
static double tUltimaAmostra = 0;   // us (relógio do ESP32)
static double periodoEst = PERIODO_NOMINAL_US;
static bool estimadorIniciado = false;

static void fecharPacoteImu() {
  if (nImu == 0) return;
  JoyCab* cab = (JoyCab*)pacImu;
  cab->tipo = J_IMU; cab->n = nImu;
  enviarDados(pacImu, sizeof(JoyCab) + nImu * sizeof(AmostraImu));
  nImu = 0;
}

static void mpuServir() {
  uint8_t st = 0;
  mpuLer(0x3A, &st, 1);
  if (st & 0x10) {                       // FIFO transbordou: dados perdidos, recomeça
    overflowFifo++;
    mpuEscrever(0x6A, 0x04); mpuEscrever(0x6A, 0x40);
    estimadorIniciado = false;
    return;
  }
  uint8_t c[2];
  uint64_t tLeitura = agoraUs();
  if (mpuLer(0x72, c, 2) != 2) return;
  uint16_t bytes = ((uint16_t)c[0] << 8) | c[1];
  uint16_t k = bytes / 12;
  if (k == 0) return;
  if (k > 80) k = 80;

  // A amostra mais recente foi medida, em média, meio período antes da leitura.
  double obs = (double)tLeitura - periodoEst * 0.5;
  double previsto = tUltimaAmostra + k * periodoEst;
  double erro = obs - previsto;
  if (!estimadorIniciado || fabs(erro) > 20000.0) {
    tUltimaAmostra = obs - k * periodoEst;
    erro = 0;
    estimadorIniciado = true;
  }
  double t0 = tUltimaAmostra;

  uint16_t lidas = 0;
  while (lidas < k) {
    uint8_t lote = (k - lidas) > 10 ? 10 : (k - lidas);   // buffer I2C do ESP32 = 128 bytes
    uint8_t b[120];
    if (mpuLer(0x74, b, lote * 12) != lote * 12) return;
    for (uint8_t i = 0; i < lote; i++) {
      const uint8_t* p = b + i * 12;
      AmostraImu a;
      a.t_us = (uint32_t)(uint64_t)(t0 + (lidas + i + 1) * periodoEst);
      a.ax = (int16_t)((p[0] << 8) | p[1]);  a.ay = (int16_t)((p[2] << 8) | p[3]);
      a.az = (int16_t)((p[4] << 8) | p[5]);  a.gx = (int16_t)((p[6] << 8) | p[7]);
      a.gy = (int16_t)((p[8] << 8) | p[9]);  a.gz = (int16_t)((p[10] << 8) | p[11]);
      memcpy(pacImu + sizeof(JoyCab) + nImu * sizeof(AmostraImu), &a, sizeof(a));
      if (++nImu >= IMU_POR_PACOTE) fecharPacoteImu();
    }
    lidas += lote;
  }
  tUltimaAmostra = t0 + k * periodoEst + 0.02 * erro;
  periodoEst += 1e-4 * erro / k;
  if (periodoEst < 1900) periodoEst = 1900;
  if (periodoEst > 2100) periodoEst = 2100;
}

// ============================== HX711 ======================================
#if TEM_FORCA
static void forcaServir() {
  if (digitalRead(PINO_HX_DOUT) != LOW) return;   // ainda não há leitura nova
  uint32_t t = (uint32_t)esp_timer_get_time();
  uint32_t v = 0;
  noInterrupts();
  for (int i = 0; i < 24; i++) {
    digitalWrite(PINO_HX_SCK, HIGH); delayMicroseconds(1);
    v = (v << 1) | (digitalRead(PINO_HX_DOUT) ? 1 : 0);
    digitalWrite(PINO_HX_SCK, LOW); delayMicroseconds(1);
  }
  digitalWrite(PINO_HX_SCK, HIGH); delayMicroseconds(1);   // 25º pulso: canal A, ganho 128
  digitalWrite(PINO_HX_SCK, LOW);
  interrupts();
  int32_t bruto = (v & 0x800000) ? (int32_t)(v | 0xFF000000) : (int32_t)v;

  AmostraForca a{t, bruto};
  memcpy(pacForca + sizeof(JoyCab) + nForca * sizeof(AmostraForca), &a, sizeof(a));
  if (++nForca >= FORCA_POR_PACOTE) {
    JoyCab* cab = (JoyCab*)pacForca;
    cab->tipo = J_FORCA; cab->n = nForca;
    enviarDados(pacForca, sizeof(JoyCab) + nForca * sizeof(AmostraForca));
    nForca = 0;
  }
}
#endif

// ============================== status =====================================
static bool carregando() { return digitalRead(PINO_CARREGANDO) == HIGH; }

static void enviarStatus() {
  uint8_t buf[sizeof(JoyCab) + sizeof(StatusJoy)];
  JoyCab* cab = (JoyCab*)buf;
  cab->tipo = J_STATUS; cab->n = 1;
  StatusJoy s{};
  s.t_us = (uint32_t)esp_timer_get_time();
  s.bateria_mv = (uint16_t)(analogReadMilliVolts(PINO_BATERIA) * DIVISOR_BATERIA);
  s.carregando = carregando() ? 1 : 0;
  s.estado = estado;
  s.overflow_fifo = overflowFifo;
  s.versao_fw = VERSAO_FIRMWARE;
  uint32_t pend = (proxSeq - 1) - seqConfirmado;
  s.pacotes_pendentes = pend > 65535 ? 65535 : (uint16_t)pend;
  s.pacotes_perdidos = pacotesPerdidos;
  s.tem_forca = TEM_FORCA;
  memcpy(buf + sizeof(JoyCab), &s, sizeof(s));
  enviarDados(buf, sizeof(buf));
}

static void tratarComando(const ComandoJoy& c) {
  switch (c.cmd) {
    case C_INICIAR:
      // Arquitetura: bloquear o uso com o carregador conectado.
      if (!carregando()) { estado = E_GRAVANDO; pacotesPerdidos = 0; }
      enviarStatus();
      break;
    case C_PARAR:
      if (estado == E_GRAVANDO) estado = E_PRONTO;
      enviarStatus();
      break;
    case C_ACK_ATE:
      if (c.a > seqConfirmado && c.a < proxSeq) seqConfirmado = c.a;
      break;
    case C_REENVIAR:
      reenviar(c.a, c.b);
      break;
  }
}

// ============================== setup/loop =================================
void setup() {
  pinMode(PINO_CARREGANDO, INPUT);
#if TEM_FORCA
  pinMode(PINO_HX_DOUT, INPUT);
  pinMode(PINO_HX_SCK, OUTPUT);
  digitalWrite(PINO_HX_SCK, LOW);
#endif
  analogReadResolution(12);

  nSlots = 4096;
  slots = (Slot*)ps_malloc(nSlots * sizeof(Slot));
  if (!slots) { nSlots = 768; slots = (Slot*)malloc(nSlots * sizeof(Slot)); }
  if (slots) memset(slots, 0, nSlots * sizeof(Slot));

  filaComandos = xQueueCreate(16, sizeof(ComandoJoy));

  Wire.begin(PINO_SDA, PINO_SCL, 400000);
  while (!mpuIniciar()) delay(500);   // sem IMU não há o que medir

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(CANAL_WIFI, WIFI_SECOND_CHAN_NONE);
  esp_now_init();
  esp_now_register_recv_cb(aoReceber);
  esp_now_peer_info_t par{};
  memcpy(par.peer_addr, MAC_BANCADA, 6);
  par.channel = CANAL_WIFI;
  par.encrypt = false;
  esp_now_add_peer(&par);
}

void loop() {
  uint64_t agora = agoraUs();

  mpuServir();
#if TEM_FORCA
  forcaServir();
#endif

  // Sincronia: PING a cada 100 ms; o PONG vira um registro numerado.
  if (agora - tUltimoPing >= INTERVALO_PING_US) {
    tUltimoPing = agora;
    uint8_t buf[sizeof(JoyCab) + sizeof(Ping)];
    JoyCab* cab = (JoyCab*)buf;
    cab->tipo = J_PING; cab->versao = VERSAO_PROTOCOLO; cab->joy_id = JOY_ID; cab->n = 1;
    cab->seq = ++pingId;
    Ping p{(uint32_t)esp_timer_get_time()};
    memcpy(buf + sizeof(JoyCab), &p, sizeof(p));
    enviarBruto(buf, sizeof(buf));
  }
  if (pongPendente) {
    pongPendente = false;
    uint8_t buf[sizeof(JoyCab) + sizeof(RegistroSync)];
    JoyCab* cab = (JoyCab*)buf;
    cab->tipo = J_SYNC; cab->n = 1;
    RegistroSync r{pongRecebido.t1, pongRecebido.t2, pongRecebido.t3, t4Pong, pongRecebido.ping_id, 0};
    memcpy(buf + sizeof(JoyCab), &r, sizeof(r));
    enviarDados(buf, sizeof(buf));
    if (estado == E_OCIOSO) estado = E_PRONTO;
  }

  if (carregando()) estado = E_CARREGANDO;
  else if (estado == E_CARREGANDO) estado = E_PRONTO;

  ComandoJoy c;
  while (xQueueReceive(filaComandos, &c, 0) == pdTRUE) tratarComando(c);

  if (agora - tUltimoStatus >= INTERVALO_STATUS_US) {
    tUltimoStatus = agora;
    enviarStatus();
  }
}
