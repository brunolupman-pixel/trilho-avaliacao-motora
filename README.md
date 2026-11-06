# Trilho — Sistema de Avaliação Motora com Joysticks em Trilho

O hardware ainda não existe, então tudo foi construído e validado contra um
**simulador fiel da bancada** (IMU com deriva de relógio, LM393 com histerese,
atraso e disparos espúrios, VL53L0X, HX711, rádio com atraso variável). Quando
o hardware chegar, só a fonte dos dados muda: o formato binário, a fusão, as
métricas, o banco, o relatório e os jogos são os mesmos.

## O que está implementado

| Seção do documento | Onde | Situação |
|---|---|---|
| Geometria dos caminhos A (guarda-chuva, 170 mm), B (espiral, 1410 mm), C (reto, 120 mm) | `geometria.py` | ✅ |
| Protocolo serial: quadros COBS + CRC, estruturas versionadas, ≤ 250 bytes (ESP-NOW) | `protocolo.py`, `firmware/comum/protocolo.hpp` | ✅ Python e C++ com teste cruzado |
| Arquivo de sessão em Parquet com checksum e versão do formato | `dados.py` | ✅ |
| Sincronia joystick ↔ bancada (trocas estilo NTP, reta diferença + deriva pós-sessão, resíduo como qualidade) | `sincronia.py` | ✅ erro ≈ 0,12 ms (meta ≤ 1 ms) |
| Tabela de transições por divisa e sentido, construída na calibração (posição + atraso do LM393), código Gray na espiral | `transicoes.py` | ✅ |
| Fusão: Kalman [s, v, a, j] ao longo do trilho + suavizador RTS, saída em tempo real e suavizada | `kalman.py`, `fusao.py` | ✅ (ver decisões) |
| Rejeição de disparos espúrios (3σ), conferência da sequência Gray, batentes, velocidade zero nos repousos | `fusao.py` | ✅ |
| Célula de carga: tara, curva de calibração (não só um fator), newtons | `fusao.py` | ✅ |
| Segmentação das tentativas e métricas (tempo de movimento e de reação, velocidade, aceleração, SPARC, LDLJ, submovimentos, pausas, retrocessos, preensão, qualidade) | `metricas.py` | ✅ |
| Banco local SQLite + Parquet; `autorizar()` único; identificação do paciente separada e criptografada; auditoria imutável; perfis e conta do paciente já criados | `armazenamento.py` | ✅ |
| Reprocessamento a partir dos brutos, guardando a versão do algoritmo | `pipeline.py`, `trilho reprocessar` | ✅ |
| Relatório PDF com evolução contra as sessões anteriores do próprio paciente | `relatorio.py` | ✅ |
| Três mini-games como módulos (carro, vidro, bolo), eventos no relógio da sessão, dificuldade do profissional, Esc/P | `jogos/` | ✅ pygame-ce |
| Lógica do firmware testável no PC: COBS/CRC, filtro de trepidação 5 ms, máquina de estados com bloqueio no carregador, cálculo da sincronia | `firmware/` | ✅ |
| Camada do firmware que fala com o hardware (I²C, interrupções, ESP-NOW, PSRAM) | — | ⏳ Fase 0, precisa das placas |
| Interface do profissional em PySide6 + pyqtgraph; processos separados com ZeroMQ; instalador PyInstaller | — | ⏳ hoje a interface é a CLI e o jogo roda no mesmo processo |
| Nuvem (FastAPI + PostgreSQL + S3), login | — | ⏳ Fase 3 (o modelo de dados já está preparado) |

## Mini-games

| Carro (C) | Vidro (A) | Bolo (B) |
|---|---|---|
| ![carro](trilho-avaliacao/docs/carro.png) | ![vidro](trilho-avaliacao/docs/vidro.png) | ![bolo](trilho-avaliacao/docs/bolo.png) |

## Como usar

```bash
pip install -e ".[jogos,dev]"
```

```bash
python -m trilho demo
```
Cria um banco em `dados_demo/`, cadastra um paciente, calibra a bancada,
simula 4 semanas de sessões nos três caminhos (o paciente melhora com o tempo),
processa tudo e gera um PDF por caminho.

```bash
python -m trilho jogar --jogo vidro --entrada simulada
```
Abre um mini-game alimentado pela fusão ao vivo de uma partida simulada
(`--entrada mouse` para jogar com o mouse; `carro`, `vidro` ou `bolo`).

```bash
python -m trilho validar --comparar-imu
```
Mede o erro da fusão contra a verdade do simulador, por caminho e velocidade, com o MPU6050 e com uma IMU de baixo ruído.

Outros comandos: `calibrar-ir`, `simular`, `processar`, `reprocessar` (`python -m trilho -h`).

```bash
python -m pytest
```
35 testes: protocolo, sincronia, calibração, fusão, métricas, banco, relatório, jogos e firmware C++.

## Decisões tomadas na implementação

Pontos em que o código precisou ir além do documento. Todos foram motivados por
falhas reais encontradas na validação com o simulador:

1. **Medida 2D da IMU (EKF), não só a projeção na tangente.** Projetar a
   aceleração só na tangente *estimada* realimenta o erro nas curvas fechadas:
   se a posição atrasa, a componente centrípeta (v²κ, até 0,3 m/s² no arco)
   vaza como desaceleração e o atraso cresce exponencialmente. A medida usa o
   vetor no plano, `f = (a + b)·T(s) + v²·K(s)`, com mais incerteza na direção normal.
2. **Dois estados a mais no filtro:** viés residual do acelerômetro (b) e erro
   de orientação no plano (ψ, deriva do giroscópio). Sem ψ, um erro de 0,05 rad
   vira 3 mm de erro de posição no arco.
3. **Divisa identificada pelo código Gray.** O código antes/depois da troca
   identifica a divisa sem depender da IMU. Fora da porta de 3σ, uma divisa
   identificada *religa* o filtro em vez de ser descartada (é o filtro que se
   perdeu); as ambíguas seguem a regra do documento (mais próxima da previsão).
4. **Pulsos espúrios** (liga + desliga em < 40 ms) são removidos antes da
   fusão; na validação, todos os disparos de luz ambiente simulados foram descartados.
5. **Repouso:** teste da média da janela (mais sensível que RMS em movimentos
   lentos) + CUSUM para detectar a saída lenta de um batente; velocidade zero
   mais fraca longe dos batentes; restrições físicas s ∈ [0, L] e "s dentro do
   trecho do código Gray atual".
6. **Divisas da espiral igualmente espaçadas em comprimento de arco**
   (176 mm, como na tabela do documento), o que as deixa mais próximas em
   ângulo nas voltas internas. Com as divisas concentradas no meio, os 320 mm
   finais ficavam sem correção.
7. SQLite pelo `sqlite3` da biblioteca padrão atrás da camada de repositórios
   (o documento cita SQLAlchemy; a troca fica isolada em `armazenamento.py`).

## Resultados da validação (simulador)

Resumo (detalhes em [`VALIDACAO.md`](VALIDACAO.md)), erro RMS de posição, MPU6050:

| Caminho | 60 mm/s | 30 mm/s |
|---|---|---|
| C retilíneo (VL53L0X) | 0,15 mm | 0,22 mm |
| A guarda-chuva | 0,14 mm | 0,44 mm |
| B espiral | 0,85 mm | **42 mm** ⚠️ |

Sincronia ≈ 0,13 ms (meta ≤ 1 ms). A espiral muito lenta e suave fica no piso
de ruído da IMU, exatamente o risco descrito no documento; ver as opções em
`VALIDACAO.md`.

## Estrutura

```
src/trilho/
  geometria.py   caminhos e sensores        kalman.py     filtro + RTS
  protocolo.py   COBS, CRC, quadros         fusao.py      motor de fusão (ao vivo e pós-sessão)
  dados.py       arquivo de sessão          metricas.py   segmentação e métricas
  sincronia.py   relógios                   armazenamento.py  SQLite, LGPD, auditoria
  transicoes.py  tabela Gray/divisas        relatorio.py  PDF
  simulador.py   bancada + paciente         pipeline.py   bruto → métricas → banco
  cli.py         linha de comando           jogos/        carro, vidro, bolo, executor
firmware/        lógica C++17 + testes no PC
tests/           pytest
```
