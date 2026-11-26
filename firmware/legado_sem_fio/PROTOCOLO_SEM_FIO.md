# Protocolo de comunicação — versão 1

Fonte única da verdade: `firmware/libraries/TrilhoProtocolo/src/TrilhoProtocolo.h`.
O servidor (`servidor/trilho/protocolo.py`) espelha as mesmas estruturas, e o teste
`testes/test_protocolo.py` compila o cabeçalho C++ e confere que os dois lados produzem
os mesmos bytes. Tudo é little-endian e sem preenchimento.

```
Joystick (bateria) --ESP-NOW--> Bancada (fonte, relógio mestre) --USB--> Computador (servidor Python) --WebSocket--> Navegador
```

## 1. Joystick ↔ bancada (ESP-NOW, até 250 bytes)

Cabeçalho `JoyCab` (8 bytes): `tipo u8 · versao u8 (=1) · joy_id u8 ('A','B','C') · n u8 · seq u32`.

| Tipo | Sentido | Conteúdo | Numerado/guardado? |
| --- | --- | --- | --- |
| 0x01 IMU | joy → banc | `n` × AmostraImu (16 B: `t_us u32`, ax ay az gx gy gz `i16`), n = 7 | sim |
| 0x02 FORCA | joy → banc | `n` × AmostraForca (8 B: `t_us u32`, `bruto i32`), n = 10 | sim |
| 0x03 SYNC | joy → banc | RegistroSync (20 B: t1 t2 t3 t4 `u32`, ping_id `u16`) | sim |
| 0x04 STATUS | joy → banc | StatusJoy (20 B: bateria, carregando, estado, overflows, versão, pendentes, perdidos, tem_forca) | sim |
| 0x05 PING | joy → banc | `t1 u32` (seq = id do ping) | não |
| 0x06 PONG | banc → joy | `ping_id u16`, t1 t2 t3 `u32` | não |
| 0x07 CMD | banc → joy | `cmd u8` + `a u32` + `b u32` | não |

Comandos: 1 INICIAR · 2 PARAR · 3 ACK_ATE(a = última sequência contígua recebida) · 4 REENVIAR(a..b).

Todos os relógios são `esp_timer_get_time()` em µs, 32 bits (dão a volta a cada ~71,6 min; o
servidor "desenrola"). O MPU6050 está em ±4 g (8192 LSB/g) e ±500 °/s (65,5 LSB/(°/s)), 500 Hz,
filtro interno ~20 Hz.

## 2. Bancada ↔ computador (USB serial, 921600 baud)

Quadro: `COBS( tipo u8 · dados · CRC-16/CCITT-FALSE u16 LE ) + 0x00`. O CRC cobre tipo + dados.
Quadros com CRC errado são descartados sem travar a leitura.

| Tipo | Sentido | Dados |
| --- | --- | --- |
| 0x10 PACOTE_JOY | banc → PC | `t_rx u32` (relógio da bancada), `rssi i8`, 3 B reservados, pacote ESP-NOW original |
| 0x11 BORDA | banc → PC | `t_us u32`, `sensor u8`, `nivel u8` (1 = detectando), 2 B reservados |
| 0x12 VL53 | banc → PC | `t_us u32` (meio da medida), `dist_mm u16`, `estado u8` (0 ok, 1 fora do alcance, 2 erro) |
| 0x13 STATUS_BANC | banc → PC | `t_us`, versão, itens perdidos na fila, `n_joy`, `vl53_ok` + n × (`id`, `rssi`, `ms_desde_ultimo`) |
| 0x14 LOG | banc → PC | texto UTF-8 (a bancada manda o próprio MAC ao ligar) |
| 0x20 CMD_JOY | PC → banc | `joy_id u8`, `cmd u8`, 2 B, `a u32`, `b u32` — repassado ao joystick |
| 0x21 PEDIR_STATUS | PC → banc | vazio |

## 3. Sensores LM393 (índices)

| Índice | Caminho | Papel |
| --- | --- | --- |
| 0 | Guarda-chuva | lado esquerdo do arco |
| 1 | Guarda-chuva | lado direito do arco |
| 2 | Espiral | S1 = bit 0 do código Gray |
| 3 | Espiral | S2 = bit 1 |
| 4 | Espiral | S3 = bit 2 |

A bancada só envia trocas **estáveis**: ignora pulsos menores que 5 ms e informa o instante da
primeira borda da troca que ficou estável.

## 4. Confiabilidade

- Todo pacote de dados do joystick tem sequência e fica guardado (PSRAM) até o `ACK_ATE`.
- O servidor manda `ACK_ATE` a cada 0,25 s e pede `REENVIAR` dos buracos com mais de 150 ms;
  desiste depois de 12 tentativas (conta como "pacote não recuperado" na qualidade da partida).
- Ao encerrar a partida, o servidor espera até 1,5 s pelos reenvios antes de processar.

## 5. Sincronia

A cada 100 ms: o joystick envia PING (t1); a bancada anota t2 ao receber, t3 ao responder;
o joystick anota t4 e manda o registro SYNC. O servidor ajusta a reta
`t_bancada = t_joy + a + b·(t_joy − t_ref)` com as trocas de menor atraso (tempo real: últimos 20 s;
pós-processamento: a partida inteira). No simulador, o erro verdadeiro ficou abaixo de 0,25 ms
(meta: ≤ 1 ms). No hardware, medir com o fio de referência (plano de testes da Fase 0).
