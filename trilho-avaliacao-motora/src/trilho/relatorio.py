"""Relatório em PDF por sessão, comparando o paciente com as próprias sessões anteriores."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,  # noqa: E402
                                Spacer, Table, TableStyle)

from . import VERSAO_ALGORITMO  # noqa: E402
from .armazenamento import Banco, ContextoApp  # noqa: E402

NOMES_CAMINHO = {"A": "guarda-chuva", "B": "espiral", "C": "retilíneo"}
COR = "#2563eb"
COR_2 = "#d97706"

METRICAS_TABELA = [
    ("tempo_movimento_s", "Tempo de movimento", "s", 2),
    ("tempo_reacao_s", "Tempo de reação", "s", 2),
    ("velocidade_media_m_s", "Velocidade média", "mm/s", 1000),
    ("velocidade_pico_m_s", "Velocidade de pico", "mm/s", 1000),
    ("aceleracao_pico_m_s2", "Aceleração de pico", "m/s²", 3),
    ("sparc", "SPARC (suavidade, ↑ melhor)", "", 2),
    ("ldlj", "LDLJ (jerk, ↑ melhor)", "", 2),
    ("submovimentos", "Submovimentos", "", 1),
    ("pausas", "Pausas", "", 1),
    ("retrocessos", "Retrocessos", "", 1),
    ("amplitude_pct", "Amplitude", "%", 1),
    ("forca_media_N", "Força média de preensão", "N", 1),
    ("forca_pico_N", "Força de pico", "N", 1),
    ("forca_cv", "Coef. de variação da força", "", 2),
]


def _fmt(valor, fator) -> str:
    if valor is None or (isinstance(valor, float) and np.isnan(valor)):
        return "—"
    if fator == 1000:
        return f"{valor * 1000:.0f}"
    return f"{valor:.{int(fator) if fator < 10 else 0}f}"


def _png(fig) -> Image:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Image(buf, width=17 * cm, height=17 * cm * fig.get_figheight() / fig.get_figwidth())


def _estilo_eixo(ax) -> None:
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    ax.grid(alpha=0.25, linewidth=0.6)
    ax.tick_params(labelsize=8)


def _resumo_sessao(resultados: list[dict]) -> pd.DataFrame:
    linhas = [m for r in resultados for m in r["metricas"] if m.get("completa")]
    return pd.DataFrame(linhas)


def gerar_relatorio(banco: Banco, ctx: ContextoApp, sessao_id: str, saida: str | Path,
                    mostrar_nome: bool = True) -> Path:
    saida = Path(saida)
    sess = dict(banco.con.execute("SELECT * FROM sessao WHERE id = ?", (sessao_id,)).fetchone())
    pac = banco.paciente(ctx, sess["paciente_id"], identificar=mostrar_nome)
    historico = banco.sessoes_do_paciente(ctx, sess["paciente_id"], sess["caminho"])
    historico = [h for h in historico if h["data"] <= sess["data"]]
    resultados = banco.resultados_da_sessao(sessao_id)
    atual = _resumo_sessao(resultados)

    estilos = getSampleStyleSheet()
    h1, h2, corpo = estilos["Title"], estilos["Heading2"], estilos["BodyText"]
    nota = estilos["Italic"]
    el: list = []
    titulo = f"Relatório de sessão — paciente {pac['codigo']}"
    el.append(Paragraph(titulo, h1))
    data = datetime.fromisoformat(sess["data"]).strftime("%d/%m/%Y %H:%M")
    linhas_cab = [
        ["Paciente", f"{pac.get('nome', '')} ({pac['codigo']})" if mostrar_nome else pac["codigo"]],
        ["Data", data],
        ["Caminho / jogo", f"{sess['caminho']} ({NOMES_CAMINHO.get(sess['caminho'], '')}) / {sess['jogo'] or '—'}"],
        ["Joystick", sess["joystick_id"] or "—"],
        ["Partidas válidas", str(len(resultados))],
        ["Tentativas completas", str(len(atual))],
        ["Sessões no histórico", str(len(historico))],
    ]
    t = Table(linhas_cab, colWidths=[5 * cm, 12 * cm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9),
                           ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    el += [t, Spacer(1, 0.4 * cm)]

    # --------------------------------------------------- métricas desta sessão
    el.append(Paragraph("Métricas da sessão (tentativas completas)", h2))
    anterior = None
    if len(historico) >= 2:
        anterior = _resumo_sessao(banco.resultados_da_sessao(historico[-2]["id"]))
    tab = [["Métrica", "Média", "DP", "Sessão anterior", "Unid."]]
    for col, nome, unid, fator in METRICAS_TABELA:
        if col not in atual.columns:
            continue
        x = pd.to_numeric(atual[col], errors="coerce").dropna()
        if x.empty:
            continue
        ant = "—"
        if anterior is not None and col in anterior.columns:
            xa = pd.to_numeric(anterior[col], errors="coerce").dropna()
            if len(xa):
                ant = _fmt(float(xa.mean()), fator)
        tab.append([nome, _fmt(float(x.mean()), fator),
                    _fmt(float(x.std(ddof=1)) if len(x) > 1 else 0.0, fator), ant, unid])
    t = Table(tab, colWidths=[6.5 * cm, 2.5 * cm, 2.5 * cm, 3.2 * cm, 2 * cm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
    ]))
    el += [t, Spacer(1, 0.4 * cm)]

    # ----------------------------------------------------------- curva típica
    if resultados and resultados[0]["arquivo_series"]:
        serie = banco.serie(resultados[0]["arquivo_series"])
        tem_forca = "forca_N" in serie.columns
        fig, eixos = plt.subplots(3 if tem_forca else 2, 1, figsize=(8, 5.5 if tem_forca else 4),
                                  sharex=True)
        eixos[0].plot(serie["t"], serie["s"] * 1000, color=COR, lw=1)
        eixos[0].set_ylabel("posição (mm)", fontsize=8)
        eixos[1].plot(serie["t"], serie["v"] * 1000, color=COR, lw=1)
        eixos[1].set_ylabel("velocidade (mm/s)", fontsize=8)
        if tem_forca:
            eixos[2].plot(serie["t"], serie["forca_N"], color=COR_2, lw=1)
            eixos[2].set_ylabel("força (N)", fontsize=8)
        eixos[-1].set_xlabel("tempo (s)", fontsize=8)
        for ax in eixos:
            _estilo_eixo(ax)
        el.append(KeepTogether([
            Paragraph("Primeira partida: posição, velocidade" +
                      (" e força de preensão" if tem_forca else ""), h2),
            _png(fig)]))

    # ------------------------------------------------- evolução entre sessões
    evol = []
    for h in historico:
        df = _resumo_sessao(banco.resultados_da_sessao(h["id"]))
        if df.empty:
            continue
        linha = {"data": datetime.fromisoformat(h["data"])}
        for col in ("sparc", "tempo_movimento_s", "velocidade_pico_m_s",
                    "forca_media_N", "forca_pico_N"):
            if col in df.columns:
                linha[col] = pd.to_numeric(df[col], errors="coerce").mean()
        evol.append(linha)
    if len(evol) >= 2:
        ev = pd.DataFrame(evol)
        paineis = [("sparc", "SPARC (↑ mais suave)", COR),
                   ("tempo_movimento_s", "Tempo de movimento (s)", COR)]
        if "forca_media_N" in ev.columns:
            paineis.append(("forca_media_N", "Força média / pico (N)", COR_2))
        fig, eixos = plt.subplots(1, len(paineis), figsize=(8, 2.6))
        for ax, (col, rot, cor) in zip(np.atleast_1d(eixos), paineis):
            ax.plot(ev["data"], ev[col], "o-", color=cor, lw=1.5, ms=4)
            if col == "forca_media_N" and "forca_pico_N" in ev.columns:
                ax.plot(ev["data"], ev["forca_pico_N"], "s--", color=cor, lw=1, ms=3, alpha=0.6)
            ax.set_title(rot, fontsize=9)
            ax.set_xticks(ev["data"])
            ax.set_xticklabels([d.strftime("%d/%m") for d in ev["data"]])
            _estilo_eixo(ax)
        fig.tight_layout()
        el.append(KeepTogether([Paragraph("Evolução nas sessões do mesmo caminho", h2), _png(fig)]))
        if "forca_media_N" in ev.columns:
            el.append(Paragraph(
                "A queda da força de preensão ao longo das sessões é interpretada como "
                "melhora (menos tônus involuntário na mão).", corpo))

    # ---------------------------------------------------------- qualidade
    el.append(Paragraph("Qualidade do dado", h2))
    linhas_q = [["Partida", "Sincronia (µs)", "Divisas aceitas", "Rejeitadas",
                 "Religamentos", "Divergências Gray", "Lacunas IMU"]]
    for r in resultados:
        q = r["qualidade"]
        linhas_q.append([str(r["numero"]),
                         f"{q.get('erro_sincronia_rms_us') or 0:.0f}",
                         str(q.get("divisas_aceitas", 0)), str(q.get("divisas_rejeitadas", 0)),
                         str(q.get("religamentos", 0)), str(q.get("divergencias_gray", 0)),
                         str(q.get("lacunas_imu", 0))])
    t = Table(linhas_q)
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                           ("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
    el += [t, Spacer(1, 0.3 * cm)]
    versoes = sorted({r["versao_algoritmo"] for r in resultados}) or [VERSAO_ALGORITMO]
    el.append(Paragraph(
        f"Comparação apenas com as sessões anteriores do próprio paciente. "
        f"Algoritmo: {', '.join(versoes)}. Gerado em {datetime.now():%d/%m/%Y %H:%M}.", nota))

    doc = SimpleDocTemplate(str(saida), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.6 * cm, title=titulo)
    doc.build(el)
    with banco._acao(ctx, "gerar_relatorio", "sessao", sess["paciente_id"]):
        pass
    return saida
