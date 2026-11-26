/*
  TRILHO — firmware da ESP32 da CAIXA (versão para o software Trilho)

  Baseado no código de teste da equipe de hardware: mesmos pinos, mesmos
  barramentos e os mesmos sensores. A diferença é o que sai pela USB: em vez de
  resultados já calculados (passos, ângulo, tremor), a caixa manda as LEITURAS
  BRUTAS com o horário exato de cada uma. Os cálculos (posição, velocidade,
  tremor, suavidade...) são feitos pelo programa Trilho no computador.

  LIGAÇÕES (iguais às do código de teste)
  ESPIRAL (bolo)          IR S1 -> D25   IR S2 -> D27   IR S3 -> D13
                          IMU  SDA -> D18  SCL -> D19  AD0 -> GND ou solto (0x68)   [joystick B, MPU-92.65]
                               (MPU-92.65: NCS -> 3V3 e FSYNC -> GND; EDA/ECL e INT ficam soltos.
                                A placa usada ignora o próprio AD0 e fica sempre em 0x68.)
  RETO (carro)            VL53L0X SDA -> D21  SCL -> D22 (0x29)
                          IMU  SDA -> D21  SCL -> D22  AD0 -> GND (0x68)   [joystick C]
  GUARDA-CHUVA (janela)   IR ponta A (esquerda) -> D14   IR ponta B (direita) -> D26
                          IMU  SDA -> D18  SCL -> D19  AD0 -> 3V3 (0x69)   [joystick A]
  Todos: VCC/VIN -> 3V3   GND -> GND

  Biblioteca: "VL53L0X" by Pololu.
  Placa: "ESP32 Dev Module".

  MONITOR SERIAL: 921600 baud (não 115200). Tudo é texto e dá para ler.
  Comandos (digite e aperte Enter):
    ?        ajuda
    v        liga/desliga um resumo legível a cada segundo (linhas com #)
    status   manda o status agora
    i2c      lista os endereços que respondem em cada barramento I2C
    ativo B  escolhe o IMU que manda leituras rápidas (A, B, C ou 0 = nenhum).
             O programa Trilho faz isso sozinho ao começar e terminar cada jogo.

  LINHAS PARA O PROGRAMA (ver docs/PROTOCOLO_CAIXA.md)
  Toda linha termina com *XX: "ou exclusivo" de todos os caracteres antes do '*',
  em hexadecimal. O programa descarta linhas com erro. Linhas com # são só para
  pessoas e o programa ignora.
    S,<t_us>,<fw>,<vl53>,<imuA>,<imuB>,<imuC>,<ativo>,<ir>,<descartadas>   status (2x por segundo)
    I,<t_us>,<joy>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>   IMU bruto (joy A/B/C); o ativo a 250 Hz, os outros a 10 Hz
    B,<t_us>,<sensor>,<nivel>                      IR mudou (nivel 1 = viu o joystick); horário da 1ª troca estável
    D,<t_us>,<mm>,<estado>                         VL53L0X (estado 0 ok, 1 fora do alcance, 2 sem resposta)
    L,<texto>                                      mensagem
  <t_us> = microssegundos desde que a placa ligou (64 bits, não dá a volta).
  Sensores IR: 0 = ponta A, 1 = ponta B, 2 = S1, 3 = S2, 4 = S3.
*/

#include <Wire.h>
#include <VL53L0X.h>
#include <stdarg.h>
#include "esp_timer.h"
#include "caixa_tipos.h"   // tipos Imu e Ir (num .h para a Arduino IDE enxergar antes das funções)

#define VERSAO_FIRMWARE 207

// ======================= Ajustes =======================
const uint32_t BAUD          = 921600;
const uint32_t I2C_HZ        = 100000;   // a mesma do código de teste (funciona com os cabos dos joysticks)
const int64_t  PERIODO_IMU_ATIVO_US  = 4000;     // 250 Hz
const int64_t  PERIODO_IMU_MONITOR_US = 100000;  // 10 Hz para os outros (página Calibração)
const int64_t  DEBOUNCE_IR_US = 20000;           // ignora trocas mais curtas que 20 ms (reflexo, mão, luz: piscadas curtas)
const int64_t  PERIODO_STATUS_US = 500000;
const int64_t  PERIODO_REINICIAR_IMU_US = 2000000;  // tenta religar IMU desconectado a cada 2 s

// Endereços dos dois IMUs que dividem D18/D19 (dependem de onde o pino AD0 está ligado:
// GND = 0x68, 3V3 = 0x69). Se a espiral e a janela aparecerem trocadas, troque estes dois.
// A MPU-92.65 da espiral ignora o AD0 (fica em 0x68), então a janela é que vai para 0x69.
const uint8_t END_IMU_JANELA  = 0x69;   // joystick A (AD0 da MPU6050 da janela em 3V3)
const uint8_t END_IMU_ESPIRAL = 0x68;   // joystick B (MPU-92.65 com AD0 em GND ou solto)

// ======================= Pinos =======================
// índice no programa Trilho: 0 ponta A, 1 ponta B, 2 S1, 3 S2, 4 S3
const int PINO_IR[5] = {14, 26, 25, 27, 13};

// ======================= Saída em linhas com verificação =======================
static uint32_t descartadas = 0;      // linhas não enviadas porque a USB estava cheia

static void enviar(const char* fmt, ...) __attribute__((format(printf, 1, 2)));   // o compilador confere os campos
static void enviar(const char* fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof(buf) - 6, fmt, ap);
  va_end(ap);
  if (n <= 0) return;
  if (n > (int)sizeof(buf) - 6) n = sizeof(buf) - 6;
  uint8_t ck = 0;
  for (int i = 0; i < n; i++) ck ^= (uint8_t)buf[i];
  n += snprintf(buf + n, 6, "*%02X\n", ck);
  if (Serial.availableForWrite() < n) { descartadas++; return; }   // nunca trava o laço
  Serial.write((const uint8_t*)buf, n);
}

static void varrerI2c(TwoWire& bus, const char* nome);   // declarada antes (usada nos comandos)

static inline unsigned long long agoraUs() { return (unsigned long long)esp_timer_get_time(); }

// ======================= IMU (MPU6050 ou MPU-9250) =======================
// O MPU-9250 (placa "MPU-92.65") e o MPU6500 têm o acelerômetro e o giroscópio com os mesmos
// registradores e as mesmas escalas do MPU6050, então as leituras saem iguais para o programa.
// Diferenças tratadas aqui: o filtro do acelerômetro fica num registrador próprio (0x1D) e o
// WHO_AM_I (0x75) identifica o modelo. O magnetômetro do MPU-9250 não é usado.
Imu imus[3] = {
  {'A', &Wire1, END_IMU_JANELA, false, 0, 0, 0},    // guarda-chuva (janela)
  {'B', &Wire1, END_IMU_ESPIRAL, false, 0, 0, 0},   // espiral (bolo)
  {'C', &Wire,  0x68, false, 0, 0, 0},   // reto (carro)
};
char ativo = '0';

static bool imuEscreve(Imu& m, uint8_t reg, uint8_t valor) {
  m.bus->beginTransmission(m.end);
  m.bus->write(reg);
  m.bus->write(valor);
  return m.bus->endTransmission() == 0;
}

static uint8_t imuLeRegistro(Imu& m, uint8_t reg) {
  m.bus->beginTransmission(m.end);
  m.bus->write(reg);
  if (m.bus->endTransmission(false) != 0) return 0;
  if (m.bus->requestFrom((int)m.end, 1) != 1) return 0;
  return m.bus->read();
}

static bool familia6500(uint8_t quem) {   // MPU6500, MPU9250, MPU9255 e parentes
  return quem == 0x70 || quem == 0x71 || quem == 0x73 || quem == 0x74 || quem == 0x75;
}

static const char* nomeImu(const Imu& m) {
  if (!m.ok) return "ausente";
  switch (m.quem) {
    case 0x68: return "MPU6050";
    case 0x70: return "MPU6500";
    case 0x71: return "MPU9250";
    case 0x73: return "MPU9255";
    case 0x74: case 0x75: return "MPU6515";
    default: {                         // cópia ou outro chip: mostra o código para identificar
      static char txt[3][28];
      char* t = txt[m.id - 'A'];
      snprintf(t, sizeof(txt[0]), "IMU desconhecido 0x%02X", m.quem);
      return t;
    }
  }
}

static bool imuIniciar(Imu& m) {
  m.ok = imuEscreve(m, 0x6B, 0x01);     // acorda, relógio do giroscópio X (ou automático no MPU-9250)
  if (!m.ok) return false;
  m.quem = imuLeRegistro(m, 0x75);      // WHO_AM_I
  if (familia6500(m.quem)) {
    // MPU6500/9250: reinício completo antes de configurar. Sem isso, algumas placas ficam com o
    // giroscópio parado (lendo 0) mesmo com o acelerômetro funcionando.
    imuEscreve(m, 0x6B, 0x80);          // reinicia o chip
    delay(100);
    imuEscreve(m, 0x6B, 0x01);          // acorda, relógio automático (PLL do giroscópio)
    delay(20);
    imuEscreve(m, 0x6A, 0x00);          // USER_CTRL: sem I2C mestre interno nem FIFO
    imuEscreve(m, 0x37, 0x02);          // INT_PIN_CFG: bypass (o magnetômetro não atrapalha o I2C)
  }
  imuEscreve(m, 0x6C, 0x00);            // liga os 6 eixos (no MPU-9250 podem voltar desligados)
  imuEscreve(m, 0x1A, 0x03);            // filtro do giro ~42 Hz (deixa passar o tremor de 4 a 12 Hz)
  imuEscreve(m, 0x19, 0x03);            // 1 kHz / (1 + 3) = 250 amostras por segundo
  imuEscreve(m, 0x1B, 0x08);            // giro ±500 °/s (65,5 por °/s)
  imuEscreve(m, 0x1C, 0x08);            // acel ±4 g (8192 por g)
  if (familia6500(m.quem)) imuEscreve(m, 0x1D, 0x03);   // filtro do acel ~45 Hz (no MPU6050 vem do 0x1A)
  m.falhas = 0;
  return true;
}

// lê acel (3), temperatura (ignorada) e giro (3) de uma vez: 14 bytes a partir de 0x3B
static bool imuLer(Imu& m, int16_t v[7]) {
  m.bus->beginTransmission(m.end);
  m.bus->write(0x3B);
  if (m.bus->endTransmission(false) != 0) return false;
  if (m.bus->requestFrom((int)m.end, 14) != 14) return false;
  for (int i = 0; i < 7; i++) {
    uint8_t alto = m.bus->read(), baixo = m.bus->read();
    v[i] = (int16_t)((alto << 8) | baixo);
  }
  return true;
}

static void servirImu(Imu& m, int64_t agora) {
  if (!m.ok || agora < m.proxima) return;
  const bool rapido = (m.id == ativo);
  m.proxima = (m.proxima == 0 ? agora : m.proxima) + (rapido ? PERIODO_IMU_ATIVO_US : PERIODO_IMU_MONITOR_US);
  if (m.proxima < agora) m.proxima = agora + (rapido ? PERIODO_IMU_ATIVO_US : PERIODO_IMU_MONITOR_US);  // atrasou: não acumula
  int16_t v[7];
  unsigned long long t = agoraUs();
  if (!imuLer(m, v)) {
    if (++m.falhas >= 5) { m.ok = false; enviar("L,IMU %c parou de responder", m.id); }
    return;
  }
  m.falhas = 0;
  // Só zeros = o IMU reiniciou (mau contato, queda de tensão) e voltou "dormindo": continua
  // respondendo no I2C, mas não mede. Acorda e configura de novo; a leitura zerada não é enviada.
  if (v[0] == 0 && v[1] == 0 && v[2] == 0 && v[4] == 0 && v[5] == 0 && v[6] == 0) {
    if (imuIniciar(m)) enviar("L,IMU %c estava dormindo (reiniciou); acordado de novo", m.id);
    return;
  }
  enviar("I,%llu,%c,%d,%d,%d,%d,%d,%d", t, m.id, v[0], v[1], v[2], v[4], v[5], v[6]);
}

// ======================= Sensores IR (interrupção) =======================
Ir irs[5];
portMUX_TYPE muxIr = portMUX_INITIALIZER_UNLOCKED;

static void IRAM_ATTR isrIr(void* arg) {
  Ir* s = (Ir*)arg;
  int64_t t = esp_timer_get_time();
  portENTER_CRITICAL_ISR(&muxIr);
  if (!s->emTransicao) { s->emTransicao = true; s->tPrimeira = t; }
  s->tUltima = t;
  portEXIT_CRITICAL_ISR(&muxIr);
}

static inline uint8_t nivelIr(int i) { return digitalRead(PINO_IR[i]) == LOW ? 1 : 0; }   // LOW = viu o joystick

static void servirIr(int64_t agora) {
  for (int i = 0; i < 5; i++) {
    bool pronto = false;
    int64_t t0 = 0;
    portENTER_CRITICAL(&muxIr);
    if (irs[i].emTransicao && agora - irs[i].tUltima >= DEBOUNCE_IR_US) {
      irs[i].emTransicao = false;
      t0 = irs[i].tPrimeira;
      pronto = true;
    }
    portEXIT_CRITICAL(&muxIr);
    if (!pronto) continue;
    uint8_t n = nivelIr(i);
    if (n != irs[i].reportado) {          // trepidação que voltou ao mesmo nível não conta
      irs[i].reportado = n;
      enviar("B,%llu,%d,%d", (unsigned long long)t0, i, n);
    }
  }
}

static int mascaraIr() {
  int m = 0;
  for (int i = 0; i < 5; i++) m |= irs[i].reportado << i;
  return m;
}

// ======================= VL53L0X =======================
VL53L0X vl53;
bool temVl53 = false;
int ultimaDist = -1;

static int64_t proxVl53 = 0;

static void servirVl53() {
  if (!temVl53) return;
  int64_t agora = esp_timer_get_time();
  if (agora < proxVl53) return;          // pergunta a cada 5 ms (não ocupa o I2C do IMU do carro à toa)
  proxVl53 = agora + 5000;
  if ((vl53.readReg(VL53L0X::RESULT_INTERRUPT_STATUS) & 0x07) == 0) return;   // medida ainda não pronta
  unsigned long long t = agoraUs() - 10000;      // meio da janela de medida de 20 ms
  int mm = vl53.readRangeContinuousMillimeters();
  int estado = vl53.timeoutOccurred() ? 2 : (mm >= 8190 ? 1 : 0);
  ultimaDist = estado == 0 ? mm : -1;
  enviar("D,%llu,%d,%d", t, mm, estado);
}

// ======================= Status e comandos =======================
bool resumo = false;

static void enviarStatus() {
  enviar("S,%llu,%d,%d,%d,%d,%d,%c,%d,%lu", agoraUs(), VERSAO_FIRMWARE, temVl53,
         imus[0].ok, imus[1].ok, imus[2].ok, ativo, mascaraIr(), (unsigned long)descartadas);
}

static void mostrarResumo() {
  Serial.printf("# IMU A(janela)=%d B(espiral)=%d C(carro)=%d | ativo=%c | VL53=%d dist=%d mm | IR(A,B,S1,S2,S3)=%d%d%d%d%d | descartadas=%lu\n",
                imus[0].ok, imus[1].ok, imus[2].ok, ativo, temVl53, ultimaDist,
                irs[0].reportado, irs[1].reportado, irs[2].reportado, irs[3].reportado, irs[4].reportado,
                (unsigned long)descartadas);
}

static void ajuda() {
  Serial.println("# Comandos: ?  v (resumo a cada 1 s)  status  i2c  regs (diagnóstico dos IMUs)  ativo A|B|C|0");
  Serial.println("# Linhas: S status, I IMU bruto, B sensor IR, D distancia, L mensagem");
}

static char cmd[40];
static uint8_t nCmd = 0;

static void tratarComando(char* c) {
  for (char* p = c; *p; p++) *p = toupper(*p);
  if (!strcmp(c, "?") || !strcmp(c, "AJUDA")) ajuda();
  else if (!strcmp(c, "V")) { resumo = !resumo; Serial.println(resumo ? "# resumo LIGADO" : "# resumo DESLIGADO"); }
  else if (!strcmp(c, "STATUS")) enviarStatus();
  else if (!strcmp(c, "REGS")) {
    // diagnóstico: registros de energia/escala e uma leitura bruta de cada IMU
    for (int i = 0; i < 3; i++) {
      Imu& m = imus[i];
      if (!m.ok) { Serial.printf("# IMU %c: ausente\n", m.id); continue; }
      int16_t v[7] = {0};
      bool lida = imuLer(m, v);
      Serial.printf("# IMU %c (%s) end 0x%02X: PWR1=0x%02X PWR2=0x%02X CFG=0x%02X GCFG=0x%02X ACFG=0x%02X ACFG2=0x%02X | acel %d %d %d  giro %d %d %d%s\n",
                    m.id, nomeImu(m), m.end, imuLeRegistro(m, 0x6B), imuLeRegistro(m, 0x6C), imuLeRegistro(m, 0x1A),
                    imuLeRegistro(m, 0x1B), imuLeRegistro(m, 0x1C), imuLeRegistro(m, 0x1D),
                    v[0], v[1], v[2], v[4], v[5], v[6], lida ? "" : "  (leitura falhou)");
    }
  }
  else if (!strcmp(c, "I2C")) {
    varrerI2c(Wire, "D21/D22 (carro)"); varrerI2c(Wire1, "D18/D19 (janela 0x69, espiral 0x68)");
    Serial.printf("# modelos: A(janela)=%s  B(espiral)=%s  C(carro)=%s\n", nomeImu(imus[0]), nomeImu(imus[1]), nomeImu(imus[2]));
  }
  else if (!strncmp(c, "ATIVO", 5)) {
    char* p = c + 5;
    while (*p == ' ' || *p == ',') p++;
    char a = *p;
    if (a == 'A' || a == 'B' || a == 'C' || a == '0') {
      ativo = a;
      for (int i = 0; i < 3; i++) imus[i].proxima = 0;
      enviarStatus();
    }
  }
}

static void lerComandos() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') { cmd[nCmd] = 0; if (nCmd) tratarComando(cmd); nCmd = 0; }
    else if (nCmd < sizeof(cmd) - 1) cmd[nCmd++] = c;
  }
}

// lista os endereços que respondem em cada barramento I2C (diagnóstico de fiação)
static void varrerI2c(TwoWire& bus, const char* nome) {
  char txt[120];
  int n = snprintf(txt, sizeof(txt), "I2C %s:", nome);
  int achados = 0;
  for (uint8_t a = 1; a < 127 && n < (int)sizeof(txt) - 6; a++) {
    bus.beginTransmission(a);
    if (bus.endTransmission() == 0) { n += snprintf(txt + n, sizeof(txt) - n, " 0x%02X", a); achados++; }
  }
  if (!achados) snprintf(txt + n, sizeof(txt) - n, " nada respondeu");
  enviar("L,%s", txt);
  Serial.printf("# %s\n", txt);
}

// ======================= setup / loop =======================
void setup() {
  Serial.setTxBufferSize(8192);     // as linhas saem sem segurar o laço
  Serial.begin(BAUD);
  delay(300);

  for (int i = 0; i < 5; i++) {
    pinMode(PINO_IR[i], INPUT_PULLUP);
    irs[i].emTransicao = false;
    irs[i].reportado = nivelIr(i);
    attachInterruptArg(digitalPinToInterrupt(PINO_IR[i]), isrIr, &irs[i], CHANGE);
  }

  // Canal 1 (D21/D22): VL53L0X + IMU do carro   |   Canal 2 (D18/D19): IMU da espiral + IMU da janela
  Wire.begin(21, 22, I2C_HZ);
  Wire.setTimeOut(10);
  Wire1.begin(18, 19, I2C_HZ);
  Wire1.setTimeOut(10);
  for (int i = 0; i < 3; i++) imuIniciar(imus[i]);

  vl53.setBus(&Wire);
  vl53.setTimeout(100);
  temVl53 = vl53.init();
  if (temVl53) { vl53.setMeasurementTimingBudget(20000); vl53.startContinuous(); }   // ~50 medidas/s

  Serial.printf("# TRILHO caixa, firmware %d. Digite ? para ajuda.\n", VERSAO_FIRMWARE);
  enviar("L,caixa iniciada; fw %d; VL53L0X %s; IMU A %s, B %s, C %s", VERSAO_FIRMWARE,
         temVl53 ? "ok" : "ausente",
         nomeImu(imus[0]), nomeImu(imus[1]), nomeImu(imus[2]));
  // esperado: D21/D22 -> 0x29 (VL53L0X) e 0x68 (IMU carro); D18/D19 -> 0x69 (IMU janela) e 0x68 (IMU espiral)
  varrerI2c(Wire, "D21/D22 (carro)");
  varrerI2c(Wire1, "D18/D19 (janela 0x69, espiral 0x68)");
  enviarStatus();
}

static int64_t proxStatus = 0, proxReiniciar = 0, proxResumo = 0;

void loop() {
  lerComandos();
  int64_t agora = esp_timer_get_time();

  for (int i = 0; i < 3; i++) servirImu(imus[i], agora);
  servirIr(agora);
  servirVl53();

  if (agora >= proxStatus) { proxStatus = agora + PERIODO_STATUS_US; enviarStatus(); }
  if (agora >= proxReiniciar) {          // cabo de IMU reconectado volta sozinho
    proxReiniciar = agora + PERIODO_REINICIAR_IMU_US;
    for (int i = 0; i < 3; i++)
      if (!imus[i].ok && imuIniciar(imus[i])) enviar("L,IMU %c voltou a responder (%s)", imus[i].id, nomeImu(imus[i]));
  }
  if (resumo && agora >= proxResumo) { proxResumo = agora + 1000000; mostrarResumo(); }
}
