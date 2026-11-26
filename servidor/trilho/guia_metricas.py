"""Guia das métricas de movimento para o profissional de saúde.

Usado na área de avaliação (/avaliacao) e no cabeçalho do CSV. Texto em
linguagem clínica simples: o que é, como ler e em que direção indica melhora.
"""

# melhora: "maior" | "menor" | "contexto" (depende do objetivo do exercício)
GUIA: dict[str, dict] = {
    "duracao_s": dict(
        rotulo="Duração analisada", unidade="s", melhora="contexto",
        o_que_e="Tempo da partida usado na análise, do fim da contagem regressiva até o encerramento.",
        como_ler="Serve de referência para as outras medidas; não indica desempenho sozinho."),
    "tempo_em_movimento_s": dict(
        rotulo="Tempo em movimento", unidade="s", melhora="contexto",
        o_que_e="Quanto tempo o joystick esteve de fato se movendo (velocidade acima de 5 mm/s).",
        como_ler="Comparado à duração, mostra quanto do exercício foi gasto parado ou hesitando."),
    "tempo_de_reacao_s": dict(
        rotulo="Tempo até começar a mover", unidade="s", melhora="menor",
        o_que_e="Tempo entre o início do jogo e o primeiro movimento.",
        como_ler="Valores menores indicam iniciação do movimento mais rápida."),
    "velocidade_media_mm_s": dict(
        rotulo="Velocidade média", unidade="mm/s", melhora="contexto",
        o_que_e="Velocidade média durante os trechos de movimento.",
        como_ler="Aumento costuma indicar ganho de desempenho, desde que a suavidade não piore. "
                 "Nos jogos com limite de velocidade (bolo), ir rápido demais é penalizado."),
    "velocidade_pico_mm_s": dict(
        rotulo="Velocidade de pico", unidade="mm/s", melhora="contexto",
        o_que_e="Maior velocidade atingida na partida.",
        como_ler="Picos muito altos e isolados podem indicar movimentos bruscos ou pouco controlados."),
    "pico_no_trecho_pct": dict(
        rotulo="Posição do pico de velocidade no movimento", unidade="%", melhora="contexto",
        o_que_e="Em que ponto do movimento (0% = início, 100% = fim) a velocidade foi máxima.",
        como_ler="Movimentos bem coordenados tendem a ter o pico perto do meio (40–60%). "
                 "Pico muito cedo sugere aceleração brusca seguida de frenagem longa."),
    "aceleracao_pico_mm_s2": dict(
        rotulo="Aceleração de pico", unidade="mm/s²", melhora="contexto",
        o_que_e="Maior aceleração ao longo do caminho.",
        como_ler="Valores muito altos indicam arrancadas bruscas."),
    "desaceleracao_pico_mm_s2": dict(
        rotulo="Desaceleração de pico", unidade="mm/s²", melhora="contexto",
        o_que_e="Maior frenagem ao longo do caminho.",
        como_ler="Valores muito altos indicam paradas bruscas."),
    "distancia_percorrida_mm": dict(
        rotulo="Distância percorrida", unidade="mm", melhora="contexto",
        o_que_e="Soma de todo o deslocamento do joystick ao longo do caminho, nos dois sentidos.",
        como_ler="Indica o volume de movimento realizado na partida."),
    "amplitude_pct": dict(
        rotulo="Amplitude do caminho usada", unidade="%", melhora="maior",
        o_que_e="Quanto do comprimento total do caminho o paciente conseguiu percorrer.",
        como_ler="Aumento indica maior amplitude de movimento."),
    "sparc": dict(
        rotulo="Suavidade (SPARC)", unidade="", melhora="maior",
        o_que_e="Medida de suavidade baseada no perfil de velocidade (Spectral Arc Length). "
                "É a principal métrica de suavidade, mais robusta a ruído.",
        como_ler="Valores negativos; quanto mais próximo de zero, mais suave. Movimentos saudáveis "
                 "costumam ficar perto de −1,5 a −2; movimentos fragmentados ficam mais negativos."),
    "ldlj": dict(
        rotulo="Suavidade (LDLJ)", unidade="", melhora="maior",
        o_que_e="Jerk adimensional em logaritmo: mede as mudanças bruscas de aceleração.",
        como_ler="Quanto maior (menos negativo), mais suave. Complementar ao SPARC; "
                 "com movimentos muito lentos é menos confiável."),
    "submovimentos": dict(
        rotulo="Submovimentos", unidade="", melhora="menor",
        o_que_e="Número de picos de velocidade: cada pico é um 'impulso' do movimento.",
        como_ler="Um movimento fluido tem poucos picos; muitos picos indicam movimento fragmentado, "
                 "em 'trancos'. Compare sempre o mesmo jogo e a mesma dificuldade."),
    "pausas": dict(
        rotulo="Pausas", unidade="", melhora="menor",
        o_que_e="Número de paradas de pelo menos 0,5 s no meio do exercício.",
        como_ler="Redução indica movimento mais contínuo."),
    "maior_pausa_s": dict(
        rotulo="Maior pausa", unidade="s", melhora="menor",
        o_que_e="Duração da parada mais longa.",
        como_ler="Pausas longas podem indicar fadiga, dificuldade em retomar o movimento ou distração."),
    "mudancas_de_sentido": dict(
        rotulo="Mudanças de sentido", unidade="", melhora="contexto",
        o_que_e="Quantas vezes o joystick inverteu o sentido do movimento.",
        como_ler="No carro e na janela inverter faz parte da tarefa; no bolo, inversões fora do "
                 "centro e da borda indicam correções de trajetória."),
    "tremor_rms_m_s2": dict(
        rotulo="Tremor (4–12 Hz)", unidade="m/s²", melhora="menor",
        o_que_e="Intensidade das vibrações rápidas do joystick, na faixa típica de tremor (4 a 12 Hz).",
        como_ler="Aumento persistente merece atenção clínica. Valores muito baixos (< 0,02) estão "
                 "no nível do ruído do sensor."),
}

QUALIDADE = {
    "residuo_ms": "Erro estimado da sincronia de relógio entre joystick e bancada. Acima de 1 ms, desconfie das medidas de tempo.",
    "pacotes_nao_recuperados": "Pacotes de dados perdidos no rádio e não recuperados. Acima de zero, parte da partida ficou sem dados da IMU.",
    "bordas_rejeitadas": "Marcos do caminho (sensores LM393) descartados por não baterem com a posição esperada. Muitos rejeitados sugerem sensor desalinhado ou tabela de transições desatualizada.",
    "inconsistencias_gray": "Leituras dos sensores da espiral fora da sequência esperada. Indica sensor falhando ou desalinhado.",
}


def cabecalho_csv(chave: str) -> str:
    g = GUIA[chave]
    return f"{g['rotulo']} ({g['unidade']})" if g["unidade"] else g["rotulo"]
