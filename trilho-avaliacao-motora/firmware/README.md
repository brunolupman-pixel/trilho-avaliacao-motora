# Firmware (ESP32-S3, C++17)

Esta pasta tem a **lógica do firmware separada do acesso ao hardware**, como
pede a arquitetura ("separar a lógica — parsers, máquina de estados — do acesso
ao hardware"). Ela compila no PC e é testada a cada mudança; no ESP-IDF entra
como biblioteca de cabeçalhos.

| Arquivo | Conteúdo |
|---|---|
| `comum/protocolo.hpp` | estruturas binárias (IMU, carga, IR, ToF, sincronia), CRC-16/CCITT, COBS, montagem e leitura de quadros — mesmo formato de `src/trilho/protocolo.py` |
| `comum/logica.hpp` | filtro contra trepidação dos LM393 (5 ms), máquina de estados da sessão (ocioso → sincronizando → pronto → gravando → enviando → ocioso, bloqueio com carregador), cálculo da troca de sincronia estilo NTP |
| `testes/testes.cpp` | testes no PC; com um argumento grava quadros de exemplo que o Python lê (teste cruzado em `tests/test_firmware.py`) |

```bash
g++ -std=c++17 -Wall -Wextra -Icomum testes/testes.cpp -o testes && ./testes
```

## Onde isto entra no ESP-IDF (ainda não implementado)

Falta a camada que depende do hardware, que só faz sentido escrever e testar
com as placas na bancada (Fase 0):

**Joystick** (FreeRTOS, dois núcleos)
- Núcleo 1: tarefa de aquisição — lê o FIFO do MPU6050 por I²C a 400 kHz
  (500 Hz, DLPF ~20–44 Hz), estima o instante de cada amostra a partir do
  momento da leitura do bloco e da taxa nominal; no joystick C, lê o HX711 no
  pino de dado pronto (80 Hz). Carimbo com `esp_timer_get_time()` + `seq`.
- Núcleo 0: ESP-NOW — envia blocos de `kAmostrasImuPorQuadro` amostras,
  responde às trocas de sincronia a cada ~100 ms, guarda a partida na PSRAM
  até o computador confirmar (`Tipo::Confirma`).
- `MaquinaSessao` + `carregadorConectado()` ligado ao pino de detecção do carregador.

**Bancada**
- Cinco entradas com interrupção na borda (LM393) → `FiltroTrepidacao::borda()`;
  uma tarefa chama `verificar()` a cada 1 ms e publica `EventoIr`.
- VL53L0X em modo contínuo (orçamento de 20–33 ms) → `LeituraTof`.
- Relógio mestre da sessão; repassa tudo ao computador por USB (isolador
  galvânico) com `montarQuadro()`; recebe comandos (iniciar, parar, tarar, calibrar).
