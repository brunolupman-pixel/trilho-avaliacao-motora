// TrilhoProtocolo.h — protocolo comum entre joystick, bancada e computador.
//
// Versão 1. Tudo little-endian (o ESP32 é little-endian) e sem preenchimento
// automático (structs empacotadas). A especificação completa está em
// docs/PROTOCOLO.md; o servidor Python (servidor/trilho/protocolo.py) espelha
// exatamente estas estruturas.
//
// Dois enlaces:
//   1. Joystick <-> bancada: pacotes ESP-NOW (até 250 bytes), cabeçalho JoyCab.
//   2. Bancada <-> computador: USB serial, quadros COBS terminados em 0x00,
//      conteúdo = [tipo u8][dados...][CRC-16/CCITT-FALSE u16 LE].
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

namespace trilho {

constexpr uint8_t VERSAO_PROTOCOLO = 1;
constexpr uint16_t VERSAO_FIRMWARE = 100;  // 1.0.0

// ---------------------------------------------------------------------------
// Enlace 1: ESP-NOW (joystick <-> bancada)
// ---------------------------------------------------------------------------
enum TipoJoy : uint8_t {
  // joystick -> bancada (pacotes de dados: numerados, guardados até o ACK)
  J_IMU    = 0x01,
  J_FORCA  = 0x02,
  J_SYNC   = 0x03,
  J_STATUS = 0x04,
  // joystick -> bancada (controle: não numerados, não repassados ao PC)
  J_PING   = 0x05,
  // bancada -> joystick
  J_PONG   = 0x06,
  J_CMD    = 0x07,
};

#pragma pack(push, 1)
struct JoyCab {           // 8 bytes
  uint8_t tipo;           // TipoJoy
  uint8_t versao;         // VERSAO_PROTOCOLO
  uint8_t joy_id;         // 'A', 'B' ou 'C'
  uint8_t n;              // número de amostras (IMU/FORCA) ou 1
  uint32_t seq;           // pacotes de dados: sequência global do joystick; PING: id do ping
};

struct AmostraImu {       // 16 bytes — valores brutos do MPU6050
  uint32_t t_us;          // relógio do joystick (esp_timer, 32 bits baixos)
  int16_t ax, ay, az;     // ±4 g  -> 8192 LSB/g
  int16_t gx, gy, gz;     // ±500 °/s -> 65.5 LSB/(°/s)
};

struct AmostraForca {     // 8 bytes — valor bruto do HX711 (24 bits com sinal)
  uint32_t t_us;
  int32_t bruto;
};

struct RegistroSync {     // 20 bytes — uma troca de ida e volta (estilo NTP)
  uint32_t t1;            // joystick envia PING      (relógio do joystick)
  uint32_t t2;            // bancada recebe PING      (relógio da bancada)
  uint32_t t3;            // bancada envia PONG       (relógio da bancada)
  uint32_t t4;            // joystick recebe PONG     (relógio do joystick)
  uint16_t ping_id;
  uint16_t reservado;
};

struct StatusJoy {        // 20 bytes
  uint32_t t_us;
  uint16_t bateria_mv;
  uint8_t carregando;     // 1 = carregador conectado (sessão bloqueada)
  uint8_t estado;         // EstadoJoy
  uint16_t overflow_fifo; // quantas vezes o FIFO do MPU6050 transbordou
  uint16_t versao_fw;
  uint16_t pacotes_pendentes; // pacotes guardados aguardando ACK
  uint16_t pacotes_perdidos;  // pacotes descartados por falta de espaço
  uint8_t tem_forca;      // 1 = célula de carga presente
  uint8_t reservado[3];
};

struct Ping { uint32_t t1; };

struct Pong {             // 16 bytes
  uint16_t ping_id;
  uint16_t reservado;
  uint32_t t1, t2, t3;
};

struct ComandoJoy {       // 12 bytes
  uint8_t cmd;            // CmdJoy
  uint8_t reservado[3];
  uint32_t a, b;
};
#pragma pack(pop)

enum EstadoJoy : uint8_t { E_OCIOSO = 0, E_PRONTO = 1, E_GRAVANDO = 2, E_CARREGANDO = 3 };
enum CmdJoy : uint8_t { C_INICIAR = 1, C_PARAR = 2, C_ACK_ATE = 3, C_REENVIAR = 4 };

constexpr size_t MAX_ESPNOW = 250;
constexpr uint8_t MAX_IMU_POR_PACOTE = (MAX_ESPNOW - sizeof(JoyCab)) / sizeof(AmostraImu);  // 15
// 7 amostras a 500 Hz = 14 ms por pacote: mantém a latência do jogo baixa
// (meta < 50 ms do movimento à tela) ao custo de ~71 pacotes/s.
constexpr uint8_t IMU_POR_PACOTE = 7;    // 7 * 16 + 8 = 120 bytes
constexpr uint8_t FORCA_POR_PACOTE = 10; // 10 * 8 + 8 = 88 bytes
static_assert(IMU_POR_PACOTE <= MAX_IMU_POR_PACOTE, "pacote IMU grande demais");
static_assert(sizeof(JoyCab) == 8, "JoyCab");
static_assert(sizeof(AmostraImu) == 16, "AmostraImu");
static_assert(sizeof(AmostraForca) == 8, "AmostraForca");
static_assert(sizeof(RegistroSync) == 20, "RegistroSync");
static_assert(sizeof(StatusJoy) == 20, "StatusJoy");
static_assert(sizeof(Pong) == 16, "Pong");
static_assert(sizeof(ComandoJoy) == 12, "ComandoJoy");

// ---------------------------------------------------------------------------
// Enlace 2: USB serial (bancada <-> computador)
// ---------------------------------------------------------------------------
enum TipoQuadro : uint8_t {
  // bancada -> PC
  Q_PACOTE_JOY   = 0x10,  // CabPacoteJoy + pacote ESP-NOW original
  Q_BORDA        = 0x11,  // QuadroBorda
  Q_VL53         = 0x12,  // QuadroVl53
  Q_STATUS_BANC  = 0x13,  // QuadroStatusBancada + n x InfoJoy
  Q_LOG          = 0x14,  // texto UTF-8
  // PC -> bancada
  Q_CMD_JOY      = 0x20,  // QuadroCmdJoy (repassado ao joystick por ESP-NOW)
  Q_PEDIR_STATUS = 0x21,  // sem dados
};

#pragma pack(push, 1)
struct CabPacoteJoy {     // 8 bytes
  uint32_t t_rx;          // relógio da bancada quando o pacote chegou
  int8_t rssi;
  uint8_t reservado[3];
};

struct QuadroBorda {      // 8 bytes — troca estável de um LM393
  uint32_t t_us;          // instante da primeira borda da troca (relógio da bancada)
  uint8_t sensor;         // 0..4 (ver docs/PROTOCOLO.md)
  uint8_t nivel;          // 1 = detectando o joystick, 0 = livre
  uint16_t reservado;
};

struct QuadroVl53 {       // 8 bytes
  uint32_t t_us;
  uint16_t dist_mm;
  uint8_t estado;         // 0 = válido, 1 = fora do alcance, 2 = erro
  uint8_t reservado;
};

struct QuadroStatusBancada {  // 12 bytes
  uint32_t t_us;
  uint16_t versao_fw;
  uint16_t fila_perdida;  // itens perdidos por fila cheia
  uint8_t n_joy;          // quantos InfoJoy vêm a seguir
  uint8_t vl53_ok;        // 1 = VL53L0X respondeu na inicialização
  uint8_t reservado[2];
};

struct InfoJoy {          // 4 bytes
  uint8_t joy_id;
  int8_t rssi;
  uint16_t ms_desde_ultimo;
};

struct QuadroCmdJoy {     // 12 bytes
  uint8_t joy_id;
  uint8_t cmd;            // CmdJoy
  uint16_t reservado;
  uint32_t a, b;
};
#pragma pack(pop)

static_assert(sizeof(CabPacoteJoy) == 8, "CabPacoteJoy");
static_assert(sizeof(QuadroBorda) == 8, "QuadroBorda");
static_assert(sizeof(QuadroVl53) == 8, "QuadroVl53");
static_assert(sizeof(QuadroStatusBancada) == 12, "QuadroStatusBancada");
static_assert(sizeof(InfoJoy) == 4, "InfoJoy");
static_assert(sizeof(QuadroCmdJoy) == 12, "QuadroCmdJoy");

constexpr size_t MAX_QUADRO = 300;  // conteúdo antes do COBS (tipo + dados + CRC)

// CRC-16/CCITT-FALSE (polinômio 0x1021, valor inicial 0xFFFF).
inline uint16_t crc16(const uint8_t* d, size_t n) {
  uint16_t c = 0xFFFF;
  for (size_t i = 0; i < n; i++) {
    c ^= (uint16_t)d[i] << 8;
    for (int b = 0; b < 8; b++) c = (c & 0x8000) ? (uint16_t)((c << 1) ^ 0x1021) : (uint16_t)(c << 1);
  }
  return c;
}

// COBS: codifica n bytes de entrada; devolve o tamanho escrito em `saida`
// (sem o 0x00 final). `saida` precisa de n + n/254 + 1 bytes.
inline size_t cobs_codificar(const uint8_t* entrada, size_t n, uint8_t* saida) {
  size_t ler = 0, escr = 1, pos_cod = 0;
  uint8_t cod = 1;
  while (ler < n) {
    if (entrada[ler] == 0) {
      saida[pos_cod] = cod; pos_cod = escr++; cod = 1; ler++;
    } else {
      saida[escr++] = entrada[ler++]; cod++;
      if (cod == 0xFF) { saida[pos_cod] = cod; pos_cod = escr++; cod = 1; }
    }
  }
  saida[pos_cod] = cod;
  return escr;
}

// COBS: decodifica; devolve o tamanho decodificado ou 0 se inválido.
inline size_t cobs_decodificar(const uint8_t* entrada, size_t n, uint8_t* saida) {
  size_t ler = 0, escr = 0;
  while (ler < n) {
    uint8_t cod = entrada[ler];
    if (cod == 0 || ler + cod > n + 1) return 0;
    ler++;
    for (uint8_t i = 1; i < cod; i++) {
      if (ler >= n) return 0;
      saida[escr++] = entrada[ler++];
    }
    if (cod != 0xFF && ler < n) saida[escr++] = 0;
  }
  return escr;
}

// Monta um quadro completo pronto para a serial: COBS([tipo][dados][crc]) + 0x00.
// Devolve o número de bytes em `saida` (precisa de MAX_QUADRO + 4 bytes).
inline size_t montar_quadro(uint8_t tipo, const void* dados, size_t n, uint8_t* saida) {
  uint8_t bruto[MAX_QUADRO];
  if (n + 3 > MAX_QUADRO) return 0;
  bruto[0] = tipo;
  if (n) memcpy(bruto + 1, dados, n);
  uint16_t c = crc16(bruto, n + 1);
  bruto[n + 1] = (uint8_t)(c & 0xFF);
  bruto[n + 2] = (uint8_t)(c >> 8);
  size_t m = cobs_codificar(bruto, n + 3, saida);
  saida[m] = 0x00;
  return m + 1;
}

// Acumula bytes da serial e entrega quadros válidos (CRC conferido).
class LeitorQuadros {
 public:
  // Devolve true quando um quadro completo e válido foi recebido;
  // então tipo() / dados() / tamanho() descrevem o quadro.
  bool alimentar(uint8_t b) {
    if (b != 0x00) {
      if (n_ < sizeof(buf_)) buf_[n_++] = b; else erro_ = true;
      return false;
    }
    bool ok = false;
    if (!erro_ && n_ > 0) {
      size_t m = cobs_decodificar(buf_, n_, dec_);
      if (m >= 3) {
        uint16_t c = (uint16_t)dec_[m - 2] | ((uint16_t)dec_[m - 1] << 8);
        if (crc16(dec_, m - 2) == c) { tam_ = m - 3; ok = true; }
        else invalidos_++;
      } else {
        invalidos_++;
      }
    }
    n_ = 0; erro_ = false;
    return ok;
  }
  uint8_t tipo() const { return dec_[0]; }
  const uint8_t* dados() const { return dec_ + 1; }
  size_t tamanho() const { return tam_; }
  uint32_t invalidos() const { return invalidos_; }

 private:
  uint8_t buf_[MAX_QUADRO + 8];
  uint8_t dec_[MAX_QUADRO + 8];
  size_t n_ = 0, tam_ = 0;
  bool erro_ = false;
  uint32_t invalidos_ = 0;
};

}  // namespace trilho
