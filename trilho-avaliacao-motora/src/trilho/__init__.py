"""Sistema de Avaliação Motora com Joysticks em Trilho.

Biblioteca de processamento do aplicativo da clínica: protocolo, sincronia de
relógios, fusão (Kalman + RTS), métricas clínicas, armazenamento e relatório.
Ela é independente da interface e dos jogos, para ser testada sozinha e
reutilizada em notebooks de pesquisa e, no futuro, na nuvem.
"""

__version__ = "0.1.0"

# Todo resultado guarda a versão do algoritmo que o gerou. Mude este número
# sempre que a fusão ou as métricas mudarem, para poder reprocessar sessões.
VERSAO_ALGORITMO = "fusao-1.0/metricas-1.0"

# Versão do formato binário dos arquivos de sessão e dos quadros seriais.
VERSAO_FORMATO = 1
