# Trilho — sistema de avaliação motora com joysticks em trilho

Site dos mini-games (bolo, carro, janela) integrado à arquitetura do projeto: os sensores
passam pela caixa (uma ESP32) e por um servidor local em Python, que faz a fusão dos sensores,
grava tudo num banco SQLite e entrega ao site o movimento já processado.

```
Joysticks A, B, C (por fio)          Caixa: uma ESP32 (DevKit)           Computador da clínica
IMU em cada um                       5 sensores IR, VL53L0X              servidor Python (python -m trilho)
(A e C: MPU6050; B: MPU-9250)   ──▶ leituras brutas com horário ── USB (texto, 921600) ──▶ fusão · banco · API
                                                                         └─ WebSocket ─▶ site no navegador
```

Como todos os sensores estão na mesma placa, todos os horários vêm do mesmo relógio: não há
sincronia de relógios a fazer.

## Pastas

| Pasta | Conteúdo |
| --- | --- |
| `firmware/caixa/` | **Firmware da caixa** (a montagem atual): 2 MPU6050 (janela e carro) e 1 MPU-9250 (espiral), 5 sensores IR por interrupção, VL53L0X; linhas de texto com verificação |
| `firmware/legado_sem_fio/` | Firmware da primeira ideia (joysticks sem fio); não usar |
| `servidor/trilho/` | Servidor: aquisição, sincronia, fusão (Kalman + RTS), métricas, banco, API, simulador |
| `web/index.html` | O site (painel, três jogos, evolução) |
| `web/avaliacao.html` | Área do profissional: métricas clínicas por partida, gráfico de evolução e guia explicando cada métrica |
| `web/calibracao.html` | Página do técnico: status, sensores ao vivo, tabela de transições, auditoria |
| `docs/PROTOCOLO_CAIXA.md` | Protocolo de texto da caixa (o que cada linha significa) |
| `testes/` | Testes automáticos (`python -m pytest testes`) |

## Rodar no computador da clínica (Windows)

1. Instale o Python 3.12 ou mais novo (marque "Add Python to PATH").
2. Dê dois cliques em `iniciar.bat`. Na primeira vez ele instala as dependências.
3. O navegador abre em `http://127.0.0.1:8000`. Use Chrome ou Edge.

Sem hardware, use `iniciar_simulador.bat`: uma caixa com os três joysticks simulados. Durante o
jogo, o **mouse guia o joystick virtual**, e todo o caminho real é exercitado (protocolo,
sincronia, fusão, banco).

Os dados ficam em `%LOCALAPPDATA%\Trilho`: o banco `trilho.db`, a chave de criptografia
`chave.bin` (protegida pelo DPAPI do Windows) e a pasta `brutos\` com os dados brutos de cada
partida em Parquet. **Faça cópia de segurança dessa pasta inteira**: sem `chave.bin`, os nomes
dos pacientes não podem ser lidos. A chave só abre no mesmo usuário do Windows.

**Conectar a ESP32 (caixa).** O Trilho procura sozinho a caixa nas portas USB. Para escolher
a porta à mão, clique em **"Conectar ESP32"** no topo do painel (vira "Porta USB" quando já está
conectada) ou na página Calibração. A janela lista as portas que o computador vê, com as placas
ESP32 primeiro; escolha uma e clique em "Conectar". A escolha fica salva para as próximas vezes.
Quem abre e lê a porta é o programa Trilho, não o navegador, por isso a leitura continua quando você
troca de página no site. Se a lista estiver vazia, confira o cabo (alguns só carregam) e o driver do
chip USB da placa (CP210x ou CH340). Se a porta abrir mas a caixa não responder, a janela diz o
motivo (por exemplo, "a placa está com o programa antigo": falta gravar `firmware/caixa/caixa.ino`).

Opções: `python -m trilho --porta COM5` (fixa a porta da caixa), `--http 8080`, `--sem-navegador`.

## Onde ficam as métricas clínicas

O paciente vê só o resultado do jogo (pontos, estrelas e o placar do jogo). As métricas de
movimento medidas pelos sensores ficam separadas, para o profissional:

- **Área de avaliação** (link "Avaliação" no topo do painel, ou `http://127.0.0.1:8000/avaliacao`):
  escolha paciente e jogo; veja o gráfico de evolução de qualquer métrica, a tabela das partidas
  e o **guia das métricas** (o que cada uma mede, como ler e se melhora quando sobe ou desce).
- **CSV** ("Exportar CSV"): uma linha por partida e **uma coluna por métrica**, com vírgula
  decimal e horário de Brasília, pronto para abrir no Excel.

## Gravar o firmware da caixa (Arduino IDE 2)

1. Na Arduino IDE, instale a placa **"esp32" da Espressif** (Gerenciador de Placas) e a
   biblioteca **"VL53L0X" by Pololu** (a mesma do código de teste).
2. Abra `firmware/caixa/caixa.ino`. A pasta `firmware/caixa/` precisa ter também o arquivo
   `caixa_tipos.h` (a IDE mostra os dois como abas). Placa: **ESP32 Dev Module**. Porta: a COM da caixa.
3. Se o Trilho estiver aberto, clique em "Conectar ESP32 → Desconectar" para liberar a porta.
   Grave (seta →).
4. Para conferir: abra o Monitor Serial em **921600 baud**. Aparece `# TRILHO caixa, firmware 207`
   e linhas como `S,...`, `I,...`, `D,...`. Digite `v` e Enter para ver um resumo legível a cada
   segundo (IMUs, distância, sensores IR). Feche o Monitor antes de conectar no Trilho:
   só um programa usa a porta por vez.
5. No Trilho, clique em "Conectar ESP32 → Conectar" e abra a página **Calibração**: ela mostra os
   três IMUs, os cinco sensores IR e a distância ao vivo, e as **mensagens da caixa**,
   inclusive quem responde em cada barramento I2C (esperado: D21/D22 → 0x29 e 0x68;
   D18/D19 → 0x68 e 0x69). No Monitor Serial, o comando `i2c` mostra a mesma lista.

**Ligações** (iguais às do código de teste da equipe de hardware; estão no começo do `.ino`):
I2C 1 (D21/D22): VL53L0X e IMU do carro (0x68). I2C 2 (D18/D19): IMU da janela (0x69, AD0 no 3V3) e IMU da
espiral (MPU-92.65 em 0x68: essa placa ignora o AD0). Se aparecerem trocados, troque `END_IMU_JANELA` e `END_IMU_ESPIRAL` no começo do `caixa.ino`. IR: S1 D25, S2 D27, S3 D13, ponta A D14,
ponta B D26.

**Diferenças em relação ao código de teste:** as leituras saem brutas, com o horário exato, e os
cálculos (posição, tremor, trancos, suavidade) passam a ser feitos no computador; os sensores IR
são lidos por interrupção (horário exato da troca, filtro de 20 ms contra piscadas); os IMUs ficam em ±4 g e
±500 °/s; o IMU do jogo em andamento manda 250 leituras por segundo e os outros, 10; o I2C roda
a 100 kHz, como no código de teste;
a USB vai a 921600 baud.

## Antes de usar com pacientes

- **Calibrar** (página Calibração e cartão do carro): sensor do caminho reto (trás/frente)
  e **tabela de transições** dos sensores IR. A tabela que
  vem de fábrica tem posições provisórias e aparece marcada como "valores provisórios".
- **Espiral**: os cruzamentos S1, S2, S3, S1, S3 foram posicionados pela foto da placa (ângulo de
  cada sensor em volta do centro; ver `ANGULOS_ESPIRAL_GRAUS` em `servidor/trilho/caminhos.py`).
  Meça a posição real de cada cruzamento (em mm ao longo do caminho, a partir da entrada) e salve
  na tabela. Depois do último cruzamento (cerca de 850 dos 1410 mm) não há sensor: até o centro a
  posição vem só do IMU, e o erro cresce nesse trecho.
- **Janela (guarda-chuva)**: os sensores ficam nas pontas. Como no código de teste da equipe de
  hardware, a posição entre as pontas vem do giroscópio: o programa mede quanto o IMU gira de uma
  ponta à outra e guarda essa medida como **calibração automática** (não depende do raio do
  desenho nem do lado em que o IMU foi montado). Na primeiríssima partida, a primeira passada
  ainda é aproximada; a partir da segunda, o joystick é acompanhado em todo o arco. Se o IMU não
  girar com o arco, o programa percebe e usa a aceleração.
- **Geometria dos caminhos**: em `servidor/trilho/caminhos.py` estão premissas (raio da espiral
  95 → 15 mm, arco em semicírculo de 170 mm, reto de 120 mm). Troque pelas medidas do CAD. A fusão
  depende da curvatura correta.
- **Montagem da IMU**: a fusão supõe que o eixo x do IMU fica alinhado ao eixo x da placa
  (lado de 425 mm) quando a partida começa. Se não ficar, ajuste `rotacao_montagem_graus` em
  `ConfigFusao` (`servidor/trilho/fusao.py`).
- **Posição inicial**: a contagem regressiva mostra onde deixar o joystick (borda da espiral,
  ponta esquerda do arco, meio do trilho reto).

## Espiral (jogo do bolo): como a posição é medida

Os feixes IR da borda só alcançam as duas voltas de fora (3 sensores, 6 cruzamentos). Por isso:

1. **Base: os sensores IR** (`RastreadorZonas` em `servidor/trilho/fusao.py`). A ordem das entradas
   nos feixes (S1, S2, S3, S1, S2, S3) diz em que trecho o joystick está; um cruzamento só vale se
   houve tempo para chegar até ele e se não pula mais de um da ordem. A saída do feixe é uma segunda
   posição conhecida, e o tempo dentro do feixe dá a velocidade naquele ponto. A colher nunca passa
   de um feixe que ainda não foi cruzado, não anda para trás enquanto os sensores indicam ida (e
   vice-versa) e, se a mão hesita na beira de um feixe (sai e entra de novo em menos de 1 s), ela
   espera no feixe em vez de inverter.
2. **O IMU diz se a mão está parada** (pouca agitação e aceleração horizontal média perto de zero;
   não depende de como a placa foi montada): a colher para e volta a andar com a mão.
3. **O ângulo da placa do IMU é descoberto pelos dados**: nas curvas, a aceleração aponta para o
   centro da curva, direção conhecida pela geometria. Depois de confirmado (duas rodadas que
   concordam), a fusão com o IMU passa a desenhar a colher entre os feixes, sempre dentro do trecho
   que os sensores permitem; se ela se afastar dos sensores, a tela volta para eles. No
   pós-processamento (métricas) a partida é processada duas vezes: a primeira só para descobrir o
   ângulo, a segunda já com ele desde o início.

Na qualidade da fusão de cada partida: `montagem_graus`, `montagem_confirmada`, `zonas_paradas`,
`zonas_velocidades_feixe`, `bordas_rejeitadas`. A largura do feixe usada para a velocidade vem da
tabela de transições (14 mm por padrão); medir a largura real que o joystick bloqueia melhora a
velocidade.

## Arco (jogo da janela): proteções

- **Escala do giro absurda**: a escala do giroscópio (giro por mm) é guardada de uma partida para a
  outra. Uma escala medida com o IMU com defeito (ex.: durante o conflito de endereço das IMUs)
  fazia o ruído do giroscópio virar velocidades enormes, e a estimativa batia de uma ponta na
  outra. Agora só vale uma escala entre 1/3 e 3 vezes a do desenho do arco; fora disso ela é
  descartada (ao carregar e ao medir) e medida de novo.
- **Sensor de ponta acionando à toa**: a ponta só vale se o joystick pode ter chegado lá (tempo
  desde a outra ponta e, com o giroscópio em uso, pelo menos 35% do giro esperado).
- **Velocidade estimada limitada** a 800 mm/s.

Na qualidade da fusão: `escala_giro_descartada`, `pontas_implausiveis`, `velocidade_limitada`.

## Célula de carga: retirada do projeto

A célula de carga (HX711) foi retirada. Firmware 207 em diante não lê nem manda força; o programa
não mede carga normal, não avisa "carga alta" e não calcula métricas de força. Firmware antigo
(até o 206) ainda é entendido: o campo da célula na linha `S` é ignorado. Partidas antigas
continuam no banco com as métricas que tinham; metas de métricas de força deixam de aparecer.

## O que foi verificado e como

| Verificação | Resultado |
| --- | --- |
| Firmware da caixa compilado com cabeçalhos substitutos das bibliotecas Arduino (g++, `-Wall -Wextra -Wformat=2`, campos de cada linha conferidos pelo compilador) | sem erros nem avisos |
| Linhas geradas pelo firmware (rodando com dados de teste) lidas pelo servidor | 8279 linhas, nenhuma rejeitada |
| Linha corrompida ou do firmware antigo | descartada |
| Posição em tempo real com a caixa simulada (RMS) — espiral / guarda-chuva / reto | ~7,5 / ~4,5 / ~0,9 mm |
| Posição depois do suavizador RTS (RMS) | ~7,2 / ~2,4 / ~1,1 mm |
| Giroscópio do arco: sentido aprendido sozinho (montagem nos dois sentidos) e desligado se o IMU não gira | ok |
| Banco: nome cifrado, auditoria não aceita UPDATE/DELETE, consentimento obrigatório | ok |
| Site no Chromium com o simulador: cadastro, os três jogos, métricas, histórico, CSV | sem erros no console |

**Limites destes números:** o simulador usa o mesmo modelo de geometria que a fusão e um ruído de
IMU idealizado (ruído branco, viés constante). No hardware real, os erros serão maiores. Os
números valem para comparar versões do algoritmo, não como precisão clínica. A espiral é a menos
precisa porque só tem 5 cruzamentos, todos nas voltas de fora. O firmware não foi compilado com o
núcleo real do ESP32 (sem acesso aos servidores da Espressif aqui): compile na Arduino IDE antes
de tudo. A validação de verdade é a da Fase 0/1 da arquitetura (fio de
referência para a sincronia, câmera lenta ou encoder para a posição).

## Login, equipe e acesso aos pacientes

Todo o sistema exige login. Ninguém vê pacientes, partidas ou o CSV sem entrar.

**Primeiro acesso.** Na primeira vez que o site abre, sem nenhuma conta, aparece a tela
"Primeiro acesso". Ela cria o **administrador**. Use um e-mail que será o login e uma senha de
pelo menos 8 caracteres, com letras e números.

**Perfis**

| Perfil | O que faz |
|---|---|
| Administrador | Tudo. Vê todos os pacientes, cadastra profissionais (página **Equipe**), define quem atende cada paciente, vê a auditoria. |
| Fisioterapeuta | Vê **só os pacientes vinculados a ele**. Cadastra pacientes (fica vinculado automaticamente), joga, registra avaliações clínicas, exporta o CSV dos seus pacientes. |
| Técnico | Só a página de calibração e o estado do equipamento. Não vê pacientes. |

**Cadastrar um profissional.** O administrador abre **Equipe → Novo profissional**, clica em
"Gerar senha" e entrega o e-mail e a senha provisória pessoalmente. No primeiro login, a pessoa
é obrigada a criar uma senha própria.

**Esqueceu a senha?** O administrador clica em "Redefinir senha" na Equipe. Uma nova senha
provisória é gerada e as sessões abertas dessa pessoa são encerradas. O mesmo vale para
"Desativar", usado quando alguém sai da clínica. O histórico dessa pessoa continua guardado.

**Vínculos.** Na Equipe, a tabela "Quem atende cada paciente" tem uma caixa por paciente e por
profissional. Um fisioterapeuta que tenta abrir um paciente não vinculado recebe "este paciente
não está vinculado a você".

**Segurança**

- As senhas são guardadas com Argon2: o banco não tem as senhas, só um resumo que não pode ser
  revertido.
- A sessão expira depois de 30 minutos sem uso, ou 12 horas no máximo. Durante uma partida, a
  sessão é mantida viva.
- Depois de 5 senhas erradas, a conta fica bloqueada por 15 minutos.
- Logins, falhas, bloqueios, trocas de senha e cada acesso a paciente ficam registrados na
  auditoria, que não pode ser apagada.
- O servidor só aceita conexões do próprio computador (127.0.0.1).

## Ficha clínica e avaliações

O cadastro do paciente fica na área de **Avaliação**: "+ Novo paciente", ou o botão no painel
de jogos. Ele tem:

- **Identificação:** nome.
- **Diagnóstico e lesão:** condição (AVC isquêmico ou hemorrágico, TCE, paralisia cerebral,
  lesão medular, esclerose múltipla, outra), detalhe, data da lesão (o tempo desde a lesão é
  calculado), lado acometido e mão dominante.
- **Avaliação clínica inicial:** Ashworth modificada (punho, dedos, cotovelo), força de preensão
  dos dois lados em kgf (dinamômetro), Fugl-Meyer de membro superior (0–66), outras escalas e
  observações.
- **Tratamento e objetivos:** toxina botulínica (data da última aplicação e músculos),
  medicações, objetivos terapêuticos e observações.
- **Consentimento:** obrigatório para cadastrar.

Os campos de texto livre (detalhe, músculos, medicações, objetivos, observações, outras escalas)
são **criptografados** no banco, como o nome.

Em "Avaliações clínicas → + Nova avaliação", registre as reavaliações com as mesmas escalas.
A tabela mostra a evolução lado a lado com as métricas medidas pelos sensores. Para editar a
ficha, use "Editar ficha". Para abrir a ficha de um paciente pelo painel de jogos, dê dois
cliques no nome dele.

**Banco antigo:** ao abrir um `trilho.db` criado antes do login, as colunas novas são
adicionadas sozinhas e nada se perde. Na primeira abertura aparece o "Primeiro acesso", e o
administrador vê todos os pacientes antigos e pode vinculá-los aos fisioterapeutas na Equipe.

## Baixa visão

- **"Aa Alto contraste"** (no topo de todas as páginas, inclusive na tela de login): letras maiores,
  texto preto sobre branco, bordas grossas e botões preto/amarelo. Fica ligado neste computador até
  ser desligado.
- **"Modo baixa visão nos jogos deste paciente"** (no painel, abaixo do nome do paciente): salvo na
  ficha do paciente. Os jogos ficam com letras ~45% maiores e contornadas, cores fortes (fundo preto,
  faixas amarelas, joystick e colher em cores vivas com contorno) e placar maior. O formato do vidro
  e o desenho do carro são os mesmos; mudam só cores, contornos e tamanhos. Com o "Alto contraste"
  ligado, os jogos também entram nesse modo.
