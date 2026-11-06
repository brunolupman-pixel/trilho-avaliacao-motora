# Validação da fusão com sinais sintéticos

Gerado por `python -m trilho validar --repeticoes 3 --comparar-imu` (detalhes por
partida em `validacao.csv`). Cada linha é a mediana de 3 partidas simuladas
(4 tentativas nos caminhos A e C; 1 tentativa borda → centro → borda na espiral),
com movimentos de jerk mínimo compostos de submovimentos, pausas e retrocessos.
Erros RMS contra a verdade do simulador, ao longo de toda a partida.

| IMU | Caminho | Vel. média | Posição RTS (mediana / pior) | Posição tempo real | Velocidade | Aceleração | Jerk (pico real) | Sincronia | Religamentos |
|---|---|---|---|---|---|---|---|---|---|
| MPU6050 | A guarda-chuva | 60 mm/s | 0.14 / 0.14 mm | 0.78 mm | 0.4 mm/s | 0.006 m/s² | 0.30 (14.8) m/s³ | 135 µs | 0 |
| MPU6050 | A guarda-chuva | 30 mm/s | 0.44 / 2.02 mm | 2.25 mm | 0.9 mm/s | 0.007 m/s² | 0.29 (5.4) m/s³ | 150 µs | 3 |
| MPU6050 | B espiral | 60 mm/s | 0.85 / 14.55 mm | 5.71 mm | 0.9 mm/s | 0.006 m/s² | 0.29 (0.3) m/s³ | 128 µs | 1 |
| MPU6050 | B espiral | 30 mm/s | 41.77 / 53.55 mm | 41.92 mm | 17.9 mm/s | 0.041 m/s² | 0.70 (0.0) m/s³ | 129 µs | 11 |
| MPU6050 | C retilíneo | 60 mm/s | 0.15 / 0.26 mm | 0.41 mm | 0.4 mm/s | 0.006 m/s² | 0.41 (10.4) m/s³ | 136 µs | 0 |
| MPU6050 | C retilíneo | 30 mm/s | 0.22 / 0.84 mm | 0.51 mm | 0.5 mm/s | 0.006 m/s² | 0.28 (10.4) m/s³ | 119 µs | 0 |
| ICM-42688-P | A guarda-chuva | 60 mm/s | 0.04 / 0.53 mm | 0.15 mm | 0.1 mm/s | 0.004 m/s² | 0.13 (14.8) m/s³ | 135 µs | 1 |
| ICM-42688-P | A guarda-chuva | 30 mm/s | 0.12 / 1.74 mm | 0.66 mm | 0.2 mm/s | 0.001 m/s² | 0.06 (5.4) m/s³ | 150 µs | 1 |
| ICM-42688-P | B espiral | 60 mm/s | 0.24 / 0.55 mm | 1.19 mm | 0.2 mm/s | 0.001 m/s² | 0.05 (0.3) m/s³ | 128 µs | 0 |
| ICM-42688-P | B espiral | 30 mm/s | 28.12 / 35.02 mm | 26.34 mm | 10.6 mm/s | 0.012 m/s² | 0.14 (0.0) m/s³ | 129 µs | 5 |
| ICM-42688-P | C retilíneo | 60 mm/s | 0.15 / 0.18 mm | 0.36 mm | 0.3 mm/s | 0.003 m/s² | 0.28 (10.4) m/s³ | 136 µs | 0 |
| ICM-42688-P | C retilíneo | 30 mm/s | 0.18 / 0.55 mm | 0.35 mm | 0.3 mm/s | 0.002 m/s² | 0.05 (10.4) m/s³ | 119 µs | 0 |

"Posição RTS" é a saída suavizada usada nas métricas; "tempo real" é o filtro
que alimenta o jogo. ICM-42688-P = mesma simulação com ruído de 0,005 m/s² e
viés residual menor.

## Leitura

* **Sincronia:** ~0,13 ms RMS em todas as partidas, bem abaixo da meta de 1 ms.
* **Caminho C (VL53L0X):** submilimétrico em todos os casos, como o documento previa
  ("deve ser o caminho de melhor precisão").
* **Caminho A (2 LM393 + batentes):** submilimétrico na mediana; o pior caso a
  30 mm/s fica em 2 mm.
* **Espiral a 60 mm/s:** < 1 mm na mediana. O pior caso (14,5 mm) é um único
  movimento perfeitamente suave de ~25 s.
* **Espiral a 30 mm/s, movimento único e suave (~47 s por sentido):** 30–50 mm.
  A aceleração de pico desse movimento é ~0,004 m/s², abaixo do ruído do MPU6050
  e da ordem dos erros sistemáticos (viés, inclinação), então a IMU não
  distingue "devagar e constante" de "parado". Entre divisas (176 mm) a posição
  depende quase só dos LM393. É o risco "Nível do sinal em movimentos lentos"
  do documento. A IMU de baixo ruído melhora, mas não resolve.
* **Jerk:** o erro RMS do jerk (0,3 m/s³ com o MPU6050) é pequeno perto dos
  picos dos movimentos rápidos, mas maior que o próprio sinal nos movimentos
  lentos da espiral. Isso confirma o SPARC como métrica principal de suavidade.

## Próximos passos sugeridos

1. Medir na bancada real o ruído e o viés de cada MPU6050 e a velocidade típica dos
   pacientes na espiral (a simulação a 30 mm/s com um único movimento suave é um
   caso extremo).
2. Se os movimentos lentos forem a regra, há três opções: mais divisas na
   espiral (um 4º LM393 dobra as correções), um encoder ou sensor contínuo
   também na espiral, ou o ICM-42688-P.
3. Repetir esta tabela com a referência de laboratório (câmera rápida ou
   encoder), como pede a seção de calibração e validação.
