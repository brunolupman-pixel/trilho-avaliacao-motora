// comum.js — sessão de login e utilidades compartilhadas pelas páginas do Trilho.

const Trilho = {
  usuario: null,

  // Chama a API. Sem sessão -> tela de login; senha provisória -> troca de senha.
  async api(metodo, url, corpo) {
    const r = await fetch(url, { method: metodo, headers: corpo ? { "Content-Type": "application/json" } : {},
                                 body: corpo ? JSON.stringify(corpo) : undefined });
    let dados = null;
    try { dados = await r.json(); } catch { /* sem corpo */ }
    if (r.status === 401 && !url.startsWith("/api/auth/")) {
      location.href = "/login?volta=" + encodeURIComponent(location.pathname + location.search);
      throw new Error("sessão encerrada");
    }
    if (r.status === 403 && dados && /senha provisória/.test(dados.detail || "")) {
      location.href = "/conta";
      throw new Error(dados.detail);
    }
    if (!r.ok) throw new Error((dados && dados.detail) || ("erro " + r.status));
    return dados;
  },

  esc(t) { return String(t ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); },

  // Preenche o elemento com "Nome (perfil) · links · Sair".
  async montarUsuario(id, atual) {
    const est = await Trilho.api("GET", "/api/auth/estado");
    if (!est.usuario) { location.href = "/login?volta=" + encodeURIComponent(location.pathname); return null; }
    Trilho.usuario = est.usuario;
    const u = est.usuario, p = u.perfil;
    const links = [];
    if (p !== "tecnico") links.push(["/", "Jogos"], ["/avaliacao", "Avaliação"]);
    links.push(["/calibracao", "Calibração"]);
    if (p === "administrador") links.push(["/equipe", "Equipe"]);
    const el = document.getElementById(id);
    if (el) {
      el.innerHTML = `<button type="button" class="botao-contraste" onclick="Trilho.alternarBaixaVisao()"
          aria-pressed="${Trilho.baixaVisao()}" title="Letras maiores e contraste mais forte">Aa Alto contraste</button>` +
        links.filter(([h]) => h !== atual).map(([h, t]) => `<a class="botao-link" href="${h}">${t}</a>`).join("") +
        `<a class="conta-chip${atual === "/conta" ? " atual" : ""}" href="/conta"
             title="Configurações da conta — ${Trilho.esc(Trilho.ocupacao(u))} · ${Trilho.esc(u.email)}">
           <span class="conta-texto"><b>${Trilho.esc(u.nome)}</b><small>${Trilho.esc(u.perfil_nome)}</small></span>
           ${Trilho.avatar(u, 36)}
         </a>
         <a class="botao-link" href="#" onclick="Trilho.sair(); return false;">Sair</a>`;
    }
    return u;
  },

  // Ocupação mostrada junto do nome: o perfil e, se houver, o registro profissional
  ocupacao(u) { return u.registro_profissional ? `${u.perfil_nome} · ${u.registro_profissional}` : u.perfil_nome; },

  // Foto redonda (ou as iniciais numa cor fixa por pessoa, enquanto não há foto)
  avatar(u, tam = 36, id = "") {
    const ini = (u.nome || "?").split(/\s+/).filter(Boolean);
    const sigla = ((ini[0] || "?")[0] + (ini.length > 1 ? ini[ini.length - 1][0] : "")).toUpperCase();
    const cores = ["#2f6fed", "#16a34a", "#f59e0b", "#7c3aed", "#db2777", "#0891b2"];
    const cor = cores[[...(u.email || u.nome || "")].reduce((a, c) => a + c.charCodeAt(0), 0) % cores.length];
    const estilo = `width:${tam}px;height:${tam}px;font-size:${Math.round(tam * 0.4)}px;background:${cor}`;
    return u.foto_versao
      ? `<span class="avatar" ${id ? `id="${id}"` : ""} style="${estilo}"><img src="/api/auth/foto?v=${u.foto_versao}" alt=""></span>`
      : `<span class="avatar" ${id ? `id="${id}"` : ""} style="${estilo}" aria-hidden="true">${Trilho.esc(sigla)}</span>`;
  },

  // ---------------------------------------------------------------- baixa visão (alto contraste)
  // Vale para este computador (fica salvo no navegador). Cada paciente também pode ter o
  // "modo baixa visão" ligado na ficha de jogos: aí os jogos ficam maiores e com mais contraste.
  baixaVisao() {
    try { return localStorage.getItem("trilho_baixa_visao") === "1"; } catch { return false; }
  },
  alternarBaixaVisao(ligar) {
    const on = ligar === undefined ? !Trilho.baixaVisao() : !!ligar;
    try { localStorage.setItem("trilho_baixa_visao", on ? "1" : "0"); } catch { /* sem armazenamento */ }
    Trilho.aplicarBaixaVisao(on);
    document.querySelectorAll(".botao-contraste").forEach(b => b.setAttribute("aria-pressed", on));
    document.dispatchEvent(new CustomEvent("trilho-baixa-visao", { detail: on }));
  },
  aplicarBaixaVisao(on) {
    Trilho._estiloBaixaVisao();
    document.documentElement.classList.toggle("baixa-visao", !!on);
  },
  _estiloBaixaVisao() {
    if (document.getElementById("tbv-estilo")) return;
    const s = document.createElement("style"); s.id = "tbv-estilo";
    s.textContent = `
      .botao-contraste { font:inherit; font-size:14px; font-weight:700; cursor:pointer; border:2px solid currentColor; border-radius:10px;
                         padding:4px 10px; background:transparent; color:#1d2433; }
      .botao-contraste[aria-pressed="true"] { background:#000; color:#ffe600; border-color:#000; }
      html.baixa-visao { --fundo:#ffffff; --cartao:#ffffff; --texto:#000000; --suave:#1a1a1a; --linha:#000000;
        --azul:#0038a8; --azul-claro:#ffe600; --verde:#005c26; --verde-claro:#c8f7d8; --vermelho:#a30000; --laranja:#7a4000; }
      html.baixa-visao body > :not(#tela-jogo):not(.fundo-modal):not(.tesp-fundo):not(script):not(style) { zoom:1.25; }
      html.baixa-visao .modal, html.baixa-visao .tesp-caixa { zoom:1.2; border:3px solid #000; }
      html.baixa-visao body { color:#000; background:#fff; }
      html.baixa-visao .cartao, html.baixa-visao .jogo-card, html.baixa-visao .paciente { border:2px solid #000 !important; }
      html.baixa-visao input, html.baixa-visao select, html.baixa-visao textarea { border:2px solid #000 !important; color:#000; background:#fff; }
      html.baixa-visao button:not(.botao-contraste) { background:#000 !important; color:#fff !important; border:2px solid #000 !important; }
      html.baixa-visao button.secundario { background:#ffe600 !important; color:#000 !important; }
      html.baixa-visao button:disabled { background:#777 !important; border-color:#777 !important; }
      html.baixa-visao a { color:#0038a8 !important; text-decoration:underline !important; font-weight:700; }
      html.baixa-visao small, html.baixa-visao .nota, html.baixa-visao th, html.baixa-visao label { color:#000 !important; }
      html.baixa-visao .selo, html.baixa-visao .etiqueta, html.baixa-visao .melhora { color:#000 !important; border:2px solid #000; background:#fff !important; }
      html.baixa-visao .paciente.sel, html.baixa-visao .paciente.ativo { background:#ffe600 !important; }
      html.baixa-visao :focus-visible { outline:4px solid #c800c8 !important; outline-offset:2px; }`;
    document.head.appendChild(s);
  },

  // ---------------------------------------------------------------- "Conectar ESP32"
  // Janela com as portas USB que o computador vê. Quem abre e lê a porta é o programa
  // Trilho (não o navegador): assim a leitura continua ao trocar de página no site.
  async abrirConexaoEsp(aoMudar) {
    Trilho._estiloEsp();
    let fundo = document.getElementById("trilho-esp");
    if (!fundo) {
      fundo = document.createElement("div");
      fundo.id = "trilho-esp"; fundo.className = "tesp-fundo";
      fundo.innerHTML = `<div class="tesp-caixa" role="dialog" aria-labelledby="tesp-titulo">
        <h2 id="tesp-titulo">Conectar ESP32 (caixa)</h2>
        <p class="tesp-nota">Ligue o cabo USB da caixa e escolha a porta. As placas ESP32 aparecem primeiro.</p>
        <div id="tesp-lista" class="tesp-lista">Procurando portas…</div>
        <div id="tesp-msg" class="tesp-msg"></div>
        <div class="tesp-botoes">
          <button type="button" class="tesp-sec" id="tesp-atualizar">Procurar de novo</button>
          <button type="button" class="tesp-sec" id="tesp-desligar" title="Fecha a porta para a Arduino IDE poder gravar o firmware">Desconectar</button>
          <span style="flex:1"></span>
          <button type="button" class="tesp-sec" id="tesp-fechar">Fechar</button>
          <button type="button" id="tesp-conectar">Conectar</button>
        </div></div>`;
      document.body.appendChild(fundo);
      fundo.addEventListener("click", ev => { if (ev.target === fundo) fundo.classList.remove("ativa"); });
      document.getElementById("tesp-fechar").onclick = () => fundo.classList.remove("ativa");
      document.getElementById("tesp-atualizar").onclick = () => Trilho._listarPortas();
      document.getElementById("tesp-conectar").onclick = () => Trilho._conectarPorta();
      document.getElementById("tesp-desligar").onclick = () => Trilho._desligarPorta();
    }
    Trilho._aoMudarEsp = aoMudar;
    fundo.classList.add("ativa");
    await Trilho._listarPortas();
  },

  _msgEsp(texto, tipo) {
    const m = document.getElementById("tesp-msg");
    m.className = "tesp-msg " + (tipo || ""); m.innerHTML = texto || "";
  },

  async _listarPortas() {
    const lista = document.getElementById("tesp-lista");
    Trilho._msgEsp("");
    let r;
    try { r = await Trilho.api("GET", "/api/hardware/portas"); }
    catch (e) { lista.textContent = ""; Trilho._msgEsp(Trilho.esc(e.message), "erro"); return; }
    const est = r.estado, esc = Trilho.esc;
    const atual = est.ligada ? est.modo : null;
    const linha = (valor, titulo, sub, marcado) => `<label class="tesp-porta">
        <input type="radio" name="tesp-porta" value="${esc(valor)}" ${marcado ? "checked" : ""}>
        <span><b>${titulo}</b><small>${sub}</small></span></label>`;
    let h = linha("auto", "Automático", "usa a primeira ESP32 encontrada", atual === "auto" || !atual);
    h += r.portas.map(p => linha(p.porta, `${esc(p.porta)}${p.parece_esp32 ? ' <span class="tesp-selo">ESP32</span>' : ""}`,
                                 `${esc(p.descricao)} · ${esc(p.chip)}`, atual === p.porta)).join("");
    if (!r.portas.length) h += `<div class="tesp-vazio">Nenhuma porta USB encontrada. Confira o cabo (alguns cabos só carregam,
      não transmitem dados) e o driver do chip USB da placa (CP210x ou CH340).</div>`;
    lista.innerHTML = h;
    if (est.aberta && r.bancada_respondendo) Trilho._msgEsp(`Conectada em <b>${esc(est.aberta)}</b>; a caixa está respondendo.`, "ok");
    else if (est.aberta) Trilho._msgEsp(`Porta <b>${esc(est.aberta)}</b> aberta, mas a caixa não responde. Clique em
      <b>Conectar</b> para o Trilho verificar o que a placa está enviando.`, "aviso");
    else if (!est.ligada) Trilho._msgEsp("Desconectada pelo site. Escolha a porta e clique em Conectar.", "aviso");
    else if (est.erro) Trilho._msgEsp(esc(est.erro), "erro");
  },

  async _conectarPorta() {
    const sel = document.querySelector('input[name="tesp-porta"]:checked');
    const porta = sel && sel.value !== "auto" ? sel.value : null;
    const btn = document.getElementById("tesp-conectar");
    btn.disabled = true; Trilho._msgEsp("Conectando… (se a placa não responder, o Trilho leva alguns segundos verificando o motivo)", "aviso");
    try {
      const r = await Trilho.api("POST", "/api/hardware/conectar", { porta });
      if (r.bancada_respondendo) Trilho._msgEsp(`Conectada em <b>${Trilho.esc(r.estado.aberta)}</b>; a caixa respondeu.`, "ok");
      else Trilho._msgEsp(Trilho.esc(r.aviso || "a caixa não respondeu"), r.estado.aberta ? "aviso" : "erro");
      if (Trilho._aoMudarEsp) Trilho._aoMudarEsp(r);
    } catch (e) { Trilho._msgEsp(Trilho.esc(e.message), "erro"); }
    finally { btn.disabled = false; }
  },

  async _desligarPorta() {
    try {
      await Trilho.api("POST", "/api/hardware/desconectar");
      Trilho._msgEsp("Porta liberada. Agora a Arduino IDE pode gravar o firmware. Para voltar, clique em Conectar.", "aviso");
      if (Trilho._aoMudarEsp) Trilho._aoMudarEsp(null);
    } catch (e) { Trilho._msgEsp(Trilho.esc(e.message), "erro"); }
  },

  _estiloEsp() {
    if (document.getElementById("tesp-estilo")) return;
    const s = document.createElement("style"); s.id = "tesp-estilo";
    s.textContent = `
      .tesp-fundo { position:fixed; inset:0; background:rgba(16,24,40,.45); display:none; align-items:center; justify-content:center; z-index:50; padding:16px; }
      .tesp-fundo.ativa { display:flex; }
      .tesp-caixa { background:#fff; color:#1d2433; border-radius:16px; padding:22px; width:100%; max-width:520px; max-height:90vh; overflow:auto;
                    font-family:"Segoe UI",system-ui,sans-serif; box-shadow:0 10px 40px rgba(0,0,0,.2); }
      .tesp-caixa h2 { font-size:18px; margin:0 0 4px; }
      .tesp-nota { color:#667085; font-size:13px; margin:0 0 12px; }
      .tesp-lista { display:flex; flex-direction:column; gap:6px; }
      .tesp-porta { display:flex; gap:10px; align-items:flex-start; padding:10px 12px; border:1px solid #e3e7ef; border-radius:10px; cursor:pointer; }
      .tesp-porta:has(input:checked) { border-color:#2f6fed; background:#e8f0ff; }
      .tesp-porta input { margin-top:3px; width:auto; }
      .tesp-porta > span { display:flex; flex-direction:column; gap:2px; font-size:14px; }
      .tesp-porta b { font-weight:600; }
      .tesp-porta small { color:#667085; font-size:12px; }
      .tesp-selo { display:inline-block; margin-left:6px; background:#e7f7ed; color:#16a34a; font-size:11px; padding:1px 6px; border-radius:99px; font-weight:600; vertical-align:1px; }
      .tesp-vazio { color:#667085; font-size:13px; padding:8px 2px; }
      .tesp-msg { font-size:13px; min-height:1.2em; margin:12px 0 0; }
      .tesp-msg.ok { color:#16a34a; } .tesp-msg.erro { color:#dc2626; } .tesp-msg.aviso { color:#b45309; }
      .tesp-botoes { display:flex; gap:8px; margin-top:16px; flex-wrap:wrap; }
      .tesp-botoes button { font:inherit; cursor:pointer; border:0; border-radius:10px; padding:8px 14px; background:#2f6fed; color:#fff; font-weight:600; font-size:14px; }
      .tesp-botoes button.tesp-sec { background:#e8f0ff; color:#2f6fed; }
      .tesp-botoes button:disabled { opacity:.6; cursor:default; }`;
    document.head.appendChild(s);
  },

  async sair() {
    try { await Trilho.api("POST", "/api/auth/logout"); } catch { /* já saiu */ }
    location.href = "/login";
  },
};

// Marca do Trilho no canto superior esquerdo de TODAS as páginas, sempre no mesmo lugar e tamanho
// da tela inicial (e levando de volta a ela). Nas páginas com barra de topo, o título fica ao lado.
Trilho.montarMarca = function () {
  if (!document.getElementById("estilo-marca")) {
    const st = document.createElement("style");
    st.id = "estilo-marca";
    st.textContent = `
      .topo { min-height: 66px; box-sizing: border-box; padding-top: 10px !important; padding-bottom: 10px !important;
              padding-left: 28px !important; }
      .topo-esq { display: flex; align-items: center; gap: 18px; min-width: 0; }
      .topo-esq h1 { border-left: 1px solid #e3e7ef; padding-left: 18px; margin: 0; font-size: 18px !important;
                     font-weight: 600; color: #344054; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      a.marca-trilho { display: inline-flex; align-items: center; font-size: 22px; font-weight: 700; color: #1d2433;
                       text-decoration: none; line-height: 1; flex: none; }
      a.marca-trilho:hover, a.marca-trilho:focus-visible { opacity: .8; }
      a.marca-trilho .pontos { width: 14px; height: 14px; border-radius: 50%; background: #2f6fed; flex: none;
                               box-shadow: 18px 0 0 #16a34a, 36px 0 0 #f59e0b; margin-right: 50px; }
      a.marca-trilho.solta { position: absolute; top: 22px; left: 28px; }
      html.baixa-visao .topo-esq h1 { color: #000; border-left-color: #000; }
      html.baixa-visao a.marca-trilho { color: #000; }
      @media (max-width: 700px) { .topo-esq h1 { display: none; } }
      .topo .direita button.botao-contraste { margin-top: 0; }
      .avatar { display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; overflow: hidden;
                color: #fff; font-weight: 700; flex: none; letter-spacing: .5px; box-shadow: 0 0 0 2px #fff, 0 0 0 3px #e3e7ef; }
      .avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
      a.conta-chip { display: inline-flex; align-items: center; gap: 13px; padding: 4px 4px 4px 12px; border-radius: 999px;
                     text-decoration: none; color: #1d2433; border: 1px solid transparent; transition: background .15s, border-color .15s; }
      a.conta-chip:hover, a.conta-chip:focus-visible, a.conta-chip.atual { background: #f2f5fb; border-color: #e3e7ef; outline: none; }
      a.conta-chip .conta-texto { display: flex; flex-direction: column; align-items: flex-end; text-align: right; line-height: 1.2; min-width: 0; }
      a.conta-chip .conta-texto b { font-size: 14px; font-weight: 600; white-space: nowrap; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
      a.conta-chip .conta-texto small { font-size: 12px; color: #667085; white-space: nowrap; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
      html.baixa-visao a.conta-chip { color: #000; }
      html.baixa-visao a.conta-chip .conta-texto small { color: #000; }
      @media (max-width: 900px) { a.conta-chip .conta-texto { display: none; } a.conta-chip { padding: 2px; } }`;
    document.head.appendChild(st);
  }
  if (document.querySelector("a.marca-trilho")) return;
  const marca = document.createElement("a");
  marca.className = "marca-trilho"; marca.href = "/"; marca.title = "Tela inicial";
  marca.setAttribute("aria-label", "Trilho — tela inicial");
  marca.innerHTML = '<span class="pontos"></span>Trilho';
  const topo = document.querySelector(".topo");
  if (!topo) {                                   // páginas sem barra (login): solta no mesmo ponto
    marca.classList.add("solta");
    document.body.prepend(marca);
    return;
  }
  const antiga = topo.querySelector(":scope > .logo");
  if (antiga) { antiga.replaceWith(marca); return; }    // tela inicial: mesma marca das outras
  const esq = document.createElement("div");
  esq.className = "topo-esq";
  esq.appendChild(marca);
  const titulo = topo.querySelector(":scope > h1");
  if (titulo) esq.appendChild(titulo);
  topo.prepend(esq);
};
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", Trilho.montarMarca);
else Trilho.montarMarca();

// aplica a preferência deste computador assim que a página carrega
Trilho.aplicarBaixaVisao(Trilho.baixaVisao());

// ---------------------------------------------------------------- tela cheia (F11)
// No F11 do navegador, passar o mouse no alto da tela faz o navegador mostrar a barra dele (ou o
// botão de sair da tela cheia) POR CIMA do cabeçalho do site, e os botões do cabeçalho deixam de
// responder. Por isso o F11 aqui usa a tela cheia da própria página, que não mostra nada por cima.
// Ao trocar de página o navegador sai da tela cheia; ela volta no primeiro clique da página nova.
(function () {
  const CHAVE = "trilho_tela_cheia";
  const guardar = v => { try { sessionStorage.setItem(CHAVE, v); } catch (e) { /* sem armazenamento */ } };
  const desejada = () => { try { return sessionStorage.getItem(CHAVE) === "1"; } catch (e) { return false; } };
  let saindoDaPagina = false;
  function entrar() {
    const el = document.documentElement;
    if (!document.fullscreenElement && el.requestFullscreen) el.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
  }
  Trilho.alternarTelaCheia = function () {
    if (document.fullscreenElement) { guardar("0"); document.exitFullscreen().catch(() => {}); }
    else { guardar("1"); entrar(); }
  };
  document.addEventListener("keydown", e => {
    if (e.key === "F11") { e.preventDefault(); Trilho.alternarTelaCheia(); }
  });
  // saiu com Esc (ou pelo navegador): não volta sozinha; trocou de página: volta no primeiro clique
  window.addEventListener("beforeunload", () => { saindoDaPagina = true; });
  window.addEventListener("pagehide", () => { saindoDaPagina = true; });
  document.addEventListener("fullscreenchange", () => {
    if (!saindoDaPagina) guardar(document.fullscreenElement ? "1" : "0");
  });
  if (desejada()) document.addEventListener("pointerdown", entrar, { once: true, capture: true });
})();
