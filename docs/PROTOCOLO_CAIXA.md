# Protocolo da caixa (ESP32 ↔ programa Trilho)

USB serial, **921600 baud**, texto. Uma mensagem por linha.

## Formato

```
TIPO,campo,campo,...*XX
```

- `XX` = "ou exclusivo" (XOR) de todos os caracteres antes do `*`, em hexadecimal com 2 dígitos.
  O programa descarta linhas com a verificação errada (ruído no cabo, linha cortada).
- Linhas que começam com `#` são só para pessoas (Monitor Serial) e o programa ignora.
- `t_us`: microssegundos desde que a placa ligou (64 bits, não dá a volta). Todos os sensores
  usam esse mesmo relógio, então os horários podem ser comparados diretamente.

## Da caixa para o computador

| Linha | Quando | Campos |
| --- | --- | --- |
| `S,t_us,fw,vl53,imuA,imuB,imuC,ativo,ir,descartadas` | 2 vezes por segundo e ao receber `STATUS` | versão do firmware; 1/0 para VL53L0X e cada IMU respondendo (até o firmware 206 havia também o campo `hx`, da célula de carga, depois de `vl53`; o programa ainda entende); joystick ativo (`A`, `B`, `C` ou `0`); nível dos 5 sensores IR (bit 0 = ponta A ... bit 4 = S3); linhas descartadas porque a USB estava cheia |
| `I,t_us,joy,ax,ay,az,gx,gy,gz` | IMU do jogo ativo: 250/s; os outros: 10/s | `joy` = `A` (janela), `B` (espiral), `C` (carro). Valores brutos do IMU (MPU6050 nos joysticks A e C, MPU-9250 no B; mesmas escalas): aceleração ±4 g (8192 por g), giro ±500 °/s (65,5 por °/s) |
| `B,t_us,sensor,nivel` | quando um sensor IR muda e fica estável por 20 ms (5 ms até o firmware 204) | sensor 0 = ponta A, 1 = ponta B, 2 = S1, 3 = S2, 4 = S3; nível 1 = vendo o joystick. O horário é o da **primeira** troca (antes do filtro) |
| `D,t_us,mm,estado` | ~50/s | distância do VL53L0X; estado 0 = ok, 1 = fora do alcance, 2 = sem resposta. O horário é o meio da janela de medida |
| `L,texto` | ao ligar e em avisos | mensagem livre (ex.: "caixa iniciada; fw 206; ...; IMU A MPU6050, B MPU6500, C MPU6050", "IMU B parou de responder", "IMU A estava dormindo (reiniciou); acordado de novo") |

A partir do firmware 203, uma leitura do MPU6050 com aceleração e giro todos zerados (o IMU reiniciou por
mau contato e voltou "dormindo") não é enviada: a caixa acorda e configura o IMU de novo e manda uma linha `L`.

A partir do firmware 204, a caixa aceita também o MPU-9250 (placa "MPU-92.65") e o MPU6500. Ela lê o
WHO_AM_I (registrador 0x75) para saber o modelo: 0x68 MPU6050, 0x70 MPU6500, 0x71 MPU9250, 0x73 MPU9255.
Nos dois últimos tipos o filtro do acelerômetro é configurado à parte (registrador 0x1D, ~45 Hz), para a
leitura ficar igual à do MPU6050. O modelo aparece na linha `L` de início e no comando `i2c`. As linhas
`I` não mudam. O magnetômetro do MPU-9250 não é usado.

A partir do firmware 205, o filtro dos sensores IR é de 20 ms: uma troca só é enviada se o nível ficar
estável por 20 ms. Piscadas curtas (reflexo na parede do canal, mão, luz) não viram cruzamento. O horário
enviado continua sendo o da primeira troca. A 300 mm/s, o joystick fica mais de 30 ms na frente do feixe.

A partir do firmware 207, a célula de carga (HX711) foi retirada: não há mais linhas `F` nem o
campo `hx` na linha `S`.

A partir do firmware 206, o MPU6500/MPU-9250 é reiniciado por completo antes de ser configurado (algumas
placas ficavam com o giroscópio lendo 0). O comando `regs` mostra os registros de energia e escala e uma
leitura bruta de cada IMU (diagnóstico).

## Do computador para a caixa

Texto simples terminado em Enter (sem verificação; maiúsculas ou minúsculas):

| Comando | Efeito |
| --- | --- |
| `ATIVO A` / `ATIVO B` / `ATIVO C` | esse IMU passa a mandar 250 leituras por segundo (o programa manda ao começar cada jogo) |
| `ATIVO 0` | todos a 10 por segundo (fim do jogo) |
| `STATUS` | manda a linha `S` agora |
| `REGS` | diagnóstico dos IMUs (linhas `#`): registros de energia e escala e uma leitura bruta |
| `?` | ajuda (linhas `#`) |
| `v` | liga/desliga um resumo legível a cada segundo (linhas `#`) |

## Exemplo

```
# TRILHO caixa, firmware 200. Digite ? para ajuda.
L,caixa iniciada; fw 207; VL53L0X ok; IMU A MPU6050, B MPU6500, C MPU6050*XX
S,4000000700,200,1,1,1,1,1,0,0,0*63
I,4000002100,A,9546,28564,-17954,19826,-26692,-7930*2F
B,4000123456,2,1*..
D,4000130000,142,0*..
F,4000131000,-123456*..
```
