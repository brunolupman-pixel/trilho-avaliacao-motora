// Protocolo serial bancada <-> computador (mesmo formato de src/trilho/protocolo.py).
//
//   quadro = COBS( tipo:u8 | versao:u8 | seq:u16 | payload | crc16:u16 ) + 0x00
//
// Lógica pura, sem acesso a hardware: compila no ESP-IDF e no PC (testes).
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <optional>
#include <vector>

namespace trilho {

constexpr uint8_t kVersaoFormato = 1;

enum class Tipo : uint8_t {
    Imu = 0x01, Carga = 0x02, Ir = 0x03, Tof = 0x04, Sinc = 0x05,
    Estado = 0x10, Comando = 0x11, FimPartida = 0x12, Confirma = 0x13,
};

#pragma pack(push, 1)
struct AmostraImu { uint32_t seq; uint64_t t_us; int16_t ax, ay, az, gx, gy, gz; };
struct AmostraCarga { uint32_t seq; uint64_t t_us; int32_t bruto; };
struct EventoIr { uint32_t seq; uint64_t t_us; uint8_t sensor; uint8_t estado; };
struct LeituraTof { uint32_t seq; uint64_t t_us; uint16_t distancia_mm; uint8_t status; };
struct TrocaSinc { uint64_t t1, t2, t3, t4; };
struct Cabecalho { uint8_t tipo; uint8_t versao; uint16_t seq; };
#pragma pack(pop)

static_assert(sizeof(AmostraImu) == 24, "AmostraImu deve ter 24 bytes");
static_assert(sizeof(EventoIr) == 14, "EventoIr deve ter 14 bytes");
static_assert(sizeof(LeituraTof) == 15, "LeituraTof deve ter 15 bytes");

// Pacotes ESP-NOW têm até 250 bytes
constexpr size_t kAmostrasImuPorQuadro = (250 - sizeof(Cabecalho) - 2) / sizeof(AmostraImu);

// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF. crc16("123456789") = 0x29B1
inline uint16_t crc16(const uint8_t* d, size_t n, uint16_t crc = 0xFFFF) {
    for (size_t i = 0; i < n; ++i) {
        crc ^= static_cast<uint16_t>(d[i]) << 8;
        for (int b = 0; b < 8; ++b)
            crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                                 : static_cast<uint16_t>(crc << 1);
    }
    return crc;
}

inline std::vector<uint8_t> cobsCodificar(const uint8_t* d, size_t n) {
    std::vector<uint8_t> out;
    out.reserve(n + n / 254 + 2);
    size_t idxCodigo = 0;
    uint8_t codigo = 1;
    out.push_back(0);
    for (size_t i = 0; i < n; ++i) {
        if (d[i] == 0) {
            out[idxCodigo] = codigo;
            idxCodigo = out.size();
            out.push_back(0);
            codigo = 1;
        } else {
            out.push_back(d[i]);
            if (++codigo == 0xFF) {
                out[idxCodigo] = codigo;
                idxCodigo = out.size();
                out.push_back(0);
                codigo = 1;
            }
        }
    }
    out[idxCodigo] = codigo;
    return out;
}

inline std::optional<std::vector<uint8_t>> cobsDecodificar(const uint8_t* d, size_t n) {
    std::vector<uint8_t> out;
    size_t i = 0;
    while (i < n) {
        uint8_t codigo = d[i++];
        if (codigo == 0 || i + codigo - 1 > n) return std::nullopt;
        for (uint8_t k = 1; k < codigo; ++k) out.push_back(d[i++]);
        if (codigo < 0xFF && i < n) out.push_back(0);
    }
    return out;
}

// Monta um quadro completo (já com o delimitador 0x00 no fim)
inline std::vector<uint8_t> montarQuadro(Tipo tipo, uint16_t seq, const void* payload, size_t n) {
    std::vector<uint8_t> corpo(sizeof(Cabecalho) + n + 2);
    Cabecalho c{static_cast<uint8_t>(tipo), kVersaoFormato, seq};
    std::memcpy(corpo.data(), &c, sizeof c);
    if (n) std::memcpy(corpo.data() + sizeof c, payload, n);
    uint16_t crc = crc16(corpo.data(), sizeof c + n);
    std::memcpy(corpo.data() + sizeof c + n, &crc, 2);
    auto q = cobsCodificar(corpo.data(), corpo.size());
    q.push_back(0);
    return q;
}

struct Quadro {
    Cabecalho cab;
    std::vector<uint8_t> payload;
};

// Lê um quadro (sem o 0x00 final). Devolve vazio se o COBS ou o CRC falharem.
inline std::optional<Quadro> lerQuadro(const uint8_t* d, size_t n) {
    auto corpo = cobsDecodificar(d, n);
    if (!corpo || corpo->size() < sizeof(Cabecalho) + 2) return std::nullopt;
    const size_t m = corpo->size() - 2;
    uint16_t crc;
    std::memcpy(&crc, corpo->data() + m, 2);
    if (crc16(corpo->data(), m) != crc) return std::nullopt;
    Quadro q;
    std::memcpy(&q.cab, corpo->data(), sizeof q.cab);
    q.payload.assign(corpo->begin() + sizeof(Cabecalho), corpo->begin() + m);
    return q;
}

}  // namespace trilho
