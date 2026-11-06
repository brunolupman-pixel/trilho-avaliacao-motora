// Lógica do firmware separada do acesso ao hardware (testável no PC):
//  - filtro contra trepidação dos LM393 (bancada)
//  - máquina de estados da sessão (joystick e bancada)
//  - cálculo da troca de sincronia estilo NTP (joystick)
#pragma once

#include <cstdint>

namespace trilho {

// ------------------------------------------------------------------ LM393
// Ignora pulsos menores que `minimo_us` e registra o instante da primeira troca
// da rajada que levou ao novo estado estável (mesma regra de simulador.debounce).
// Uso: chamar `borda()` na interrupção e `verificar()` periodicamente (ou na
// próxima borda); quando houver troca confirmada, `verificar` devolve true.
class FiltroTrepidacao {
public:
    explicit FiltroTrepidacao(uint8_t estadoInicial = 0, uint32_t minimo_us = 5000)
        : estavel_(estadoInicial), atual_(estadoInicial), minimo_(minimo_us) {}

    void borda(uint64_t t_us, uint8_t estado) {
        if (estado == atual_) return;
        if (estado != estavel_ && !emRajada_) {
            emRajada_ = true;
            inicioRajada_ = t_us;
        }
        atual_ = estado;
        ultimaBorda_ = t_us;
    }

    // Devolve true e preenche (t_us, estado) quando um novo estado ficou estável.
    bool verificar(uint64_t agora_us, uint64_t& t_evento, uint8_t& estado) {
        if (agora_us - ultimaBorda_ < minimo_) return false;
        if (atual_ == estavel_) {          // voltou ao estado anterior: era pulso
            emRajada_ = false;
            return false;
        }
        estavel_ = atual_;
        emRajada_ = false;
        t_evento = inicioRajada_;
        estado = estavel_;
        return true;
    }

    uint8_t estavel() const { return estavel_; }

private:
    uint8_t estavel_, atual_;
    uint32_t minimo_;
    bool emRajada_ = false;
    uint64_t inicioRajada_ = 0, ultimaBorda_ = 0;
};

// ------------------------------------------------------- máquina de sessão
enum class Estado : uint8_t { Ocioso, Sincronizando, Pronto, Gravando, Enviando };
enum class Comando : uint8_t { Iniciar = 1, Parar = 2, Tarar = 3, Calibrar = 4 };

class MaquinaSessao {
public:
    Estado estado() const { return estado_; }

    // Segurança elétrica: o joystick recusa iniciar sessão com carregador ligado.
    void carregadorConectado(bool c) { carregando_ = c; }

    bool comando(Comando c) {
        switch (c) {
            case Comando::Iniciar:
                if (carregando_) return false;
                if (estado_ == Estado::Ocioso) { estado_ = Estado::Sincronizando; return true; }
                if (estado_ == Estado::Pronto) { estado_ = Estado::Gravando; return true; }
                return false;
            case Comando::Parar:
                if (estado_ == Estado::Gravando) { estado_ = Estado::Enviando; return true; }
                if (estado_ == Estado::Sincronizando || estado_ == Estado::Pronto) {
                    estado_ = Estado::Ocioso;
                    return true;
                }
                return false;
            case Comando::Tarar:
            case Comando::Calibrar:
                return estado_ == Estado::Ocioso || estado_ == Estado::Pronto;
        }
        return false;
    }

    // Sincronia concluída (trocas suficientes com atraso baixo)
    void sincronizado() { if (estado_ == Estado::Sincronizando) estado_ = Estado::Pronto; }

    // O computador confirmou o recebimento da partida: a PSRAM pode ser liberada
    void confirmado() { if (estado_ == Estado::Enviando) estado_ = Estado::Ocioso; }

private:
    Estado estado_ = Estado::Ocioso;
    bool carregando_ = false;
};

// ------------------------------------------------------------- sincronia
struct ResultadoTroca {
    int64_t diferenca_us;   // relógio da bancada - relógio do joystick
    int64_t atraso_us;      // ida e volta, sem o tempo de processamento da bancada
};

inline ResultadoTroca calcularTroca(uint64_t t1, uint64_t t2, uint64_t t3, uint64_t t4) {
    const int64_t a = static_cast<int64_t>(t2) - static_cast<int64_t>(t1);
    const int64_t b = static_cast<int64_t>(t3) - static_cast<int64_t>(t4);
    return {(a + b) / 2, (static_cast<int64_t>(t4) - static_cast<int64_t>(t1)) -
                             (static_cast<int64_t>(t3) - static_cast<int64_t>(t2))};
}

}  // namespace trilho
