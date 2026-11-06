// Testes da lógica do firmware no PC (sem hardware).
// Uso: g++ -std=c++17 -I../comum testes.cpp -o testes && ./testes [arquivo_quadros.bin]
// Com um argumento, grava quadros de exemplo para o teste cruzado com o Python.
#include <cstdio>
#include <fstream>
#include <string>

#include "logica.hpp"
#include "protocolo.hpp"

using namespace trilho;

static int falhas = 0;
#define CHECAR(cond)                                                     \
    do {                                                                 \
        if (!(cond)) {                                                   \
            std::printf("FALHOU %s:%d  %s\n", __FILE__, __LINE__, #cond); \
            ++falhas;                                                    \
        }                                                                \
    } while (0)

static void testeCrc() {
    const char* s = "123456789";
    CHECAR(crc16(reinterpret_cast<const uint8_t*>(s), 9) == 0x29B1);
}

static void testeCobs() {
    std::vector<std::vector<uint8_t>> casos = {{}, {0}, {0, 0}, {1, 2, 0, 3}};
    std::vector<uint8_t> longo;
    for (int i = 0; i < 700; ++i) longo.push_back(static_cast<uint8_t>(i % 7 == 0 ? 0 : i));
    casos.push_back(longo);
    casos.push_back(std::vector<uint8_t>(600, 0xFF));
    for (auto& c : casos) {
        auto e = cobsCodificar(c.data(), c.size());
        for (auto b : e) CHECAR(b != 0);
        auto d = cobsDecodificar(e.data(), e.size());
        CHECAR(d && *d == c);
    }
}

static void testeQuadro() {
    EventoIr ev{3, 123456789ULL, 2, 1};
    auto q = montarQuadro(Tipo::Ir, 42, &ev, sizeof ev);
    CHECAR(q.back() == 0);
    auto lido = lerQuadro(q.data(), q.size() - 1);
    CHECAR(lido && lido->cab.tipo == static_cast<uint8_t>(Tipo::Ir) && lido->cab.seq == 42);
    EventoIr volta;
    std::memcpy(&volta, lido->payload.data(), sizeof volta);
    CHECAR(volta.t_us == 123456789ULL && volta.sensor == 2 && volta.estado == 1);
    q[4] ^= 0x10;   // corrompe um byte
    CHECAR(!lerQuadro(q.data(), q.size() - 1));
    CHECAR(kAmostrasImuPorQuadro == 10);
}

static void testeTrepidacao() {
    FiltroTrepidacao f(0, 5000);
    uint64_t t;
    uint8_t e;
    // troca real em 1.000 s com trepidação de 1 ms
    f.borda(1000000, 1);
    f.borda(1001000, 0);
    f.borda(1002000, 1);
    CHECAR(!f.verificar(1004000, t, e));
    CHECAR(f.verificar(1008000, t, e) && t == 1000000 && e == 1);
    // pulso espúrio de 3 ms é ignorado
    f.borda(1500000, 0);
    f.borda(1503000, 1);
    CHECAR(!f.verificar(1510000, t, e));
    CHECAR(f.estavel() == 1);
}

static void testeMaquina() {
    MaquinaSessao m;
    m.carregadorConectado(true);
    CHECAR(!m.comando(Comando::Iniciar));          // bloqueado carregando
    m.carregadorConectado(false);
    CHECAR(m.comando(Comando::Iniciar) && m.estado() == Estado::Sincronizando);
    m.sincronizado();
    CHECAR(m.comando(Comando::Tarar));
    CHECAR(m.comando(Comando::Iniciar) && m.estado() == Estado::Gravando);
    CHECAR(!m.comando(Comando::Tarar));            // não tara gravando
    CHECAR(m.comando(Comando::Parar) && m.estado() == Estado::Enviando);
    m.confirmado();
    CHECAR(m.estado() == Estado::Ocioso);
}

static void testeSincronia() {
    // bancada adiantada 5 ms; ida 1,0 ms, volta 1,4 ms, bancada leva 0,15 ms
    const uint64_t t1 = 10'000'000, t2 = t1 + 5000 + 1000, t3 = t2 + 150, t4 = t3 - 5000 + 1400;
    auto r = calcularTroca(t1, t2, t3, t4);
    CHECAR(r.atraso_us == 2400);
    CHECAR(r.diferenca_us == 5000 - 200);          // assimetria vira erro de metade dela
}

static void gravarExemplo(const std::string& arq) {
    std::ofstream out(arq, std::ios::binary);
    uint16_t seq = 0;
    AmostraImu bloco[kAmostrasImuPorQuadro];
    for (uint32_t i = 0; i < kAmostrasImuPorQuadro; ++i)
        bloco[i] = {i, 1'000'000ULL + i * 2000, int16_t(100 + i), -200, 16384, 1, -2, 3};
    auto q = montarQuadro(Tipo::Imu, seq++, bloco, sizeof bloco);
    out.write(reinterpret_cast<const char*>(q.data()), q.size());
    EventoIr ev{0, 2'500'000ULL, 3, 1};
    q = montarQuadro(Tipo::Ir, seq++, &ev, sizeof ev);
    out.write(reinterpret_cast<const char*>(q.data()), q.size());
    TrocaSinc s{1, 2, 3, 4};
    q = montarQuadro(Tipo::Sinc, seq++, &s, sizeof s);
    out.write(reinterpret_cast<const char*>(q.data()), q.size());
}

int main(int argc, char** argv) {
    testeCrc();
    testeCobs();
    testeQuadro();
    testeTrepidacao();
    testeMaquina();
    testeSincronia();
    if (argc > 1) gravarExemplo(argv[1]);
    std::printf(falhas ? "%d falha(s)\n" : "todos os testes do firmware passaram\n", falhas);
    return falhas ? 1 : 0;
}
