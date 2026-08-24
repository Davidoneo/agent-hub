/* Agent Hub — frontend senza framework. */
"use strict";

let BOOT = null;

// Gli account Unix arrivano dal bootstrap: il frontend non li cabla piu'.
// I fallback valgono solo finche' il bootstrap non e' ancora arrivato.
const projectUser = () => (BOOT && BOOT.unix_users && BOOT.unix_users.project) || "devagent";
const serverUser  = () => (BOOT && BOOT.unix_users && BOOT.unix_users.server)  || "hostagent";
const serverHome  = () => (BOOT && BOOT.server_home) || `/home/${serverUser()}`;
const serviceUser = () => (BOOT && BOOT.unix_users && BOOT.unix_users.service) || "agenthub";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const view = () => $("#view");

// La delega a sub agent non dipende dall'harness del coordinatore: il bridge
// `agent-executor` e' un comando di shell e qualsiasi profilo puo' invocarlo.
function supportsSubagents(profileId) {
  return true;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function help(text) {
  return `<div class="help">${esc(text)}</div>`;
}

function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = kind;
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.style.display = "none"; }, kind === "err" ? 9000 : 3500);
}

// --------------------------------------------------------------- errori
//
// Nessun riquadro rosso vuoto: ogni errore porta con se' codice HTTP,
// `detail` del backend, operazione fallita e — quando possibile — un
// suggerimento su cosa fare.

class ApiError extends Error {
  constructor(status, detail, operation, code, sessionId) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
    this.operation = operation;
    this.code = code || "";
    this.sessionId = sessionId || "";
  }
  get hint() {
    if (this.code === "csrf") return "Ricarica la pagina: la sessione del backend è cambiata.";
    if (this.status === 403) return "Accedi tramite Tailscale Serve con l'identità autorizzata.";
    if (this.status === 404) return "L'oggetto richiesto non esiste più: torna alla dashboard.";
    if (this.status === 0) return "Il backend non risponde: controlla agent-hub.service.";
    if (/[Dd]irectory inesistente/.test(this.detail || "")) {
      return "Usa il selettore di directory per costruire un percorso valido.";
    }
    return "";
  }
  render() {
    return `<div class="errbox">
      <b>Errore ${this.status || "di rete"} — ${esc(this.operation || "operazione")}</b>
      <div>${esc(this.detail || this.message || "nessun dettaglio fornito dal backend")}</div>
      ${this.hint ? `<div class="hint">${esc(this.hint)}</div>` : ""}
    </div>`;
  }
  toString() {
    return `[${this.status}] ${this.operation}: ${this.detail || this.message}`;
  }
}

function showError(err, where) {
  const box = where || $("#banner");
  const e = err instanceof ApiError ? err : new ApiError(0, String(err && err.message || err), "operazione");
  box.innerHTML = e.render();
  toast(e.toString(), "err");
  return e;
}

function clearError() { $("#banner").innerHTML = ""; }

async function refreshBoot() {
  const r = await fetch("/api/bootstrap", { headers: { Accept: "application/json" }, cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, data.detail || data.error, "bootstrap", data.code);
  const loaded = document.querySelector('meta[name="agent-hub-asset-version"]')?.content || "";
  if (loaded && data.asset_version && loaded !== data.asset_version) {
    location.reload();
  }
  BOOT = data;                        // il bootstrap corrente sostituisce sempre il token
  $("#who").textContent = BOOT.user;
  return BOOT;
}

async function rawApi(path, opts, operation) {
  const o = Object.assign({ headers: {} }, opts);
  if (o.body !== undefined && typeof o.body !== "string" && !(o.body instanceof FormData)) {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  if (o.method && o.method !== "GET") o.headers["X-CSRF-Token"] = (BOOT && BOOT.csrf) || "";
  let r;
  try {
    r = await fetch(path, o);
  } catch (e) {
    throw new ApiError(0, "il backend non ha risposto (" + e.message + ")", operation);
  }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json().catch(() => ({})) : await r.text();
  if (!r.ok) {
    const detail = (data && (data.detail || data.error)) || (typeof data === "string" ? data.slice(0, 300) : "");
    const code = (data && data.code) || r.headers.get("X-Agent-Hub-Error-Code") || "";
    const sessionId = r.headers.get("X-Agent-Hub-Session-Id") || "";
    throw new ApiError(r.status, detail, operation, code, sessionId);
  }
  return data;
}

// Un solo retry dopo un errore CSRF: si rinnova il bootstrap e si riprova.
async function api(path, opts = {}, operation) {
  operation = operation || `${(opts.method || "GET")} ${path}`;
  try {
    return await rawApi(path, opts, operation);
  } catch (e) {
    if (e instanceof ApiError && e.status === 403 && e.code === "csrf") {
      await refreshBoot();
      return await rawApi(path, opts, operation);  // massimo un tentativo aggiuntivo
    }
    throw e;
  }
}

function post(path, body, operation) { return api(path, { method: "POST", body: body || {} }, operation); }
function del(path, operation) { return api(path, { method: "DELETE" }, operation); }

// Le notifiche Telegram ordinarie non servono mentre l'utente sta gia'
// lavorando in Agent Hub. Segnaliamo soltanto interazioni reali e al massimo
// una volta ogni 30 secondi; una scheda lasciata aperta non conta come attiva.
let lastActivityPing = 0;
function recordActivity(force = false) {
  if (!BOOT || document.hidden) return;
  const t = Date.now();
  if (!force && t - lastActivityPing < 30_000) return;
  lastActivityPing = t;
  post("/api/activity", {}, "presenza UI").catch(() => {});
}

// Stati delle riunioni in italiano, mappati sui tag CSS esistenti.
const MEETING_STATUS = {
  queued:              ["In coda",                "pending"],
  transcribing:        ["Trascrizione in corso",  "running"],
  transcribed:         ["Trascrizione completata", "running"],
  analyzing:           ["Analisi in corso",       "running"],
  awaiting_approval:   ["In attesa di approvazione", "launching"],
  revision_requested:  ["Modifiche richieste",    "idle"],
  revising:            ["Revisione in corso",     "running"],
  approved:            ["Approvato",              "done"],
  implementing:        ["Aggiornamento documentazione", "running"],
  completed:           ["Completato",             "done"],
  failed:              ["Fallito",                "failed"],
};

// Stati non terminali: il polling continua finché almeno una riunione
// del progetto selezionato si trova in uno di questi.
const MEETING_ACTIVE = new Set([
  "queued", "transcribing", "analyzing", "awaiting_approval",
  "revision_requested", "revising", "approved", "implementing",
]);

function meetingRow(m, opts = {}) {
  const [label, cls] = MEETING_STATUS[m.status] || [m.status || "sconosciuto", "ended"];
  const approvals = m.approval_count != null ? m.approval_count : 0;
  const project = opts.showProject && m.project_slug
    ? `<span class="tag ended">${esc(m.project_slug)}</span>` : "";
  return `<div class="list-item meeting-row">
    <div class="grow">
      <div class="row">
        <span class="tag ${cls}">${esc(label)}</span>${project}
        <b>${esc(m.title || "senza titolo")}</b>
      </div>
      <div class="muted">${esc(m.meeting_date ? ts(m.meeting_date) : "—")} · Round ${
        esc(String(m.round || 1))} · approvazioni ${approvals}/2</div>
    </div>
    <div class="row">
      <a class="plain" href="#/meetings/${encodeURIComponent(m.id)}"><button class="small">Apri</button></a>
      ${m.analysis_session_id ? `<a class="plain" href="#/session/${encodeURIComponent(m.analysis_session_id)}"><button class="small">Analisi</button></a>` : ""}
      ${m.implementation_session_id ? `<a class="plain" href="#/session/${encodeURIComponent(m.implementation_session_id)}"><button class="small">Documentazione</button></a>` : ""}
    </div>
  </div>`;
}

// ------------------------------------------------------------------ router

const ROUTES = [
  [/^\/$/, viewDashboard],
  [/^\/meetings$/, viewMeetings],
  [/^\/meetings\/([^/?]+)/, viewMeeting],
  [/^\/projects$/, viewProjects],
  [/^\/projects\/([^/?]+)/, viewProject],
  [/^\/documents/, viewDocuments],
  [/^\/new/, viewNewSession],
  [/^\/accounts$/, viewAccounts],
  [/^\/status$/, viewStatus],
  [/^\/session\/([^/?]+)/, viewSession],
];

let cleanup = null;

async function route() {
  if (cleanup) { try { cleanup(); } catch (e) { /* noop */ } cleanup = null; }
  clearError();
  const hash = location.hash.replace(/^#/, "") || "/";
  const base = "#" + hash.split("?")[0];
  // Le riunioni sono una sottosezione dei progetti, non una destinazione
  // globale. Anche i dettagli progetto mantengono quindi attiva la tab padre.
  const navBase = base.startsWith("#/meetings") ? "#/projects" : base;
  $$("#nav a").forEach(a => {
    const href = a.getAttribute("href");
    const nested = href !== "#/" && navBase.startsWith(href + "/");
    a.classList.toggle("active", href === navBase || nested);
  });
  for (const [re, fn] of ROUTES) {
    const m = hash.split("?")[0].match(re);
    if (m) {
      view().innerHTML = '<p class="muted">Caricamento…</p>';
      try { await fn(...m.slice(1)); } catch (e) {
        view().innerHTML = "";
        showError(e, view());
      }
      return;
    }
  }
  view().innerHTML = '<div class="card">Pagina non trovata.</div>';
}

function qparams() {
  return new URLSearchParams((location.hash.split("?")[1] || ""));
}

// Provider raggiungibili da OpenCode. Sono gli stessi che il backend usa per
// i sub agent (EXECUTOR_PROVIDERS in main.py): la scelta del modello
// principale del profilo OpenCode e quella del sub agent leggono così lo
// stesso catalogo e non possono mostrare insiemi diversi.
const OPENCODE_PROVIDERS = ["deepseek", "opencode"];

// Il catalogo è per utente Unix: le credenziali di un provider vivono nella
// home di chi esegue l'harness, quindi «disponibile» non è una proprietà del
// server ma di quella coppia utente/provider.
function catalogGroups(unixUser, profileId) {
  const groups = (BOOT && BOOT.catalog && BOOT.catalog[unixUser]) || [];
  const providers = profileId === "codex-openai" ? ["codex"]
    : profileId === "claude-anthropic" ? ["claude"]
    : profileId === "opencode-universal" ? OPENCODE_PROVIDERS : [];
  return groups.filter(g => providers.includes(g.provider));
}

function catalogModels(unixUser, profileId) {
  return catalogGroups(unixUser, profileId).flatMap(g => g.models || []);
}

// Stato del catalogo, con la distinzione che conta: «non verificabile ora»
// (elenco probabilmente ancora valido) non è «non utilizzabile» (nessuna
// credenziale, quindi nessun modello da offrire).
const CATALOG_STATE = {
  available: ["verificato", "ok", "elenco letto dal provider con le credenziali di questo utente"],
  unavailable: ["non utilizzabile", "err", "il provider non ha credenziali per questo utente: nessun modello da offrire"],
  unverified: ["non verificato ora", "warn", "l'ultimo elenco noto è mostrato ma non è stato possibile riconfermarlo"],
  never_checked: ["mai controllato", "muted", "nessuna verifica ancora eseguita"],
};

// Dal più confermato al meno: serve a riassumere in una riga sola lo stato di
// un profilo servito da più provider.
const CATALOG_RANK = { available: 0, unverified: 1, never_checked: 2, unavailable: 3 };

function catalogState(unixUser, profileId) {
  const groups = catalogGroups(unixUser, profileId);
  if (!groups.length) return null;
  // Un provider che non offre nulla non deve declassare lo stato di uno che
  // invece ha restituito un elenco: «non utilizzabile» accanto a 60 modelli
  // verificati sarebbe falso. Contano quindi i provider che contribuiscono
  // modelli, e solo se nessuno lo fa si guarda a tutti.
  const withModels = groups.filter(g => (g.models || []).length);
  const considered = withModels.length ? withModels : groups;
  const worst = considered.reduce((a, b) =>
    ((CATALOG_RANK[b.status] ?? 9) > (CATALOG_RANK[a.status] ?? 9) ? b : a));
  const [label, kind, why] = CATALOG_STATE[worst.status] || [worst.status, "muted", ""];
  const checked = groups.map(g => g.last_checked).filter(Boolean).sort();
  return {
    group: { ...worst, models: groups.flatMap(g => g.models || []) },
    groups, label, kind, why,
    error: groups.map(g => g.error).filter(Boolean).join(" · "),
    // la verifica più vecchia è quella che invecchia per prima: è la data
    // onesta da mostrare accanto a un elenco che unisce più provider
    last_checked: checked[0] || "",
  };
}

function catalogModelNote(unixUser, profileId, model) {
  if (!model) return "";
  const groups = catalogGroups(unixUser, profileId);
  if (!groups.length) return "";
  const owner = groups.find(g => (g.models || []).some(m => m.model_id === model));
  // il caveat che conta è quello del provider che possiede il modello
  if (owner) {
    return owner.status === "unverified"
      ? " · catalogo non riverificato: disponibilità non confermata" : "";
  }
  if (groups.some(g => g.status === "unverified")) {
    return " · catalogo non riverificato: disponibilità non confermata";
  }
  return " · modello non presente nel catalogo del provider (ritirato o mai disponibile)";
}

// Livelli di effort realmente offribili: intersezione fra quelli che
// l'harness sa passare (profilo) e quelli che il modello scelto dichiara di
// supportare (catalogo). Un modello senza effort — Haiku, per esempio — non
// deve mostrarne nessuno.
function effortLevelsFor(profile, unixUser, model) {
  const fromProfile = profile.supports_effort ? (profile.effort_levels || []) : [];
  if (!model) return { levels: fromProfile, source: "profilo" };
  const entry = catalogModels(unixUser, profile.id).find(m => m.model_id === model);
  // Un alias (opus, sonnet, …) non dice su quale modello si risolverà, quindi
  // i suoi effort non sono noti in anticipo: si mostrano quelli dell'harness.
  if (!entry || entry.kind === "alias") return { levels: fromProfile, source: "profilo" };
  // Un modello del catalogo che non dichiara alcun effort non ne ha davvero.
  if (!entry.efforts || !entry.efforts.length) return { levels: [], source: "modello" };
  return { levels: fromProfile.filter(l => entry.efforts.includes(l)), source: "modello" };
}

// Menu a tendina dei modelli, con l'ultima voce che riapre il campo libero:
// il catalogo copre i casi normali senza impedire un id che non contiene.
function modelOptions(unixUser, profileId, current) {
  const models = catalogModels(unixUser, profileId);
  const known = models.some(m => m.model_id === current);
  return [
    `<option value="">(default del piano)</option>`,
    ...models.map(m => `<option value="${esc(m.model_id)}"${m.model_id === current ? " selected" : ""}>${
      esc(m.display_name && m.display_name !== m.model_id ? `${m.model_id} — ${m.display_name}` : m.model_id)}</option>`),
    current && !known
      ? `<option value="${esc(current)}" selected>${esc(current)} — non nel catalogo</option>` : "",
    `<option value="__other__">altro… (scrivi l'id a mano)</option>`,
  ].filter(Boolean).join("");
}

const EXECUTOR_DEFAULT_MODEL = "deepseek/deepseek-v4-flash";

// I sub agent girano sempre come devagent tramite `agent-executor`, che
// instrada il modello alla CLI del suo provider (`opencode run`, `claude -p`,
// `codex exec`). Delegabile è quindi tutto il catalogo verificato di devagent:
// gli stessi modelli che può usare come coordinatore, né più né meno. Niente
// elenco di scorta scritto a mano: una lista compilata qui invecchierebbe in
// silenzio e finirebbe per offrire modelli ritirati o mai esistiti.
// Deve restare allineato a EXECUTOR_PROVIDERS in main.py.
const EXECUTOR_PROVIDERS = ["claude", "codex", "deepseek", "opencode"];
// Gli alias (opus, sonnet, …) valgono solo dove la CLI li risolve.
const EXECUTOR_ALIAS_PROVIDERS = ["claude"];

// Il prefisso non è decorazione: è ciò che dice ad agent-executor quale CLI
// lanciare. I provider OpenCode nominano già così i propri modelli.
function executorModelId(provider, modelId) {
  return String(modelId).startsWith(provider + "/") ? modelId : provider + "/" + modelId;
}

function executorModels() {
  const groups = (BOOT && BOOT.catalog && BOOT.catalog.devagent) || [];
  return groups.filter(g => EXECUTOR_PROVIDERS.includes(g.provider)).flatMap(g =>
    (g.models || [])
      .filter(m => m.kind !== "alias" || EXECUTOR_ALIAS_PROVIDERS.includes(g.provider))
      .map(m => ({
        provider: g.provider, group: g.label,
        model_id: executorModelId(g.provider, m.model_id),
        display_name: m.display_name,
      })));
}

function executorDefaultModel() {
  const models = executorModels();
  if (models.some(m => m.model_id === EXECUTOR_DEFAULT_MODEL)) return EXECUTOR_DEFAULT_MODEL;
  return models.length ? models[0].model_id : "";
}

function executorModelOptions(current) {
  const models = executorModels();
  if (!models.length) {
    return `<option value="">nessun modello disponibile per devagent</option>`;
  }
  const chosen = models.some(m => m.model_id === current) ? current : executorDefaultModel();
  // raggruppati per provider: con tre harness in elenco, sapere da quale piano
  // arriva un modello è parte della scelta
  const byGroup = new Map();
  for (const m of models) {
    if (!byGroup.has(m.provider)) byGroup.set(m.provider, { label: m.group || m.provider, items: [] });
    byGroup.get(m.provider).items.push(m);
  }
  return Array.from(byGroup.values()).map(g => `<optgroup label="${esc(g.label)}">${
    g.items.map(m => `<option value="${esc(m.model_id)}"${m.model_id === chosen ? " selected" : ""}>${
      esc(m.display_name && m.display_name !== m.model_id ? `${m.model_id} — ${m.display_name}` : m.model_id)
    }</option>`).join("")}</optgroup>`).join("");
}

// --------------------------------------------------------------- dashboard

function envLabel(s) {
  return s.environment === "SERVER" ? "SERVER — privileged" : "PROJECT — rootless";
}

// Stato deterministico: quello dichiarato dall'agente con `agent-report`
// (COMPLETED, NEEDS_INPUT, …) oppure quello dedotto dal controller da fatti
// verificabili (CRASHED, ENDED_UNREPORTED, POSSIBLY_STALLED, AUTH_REQUIRED,
// USAGE_LIMIT). Il pallino distingue le due origini.
const LIFECYCLE = {
  COMPLETED: ["Completato", "done"],
  NEEDS_INPUT: ["Attende input", "launching"],
  WAITING_SESSION: ["Attende un'altra sessione", "idle"],
  NEEDS_HOST_ACTION: ["Richiede azione host", "server"],
  FAILED: ["Fallito (dichiarato)", "failed"],
  CANCELLED: ["Annullato", "ended"],
  CRASHED: ["Crash", "failed"],
  ENDED_UNREPORTED: ["Concluso senza report", "unreported"],
  POSSIBLY_STALLED: ["Forse bloccato", "stalled"],
  AUTH_REQUIRED: ["Login richiesto", "failed"],
  USAGE_LIMIT: ["Limite d'uso", "launching"],
  RUNNING: ["Al lavoro", "running"],
  STARTING: ["Avvio", "starting"],
  LAUNCH_FAILED: ["Avvio fallito", "failed"],
};

function lifecycleTag(s) {
  if (!s.lifecycle) return "";
  let [label, cls] = LIFECYCLE[s.lifecycle] || [s.lifecycle, "ended"];
  // COMPLETED/CANCELLED con pane vivo descrivono il turno appena concluso,
  // non la chiusura della sessione. La dicitura esplicita evita l'ambiguita'.
  if (sessionIsOpen(s) && s.lifecycle === "COMPLETED") label = "Turno completato";
  if (sessionIsOpen(s) && s.lifecycle === "CANCELLED") label = "Turno annullato";
  const origin = s.lifecycle_reported
    ? "dichiarato dall'agente con agent-report"
    : "dedotto dal controller da fatti osservabili";
  const mark = s.lifecycle_reported ? "◆" : "◇";
  return `<span class="tag life ${cls}" title="${esc(origin)}">${mark} ${esc(label)}</span>`;
}

function sessionIsOpen(s) {
  return !!s.alive || s.status === "running";
}

function sessionOpenTag(s) {
  return sessionIsOpen(s)
    ? '<span class="tag session-open" title="Il pane tmux esiste ancora">Sessione aperta</span>'
    : '<span class="tag ended" title="Il pane tmux non esiste più">Sessione chiusa</span>';
}

// Processo/sessione e risultato del turno sono due assi diversi: entrambi si
// vedono, senza esporre il comando tecnico del pane come se fosse uno stato.
function stateTags(s) {
  const out = [sessionOpenTag(s), lifecycleTag(s)].filter(Boolean);
  if (s.undelivered) out.push(`<span class="tag delivery_failed">${s.undelivered} testo/i non consegnati</span>`);
  if (s.kind === "login") out.push('<span class="tag login">LOGIN</span>');
  return out.join(" ");
}

function compactResult(text, max = 180) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  return clean.length > max ? clean.slice(0, max - 1).trimEnd() + "…" : clean;
}

// Prima vista: nome, stato, tre dati di orientamento e il riassunto dell'agente.
// Tutto il resto (percorsi, utente, timestamp) sta dietro «Dettagli», chiuso.
function sessionCard(s) {
  const envCls = s.environment === "SERVER" ? "server" : "project";
  const envShort = s.environment === "SERVER" ? "SERVER" : "PROJECT";
  return `<div class="card ${envCls}">
    <div class="row spread">
      <div class="sess-head">
        <b>${esc(s.name)}</b>
        <span class="tag ${envCls}" title="${esc(envLabel(s))}">${envShort}</span>
      </div>
      <label class="inline"><input type="checkbox" class="sel-session" value="${esc(s.id)}"> seleziona</label>
    </div>
    <div class="row" style="margin:6px 0">${stateTags(s)}</div>
    <div class="summary-line">
      <span>Progetto <b>${esc(s.project_slug || "—")}</b></span>
      <span title="Tempo in cui l'harness ha davvero elaborato: le attese di un input non contano">Lavoro <b>${esc(s.work_label || "—")}</b></span>
      <span title="Tempo trascorso dall'avvio della sessione, attese comprese">Aperta da <b>${esc(s.duration)}</b></span>
      <span>Ultimo output <b>${esc(s.output_age_label || "—")}</b></span>
      <span>Modello <b>${esc(s.model || "default")}${s.effort ? " · " + esc(s.effort) : ""}</b></span>
    </div>
    ${s.report_summary ? `<div class="session-result" title="Il riepilogo completo è nel dettaglio della sessione">${
      esc(compactResult(s.report_summary))}</div>` : ""}
    <details class="more">
      <summary>Dettagli</summary>
      <div class="kv">
        <div>Harness</div><div>${esc(s.profile_id)}${s.model ? " · " + esc(s.model) : ""}${
          s.effort ? " · effort " + esc(s.effort) : ""}</div>
        ${s.executor_enabled ? `<div>Sub agent</div><div><b>${esc(s.executor_model)}</b> · massimo ${
          esc(s.executor_max_agents)} simultanei</div>` : ""}
        <div>Ambiente</div><div>${esc(envLabel(s))}</div>
        <div>Utente Unix</div><div class="mono">${esc(s.unix_user)}</div>
        <div>Directory</div><div class="mono">${esc(s.workdir)}</div>
        <div>Avvio</div><div>${esc(ts(s.created_at))}</div>
        <div>Ultimo input</div><div>${esc(ts(s.last_input_at) || "—")}${
          s.last_delivered_at ? " · consegnato " + esc(ts(s.last_delivered_at)) : ""}</div>
        ${s.waiting_for_session ? `<div>Dipende da</div><div><a href="#/session/${
          encodeURIComponent(s.waiting_for_session)}">${esc(s.waiting_for_name || s.waiting_for_session)}</a></div>` : ""}
        ${s.report_status ? `<div>Report agente</div><div><b>${esc(s.report_status)}</b> ·
          ${esc(ts(s.reported_at))}</div>` : ""}
        ${s.exit_code && s.exit_code !== "?" ? `<div>Exit code</div><div class="mono">${esc(s.exit_code)}</div>` : ""}
      </div>
    </details>
    <div class="row" style="margin-top:10px">
      <a class="plain" href="#/session/${encodeURIComponent(s.id)}"><button class="primary small">Apri</button></a>
      <button class="small danger" data-del="${esc(s.id)}">Elimina</button>
    </div>
  </div>`;
}

async function viewDashboard() {
  const d = await api("/api/sessions", {}, "elenco sessioni");
  // La collocazione dipende esclusivamente dal pane tmux. Un report COMPLETED
  // chiude il turno, ma non archivia una sessione ancora viva.
  const open = d.sessions.filter(sessionIsOpen);
  const ended = d.sessions.filter(s => !sessionIsOpen(s));
  view().innerHTML = `
    <div class="row spread"><h2>Dashboard</h2>
      <a class="plain" href="#/new"><button class="primary small">+ Nuova sessione</button></a></div>
    ${help("PROJECT gira come devagent con Docker rootless e resta dentro /srv/agent-workspace/projects. " +
           "SERVER gira come hostagent con sudo senza password: usalo solo per operazioni sul server. " +
           "«Sessione aperta» indica che il pane tmux esiste; «Turno completato» è invece " +
           "l'esito dell'ultima richiesta e non chiude il processo.")}
    <h3>Sessioni aperte (${open.length})</h3>
    ${open.map(sessionCard).join("") || '<div class="card muted">Nessuna sessione aperta.</div>'}
    <details class="card more concluded-sessions">
      <summary><b>Sessioni chiuse (${ended.length})</b></summary>
      <div style="margin-top:10px">
        ${ended.map(sessionCard).join("") || '<div class="muted">Nessuna sessione chiusa.</div>'}
      </div>
    </details>
    ${ended.length ? `<div class="card">
      <div class="row">
        <button class="small danger" id="dash-del-sel">Elimina selezionate</button>
        <button class="small danger" id="dash-del-ended">Elimina tutte le sessioni chiuse</button>
      </div>
      ${help("L'eliminazione rimuove la riga dal database, i messaggi salvati, il log raw e la trascrizione. " +
             "Le sessioni ancora aperte non vengono toccate dall'eliminazione in blocco.")}
    </div>` : ""}`;

  $$("[data-del]").forEach(b => {
    b.onclick = async () => {
      if (!confirm("Eliminare la sessione, i suoi messaggi e i log?")) return;
      try {
        await post(`/api/sessions/${encodeURIComponent(b.dataset.del)}/action`, { action: "delete" }, "eliminazione sessione");
        toast("Sessione eliminata"); route();
      } catch (e) { showError(e); }
    };
  });
  if ($("#dash-del-sel")) {
    $("#dash-del-sel").onclick = async () => {
      const ids = $$(".sel-session:checked").map(c => c.value);
      if (!ids.length) { toast("Nessuna sessione selezionata", "err"); return; }
      if (!confirm(`Eliminare ${ids.length} sessioni selezionate?`)) return;
      try {
        const r = await post("/api/sessions/cleanup", { ids }, "eliminazione sessioni");
        toast(`Eliminate ${r.count} sessioni`); route();
      } catch (e) { showError(e); }
    };
    $("#dash-del-ended").onclick = async () => {
      if (!confirm("Eliminare TUTTE le sessioni chiuse con i relativi log?")) return;
      try {
        const r = await post("/api/sessions/cleanup", {}, "pulizia sessioni concluse");
        toast(`Eliminate ${r.count} sessioni`); route();
      } catch (e) { showError(e); }
    };
  }
}

// ---------------------------------------------------------------- progetti

const PROJECT_VISUALS = [
  { icon: "✦", tone: "violet", label: "Esplora" },
  { icon: "◈", tone: "blue", label: "Costruisci" },
  { icon: "●", tone: "coral", label: "Segui" },
  { icon: "▲", tone: "green", label: "Cresci" },
  { icon: "◆", tone: "gold", label: "Organizza" },
];

function projectVisual(index) {
  const visual = PROJECT_VISUALS[index % PROJECT_VISUALS.length];
  const hue = (index * 67 + 258) % 360;
  return Object.assign({}, visual, {
    style: `--tile:hsl(${hue} 62% 47%);--tile-soft:color-mix(in srgb, hsl(${hue} 62% 47%) 16%, var(--panel));`,
  });
}

function projectTile(p, index) {
  const visual = projectVisual(index);
  const target = `#/projects/${encodeURIComponent(p.slug)}`;
  return `<article class="project-tile project-tile--${visual.tone}" style="${visual.style}">
    <a class="project-tile-main" href="${target}" aria-label="Apri progetto ${esc(p.slug)}">
      <span class="project-tile-icon" aria-hidden="true">${visual.icon}</span>
      <span class="project-tile-label">${esc(visual.label)}</span>
      <span class="project-tile-name">${esc(p.slug)}</span>
      <span class="project-tile-path mono">${esc(p.path)}</span>
    </a>
    <div class="project-tile-meta">
      <span>${p.documents || 0} document${p.documents === 1 ? "o" : "i"}</span>
      <span>${p.meetings || 0} riunion${p.meetings === 1 ? "e" : "i"}</span>
      <span>${p.context_ready ? "contesto pronto" : "contesto da generare"}</span>
    </div>
    <div class="project-tile-actions">
      <a href="${target}">Apri <span aria-hidden="true">→</span></a>
      <a href="#/new?project=${encodeURIComponent(p.slug)}">Nuova sessione</a>
    </div>
  </article>`;
}

async function viewProjects() {
  const d = await api("/api/projects", {}, "elenco progetti");
  const list = d.projects.map(projectTile).join("");

  const unreg = d.unregistered.map(s => `<div class="card">
      <div class="row spread"><b class="mono">${esc(s)}</b>
      <button class="small" data-register="${esc(s)}">Registra</button></div>
      <div class="muted">Directory presente sotto projects/ ma non registrata.</div></div>`).join("");

  view().innerHTML = `<div class="projects-heading">
      <div><h2>I tuoi progetti</h2><p class="muted">Apri un progetto per accedere alle sue schede, comprese le riunioni.</p></div>
      <span class="projects-count">${d.projects.length}</span>
    </div>
    ${list ? `<section class="project-grid" aria-label="Progetti registrati">${list}</section>`
      : '<div class="card muted">Nessun progetto registrato.</div>'}
    ${unreg ? `<details class="card"><summary><b>Directory non registrate</b></summary>
      <div style="margin-top:10px">${unreg}</div></details>` : ""}
    <details class="card project-admin">
      <summary><b>Aggiungi o registra un progetto</b></summary>
      <h3>Nuovo progetto</h3>
      <label>Slug (minuscole, cifre, trattini)</label>
      <input id="np-slug" placeholder="mio-progetto">
      <label>Documenti da caricare subito nel progetto (opzionale)</label>
      <input type="file" id="np-files" multiple>
      <div class="row" style="margin-top:10px"><button class="primary" id="np-go">Crea progetto</button></div>
      ${help("Crea la directory come devagent, inizializza Git e copia il template (AGENTS.md, CLAUDE.md, " +
             ".agent/HANDOFF.md, docs/DECISIONS.md, .gitignore). I documenti finiscono in docs/input/.")}
      <h3>Clona progetto</h3>
      <label>Slug</label><input id="cl-slug" placeholder="repo-clonato">
      <label>URL repository</label>
      <input id="cl-url" placeholder="https://github.com/owner/repo.git oppure github-&lt;slug&gt;:owner/repo.git">
      <label>Deploy key dedicata (opzionale, per repo privati)</label>
      <input id="cl-key" placeholder="nome chiave = slug usato in 'Genera deploy key'">
      <div class="row" style="margin-top:10px">
        <button class="primary" id="cl-go">Clona</button>
        <button id="cl-key-go">Genera deploy key read-only</button>
      </div>
      ${help("Repository pubblici: HTTPS senza credenziali. Repository privati: genera la deploy key, " +
             "aggiungila su GitHub SENZA write access, poi clona con github-<slug>:owner/repo.git.")}
      <pre id="cl-out" style="display:none"></pre>
      <h3>Registra directory esistente</h3>
      <label>Slug della directory sotto /srv/agent-workspace/projects</label>
      <input id="rg-slug" placeholder="directory-esistente">
      <div class="row" style="margin-top:10px"><button id="rg-go">Registra</button></div>
    </details>`;

  $("#np-go").onclick = async () => {
    const slug = $("#np-slug").value.trim();
    $("#np-go").disabled = true;
    try {
      const r = await post("/api/projects", { action: "new", slug }, "creazione progetto");
      const files = $("#np-files").files;
      if (files && files.length) await uploadFiles(files, r.slug);
      toast("Progetto creato: " + r.slug);
      location.hash = "#/projects/" + encodeURIComponent(r.slug);
    } catch (e) { showError(e); $("#np-go").disabled = false; }
  };
  $("#cl-go").onclick = async () => {
    $("#cl-go").disabled = true;
    try {
      await post("/api/projects", {
        action: "clone", slug: $("#cl-slug").value.trim(),
        repo_url: $("#cl-url").value.trim(), ssh_key: $("#cl-key").value.trim(),
      }, "clone repository");
      toast("Clone completato"); route();
    } catch (e) { showError(e); $("#cl-go").disabled = false; }
  };
  $("#cl-key-go").onclick = async () => {
    try {
      const r = await post("/api/projects", { action: "deploykey", slug: $("#cl-slug").value.trim() }, "generazione deploy key");
      const out = $("#cl-out");
      out.style.display = "block";
      out.textContent =
        "Chiave pubblica da aggiungere in GitHub → repo → Settings → Deploy keys\n" +
        "(NON selezionare 'Allow write access'):\n\n" + r.public_key +
        "\n\nAlias SSH configurato: " + r.ssh_alias +
        "\nURL di clone da usare: " + r.clone_url_hint +
        "\nFile chiave: " + r.path;
      $("#cl-key").value = r.slug;
      toast("Deploy key generata");
    } catch (e) { showError(e); }
  };
  $("#rg-go").onclick = async () => {
    try {
      await post("/api/projects", { action: "register", slug: $("#rg-slug").value.trim() }, "registrazione progetto");
      toast("Registrato"); route();
    } catch (e) { showError(e); }
  };
  $$("[data-register]").forEach(b => {
    b.onclick = async () => {
      try {
        await post("/api/projects", { action: "register", slug: b.dataset.register }, "registrazione progetto");
        toast("Registrato"); route();
      } catch (e) { showError(e); }
    };
  });
}

let openDocContextMenu = null;

function closeDocContextMenu() {
  if (!openDocContextMenu) return;
  openDocContextMenu.cleanup();
  openDocContextMenu = null;
}

function showDocContextMenu(x, y, path, name) {
  closeDocContextMenu();
  const menu = document.createElement("div");
  menu.className = "doc-context-menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = `<div class="doc-context-title">${esc(name)}</div>
    <div class="mono muted doc-context-path">${esc(path)}</div>
    <button class="small" type="button" data-context-copy>Copia percorso</button>`;
  document.body.appendChild(menu);
  const box = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - box.width - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - box.height - 8)) + "px";

  const outside = event => { if (!menu.contains(event.target)) closeDocContextMenu(); };
  const key = event => { if (event.key === "Escape") closeDocContextMenu(); };
  const scroll = () => closeDocContextMenu();
  const cleanup = () => {
    menu.remove();
    document.removeEventListener("pointerdown", outside);
    document.removeEventListener("keydown", key);
    document.removeEventListener("scroll", scroll, true);
  };
  openDocContextMenu = { cleanup };
  document.addEventListener("pointerdown", outside);
  document.addEventListener("keydown", key);
  document.addEventListener("scroll", scroll, true);
  $("[data-context-copy]", menu).onclick = async () => {
    await copyText(path, "Percorso copiato");
    closeDocContextMenu();
  };
  $("[data-context-copy]", menu).focus();
}

function docRow(d, opts = {}) {
  const modified = d.modified_at ? ts(d.modified_at) : "data non disponibile";
  return `<div class="list-item doc-row" tabindex="0" data-doc-path="${esc(d.path)}"
      data-doc-name="${esc(d.name)}" title="Click destro per percorso e azioni file">
    ${opts.select ? `<label class="doc-pick"><input type="checkbox" data-pick="${esc(d.id)}"></label>` : ""}
    <div class="grow">
      <div><b>${esc(d.name)}</b></div>
      <div class="muted">Ultima modifica ${esc(modified)} · ${fmtSize(d.size)}</div>
    </div>
    <a class="plain" href="/api/documents/${encodeURIComponent(d.id)}/download" download><button class="small">Download</button></a>
    ${opts.assign ? `<button class="small" data-assign="${esc(d.id)}">Associa…</button>` : ""}
    <button class="small danger" data-docdel="${esc(d.id)}">Elimina</button>
  </div>`;
}

// Dialogo applicativo: non usa confirm()/prompt(), che il browser puo'
// sopprimere in modo permanente con «non mostrare piu'» lasciando la
// funzionalita' muta. Risolve con la scelta dell'utente, mai per timeout.
function modal(title, bodyHtml, opts = {}) {
  return new Promise(resolve => {
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <h3>${esc(title)}</h3>
      <div class="modal-body">${bodyHtml}</div>
      <div class="row modal-actions">
        <button class="small" data-mod-cancel>${esc(opts.cancel || "Annulla")}</button>
        <button class="small primary" data-mod-ok>${esc(opts.ok || "Conferma")}</button>
      </div></div>`;
    const close = value => { back.remove(); document.removeEventListener("keydown", onKey); resolve(value); };
    const onKey = e => {
      if (e.key === "Escape") close(null);
      if (e.key === "Enter" && e.target.tagName !== "SELECT") { e.preventDefault(); ok(); }
    };
    const ok = () => close(opts.read ? opts.read(back) : true);
    back.querySelector("[data-mod-cancel]").onclick = () => close(null);
    back.querySelector("[data-mod-ok]").onclick = ok;
    back.onclick = e => { if (e.target === back) close(null); };
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
    const first = back.querySelector("select,input,button.primary");
    if (first) first.focus();
  });
}

// Un solo dialogo: spiega che cosa succede e lo esegue. Nessuna conferma
// aggiuntiva, che e' il passaggio che si perdeva quando il browser
// sopprimeva i dialoghi nativi.
async function assignDocuments(ids, projects, defaults = {}) {
  const slugs = (projects || []).map(p => p.slug);
  if (!slugs.length) { toast("Nessun progetto registrato", "err"); return false; }
  if (!ids.length) { toast("Nessun documento selezionato", "err"); return false; }
  const many = ids.length > 1;
  const choice = await modal(
    many ? `Associa ${ids.length} documenti al repository` : "Associa il documento al repository",
    `<label>Progetto di destinazione</label>
     <select data-mod-slug>${slugs.map(s =>
        `<option value="${esc(s)}"${s === defaults.slug ? " selected" : ""}>${esc(s)}</option>`).join("")}</select>
     <div class="help" style="margin-top:10px">${many ? "I file entrano" : "Il file entra"} nell'albero Git del progetto e
       ${many ? "verranno versionati" : "verra' versionato"} al primo commit. Il documento viene spostato: il percorso
       cambia e la copia precedente viene rimossa.</div>
     <label style="margin-top:10px">Diritti di redistribuzione del materiale</label>
     <label class="radio"><input type="radio" name="mod-lic" value="yes" checked>
       <span><b>Verificati</b><br><span class="muted">I termini e condizioni della fonte sono stati letti e consentono
         l'uso nel software. Destinazione: &lt;progetto&gt;/docs/input/</span></span></label>
     <label class="radio"><input type="radio" name="mod-lic" value="no">
       <span><b>Non ancora verificati</b><br><span class="muted">Destinazione:
         &lt;progetto&gt;/docs/input/license-unverified/ — cartella che dichiara esplicitamente il materiale come
         non garantito libero da usare, cosi' la review prima del rilascio lo intercetta.</span></span></label>`,
    { ok: many ? `Associa ${ids.length} documenti` : "Associa", read: root => ({
        slug: root.querySelector("[data-mod-slug]").value,
        licenseVerified: root.querySelector('input[name="mod-lic"]:checked').value === "yes",
      }) });
  if (!choice) return false;
  const errors = [];
  for (const id of ids) {
    try {
      await post(`/api/documents/${encodeURIComponent(id)}/assign`,
                 { project_slug: choice.slug.trim(), scope: "repository",
                   license_verified: choice.licenseVerified }, "associazione documento");
    } catch (e) { errors.push(e && e.message ? e.message : String(e)); }
  }
  if (errors.length) toast(`${errors.length} su ${ids.length} non associati: ${errors[0]}`, "err");
  else toast(`${ids.length > 1 ? ids.length + " documenti associati" : "Documento associato"} a ${choice.slug}`);
  route();
  return true;
}

function fmtSize(n) {
  n = Number(n || 0);
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KiB";
  return (n / 1024 / 1024).toFixed(1) + " MiB";
}

function pickedDocIds(root) {
  return $$("[data-pick]", root).filter(c => c.checked).map(c => c.dataset.pick);
}

function wireDocActions(root, projects, docsById = {}) {
  $$("[data-doc-path]", root).forEach(row => {
    const open = event => {
      event.preventDefault();
      event.stopPropagation();
      const box = row.getBoundingClientRect();
      const x = event.clientX || box.left + 12;
      const y = event.clientY || box.top + 12;
      showDocContextMenu(x, y, row.dataset.docPath, row.dataset.docName);
    };
    row.oncontextmenu = open;
    row.addEventListener("keydown", event => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) open(event);
    });
  });
  $$("[data-docdel]", root).forEach(b => {
    b.onclick = async () => {
      const d = docsById[b.dataset.docdel];
      const okd = await modal("Eliminare il documento?",
        `<div>Verra' rimosso dal disco e dal registro.</div>
         <div class="mono muted" style="margin-top:6px">${esc(d ? d.path : b.dataset.docdel)}</div>`,
        { ok: "Elimina" });
      if (!okd) return;
      try { await del(`/api/documents/${encodeURIComponent(b.dataset.docdel)}`, "eliminazione documento"); toast("Documento eliminato"); route(); }
      catch (e) { showError(e); }
    };
  });
  $$("[data-assign]", root).forEach(b => {
    b.onclick = () => {
      const d = docsById[b.dataset.assign] || {};
      assignDocuments([b.dataset.assign], projects, { slug: d.project_slug, scope: d.scope });
    };
  });

  // Selezione multipla: azioni cumulative e stato del contatore.
  const bar = $("#doc-bulk", root);
  const sync = () => {
    const n = pickedDocIds(root).length;
    if (bar) {
      bar.style.display = n ? "" : "none";
      const label = $("#doc-bulk-n", root);
      if (label) label.textContent = n === 1 ? "1 documento selezionato" : `${n} documenti selezionati`;
    }
    $$("[data-pick-all]", root).forEach(a => {
      const scoped = $$(`[data-pick][data-group="${a.dataset.pickAll}"]`, root);
      const on = scoped.filter(c => c.checked).length;
      a.checked = scoped.length > 0 && on === scoped.length;
      a.indeterminate = on > 0 && on < scoped.length;
    });
  };
  $$("[data-pick]", root).forEach(c => { c.onchange = sync; });
  $$("[data-pick-all]", root).forEach(a => {
    a.onchange = () => {
      $$(`[data-pick][data-group="${a.dataset.pickAll}"]`, root).forEach(c => { c.checked = a.checked; });
      sync();
    };
  });
  const assignSel = $("#doc-assign-sel", root);
  if (assignSel) assignSel.onclick = () => assignDocuments(pickedDocIds(root), projects);
  const delSel = $("#doc-del-sel", root);
  if (delSel) delSel.onclick = async () => {
    const ids = pickedDocIds(root);
    if (!ids.length) return;
    const okd = await modal(`Eliminare ${ids.length} documenti?`,
      `<div>Verranno rimossi dal disco e dal registro.</div>
       <div class="mono muted" style="margin-top:6px">${ids.map(i => esc((docsById[i] || {}).name || i)).join("<br>")}</div>`,
      { ok: `Elimina ${ids.length}` });
    if (!okd) return;
    const errors = [];
    for (const id of ids) {
      try { await del(`/api/documents/${encodeURIComponent(id)}`, "eliminazione documento"); }
      catch (e) { errors.push(e && e.message ? e.message : String(e)); }
    }
    if (errors.length) toast(`${errors.length} su ${ids.length} non eliminati: ${errors[0]}`, "err");
    else toast(`${ids.length} documenti eliminati`);
    route();
  };
  sync();
}

async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg || "Copiato");
  } catch (e) {
    toast("Copia automatica non disponibile: seleziona il testo manualmente", "err");
  }
}

async function viewProject(slug) {
  slug = decodeURIComponent(slug);
  const [d, meetingData, contextData] = await Promise.all([
    api("/api/projects/" + encodeURIComponent(slug), {}, "dettaglio progetto"),
    api("/api/meetings?project=" + encodeURIComponent(slug), {}, "riunioni progetto"),
    api("/api/projects/" + encodeURIComponent(slug) + "/context", {}, "contesto progetto"),
  ]);
  const st = d.status || {};
  const meetings = meetingData.meetings || [];
  const context = contextData.context || {};
  const packageReady = !!contextData.package_ready;
  const requestedTab = qparams().get("tab") || "overview";
  const projectTabs = new Set(["overview", "documents", "meetings", "context"]);
  const activeTab = projectTabs.has(requestedTab) ? requestedTab : "overview";
  const panelClass = tab => `project-panel${activeTab === tab ? " active" : ""}`;
  const tabLink = (tab, label) => `<a role="tab" aria-selected="${activeTab === tab}" class="${
    activeTab === tab ? "active" : ""}" href="#/projects/${encodeURIComponent(slug)}?tab=${tab}">${label}</a>`;
  const composeBtns = st.compose ? `
    <div class="row" style="margin-top:8px">
      ${["build-up", "pull-up", "restart", "stop", "down", "ps", "logs"].map(a =>
        `<button class="small" data-compose="${a}">${a}</button>`).join("")}
    </div>
    <pre id="cmp-out" style="display:none"></pre>`
    : `<div class="muted">Nessun <span class="mono">compose.yaml</span> nel progetto: le azioni di deployment
       compariranno automaticamente quando il file sarà presente.</div>`;

  view().innerHTML = `<div class="row spread"><h2>${esc(slug)}</h2>
      <a class="plain" href="#/projects"><button class="small">← Progetti</button></a></div>
    <nav class="project-tabs" aria-label="Sezioni progetto" role="tablist">
      ${tabLink("overview", "Panoramica")}
      ${tabLink("documents", `Documenti (${(d.documents || []).length})`)}
      ${tabLink("meetings", `Riunioni (${meetings.length})`)}
      ${tabLink("context", "Contesto")}
    </nav>

    <section class="${panelClass("overview")}" role="tabpanel">
      <div class="card project">
        <div class="kv">
          <div>Percorso</div><div class="mono">${esc(d.project.path)}</div>
          <div>Origine</div><div>${esc(d.project.source)}</div>
          <div>Repo</div><div class="mono">${esc(d.project.repo_url || "—")}</div>
          <div>Branch</div><div class="mono">${esc(st.branch || "—")}</div>
          <div>Dati</div><div class="mono">/srv/agent-workspace/data/${esc(slug)}</div>
          <div>Compose project</div><div class="mono">agentapp-${esc(slug)}</div>
        </div>
        <div class="row" style="margin-top:10px">
          <a class="plain" href="#/new?project=${encodeURIComponent(slug)}"><button class="primary small">Apri nuova sessione</button></a>
          <button class="small" id="pj-init">Inizializza istruzioni agenti</button>
        </div>
      </div>
      <h3>Stato Git</h3>
      <div class="card"><pre>${esc(st.short || "(working tree pulito o non un repo git)")}</pre>
        <div class="muted mono">${esc(st.remote || "nessun remote")}</div></div>
      <h3>Deployment</h3>
      <div class="card">
        <div class="muted">Docker rootless di devagent · porte host previste 12000-12999 · binding 127.0.0.1</div>
        ${composeBtns}
        <label>URL locale dichiarato</label>
        <div class="row"><input id="pj-url" value="${esc(d.project.local_url || "")}" placeholder="http://127.0.0.1:12000">
          <button class="small" id="pj-url-go">Salva</button></div>
        <label>Registro porte</label>
        <div class="row"><input id="pj-port" placeholder="12000" inputmode="numeric" style="max-width:130px">
          <input id="pj-portdesc" placeholder="descrizione" style="flex:1">
          <button class="small" id="pj-port-go">Aggiungi</button></div>
        <div style="margin-top:8px">${(d.ports || []).map(p =>
          `<div class="row spread"><span class="mono">${p.port}</span>
           <span class="muted">${esc(p.description)}</span>
           <button class="small danger" data-delport="${p.port}">Rimuovi</button></div>`).join("") ||
          '<span class="muted">Nessuna porta registrata.</span>'}</div>
      </div>
      <h3>.agent/HANDOFF.md</h3>
      <div class="card"><pre>${esc(st.handoff || "(assente)")}</pre></div>
    </section>

    <section class="${panelClass("documents")}" role="tabpanel">
      <div class="card">
        <div class="dropzone" id="pj-drop">Trascina qui i file oppure
          <input type="file" id="pj-files" multiple style="margin-top:8px"></div>
        <div class="row" style="margin-top:8px"><button class="small primary" id="pj-upload">Carica nel progetto</button></div>
        ${help("I file caricati finiscono in " + esc(d.project.path) + "/docs/input/ e appartengono a devagent:agentprojects. " +
               "Nessun file viene eseguito o interpretato automaticamente.")}
        <div style="margin-top:10px" id="pj-docs">${(d.documents || []).map(x => docRow(x)).join("") ||
          '<span class="muted">Nessun documento associato.</span>'}</div>
      </div>
    </section>

    <section class="${panelClass("meetings")}" role="tabpanel">
      <div class="row spread section-heading project-panel-heading">
        <div><h3>Riunioni del progetto</h3><div class="muted">Crea una riunione o riapri lo storico di ${esc(slug)}.</div></div>
        <a class="plain" href="#/meetings?project=${encodeURIComponent(slug)}&action=new"><button class="primary small">+ Nuova riunione</button></a>
      </div>
      <div class="card">
        ${meetings.length ? meetings.map(m => meetingRow(m)).join("")
          : '<div class="muted">Nessuna riunione per questo progetto.</div>'}
      </div>
    </section>

    <section class="${panelClass("context")}" role="tabpanel">
      <div class="card">
        <label>Architettura target consolidata</label>
        <textarea id="pj-target-arch" class="mtg-target" placeholder="Descrivi architettura, tecnologie e vincoli target…"></textarea>
        <div class="row" style="margin-top:10px">
          <button class="small primary" id="pj-pkg-update">Aggiorna package</button>
          ${packageReady
            ? `<a class="plain" href="/api/projects/${encodeURIComponent(slug)}/context/download" download><button class="small">Scarica package</button></a>`
            : '<button class="small" disabled title="Il package non è ancora stato generato">Scarica package</button>'}
        </div>
        <div class="muted" style="margin-top:8px">Il package riunisce sorgenti filtrati, decisioni approvate, target e brief LLM. Non contiene audio.</div>
        ${context.updated_at ? `<div class="muted">Ultimo aggiornamento: ${esc(ts(context.updated_at))}${
          context.trigger === "approved_meeting" ? " · da riunione approvata" : ""}</div>` : ""}
        <div id="pj-ctx-err"></div>
      </div>
    </section>`;

  const docsById = {};
  (d.documents || []).forEach(item => { docsById[item.id] = item; });
  wireDocActions(view(), [{ slug }], docsById);
  $("#pj-target-arch").value = context.target_architecture || "";
  $("#pj-pkg-update").onclick = async () => {
    const button = $("#pj-pkg-update");
    const errorBox = $("#pj-ctx-err");
    button.disabled = true;
    errorBox.innerHTML = "";
    try {
      await api(`/api/projects/${encodeURIComponent(slug)}/context`, {
        method: "POST", body: { target_architecture: $("#pj-target-arch").value || "" },
      }, "aggiornamento contesto");
      toast("Contesto aggiornato");
      route();
    } catch (e) {
      showError(e, errorBox);
      button.disabled = false;
    }
  };
  setupDrop($("#pj-drop"), $("#pj-files"));
  $("#pj-upload").onclick = async () => {
    const files = $("#pj-files").files;
    if (!files || !files.length) { toast("Nessun file selezionato", "err"); return; }
    try { await uploadFiles(files, slug); toast("Documenti caricati"); route(); }
    catch (e) { showError(e); }
  };
  $("#pj-init").onclick = async () => {
    try {
      const r = await post("/api/projects", { action: "init-instructions", slug }, "inizializzazione istruzioni");
      toast(r.created.length ? "Aggiunti: " + r.created.join(", ") : "Nessun file mancante");
      route();
    } catch (e) { showError(e); }
  };
  $("#pj-url-go").onclick = async () => {
    try { await post("/api/projects", { action: "set-local-url", slug, local_url: $("#pj-url").value }, "salvataggio URL"); toast("Salvato"); }
    catch (e) { showError(e); }
  };
  $("#pj-port-go").onclick = async () => {
    try {
      await post("/api/projects", { action: "add-port", slug, port: $("#pj-port").value, description: $("#pj-portdesc").value }, "registrazione porta");
      route();
    } catch (e) { showError(e); }
  };
  $$("[data-delport]").forEach(b => {
    b.onclick = async () => {
      try { await post("/api/projects", { action: "remove-port", slug, port: b.dataset.delport }, "rimozione porta"); route(); }
      catch (e) { showError(e); }
    };
  });
  $$("[data-compose]").forEach(b => {
    b.onclick = async () => {
      const out = $("#cmp-out");
      out.style.display = "block"; out.textContent = "esecuzione in corso…";
      b.disabled = true;
      try {
        const r = await post(`/api/projects/${encodeURIComponent(slug)}/compose`, { action: b.dataset.compose }, "compose " + b.dataset.compose);
        out.textContent = `rc=${r.rc}\n\n${r.output}`;
      } catch (e) { out.textContent = String(e); showError(e); }
      b.disabled = false;
    };
  });
}

// --------------------------------------------------------------- documenti

function setupDrop(zone, input) {
  if (!zone || !input) return;
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.add("hot");
  }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.remove("hot");
  }));
  zone.addEventListener("drop", e => {
    if (e.dataTransfer && e.dataTransfer.files.length) input.files = e.dataTransfer.files;
  });
}

async function uploadFiles(files, slug, onProgress) {
  const fd = new FormData();
  if (slug) fd.append("project_slug", slug);
  for (const f of files) fd.append("files", f, f.name);
  const send = () => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents");
    xhr.setRequestHeader("X-CSRF-Token", (BOOT && BOOT.csrf) || "");
    xhr.upload.onprogress = e => { if (onProgress && e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* noop */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(xhr.status, data.detail || data.error || xhr.responseText.slice(0, 300),
                               "upload documenti", data.code));
    };
    xhr.onerror = () => reject(new ApiError(0, "rete non disponibile durante l'upload", "upload documenti"));
    xhr.send(fd);
  });
  try {
    return await send();
  } catch (e) {
    if (e instanceof ApiError && e.status === 403 && e.code === "csrf") {
      await refreshBoot();
      return await send();   // un solo nuovo tentativo, con il token aggiornato
    }
    throw e;
  }
}

async function viewDocuments() {
  const [d, pj] = await Promise.all([
    api("/api/documents", {}, "elenco documenti"),
    api("/api/projects", {}, "elenco progetti"),
  ]);
  const free = d.documents.filter(x => !x.project_slug);
  const docsById = {};
  d.documents.forEach(x => { docsById[x.id] = x; });

  // Albero: radice per destinazione, poi un ramo per progetto.
  const branch = (key, title, docs) => {
    const slugs = [...new Set(docs.map(x => x.project_slug))].sort();
    return `<details class="tree-root" open>
      <summary><b>${esc(title)}</b> <span class="muted">${docs.length}</span></summary>
      ${slugs.map(s => {
        const items = docs.filter(x => x.project_slug === s);
        const gid = `${key}:${s}`;
        return `<details class="tree-node" open>
          <summary><label class="doc-pick" onclick="event.stopPropagation()">
              <input type="checkbox" data-pick-all="${esc(gid)}"></label>
            <b>${esc(s)}</b> <span class="muted">${items.length}</span></summary>
          ${items.map(x => docRow(x, { assign: true, select: true })
                            .replace('data-pick="', `data-group="${esc(gid)}" data-pick="`)).join("")}
        </details>`;
      }).join("") || '<div class="muted tree-empty">Nessun documento.</div>'}
    </details>`;
  };

  view().innerHTML = `<h2>Documenti</h2>
    ${help("I file non assegnati restano nell'area upload. " +
           "Associando a un progetto scegli la destinazione: knowledge privata (fuori da Git) " +
           "oppure repository (<progetto>/docs/input/, versionabile), come devagent:agentprojects. " +
           "Limite " + Math.round(d.max_upload / 1048576) + " MiB per file. Estensioni ammesse: " + d.allowed_ext.join(" "))}
    <div class="card">
      <div class="dropzone" id="up-drop">Trascina qui i file oppure
        <input type="file" id="up-files" multiple style="margin-top:8px"></div>
      <label>Progetto di destinazione (facoltativo)</label>
      <select id="up-project">
        <option value="">— nessuno: resta fra i documenti liberi —</option>
        ${pj.projects.map(p => `<option value="${esc(p.slug)}">${esc(p.slug)}</option>`).join("")}
      </select>
      <div class="row" style="margin-top:10px"><button class="primary" id="up-go">Carica</button></div>
      <div class="progress" style="margin-top:10px"><div id="up-bar"></div></div>
    </div>

    <div class="card bulk-bar" id="doc-bulk" style="display:none">
      <span class="grow"><b id="doc-bulk-n"></b></span>
      <button class="small primary" id="doc-assign-sel">Associa a progetto…</button>
      <button class="small danger" id="doc-del-sel">Elimina</button>
    </div>

    <div class="card tree">
      ${branch("repo", "Progetti",
               d.documents.filter(x => x.project_slug && x.scope === "repository"))}
      ${branch("priv", "Knowledge privata",
               d.documents.filter(x => x.project_slug && x.scope !== "repository"))}
      <details class="tree-root" open>
        <summary><b>Documenti liberi</b> <span class="muted">${free.length}</span></summary>
        ${free.length ? `<details class="tree-node" open>
            <summary><label class="doc-pick" onclick="event.stopPropagation()">
                <input type="checkbox" data-pick-all="free:-"></label>
              <b>non assegnati</b> <span class="muted">${free.length}</span></summary>
            ${free.map(x => docRow(x, { assign: true, select: true })
                             .replace('data-pick="', 'data-group="free:-" data-pick="')).join("")}
          </details>` : '<div class="muted tree-empty">Nessun documento libero.</div>'}
      </details>
    </div>`;

  setupDrop($("#up-drop"), $("#up-files"));
  wireDocActions(view(), pj.projects, docsById);
  $("#up-go").onclick = async () => {
    const files = $("#up-files").files;
    if (!files || !files.length) { toast("Seleziona almeno un file", "err"); return; }
    $("#up-go").disabled = true;
    try {
      const r = await uploadFiles(files, $("#up-project").value, p => { $("#up-bar").style.width = (p * 100) + "%"; });
      if (r.errors && r.errors.length) toast("Alcuni file non caricati: " + r.errors.join("; "), "err");
      else toast(`Caricati ${r.documents.length} documenti`);
      route();
    } catch (e) { showError(e); $("#up-go").disabled = false; }
  };
}

// ----------------------------------------------------- selettore directory

async function dirPicker(el, environment, initial, onPick) {
  let current = initial || "";
  async function load(path) {
    el.innerHTML = '<div class="path muted">caricamento…</div>';
    try {
      const d = await api(`/api/fs?environment=${encodeURIComponent(environment)}&path=${encodeURIComponent(path || "")}`,
                          {}, "elenco directory");
      current = d.path;
      onPick(current);
      el.innerHTML = `<div class="path"><span class="mono">${esc(d.path)}</span></div>
        <div class="entries">
          ${d.path !== d.parent ? '<div class="entry" data-go=".."><b>../</b> risali</div>' : ""}
          ${d.dirs.map(n => `<div class="entry" data-go="${esc(n)}">${esc(n)}/</div>`).join("") ||
            '<div class="entry muted">nessuna sottodirectory</div>'}
        </div>
        <div class="path"><button class="small primary" type="button" data-use>Usa questa directory</button></div>`;
      $$("[data-go]", el).forEach(b => {
        b.onclick = () => {
          const n = b.dataset.go;
          load(n === ".." ? d.parent : (d.path.replace(/\/$/, "") + "/" + n));
        };
      });
      $("[data-use]", el).onclick = () => { onPick(current); toast("Directory scelta: " + current); };
    } catch (e) {
      el.innerHTML = `<div class="path">${e instanceof ApiError ? e.render() : esc(String(e))}</div>`;
    }
  }
  await load(initial);
  return { reload: load, get current() { return current; } };
}

// ----------------------------------------------------------- nuova sessione

async function viewNewSession() {
  // Oltre al CSRF riallinea il catalogo: uno startup di Claude puo' avere
  // appena rinnovato l'OAuth e reso nuovamente interrogabile /v1/models.
  await refreshBoot();
  const preProject = qparams().get("project") || "";
  const [d, docs] = await Promise.all([
    api("/api/projects", {}, "elenco progetti"),
    api("/api/documents", {}, "elenco documenti"),
  ]);
  const profiles = BOOT.profiles;

  view().innerHTML = `<h2>Nuova sessione</h2>
    <div class="card">
      <label>Nome sessione</label>
      <input id="s-name" placeholder="es. rifattorizza API">

      <label>Ambiente</label>
      <select id="s-env">
        <option value="PROJECT">PROJECT — devagent / Docker rootless</option>
        <option value="SERVER">SERVER — hostagent / privileged</option>
      </select>
      ${help("PROJECT: utente devagent, nessun sudo, lavoro confinato a /srv/agent-workspace/projects. " +
             "SERVER: utente hostagent con sudo senza password — usalo solo per amministrare il server.")}

      <div id="s-project-wrap">
        <label>Progetto</label>
        <select id="s-project">
          ${d.projects.map(p => `<option value="${esc(p.slug)}"${p.slug === preProject ? " selected" : ""}>${esc(p.slug)}</option>`).join("")}
        </select>
        ${d.projects.length ? "" : '<div class="muted">Nessun progetto registrato: creane uno dalla pagina Progetti.</div>'}
      </div>

      <label>Profilo harness</label>
      <select id="s-profile">
        ${profiles.map(p => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("")}
      </select>
      <label>Modello</label>
      <select id="s-model"></select>
      <input id="s-model-other" placeholder="id del modello" style="display:none;margin-top:6px">
      <div class="muted" id="s-model-note" style="margin-top:4px"></div>
      <label>Effort di ragionamento</label>
      <select id="s-effort"></select>
      <div class="muted" id="s-effort-note" style="margin-top:4px"></div>
      ${help("L'effort viene passato alla CLI all'avvio (Claude Code: --effort, Codex: " +
             "model_reasoning_effort). Più alto significa più ragionamento, più token e più tempo. " +
             "Resta modificabile a sessione avviata dal pannello «Modello ed effort».")}
      <label>Modalità permessi</label>
      <select id="s-perm"></select>
      ${help("full = l'agente non chiede conferme (bypass approvazioni). standard = approvazioni e sandbox attive.")}

      <div class="card" id="s-executor-wrap" style="margin-top:14px">
        <label class="inline"><input type="checkbox" id="s-executor-enabled">
          Delega a sub agent per ridurre i token del coordinatore</label>
        ${help("Disponibile per qualsiasi sessione PROJECT o SERVER. I sub agent girano " +
               "sempre come devagent e restano confinati sotto /srv/agent-workspace/projects. Agent Hub aggiunge al prompt " +
               "la strategia di delega e abilita il comando agent-executor; il coordinatore conserva " +
               "decisioni, integrazione e verifica finale.")}
        <div id="s-executor-options" style="display:none">
          <div class="row">
            <div style="flex:2;min-width:260px">
              <label>Modello dei sub agent</label>
              <select id="s-executor-model">${executorModelOptions(executorDefaultModel())}</select>
            </div>
            <div style="flex:1;min-width:160px">
              <label>Sub agent simultanei massimi</label>
              <select id="s-executor-max">
                <option value="1">1</option>
                <option value="2" selected>2</option>
                <option value="3">3</option>
                <option value="4">4</option>
              </select>
            </div>
          </div>
        </div>
        <div class="muted" id="s-executor-note" style="margin-top:6px"></div>
      </div>

      <label id="s-prompt-label">Prompt iniziale (multilinea, opzionale)</label>
      <textarea id="s-prompt" placeholder="Descrivi la macro-task…"></textarea>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:6px">
        <span class="muted" style="font-size:12px">Modalità</span>
        <select id="s-mode" style="width:auto;min-width:200px;padding:6px 8px;font-size:13px"></select>
      </div>
      <div class="muted" id="s-mode-note" style="margin-top:4px;font-size:12px;text-align:right"></div>

      <label>Documenti</label>
      <div class="row">
        <button class="small" id="s-docs-toggle" type="button">Allega documenti…</button>
        <button class="small" id="s-files-pick" type="button">Carica dal dispositivo…</button>
        <span class="muted" id="s-docs-count" style="font-size:13px"></span>
      </div>
      <input type="file" id="s-files" multiple style="display:none">
      <div id="s-docs" class="picker" style="display:none;margin-top:8px;padding:8px 10px;
           max-height:220px;overflow:auto;columns:2 240px;column-gap:16px">
        ${docs.documents.map(x => `<label class="inline" style="display:flex;align-items:flex-start;
            margin:0 0 6px;break-inside:avoid">
          <input type="checkbox" class="sel-doc" value="${esc(x.id)}" style="margin-top:3px;flex:none">
          <span style="min-width:0;overflow-wrap:anywhere">${esc(x.name)}<br>
            <span class="muted mono" style="font-size:11px">${esc(x.path)}</span></span></label>`).join("") ||
          '<span class="muted">Nessun documento caricato. Vai su Documenti per aggiungerne.</span>'}
      </div>
      ${help("I percorsi completi dei documenti selezionati vengono aggiunti in fondo al prompt iniziale.")}

      <div class="row" style="margin-top:14px"><button class="primary" id="s-go">Avvia sessione</button></div>
      <div class="muted" style="margin-top:8px" id="s-hint"></div>
      <div id="s-err"></div>

      <details class="more" id="s-opts">
        <summary>Opzioni</summary>
        <label>Directory di lavoro
          <span class="help-badge" title="Puoi lasciarla vuota: verrà usata la directory predefinita">?</span></label>
        <div class="row">
          <input id="s-workdir" placeholder="(vuoto = directory predefinita)" style="flex:1">
          <button class="small" id="s-browse" type="button">Sfoglia…</button>
        </div>
        <div id="s-picker" class="picker" style="display:none;margin-top:8px"></div>
        ${help("Non serve digitare il percorso a mano: apri «Sfoglia…» e naviga. " +
               "Per PROJECT la directory resta sotto /srv/agent-workspace/projects; se lasci il campo vuoto " +
               `viene usata la radice del progetto (o ${serverHome()} per SERVER).`)}
        <div class="row">
          <div style="flex:1"><label>Colonne</label><input id="s-cols" value="100" inputmode="numeric"></div>
          <div style="flex:1"><label>Righe</label><input id="s-rows" value="30" inputmode="numeric"></div>
        </div>
        ${help("Dimensione del terminale della sessione: conta solo per come la TUI dispone il testo.")}
      </details>
    </div>`;

  const envSel = $("#s-env"), profSel = $("#s-profile");
  let picker = null;

  // Il valore effettivo del modello: la tendina, oppure il campo libero
  // quando è stata scelta la voce «altro…».
  function selectedModel() {
    const sel = $("#s-model");
    if (!sel || !sel.value) return "";
    return sel.value === "__other__" ? $("#s-model-other").value.trim() : sel.value;
  }

  // Stato del catalogo accanto alla scelta: se il provider non è utilizzabile
  // per questo utente va detto qui, non scoperto all'avvio della sessione.
  function syncModelNote(p, user) {
    const st = catalogState(user, p.id);
    const note = $("#s-model-note");
    if (!p.supports_model) { note.textContent = `${p.harness} non permette di scegliere il modello.`; return; }
    if (!st) { note.textContent = ""; return; }
    const n = (st.group.models || []).length;
    note.innerHTML = `Catalogo <b>${esc(st.label)}</b>${st.last_checked ? " · " + esc(ts(st.last_checked)) : ""} · ${
      n} modelli${st.error ? " · " + esc(st.error) : ""}<br><span class="muted">${esc(st.why)}</span>`;
  }

  // I livelli offerti seguono il modello scelto, non solo l'harness.
  function syncEffort(p, user) {
    const sel = $("#s-effort");
    const { levels, source } = effortLevelsFor(p, user, selectedModel());
    const wanted = sel.value || p.default_effort || "medium";
    sel.innerHTML = levels.length
      ? `<option value="">(default dell'harness)</option>` + levels.map(l =>
          `<option value="${esc(l)}">${esc(l)}</option>`).join("")
      : `<option value="">non disponibile per questa scelta</option>`;
    sel.value = levels.includes(wanted) ? wanted : (levels.includes("medium") ? "medium" : "");
    sel.disabled = !levels.length;
    $("#s-effort-note").textContent = levels.length
      ? (source === "modello"
          ? `Livelli dichiarati dal modello selezionato: ${levels.join(", ")}.`
          : `Livelli supportati da ${p.harness}: ${levels.join(", ")}.`)
      : (source === "modello"
          ? "Il modello selezionato non espone livelli di effort: il campo resta disattivato."
          : `${p.harness} non espone un livello di effort: il campo resta disattivato.`);
  }

  // Tre scelte, non due campi: «normale» non tocca la TUI, «plan» la porta in
  // Plan mode, «obiettivo» usa il prompt come testo di /goal. Ciò che il
  // profilo non dichiara non viene offerto: il backend lo rifiuterebbe.
  function syncModes(p) {
    const sel = $("#s-mode");
    const opts = ((p.modes || {}).options || []);
    const planNote = (opts.find(o => o.id === "plan") || {}).note || "";
    const hasPlan = opts.some(o => o.id === "plan");
    const hasGoal = !!((p.goal || {}).command);
    const keep = sel.value;
    const items = [["", "Normale"]];
    if (hasPlan) items.push(["plan", "Plan"]);
    if (hasGoal) items.push(["goal", "Obiettivo (/goal)"]);
    sel.innerHTML = items.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
    sel.value = items.some(i => i[0] === keep) ? keep : "";
    sel.disabled = items.length < 2;
    const notes = {
      "": items.length < 2
        ? `${p.harness} non espone modalità di collaborazione selezionabili.`
        : "L'agente lavora e modifica come al solito.",
      plan: planNote,
      goal: "Il prompt qui sopra diventa l'obiettivo passato a /goal: l'agente ci lavora " +
            "e lo verifica prima di fermarsi. Viene ridotto a una riga sola.",
    };
    $("#s-mode-note").textContent = notes[sel.value] || "";
    $("#s-prompt-label").textContent = sel.value === "goal"
      ? "Obiettivo della sessione (diventa il /goal)"
      : "Prompt iniziale (multilinea, opzionale)";
    $("#s-prompt").placeholder = sel.value === "goal"
      ? "Che cosa deve essere vero perché il lavoro sia finito…"
      : "Descrivi la macro-task…";
  }

  // Quanti documenti sono allegati: la lista resta chiusa, il conteggio no.
  function syncDocs() {
    const n = $$(".sel-doc:checked").length;
    const files = $("#s-files").files;
    const parts = [];
    if (n) parts.push(`${n} document${n === 1 ? "o" : "i"} dal catalogo`);
    if (files && files.length) parts.push(`${files.length} da caricare`);
    $("#s-docs-count").textContent = parts.length ? parts.join(" · ") : "nessuno allegato";
  }

  function syncExecutor() {
    const p = profiles.find(x => x.id === profSel.value);
    const models = executorModels();
    // Senza modelli verificati la delega non è offribile: il backend la
    // rifiuterebbe comunque, e una casella spuntabile a vuoto sarebbe una
    // promessa che si scopre falsa solo all'avvio della sessione.
    const available = !!(p && supportsSubagents(p.id) && models.length);
    if (!available) $("#s-executor-enabled").checked = false;
    const enabled = available && $("#s-executor-enabled").checked;
    $("#s-executor-wrap").style.display = p ? "" : "none";
    $("#s-executor-enabled").disabled = !available;
    $("#s-executor-options").style.display = enabled ? "" : "none";
    $("#s-executor-model").disabled = !enabled;
    $("#s-executor-max").disabled = !enabled;
    const perProvider = EXECUTOR_PROVIDERS
      .map(p => [p, models.filter(m => m.provider === p).length])
      .filter(([, n]) => n).map(([p, n]) => `${p} ${n}`).join(" · ");
    $("#s-executor-note").textContent = models.length
      ? `${models.length} modelli delegabili come devagent (${perProvider}): esattamente quelli ` +
        "che devagent può usare come coordinatore, e la lista si aggiorna con il catalogo. " +
        "Un sub agent Claude o Codex consuma lo stesso piano OAuth dei coordinatori — riduce il " +
        "contesto del coordinatore, non i token del piano; DeepSeek è invece a saldo separato."
      : "Nessun modello delegabile: il catalogo di devagent è vuoto per i provider raggiungibili. " +
        "Aggiornalo dalla pagina Accounts.";
  }

  function syncProfile() {
    const p = profiles.find(x => x.id === profSel.value);
    const user = envSel.value === "SERVER" ? serverUser() : projectUser();
    const keep = selectedModel();
    $("#s-model").innerHTML = modelOptions(user, p.id, keep);
    $("#s-model").disabled = !p.supports_model;
    $("#s-perm").innerHTML = Object.keys(p.permission_modes || {}).map(k =>
      `<option value="${esc(k)}"${k === p.default_permission_mode ? " selected" : ""}>${k === "full" ? "full — accesso completo (default)" : esc(k)}</option>`).join("");
    syncModelNote(p, user);
    syncEffort(p, user);
    syncModes(p);
    syncExecutor();
    const allowed = (p.allowed_users || []).includes(user);
    $("#s-go").disabled = !allowed;
    const spec = p.prompt_arg || {};
    const how = spec.mode ? "prompt iniziale passato alla CLI come argomento" : "prompt incollato nella TUI dopo il readiness";
    $("#s-hint").textContent = allowed
      ? `Sessione eseguita come ${user}, persistente in tmux (${how}).`
      : `Il profilo ${p.id} non è consentito per ${user}.`;
  }
  function syncEnv() {
    const server = envSel.value === "SERVER";
    $("#s-project-wrap").style.display = server ? "none" : "";
    $("#s-workdir").placeholder = server ? `(vuoto = ${serverHome()})` : "(vuoto = radice del progetto)";
    $("#s-picker").style.display = "none";
    picker = null;
    syncProfile();
  }
  envSel.onchange = syncEnv;
  profSel.onchange = syncProfile;
  $("#s-executor-enabled").onchange = syncExecutor;
  $("#s-mode").onchange = () => syncModes(profiles.find(x => x.id === profSel.value));
  $("#s-docs-toggle").onclick = () => {
    const box = $("#s-docs");
    const open = box.style.display === "none";
    box.style.display = open ? "" : "none";
    $("#s-docs-toggle").textContent = open ? "Chiudi elenco documenti" : "Allega documenti…";
  };
  $("#s-files-pick").onclick = () => $("#s-files").click();
  $("#s-files").onchange = syncDocs;
  $$(".sel-doc").forEach(c => { c.onchange = syncDocs; });
  syncDocs();
  $("#s-model").onchange = () => {
    const other = $("#s-model").value === "__other__";
    $("#s-model-other").style.display = other ? "" : "none";
    if (other) $("#s-model-other").focus();
    const p = profiles.find(x => x.id === profSel.value);
    syncEffort(p, envSel.value === "SERVER" ? serverUser() : projectUser());
  };
  $("#s-model-other").oninput = () => {
    const p = profiles.find(x => x.id === profSel.value);
    syncEffort(p, envSel.value === "SERVER" ? serverUser() : projectUser());
  };
  syncEnv();

  $("#s-browse").onclick = async () => {
    const box = $("#s-picker");
    if (box.style.display !== "none" && picker) { box.style.display = "none"; return; }
    box.style.display = "";
    const start = $("#s-workdir").value.trim() ||
      (envSel.value === "PROJECT"
        ? (BOOT.projects_root + "/" + ($("#s-project").value || ""))
        : serverHome());
    picker = await dirPicker(box, envSel.value, start, p => { $("#s-workdir").value = p; });
  };

  $("#s-go").onclick = async () => {
    $("#s-err").innerHTML = "";
    const files = $("#s-files").files;
    const docIds = $$(".sel-doc:checked").map(c => c.value);
    $("#s-go").disabled = true;
    try {
      if (files && files.length) {
        const up = await uploadFiles(files, envSel.value === "PROJECT" ? $("#s-project").value : "");
        (up.documents || []).forEach(x => docIds.push(x.id));
      }
      const body = {
        name: $("#s-name").value || "sessione",
        environment: envSel.value,
        project_slug: envSel.value === "PROJECT" ? ($("#s-project").value || "") : "",
        workdir: $("#s-workdir").value.trim(),
        profile_id: profSel.value,
        model: selectedModel(),
        effort: $("#s-effort").disabled ? "" : $("#s-effort").value,
        permission_mode: $("#s-perm").value,
        // «obiettivo» non è una modalità dell'harness: è il prompt usato come
        // testo di /goal, e il backend lo tratta come tale
        mode: $("#s-mode").value === "goal" ? "" : $("#s-mode").value,
        prompt_as_goal: $("#s-mode").value === "goal",
        executor_enabled: supportsSubagents(profSel.value) &&
                          $("#s-executor-enabled").checked,
        executor_model: $("#s-executor-model").value,
        executor_max_agents: parseInt($("#s-executor-max").value, 10) || 2,
        prompt: $("#s-prompt").value,
        document_ids: docIds,
        cols: parseInt($("#s-cols").value, 10) || 100,
        rows: parseInt($("#s-rows").value, 10) || 30,
      };
      const r = await post("/api/sessions", body, "creazione sessione");
      toast("Sessione avviata (consegna prompt: " + r.delivery + ")");
      location.hash = "#/session/" + r.session.id;
    } catch (e) {
      if (e instanceof ApiError && e.code === "launch_failed" && e.sessionId) {
        toast("Avvio fallito: sessione e prompt sono stati salvati", "err");
        location.hash = "#/session/" + encodeURIComponent(e.sessionId);
      } else {
        showError(e, $("#s-err"));
        $("#s-go").disabled = false;
      }
    }
  };
}

// ---------------------------------------------------------- vista sessione

const MSG_STATUS = {
  pending: "in attesa", launching: "consegna in corso", sent: "consegnato",
  delivery_failed: "consegna fallita", manually_resent: "reinviato a mano",
};

// Il backend conserva e trasmette tutto in UTC — che è la cosa giusta — ma
// leggere «08:34» quando l'orologio dice 10:34 fa sembrare rotto l'Hub. La
// conversione si fa qui, nel fuso di chi guarda: così vale sia dall'Italia sia
// dalla Danimarca (stesso fuso) sia da qualunque altro posto, senza
// configurazione. Il formato resta ISO-like, che non è ambiguo.
const TZ_LABEL = new Intl.DateTimeFormat("en-GB", { timeZoneName: "short" });

function ts(v) {
  const raw = String(v || "");
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw.replace("T", " ").replace("+00:00", " UTC");
  const zone = (TZ_LABEL.formatToParts(d).find(p => p.type === "timeZoneName") || {}).value || "";
  return `${d.toLocaleString("sv-SE")}${zone ? " " + zone : ""}`;
}

// Solo l'ora: nella barra laterale la data intera è rumore.
function hhmm(v) {
  const d = new Date(String(v || ""));
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("sv-SE", { timeStyle: "short" });
}

// ------------------------------------------------- viste su stato esterno
//
// Accounts e Status descrivono cose che vivono fuori dal servizio (sudo verso
// i wrapper, docker, systemd, tailscale, i provider). Il backend restituisce
// sempre subito l'ultimo risultato noto e dice in `meta` quanto è vecchio e se
// ne sta già costruendo uno nuovo. La pagina si apre quindi immediatamente sui
// dati vecchi, li dichiara tali, e si riaggiorna da sola quando i nuovi sono
// pronti: mai un'attesa a schermo vuoto, e mai un dato vecchio spacciato per
// attuale.

const PAGE_CACHE = new Map();

function freshness(meta) {
  if (!meta || !meta.generated_at) return "";
  const when = esc(ts(meta.generated_at));
  return (meta.stale || meta.refreshing)
    ? `<div class="stale">Dati raccolti il ${when} — aggiornamento in corso,
        la pagina si aggiorna da sola appena è pronto.</div>`
    : `<div class="muted">Dati raccolti il ${when}.</div>`;
}

async function liveView(key, fetcher, render) {
  // rientro dalla stessa vista (un pulsante che la ridisegna): il ciclo
  // precedente va fermato, o due cicli scriverebbero nella stessa pagina
  if (cleanup) { try { cleanup(); } catch (e) { /* noop */ } }
  let stopped = false;
  cleanup = () => { stopped = true; };
  const cached = PAGE_CACHE.get(key);
  if (cached) render(cached);
  else view().innerHTML = '<p class="muted">Prima raccolta in corso…</p>';
  // si continua a chiedere finché il backend dichiara di aver finito, ma con
  // un tetto: una raccolta che non converge non deve diventare un polling
  for (let attempt = 0; attempt < 20 && !stopped; attempt++) {
    let data;
    try {
      data = await fetcher();
    } catch (e) {
      if (stopped) return;
      if (!PAGE_CACHE.has(key)) throw e;   // niente da mostrare: errore pieno
      toast((e && e.detail) || "aggiornamento non riuscito", "err");
      return;
    }
    if (stopped) return;
    PAGE_CACHE.set(key, data);
    render(data);
    const meta = data.meta || {};
    if (!meta.stale && !meta.refreshing) return;
    await new Promise(r => setTimeout(r, 2500));
  }
}

// Anteprima breve: il testo integrale resta disponibile con «Apri completo»,
// così un prompt lungo non occupa tutta la pagina.
function preview(text, lines = 3, chars = 220) {
  const t = String(text || "");
  const head = t.split("\n").slice(0, lines).join("\n");
  const cut = head.length > chars ? head.slice(0, chars) + "…" : head;
  return { text: cut, truncated: cut.length < t.length };
}

function msgCard(m) {
  const p = preview(m.text);
  const resend = m.status === "sent" || m.status === "manually_resent";
  // Una riserva sulla consegna non puo' stare in grigio sotto un'etichetta
  // verde: il messaggio e' partito, ma nessuno sa se l'agente l'abbia letto.
  // L'etichetta lo dice e la nota si vede, cosi' la scelta di reinviare la fa
  // chi guarda invece di aspettare una risposta che non arrivera'.
  const reserved = !!m.note && resend;
  const cls = reserved ? "idle" : (MSG_STATUS[m.status] ? m.status : "pending");
  const label = reserved ? "consegnato con riserva"
    : (MSG_STATUS[m.status] || m.status);
  return `<div class="card msg">
    <div class="row spread">
      <b>${m.kind === "initial" ? "Prompt iniziale"
        : m.kind === "controller" ? "Sollecito del controller"
        : m.kind === "control" ? "Scelta TUI" : "Messaggio"}</b>
      <span class="tag ${cls}">${esc(label)}</span>
    </div>
    ${m.note ? `<div class="warnbox"><b>Consegna incerta</b>${esc(m.note)}</div>` : ""}
    <div class="kv" style="margin:6px 0">
      <div>Metodo</div><div class="mono">${esc(m.method || "—")}</div>
      <div>Tentativi</div><div>${m.attempts}</div>
      <div>Registrato</div><div>${esc(ts(m.created_at))}</div>
      <div>Consegnato</div><div>${esc(ts(m.delivered_at) || "—")}</div>
      ${m.last_error ? `<div>Errore</div><div class="mono err">${esc(m.last_error)}</div>` : ""}
    </div>
    <pre class="msg-preview" data-mfull="${esc(m.id)}">${esc(p.text)}</pre>
    <pre class="msg-full" id="mfull-${esc(m.id)}" style="display:none">${esc(m.text)}</pre>
    <div class="row">
      ${p.truncated ? `<button class="small" data-mopen="${esc(m.id)}">Apri completo</button>` : ""}
      <button class="small" data-mcopy="${esc(m.id)}">Copia</button>
      ${m.kind === "control" ? "" :
        `<button class="small primary" data-msend="${esc(m.id)}">${resend ? "Reinvia" : "Invia ora"}</button>`}
    </div>
  </div>`;
}

// Stato del turno e apertura della sessione restano separati anche nel
// dettaglio; il comando del pane rimane nella diagnostica tecnica.
function statusStrip(st) {
  const lm = st.last_message || {};
  const bits = [
    sessionOpenTag(st),
    lifecycleTag(st),
    `<span class="muted">ultimo output <b>${esc(st.output_age_label || "—")}</b></span>`,
    lm.created_at ? `<span class="muted">ultimo input <b>${esc(ts(lm.created_at))}</b></span>` : "",
    lm.delivered_at ? `<span class="muted">consegnato <b>${esc(ts(lm.delivered_at))}</b></span>` : "",
    `<span class="muted" title="Tempo in cui l'harness ha davvero elaborato: le attese di un input non contano">lavoro ${esc(st.work_label || "—")}</span>`,
    `<span class="muted" title="Tempo trascorso dall'avvio, attese comprese">aperta da ${esc(st.duration || "—")}</span>`,
  ].filter(Boolean).join(" · ");
  const problem = lm.status === "delivery_failed"
    ? `<div class="errbox compact"><b>Ultimo testo non consegnato</b>
       <div>${esc(lm.last_error || "nessun dettaglio registrato")}</div>
       <div class="hint">Il testo è salvato in SQLite e non è andato perso.</div>
       <div class="row"><button class="small" id="strip-resend">Apri i messaggi e reinvia</button></div></div>`
    : (st.undelivered ? `<div class="warnbox">${st.undelivered} testo/i in attesa di consegna
       — apri «Messaggi» per vederli.</div>` : "");
  return `<div class="strip">${bits}</div>${lifecycleNote(st)}${problem}`;
}

// Gli stati che chiedono un intervento dicono anche quale, senza inventare
// nulla: il testo del riquadro deriva soltanto dallo stato calcolato.
const LIFECYCLE_NOTE = {
  LAUNCH_FAILED: ["errbox compact", "L'harness non è stato avviato. Il prompt è stato salvato " +
    "e resta disponibile nei messaggi della sessione."],
  NEEDS_INPUT: ["warnbox", "L'agente ha dichiarato di attendere una tua risposta."],
  WAITING_SESSION: ["warnbox", "L'agente attende il completamento di un'altra sessione. " +
    "Agent Hub lo riprenderà automaticamente quando la dipendenza termina."],
  NEEDS_HOST_ACTION: ["warnbox", "L'agente ha dichiarato che serve un'azione amministrativa " +
    "sull'host: valuta «Escalate to hostagent»."],
  FAILED: ["errbox compact", "L'agente ha dichiarato di aver fallito."],
  CRASHED: ["errbox compact", "Il processo dell'harness è uscito con codice diverso da zero " +
    "senza registrare alcuno stato finale."],
  ENDED_UNREPORTED: ["warnbox", "Il processo è terminato senza registrare uno stato finale " +
    "con agent-report: l'esito va verificato nella trascrizione."],
  POSSIBLY_STALLED: ["warnbox", "Nessun output oltre la soglia configurata e nessun stato " +
    "finale, nemmeno dopo il sollecito automatico del controller. " +
    "La sessione NON viene terminata automaticamente."],
  AUTH_REQUIRED: ["errbox compact", "Nel log compare un errore di login riconosciuto: " +
    "rifai il login dalla pagina Accounts."],
  USAGE_LIMIT: ["errbox compact", "Nel log compare un limite d'uso riconosciuto: " +
    "l'agente non può proseguire finché non si sblocca."],
};

function lifecycleNote(st) {
  const note = LIFECYCLE_NOTE[st.lifecycle];
  if (!note) return "";
  const [cls, text] = note;
  const extra = st.lifecycle_reported && st.report_summary
    ? `<div>«${esc(st.report_summary)}»</div>` : "";
  const code = st.exit_code && st.exit_code !== "?"
    ? `<div class="hint">exit code ${esc(st.exit_code)}</div>` : "";
  return `<div class="${cls}"><b>${esc((LIFECYCLE[st.lifecycle] || [st.lifecycle])[0])}</b>
    <div>${esc(text)}</div>${extra}${code}</div>`;
}

// Che cosa è stato realmente eseguito: profilo, modalità permessi e argv
// effettivo del processo, non ciò che il profilo dichiara in astratto.
function diagCard(g) {
  if (!g) return "";
  const argv = (g.argv || []).join(" ");
  const perm = (g.permission_args || []).join(" ");
  return `<details class="card" id="diag">
    <summary>Diagnostica: harness, permessi e comando eseguito</summary>
    <div class="kv" style="margin-top:8px">
      <div>Harness</div><div class="mono">${esc(g.harness)}</div>
      <div>Profilo</div><div class="mono">${esc(g.profile_id)}${g.profile_label ? " — " + esc(g.profile_label) : ""}</div>
      <div>Modalità permessi</div><div><span class="tag ${g.permission_mode === "full" ? "running" : "idle"}">${esc(g.permission_mode)}</span>
        ${perm ? ` <span class="mono">${esc(perm)}</span>` : ' <span class="muted">(nessun argomento)</span>'}</div>
      <div>Modello</div><div class="mono">${esc(g.model)}${g.model && g.model !== "(default dell'harness)" ? esc(catalogModelNote(g.unix_user, g.profile_id, g.model)) : ""}</div>
      <div>Effort richiesto</div><div class="mono">${esc(g.effort || "(default dell'harness)")}</div>
      <div>Modalità richiesta</div><div class="mono">${esc(g.session_mode || "—")}</div>
      ${g.goal ? `<div>Obiettivo</div><div>${esc(g.goal)}</div>` : ""}
      <div>Utente Unix</div><div class="mono">${esc(g.unix_user)}</div>
      <div>Directory</div><div class="mono">${esc(g.workdir)}</div>
      <div>Prompt iniziale</div><div>${g.prompt_as_argv ? "passato come argomento della CLI" : "consegnato dopo l'avvio (paste)"}</div>
      <div>cgroup</div><div class="mono">${esc(g.cgroup || "—")}</div>
      <div>Avviato</div><div>${esc(ts(g.launched_at) || "—")}</div>
    </div>
    <label style="margin-top:10px">Comando effettivo (segreti rimossi, prompt escluso)</label>
    <pre class="mono">${esc(argv || "(non registrato: sessione avviata da una versione precedente)")}</pre>
    ${g.trust_note ? help(g.trust_note) : ""}
    ${g.contract ? `<label style="margin-top:10px">Contratto di stato finale aggiunto a ogni testo consegnato</label>
      <pre class="mono">${esc(g.contract)}</pre>
      ${help("Il blocco viene accodato al testo consegnato alla sessione, mai al testo salvato in " +
             "SQLite: la cronologia degli input resta esattamente quella scritta da te.")}` : ""}
    <div class="row"><button class="small" id="diag-copy">Copia comando</button></div>
  </details>`;
}

// ------------------------------------------ modello ed effort in esercizio
//
// Due valori distinti, mai confusi: quello *configurato* (ciò che l'Hub ha
// chiesto all'avvio, registrato in SQLite) e quello *in uso* (ciò che
// l'harness ha scritto nel proprio file di stato all'ultima risposta). Il
// pannello mostra sempre l'origine del dato, così una divergenza si vede
// invece di essere nascosta da un'etichetta ottimista.

function fmtTokens(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0).replace(".", ",") + "k";
  return (n / 1000000).toFixed(1).replace(".", ",") + "M";
}

function runtimeTags(rt) {
  if (!rt) return '<span class="muted">modello…</span>';
  const live = rt.live || {}, cfg = rt.configured || {};
  const isLive = live.state === "live";
  const model = live.model || cfg.model || "";
  const effort = live.effort || cfg.effort || "";
  const src = isLive ? "letto dallo stato dell'harness (ultima risposta)"
    : "valore richiesto all'avvio: l'harness non ha ancora scritto uno stato verificabile";
  const mark = isLive ? "◆" : "◇";
  const tags = [`<span class="tag ${isLive ? "running" : "idle"}" title="${esc(src)}">${mark} ${
    esc(model || "modello di default")}</span>`];
  if (effort) tags.push(`<span class="tag ${isLive && live.effort ? "running" : "idle"}"
    title="${esc(live.effort ? src : "effort richiesto all'avvio")}">effort ${esc(effort)}</span>`);
  // La modalità è un fatto letto dalla riga di stato della TUI, quindi vale
  // anche quando l'harness non ha ancora scritto uno stato verificabile.
  const modeId = rt.mode_live || cfg.mode || "";
  const modeOpt = ((rt.capabilities || {}).modes || []).find(m => m.id === modeId);
  if (modeId && modeId !== ((rt.capabilities || {}).default_mode || "")) {
    tags.push(`<span class="tag ${rt.mode_live ? "running" : "idle"}" title="${
      esc(rt.mode_live ? "modalità mostrata adesso dalla TUI" : "modalità richiesta all'avvio")}">${
      esc((modeOpt && modeOpt.label) || modeId)}</span>`);
  }
  if (isLive && live.context_tokens) {
    tags.push(`<span class="tag ended" title="token del contesto all'ultima richiesta (input + cache)">ctx ${
      esc(fmtTokens(live.context_tokens))}</span>`);
  }
  if (isLive && live.usage && live.usage.output_tokens) {
    tags.push(`<span class="tag ended" title="token generati dall'inizio della sessione">out ${
      esc(fmtTokens(live.usage.output_tokens))}</span>`);
  }
  return tags.join(" ");
}

function runtimeBody(rt) {
  const live = rt.live || {}, cfg = rt.configured || {}, cap = rt.capabilities || {};
  const u = live.usage || {};
  const stateNote = {
    live: "Valori letti dal file di stato che l'harness aggiorna a ogni risposta.",
    pending: "L'harness non ha ancora prodotto una risposta: finché non lo fa valgono i valori di avvio.",
    unsupported: "Questo harness non espone uno stato leggibile per sessione.",
    error: "Lo stato non è leggibile in questo momento.",
  }[live.state] || "";

  const cur = live.model || cfg.model || "";
  const curEffort = live.effort || cfg.effort || "";
  // il catalogo è per utente Unix: la sessione dice quale
  const user = rt.unix_user || "";
  const profileId = cap.profile_id || "";
  const catEntry = catalogModels(user, profileId).find(m => m.model_id === cur);
  // livelli offribili: quelli dell'harness ristretti a quelli del modello in
  // uso; un alias non li dichiara, quindi non restringe nulla
  const levels = (!catEntry || catEntry.kind === "alias")
    ? (cap.effort_levels || [])
    : (catEntry.efforts && catEntry.efforts.length
        ? (cap.effort_levels || []).filter(l => catEntry.efforts.includes(l))
        : []);
  const st = catalogState(user, profileId);

  const rows = [
    ["Modello in uso", live.model ? `<b class="mono">${esc(live.model)}</b>` :
      '<span class="muted">non ancora osservato</span>'],
    ["Modello richiesto all'avvio", cfg.model
      ? `<span class="mono">${esc(cfg.model)}</span>` : '<span class="muted">default dell\'harness</span>'],
    ["Effort in uso", live.effort ? `<b class="mono">${esc(live.effort)}</b>` :
      '<span class="muted">non registrato dall\'harness</span>'],
    ["Effort richiesto all'avvio", cfg.effort
      ? `<span class="mono">${esc(cfg.effort)}</span>` : '<span class="muted">default dell\'harness</span>'],
  ];
  const modes = cap.modes || [];
  const curMode = rt.mode_live || cfg.mode || "";
  if (modes.length) {
    const lbl = id => (modes.find(m => m.id === id) || {}).label || id;
    rows.push(["Modalità secondo la TUI", rt.mode_live
      ? `<b>${esc(lbl(rt.mode_live))}</b>`
      : '<span class="muted">non leggibile (sessione non attiva)</span>']);
    rows.push(["Modalità richiesta all'avvio", cfg.mode
      ? esc(lbl(cfg.mode)) : '<span class="muted">quella di partenza dell\'harness</span>']);
  }
  if (cap.supports_goal) {
    rows.push(["Obiettivo registrato", cfg.goal
      ? esc(cfg.goal) : '<span class="muted">nessuno</span>']);
  }
  if (live.state === "live") {
    rows.push(["Risposte osservate", esc(String(live.turns || 0))]);
    rows.push(["Contesto ultima richiesta", `${esc(fmtTokens(live.context_tokens))} token`]);
    rows.push(["Token totali", [
      u.input_tokens !== undefined ? `input ${fmtTokens(u.input_tokens)}` : "",
      u.output_tokens !== undefined ? `output ${fmtTokens(u.output_tokens)}` : "",
      u.cache_read_input_tokens !== undefined ? `cache letta ${fmtTokens(u.cache_read_input_tokens)}` : "",
      u.cache_creation_input_tokens !== undefined ? `cache scritta ${fmtTokens(u.cache_creation_input_tokens)}` : "",
      u.cached_input_tokens !== undefined ? `cache ${fmtTokens(u.cached_input_tokens)}` : "",
      u.reasoning_output_tokens !== undefined ? `reasoning ${fmtTokens(u.reasoning_output_tokens)}` : "",
    ].filter(Boolean).map(esc).join(" · ")]);
    if (live.last_at) rows.push(["Ultima risposta", esc(ts(live.last_at))]);
    if (live.permission_mode) rows.push(["Permessi secondo l'harness", `<span class="mono">${esc(live.permission_mode)}</span>`]);
    if (live.source) rows.push(["Origine del dato", `<span class="mono">${esc(live.source)}</span>`]);
  } else if (live.reason) {
    rows.push(["Motivo", esc(live.reason)]);
  }

  const canModel = cap.supports_model;
  const canEffort = cap.supports_effort && levels.length;
  const hotModel = cap.live_model_change, hotEffort = cap.live_effort_change;
  const form = (canModel || canEffort) ? `
    <div class="row" style="margin-top:12px;align-items:flex-end;gap:10px;flex-wrap:wrap">
      ${canModel ? `<div style="flex:1;min-width:220px"><label>Modello</label>
        <select id="rt-model">${modelOptions(user, profileId, cur)}</select>
        <input id="rt-model-other" placeholder="id del modello" style="display:none;margin-top:6px">
        </div>` : ""}
      ${canEffort ? `<div style="flex:1;min-width:150px"><label>Effort</label>
        <select id="rt-effort"><option value="">(invariato)</option>${levels.map(l =>
          `<option value="${esc(l)}"${l === curEffort ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
        </div>` : ""}
      ${modes.length ? `<div style="flex:1;min-width:160px"><label>Modalità</label>
        <select id="rt-mode"><option value="">(invariata)</option>${modes.map(m =>
          `<option value="${esc(m.id)}"${m.id === curMode ? " selected" : ""}>${esc(m.label)}</option>`).join("")}</select>
        </div>` : ""}
      <button class="primary small" id="rt-apply">Applica</button>
    </div>
    ${cap.supports_goal ? `<div class="row" style="margin-top:10px;align-items:flex-end;gap:10px;flex-wrap:wrap">
      <div style="flex:2;min-width:240px"><label>Obiettivo (/goal)</label>
        <input id="rt-goal" maxlength="${cap.goal_max_length || 500}" value="${esc(cfg.goal || "")}"
               placeholder="obiettivo che l'agente verifica prima di fermarsi"></div>
      <button class="small" id="rt-goal-clear">Azzera obiettivo</button>
    </div>` : ""}
    <div class="muted" style="margin-top:6px">${
      (hotModel || hotEffort)
        ? "Il cambio viene eseguito subito nella sessione con i comandi della TUI " +
          `(${[hotModel ? "/model" : "", hotEffort ? "/effort" : ""].filter(Boolean).join(" e ")}) ` +
          "e registrato per i prossimi avvii."
        : "Questo harness non permette il cambio a caldo: il valore viene registrato e " +
          "diventa effettivo al prossimo avvio (pulsante «Restart»)."}</div>
    ${modes.length ? `<div class="muted" style="margin-top:4px">${esc((cap.modes_note || "") +
      " Il cambio di modalità viene verificato sulla riga di stato della TUI: se non compare, " +
      "la risposta lo dice invece di darlo per fatto.")}</div>` : ""}
    <div id="rt-result"></div>` : '<div class="muted" style="margin-top:10px">Questo harness non permette di scegliere il modello.</div>';

  return `<div class="kv" style="margin-top:10px">${rows.map(
      ([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("")}</div>
    ${stateNote ? `<div class="muted" style="margin-top:8px">${esc(stateNote)}</div>` : ""}
    ${st ? `<div class="muted" style="margin-top:4px">Catalogo modelli: <b>${esc(st.label)}</b>${
      st.last_checked ? " · " + esc(ts(st.last_checked)) : ""}${st.error ? " · " + esc(st.error) : ""}</div>` : ""}
    ${cap.note ? help(cap.note) : ""}
    ${form}`;
}

async function viewSession(sid) {
  sid = decodeURIComponent(sid);
  const d = await api("/api/sessions/" + encodeURIComponent(sid), {}, "dettaglio sessione");
  const s = d.session;
  const envCls = s.environment === "SERVER" ? "server" : "project";
  const profiles = BOOT.profiles;
  const allDocs = await api("/api/documents", {}, "elenco documenti");

  view().innerHTML = `
    <div class="row spread"><h2>${esc(s.name)}</h2>
      <a class="plain" href="#/"><button class="small">← Dashboard</button></a></div>
    <div class="card ${envCls}">
      <div class="row">
        <span class="tag ${envCls}">${envLabel(s)}</span>
        ${stateTags(s)}
      </div>
      <div class="muted mono" style="margin-top:6px">${esc(s.unix_user)} · ${esc(s.profile_id)} · ${esc(s.workdir)}</div>
      ${s.executor_enabled ? `<div class="row" style="margin-top:7px">
        <span class="tag running">sub agent ${esc(s.executor_model)}</span>
        <span class="tag idle">max ${esc(s.executor_max_agents)}</span>
        <span class="muted">delega efficiente attiva tramite agent-executor</span>
      </div>` : ""}
      <details class="runtime" id="runtime">
        <summary><span class="rt-tags" id="rt-tags">${runtimeTags(null)}</span></summary>
        <div id="rt-body"><span class="muted">lettura dello stato dell'harness…</span></div>
      </details>
      ${help("Il processo tmux vive fuori dal cgroup di agent-hub.service: riavviare il backend o chiudere " +
             "il browser non termina la sessione. Lo stato mostrato viene sempre riletto da tmux.")}
    </div>
    ${diagCard(d.diagnostics)}
    <div id="sess-err"></div>
    <div id="term"></div>
    <div class="muted" id="ws-state" style="margin-top:6px">connessione…</div>
    <div id="status-strip">${statusStrip(s)}</div>
    <div class="sticky-actions">
      <textarea id="msg" placeholder="Messaggio o prompt multilinea…"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="primary" id="send">Invia</button>
        <button class="small" id="msg-toggle">Messaggi</button>
        <div class="keypad" role="group" aria-label="Tasti inviati alla TUI">
          <button class="key" id="a-up" title="Freccia su: voce precedente" aria-label="Freccia su">▲</button>
          <button class="key" id="a-down" title="Freccia giu: voce successiva" aria-label="Freccia giu">▼</button>
          <button class="key" id="a-enter" title="Invio: conferma la voce evidenziata">Enter</button>
          <button class="key" id="a-pause" title="Esc: ferma la generazione, la sessione resta viva">Esc</button>
        </div>
        <button class="small danger" id="a-kill">Kill</button>
        <button class="small" id="a-restart">Restart</button>
        <button class="small danger" id="a-delete">Elimina</button>
      </div>
      ${help("Invia salva il testo in SQLite prima di consegnarlo: se tmux o la TUI falliscono il testo " +
             "resta recuperabile dal pulsante «Messaggi». Il tastierino manda un tasto vero alla TUI: " +
             "Su/Giu scelgono la voce di un dialogo, Enter conferma, Esc ferma la generazione lasciando " +
             "viva la sessione. Per Ctrl-C usa il terminale qui sopra.")}
    </div>

    <div id="msg-panel" style="display:none">
      <div class="row spread"><h3 style="margin:0">Cronologia input</h3>
        <button class="small" id="msg-close">Chiudi</button></div>
      ${help("Ogni prompt e ogni messaggio è salvato in SQLite prima del tentativo di consegna: " +
             "resta leggibile anche se l'harness non parte, se tmux termina o se il backend viene riavviato.")}
      <div id="messages"></div>
    </div>

    <h3>Stati finali registrati dall'agente</h3>
    <div class="card">
      ${help("Ogni riga è una chiamata ad agent-report andata a buon fine. Il comando aggiorna SQLite " +
             "e scrive l'evento JSON in /srv/agent-workspace/reports/. Ripetere lo stesso stato con lo " +
             "stesso riepilogo non aggiunge righe: la registrazione è idempotente.")}
      ${(d.reports || []).length ? `<div class="kv">${(d.reports || []).map(r =>
        `<div><span class="tag ${(LIFECYCLE[r.status] || ["", "ended"])[1]}">${esc(r.status)}</span></div>
         <div>${esc(ts(r.reported_at))}${r.unix_user ? " · " + esc(r.unix_user) : ""}
           ${r.waiting_for_session ? `<div>Dipende da <a href="#/session/${encodeURIComponent(
             r.waiting_for_session)}">${esc(r.waiting_for_session)}</a></div>` : ""}
           <div>${esc(r.summary || "(nessun riepilogo)")}</div></div>`).join("")}</div>`
        : '<span class="muted">Nessuno stato finale registrato per questa sessione.</span>'}
      <div class="muted mono" style="margin-top:8px">agent-report COMPLETED --summary "…" · SESSION_ID ${esc(sid)}</div>
    </div>

    <h3>Documenti della sessione</h3>
    <div class="card">
      <div id="sess-docs">${(d.documents || []).map(x => docRow(x)).join("") ||
        '<span class="muted">Nessun documento allegato.</span>'}</div>
      <label>Aggiungi documenti già caricati</label>
      <div style="max-height:170px;overflow:auto">
        ${allDocs.documents.map(x => `<label class="inline" style="display:flex;margin:4px 0">
          <input type="checkbox" class="add-doc" value="${esc(x.id)}">
          <span>${esc(x.name)} <span class="muted">· ${esc(ts(x.modified_at))}</span></span></label>`).join("") ||
          '<span class="muted">Nessun documento disponibile.</span>'}
      </div>
      <div class="row" style="margin-top:8px"><button class="small primary" id="doc-attach">Allega e comunica i percorsi</button></div>
    </div>

    <h3>Continuità</h3>
    <div class="card">
      <label>Continua con altro agente</label>
      <div class="row">
        <select id="cont-profile" style="flex:1">
          ${profiles.map(p => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("")}
        </select>
        <input id="cont-model" placeholder="modello (opz.)" style="max-width:180px">
        <button id="cont-go">Continua</button>
      </div>
      ${help("Crea una nuova sessione sullo stesso progetto precompilando obiettivo, .agent/HANDOFF.md e git status.")}
      ${s.environment === "PROJECT" ? `
      <label style="margin-top:14px">Escalate to hostagent</label>
      <div class="row">
        <select id="esc-profile" style="flex:1">
          ${profiles.filter(p => (p.allowed_users || []).includes(serverUser())).map(p =>
            `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("")}
        </select>
        <button class="small" id="esc-go">Escalate</button>
      </div>
      <textarea id="esc-note" placeholder="Nota facoltativa per l'agente amministrativo…" style="min-height:60px"></textarea>` : ""}
      <details style="margin-top:10px"><summary>Anteprima handoff</summary>
        <div class="row"><button class="small" id="prev-dev">Handoff altro agente</button>
        ${s.environment === "PROJECT" ? '<button class="small" id="prev-host">Handoff hostagent</button>' : ""}</div>
        <pre id="prev-out" style="display:none"></pre></details>
    </div>

    <h3>Log</h3>
    <div class="card">
      <div class="row">
        <button class="small" id="tr-open">Apri trascrizione</button>
        <button class="small" id="tr-refresh">Aggiorna</button>
        <button class="small" id="tr-copy">Copia trascrizione</button>
        <a class="plain" href="/api/sessions/${encodeURIComponent(sid)}/log/raw" download>
          <button class="small">Download log raw</button></a>
      </div>
      <div class="row" style="margin-top:8px">
        <label class="inline"><input type="checkbox" id="tr-auto"> aggiornamento automatico</label>
        <select id="tr-source" class="small">
          <option value="auto">sorgente automatica</option>
          <option value="tmux">schermo tmux (capture-pane)</option>
          <option value="log">storico dal log raw</option>
        </select>
        <select id="tr-tail" class="small">
          <option value="500">ultime 500 righe</option>
          <option value="3000" selected>ultime 3000 righe</option>
          <option value="20000">ultime 20000 righe</option>
        </select>
        <select id="tr-chrome" class="small">
          <option value="hide" selected>cornice TUI nascosta</option>
          <option value="show">cornice TUI visibile</option>
        </select>
        <span class="muted" id="tr-meta"></span>
      </div>
      <pre id="tr-out" class="transcript">(premi Aggiorna per caricare la trascrizione)</pre>
      ${help("La trascrizione è testo semplice: le sequenze ANSI/OSC sono rimosse e il contenuto viene " +
             "sempre inserito come testo, mai come HTML. Con la cornice nascosta spariscono separatori, " +
             "riga di input vuota, spinner e footer di stato, che la TUI ridisegna a ogni refresh e che " +
             "spezzano i blocchi lunghi da copiare; il testo non viene mai deduplicato, quindi le righe " +
             "orizzontali delle tabelle stampate dall'agente spariscono ma le celle restano. " +
             "Il log raw completo resta scaricabile per la diagnostica.")}
      <div class="muted mono">raw: /srv/agent-workspace/logs/${esc(s.tmux_name)}.log</div>
    </div>`;

  wireDocActions(view(), []);
  if ($("#diag-copy")) {
    $("#diag-copy").onclick = () => copyText((d.diagnostics.argv || []).join(" "), "Comando copiato");
  }

  // terminale
  const dark = getComputedStyle(document.documentElement).getPropertyValue("--term-bg").trim() || "#000";
  const term = new Terminal({
    cursorBlink: true, fontSize: 13, scrollback: 20000,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    theme: { background: dark, foreground: "#e6e8ee" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#term"));
  setTimeout(() => { try { fit.fit(); } catch (e) { /* noop */ } }, 60);

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/session/${encodeURIComponent(sid)}`);
  ws.binaryType = "arraybuffer";
  const dec = new TextDecoder();
  // gli eventi della WebSocket arrivano anche dopo che si è cambiata pagina:
  // a quel punto gli elementi non esistono più e scriverci abortisce l'handler
  const wsState = txt => { const el = $("#ws-state"); if (el) el.textContent = txt; };
  ws.onopen = () => {
    wsState("connesso — chiudere il browser non interrompe la sessione");
    sendResize();
  };
  ws.onmessage = ev => term.write(typeof ev.data === "string" ? ev.data : dec.decode(ev.data));
  ws.onclose = () => wsState("disconnesso (la sessione tmux resta attiva)");
  ws.onerror = () => wsState("errore di connessione al terminale");
  term.onData(data => { if (ws.readyState === 1) ws.send(data); });

  function sendResize() {
    try { fit.fit(); } catch (e) { return; }
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }
  let rt, trTimer = null, stateTimer = null, runtimeTimer = null;
  const onResize = () => { clearTimeout(rt); rt = setTimeout(sendResize, 250); };
  window.addEventListener("resize", onResize);
  cleanup = () => {
    window.removeEventListener("resize", onResize);
    clearInterval(trTimer);
    clearInterval(stateTimer);
    clearInterval(runtimeTimer);
    try { ws.close(); } catch (e) { /* noop */ }
    term.dispose();
  };

  // --- cronologia input: chiusa per default, aperta dal pulsante «Messaggi» --
  let msgOpen = false;
  let counters = { messages_total: s.messages_total || 0, undelivered: s.undelivered || 0 };

  function paintToggle() {
    const b = $("#msg-toggle");
    if (!b) return;
    b.textContent = `Messaggi (${counters.messages_total})` +
      (counters.undelivered ? ` · ${counters.undelivered} non consegnati` : "");
    b.classList.toggle("danger", counters.undelivered > 0);
  }

  async function reloadMessages() {
    if (!msgOpen) return;
    const r = await api(`/api/sessions/${encodeURIComponent(sid)}/messages`, {}, "elenco messaggi");
    $("#messages").innerHTML = r.messages.map(msgCard).join("") ||
      '<div class="card muted">Nessun prompt registrato per questa sessione.</div>';
    wireMessages(r.messages);
  }

  function wireMessages(messages) {
    const root = $("#msg-panel");
    $$("[data-mopen]", root).forEach(b => {
      b.onclick = () => {
        const full = $("#mfull-" + CSS.escape(b.dataset.mopen), root);
        const short = $(`[data-mfull="${CSS.escape(b.dataset.mopen)}"]`, root);
        const open = full.style.display === "none";
        full.style.display = open ? "block" : "none";
        short.style.display = open ? "none" : "block";
        b.textContent = open ? "Riduci" : "Apri completo";
      };
    });
    $$("[data-mcopy]", root).forEach(b => {
      b.onclick = () => {
        const m = messages.find(x => x.id === b.dataset.mcopy);
        if (m) copyText(m.text, "Testo copiato");
      };
    });
    $$("[data-msend]", root).forEach(b => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          await post(`/api/sessions/${encodeURIComponent(sid)}/messages/${encodeURIComponent(b.dataset.msend)}/resend`,
                     {}, "reinvio messaggio");
          toast("Testo consegnato alla sessione");
          await refreshState();
          await reloadMessages();
        } catch (e) { showError(e, $("#sess-err")); b.disabled = false; }
      };
    });
  }

  async function setPanel(open) {
    msgOpen = open;
    $("#msg-panel").style.display = open ? "block" : "none";
    if (open) { try { await reloadMessages(); } catch (e) { showError(e, $("#sess-err")); } }
  }
  $("#msg-toggle").onclick = () => setPanel(!msgOpen);
  $("#msg-close").onclick = () => setPanel(false);
  paintToggle();

  // --- stato osservato: un solo endpoint leggero, senza testo dei messaggi ---
  async function refreshState() {
    try {
      const st = await api(`/api/sessions/${encodeURIComponent(sid)}/state`, {}, "stato sessione");
      $("#status-strip").innerHTML = statusStrip(st);
      counters = { messages_total: st.messages_total, undelivered: st.undelivered };
      paintToggle();
      const rb = $("#strip-resend");
      if (rb) rb.onclick = () => setPanel(true);
      return st;
    } catch (e) {
      // il polling non invade il banner degli errori, ma non resta muto:
      // l'esito negativo è scritto nella striscia stessa
      const strip = $("#status-strip");
      if (strip) {
        strip.innerHTML = `<div class="warnbox">Stato non aggiornabile — ${esc(
          (e && (e.detail || e.message)) || "errore sconosciuto")}${
          e && e.status ? " (HTTP " + e.status + ")" : ""}. Il terminale resta collegato.</div>`;
      }
      return null;
    }
  }
  stateTimer = setInterval(refreshState, 5000);
  refreshState();

  // --- modello, effort e consumo token letti dallo stato dell'harness -------
  // Il polling è più lento di quello di stato: la lettura apre i file di stato
  // dell'harness tramite il wrapper, e il dato cambia solo a fine risposta.
  async function refreshRuntime(render = true) {
    let rt;
    try {
      rt = await api(`/api/sessions/${encodeURIComponent(sid)}/runtime`, {}, "stato del modello");
    } catch (e) {
      const tags = $("#rt-tags");
      if (tags) tags.innerHTML = `<span class="tag failed" title="${esc(
        (e && (e.detail || e.message)) || "")}">stato del modello non leggibile</span>`;
      return null;
    }
    const tags = $("#rt-tags");
    if (tags) tags.innerHTML = runtimeTags(rt);
    // il corpo non viene riscritto mentre l'utente sta compilando i campi
    const body = $("#rt-body");
    if (body && render && !body.contains(document.activeElement)) {
      body.innerHTML = runtimeBody(rt);
      wireRuntime();
    }
    return rt;
  }

  function wireRuntime() {
    const sel = $("#rt-model");
    if (sel) {
      sel.onchange = () => {
        const other = sel.value === "__other__";
        $("#rt-model-other").style.display = other ? "" : "none";
        if (other) $("#rt-model-other").focus();
      };
    }
    const clear = $("#rt-goal-clear");
    if (clear) {
      clear.onclick = async () => {
        const out = $("#rt-result");
        clear.disabled = true;
        out.innerHTML = '<div class="muted">azzeramento in corso…</div>';
        try {
          const r = await post(`/api/sessions/${encodeURIComponent(sid)}/runtime`,
                               { clear_goal: true }, "azzeramento obiettivo");
          out.innerHTML = `<div class="${r.errors && r.errors.length ? "errbox compact" : "okbox"}">${
            esc(r.detail)}</div>`;
          if ($("#rt-goal")) $("#rt-goal").value = "";
          toast(r.ok ? "Obiettivo azzerato" : "Azzeramento parziale — leggi il dettaglio",
                r.ok ? "ok" : "err");
        } catch (e) { showError(e, out); } finally { clear.disabled = false; }
      };
    }
    const btn = $("#rt-apply");
    if (!btn) return;
    btn.onclick = async () => {
      const sel = $("#rt-model");
      const model = !sel ? ""
        : (sel.value === "__other__" ? $("#rt-model-other").value.trim() : sel.value);
      const effort = $("#rt-effort") ? $("#rt-effort").value : "";
      const mode = $("#rt-mode") ? $("#rt-mode").value : "";
      const goalEl = $("#rt-goal");
      const goal = goalEl ? goalEl.value.trim() : "";
      const out = $("#rt-result");
      if (!model && !effort && !mode && !goal) {
        toast("Scegli un modello, un effort, una modalità o scrivi un obiettivo", "err"); return; }
      btn.disabled = true;
      out.innerHTML = '<div class="muted">applicazione in corso…</div>';
      try {
        const r = await post(`/api/sessions/${encodeURIComponent(sid)}/runtime`,
                             { model, effort, mode, goal }, "cambio modello/effort/modalità");
        // l'esito mostrato è quello riportato dal backend, comprese le ultime
        // righe del terminale: nessuna conferma ottimistica
        const tails = (r.applied || []).filter(a => a.pane_tail).map(a =>
          `<label style="margin-top:8px">Terminale dopo <span class="mono">${esc(a.command)}</span></label>
           <pre class="mono">${esc(a.pane_tail)}</pre>`).join("");
        out.innerHTML = `<div class="${r.errors && r.errors.length ? "errbox compact" : "okbox"}">
          ${esc(r.detail)}</div>${tails}`;
        toast(r.ok ? "Modifica applicata" : "Modifica parziale — leggi il dettaglio", r.ok ? "ok" : "err");
        const tagsEl = $("#rt-tags");
        if (tagsEl && r.runtime) tagsEl.innerHTML = runtimeTags(r.runtime);
        // rilettura reale dopo qualche secondo: il valore definitivo è quello
        // che l'harness scriverà nel proprio stato, non quello che ha risposto l'Hub
        setTimeout(() => refreshRuntime(false), 4000);
      } catch (e) {
        showError(e, out);
      } finally { btn.disabled = false; }
    };
  }

  $("#runtime").addEventListener("toggle", () => {
    if ($("#runtime").open) refreshRuntime();
  });
  runtimeTimer = setInterval(() => refreshRuntime($("#runtime").open), 30000);
  refreshRuntime(false);

  $("#send").onclick = async () => {
    const text = $("#msg").value;
    if (!text.trim()) { toast("Scrivi un messaggio prima di inviare", "err"); return; }
    $("#send").disabled = true;
    $("#sess-err").innerHTML = "";
    try {
      // il backend risponde appena il testo è in SQLite: la conferma è
      // immediata e lo stato di consegna arriva dal successivo refresh
      await post(`/api/sessions/${encodeURIComponent(sid)}/messages`, { text }, "invio messaggio");
      $("#msg").value = "";
      toast("Messaggio registrato — consegna in corso");
      setTimeout(() => {
        refreshState();
        reloadMessages().catch(e => showError(e, $("#sess-err")));
      }, 900);
    } catch (e) { showError(e, $("#sess-err")); }
    $("#send").disabled = false;
  };

  $("#doc-attach").onclick = async () => {
    const ids = $$(".add-doc:checked").map(c => c.value);
    if (!ids.length) { toast("Seleziona almeno un documento", "err"); return; }
    try {
      const r = await post(`/api/sessions/${encodeURIComponent(sid)}/documents`, { document_ids: ids }, "allegato documenti");
      toast(`Registrati ${r.documents.length} documenti — consegna dei percorsi in corso`);
      setTimeout(route, 900);
    } catch (e) { showError(e, $("#sess-err")); }
  };

  async function act(action, confirmMsg) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    try {
      await post(`/api/sessions/${encodeURIComponent(sid)}/action`, { action }, "azione " + action);
      toast("Azione eseguita: " + action);
      if (action === "delete") location.hash = "#/";
      else if (action === "restart") setTimeout(route, 1200);
    } catch (e) { showError(e, $("#sess-err")); }
  }
  $("#a-up").onclick = () => act("up");
  $("#a-down").onclick = () => act("down");
  $("#a-enter").onclick = () => act("enter");
  $("#a-pause").onclick = () => act("pause");
  $("#a-kill").onclick = () => act("kill", "Terminare la sessione tmux?");
  $("#a-restart").onclick = () => act("restart", "Riavviare la sessione con lo stesso profilo e prompt iniziale?");
  $("#a-delete").onclick = () => act("delete", "Eliminare sessione, messaggi e log?");

  $("#cont-go").onclick = async () => {
    try {
      const r = await post(`/api/sessions/${encodeURIComponent(sid)}/continue-with`, {
        profile_id: $("#cont-profile").value, model: $("#cont-model").value.trim(),
      }, "continuazione con altro agente");
      location.hash = "#/session/" + r.session.id;
    } catch (e) { showError(e, $("#sess-err")); }
  };
  if ($("#esc-go")) {
    $("#esc-go").onclick = async () => {
      if (!confirm("Avviare una sessione SERVER privilegiata come hostagent?")) return;
      try {
        const r = await post(`/api/sessions/${encodeURIComponent(sid)}/escalate`, {
          profile_id: $("#esc-profile").value, note: $("#esc-note").value,
        }, "escalation a hostagent");
        location.hash = "#/session/" + r.session.id;
      } catch (e) { showError(e, $("#sess-err")); }
    };
  }
  async function preview(toHost) {
    try {
      const r = await api(`/api/sessions/${encodeURIComponent(sid)}/handoff-preview?to_host=${toHost}`, {}, "anteprima handoff");
      const out = $("#prev-out");
      out.style.display = "block"; out.textContent = r.prompt;
    } catch (e) { showError(e, $("#sess-err")); }
  }
  $("#prev-dev").onclick = () => preview(0);
  if ($("#prev-host")) $("#prev-host").onclick = () => preview(1);

  // --- trascrizione leggibile -------------------------------------------
  // Il log raw non viene mai iniettato nella pagina: la trascrizione arriva
  // come text/plain e finisce in un <pre> via textContent.
  const trUrl = () => `/api/sessions/${encodeURIComponent(sid)}/transcript` +
    `?source=${encodeURIComponent($("#tr-source").value)}&tail=${encodeURIComponent($("#tr-tail").value)}` +
    `&chrome=${encodeURIComponent(($("#tr-chrome") || { value: "hide" }).value)}`;

  // la fetch può concludersi dopo un cambio pagina: gli elementi vanno
  // rileggi ogni volta e possono essere spariti
  const trMeta = txt => { const el = $("#tr-meta"); if (el) el.textContent = txt; };

  async function loadTranscript(scroll = true) {
    const out = $("#tr-out");
    if (!out) return;
    try {
      const r = await fetch(trUrl(), { headers: { Accept: "text/plain" }, cache: "no-store" });
      const text = await r.text();
      if (!r.ok) throw new ApiError(r.status, text.slice(0, 300), "trascrizione");
      if (!$("#tr-out")) return;
      const stick = !scroll || out.scrollTop + out.clientHeight >= out.scrollHeight - 40;
      out.textContent = text;
      const src = r.headers.get("X-Transcript-Source") || "?";
      const lines = r.headers.get("X-Transcript-Lines") || "?";
      const cut = r.headers.get("X-Transcript-Truncated") === "1";
      trMeta(`${lines} righe · sorgente ${src}${cut ? " · troncata alle ultime righe" : ""} · ${new Date().toLocaleTimeString()}`);
      if (stick) out.scrollTop = out.scrollHeight;
    } catch (e) {
      trMeta("errore: " + (e.detail || e.message));
    }
  }

  $("#tr-refresh").onclick = () => loadTranscript();
  $("#tr-source").onchange = () => loadTranscript();
  $("#tr-tail").onchange = () => loadTranscript();
  $("#tr-chrome").onchange = () => loadTranscript();
  $("#tr-open").onclick = () => window.open(trUrl(), "_blank", "noopener");
  $("#tr-copy").onclick = () => copyText($("#tr-out").textContent, "Trascrizione copiata");
  $("#tr-auto").onchange = () => {
    clearInterval(trTimer);
    if ($("#tr-auto").checked) { loadTranscript(); trTimer = setInterval(loadTranscript, 5000); }
  };
  loadTranscript();
}

// ---------------------------------------------------------------- accounts

async function viewAccounts() {
  await liveView("accounts", () => api("/api/accounts", {}, "stato account"), renderAccounts);
}

function renderAccounts(d) {
  if (BOOT) BOOT.catalog = d.catalog || BOOT.catalog;
  // Il catalogo è letto dal provider con le credenziali dell'utente, quindi
  // ogni riga dice tre cose distinte: se è stato possibile verificarlo, quando,
  // e che cosa il provider offre davvero adesso.
  const catalog = (user) => ((d.catalog || {})[user] || []).map(g => {
    const [label, kind, why] = CATALOG_STATE[g.status] || [g.status, "muted", ""];
    const tagCls = { ok: "sent", err: "failed", warn: "idle", muted: "ended" }[kind] || "ended";
    const models = g.models || [];
    const list = models.length
      ? models.map(m => `<div class="mono">${esc(m.model_id)}${
          m.display_name !== m.model_id ? " — " + esc(m.display_name) : ""}${
          m.efforts && m.efforts.length ? ` <span class="muted">effort: ${esc(m.efforts.join(", "))}</span>` : ""}</div>`).join("")
      : `<div class="muted">${g.status === "unavailable"
          ? "nessun modello offerto: il provider non è utilizzabile da questo utente"
          : "nessun modello nel catalogo"}</div>`;
    return `<details style="margin-top:7px"><summary><b>${esc(g.label)}</b>
      <span class="tag ${tagCls}">${esc(label)}</span>${g.last_checked ? " · " + esc(ts(g.last_checked)) : ""}</summary>
      <div class="muted" style="margin-top:6px">${esc(why)}</div>
      ${g.error ? `<div class="muted" style="margin-top:4px">${esc(g.error)}</div>` : ""}
      <div class="row" style="margin-top:7px"><button class="small" data-model-refresh="${esc(g.provider)}" data-user="${esc(user)}">Aggiorna modelli</button></div>
      <div style="margin-top:7px"><b>Modelli offerti (${models.length})</b>${list}</div>
    </details>`;
  }).join("");
  const block = (user, a) => {
    const cls = user === serverUser() ? "server" : "project";
    const profs = d.profiles.filter(p => (p.allowed_users || []).includes(user) && p.login);
    return `<div class="card ${cls}">
      <div class="row spread"><b class="mono">${esc(user)}</b>
        <span class="tag ${cls}">${user === serverUser() ? "SERVER — privileged" : "PROJECT — rootless"}</span></div>
      <div class="kv" style="margin-top:8px">
        <div>Codex</div><div class="mono">${esc(a.codex_version || "—")}</div>
        <div>Codex auth</div><div class="mono">${esc(a.codex_auth || "—")}</div>
        <div>Claude Code</div><div class="mono">${esc(a.claude_version || "—")}</div>
        <div>Claude auth</div><div class="mono">${esc(a.claude_auth || "—")}</div>
        <div>OpenCode</div><div class="mono">${esc(a.opencode_version || "—")}</div>
        <div>Docker</div><div class="mono">${esc(a.docker || "—")}</div>
        <div>Server tmux</div><div class="mono">${esc(a.tmux_unit || "—")}</div>
      </div>
      <details style="margin-top:8px"><summary>Provider OpenCode configurati</summary>
        <pre>${esc(a.opencode_providers || "—")}</pre></details>
      <div style="margin-top:10px"><b>Catalogo modelli</b>${catalog(user)}</div>
      <details style="margin-top:4px"><summary>Chiavi pubbliche SSH disponibili (${(a.ssh_keys || []).length})</summary>
        ${(a.ssh_keys || []).map(k => `<div class="muted mono">${esc(k.path)}</div><pre>${esc(k.key)}</pre>`).join("")
          || '<div class="muted">Nessuna chiave pubblica.</div>'}</details>
      <div class="row" style="margin-top:10px">
        ${profs.map(p => `<button class="small" data-login="${esc(p.id)}" data-user="${esc(user)}">${esc(p.login.label)}</button>`).join("")}
      </div>
    </div>`;
  };
  view().innerHTML = `<div class="row spread"><h2>Accounts / Providers</h2>
      <button class="small" id="ac-refresh">Aggiorna</button></div>
    ${freshness(d.meta)}
    ${block(projectUser(), d.accounts[projectUser()] || {})}
    ${block(serverUser(), d.accounts[serverUser()] || {})}
    <div class="card">
      <b>Note sui login</b>
      <ul class="muted" style="padding-left:18px">
        <li><b>Codex</b>: nel menu scegli <b>2. Sign in with Device Code</b>: URL e codice compaiono nel
          terminale web e si completano dal browser. L'opzione 1 apre invece un server su
          <span class="mono">127.0.0.1:1455</span> del server e richiede un tunnel SSH
          (<span class="mono">ssh -L 1455:127.0.0.1:1455 ${serviceUser()}@${location.hostname}</span>).</li>
        <li><b>Claude Code</b>: la sessione mostra un URL; dopo l'autorizzazione si incolla il codice nel terminale web.</li>
        <li><b>OpenCode</b>: seleziona il provider (es. DeepSeek) e incolla la API key. Le credenziali restano
          in <span class="mono">~/.local/share/opencode/auth.json</span> dell'utente Unix.</li>
      </ul>
    </div>`;
  // `refresh=1` non attende la raccolta: dichiara vecchio ciò che c'è e la fa
  // ripartire. Il ciclo di `liveView` mostra i dati nuovi appena esistono.
  $("#ac-refresh").onclick = async () => {
    try { await api("/api/accounts?refresh=1", {}, "aggiornamento account"); viewAccounts(); }
    catch (e) { showError(e); }
  };
  $$("[data-login]").forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        const r = await post("/api/accounts/login", { unix_user: b.dataset.user, profile_id: b.dataset.login }, "avvio login");
        if (r.note) toast(r.note);
        location.hash = "#/session/" + r.session.id;
      } catch (e) { showError(e); b.disabled = false; }
    };
  });
  $$("[data-model-refresh]").forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        const r = await post("/api/accounts/models/refresh", { unix_user: b.dataset.user, provider: b.dataset.modelRefresh }, "aggiornamento modelli");
        if (BOOT) BOOT.catalog = Object.assign({}, BOOT.catalog, { [b.dataset.user]: r.catalog });
        toast("Catalogo modelli aggiornato");
        await api("/api/accounts?refresh=1", {}, "aggiornamento account");
        viewAccounts();
      } catch (e) { showError(e); b.disabled = false; }
    };
  });
}

// ------------------------------------------------------------------ status

// ------------------------------------------------------- consumo dei piani
//
// Il consumo è dell'account, non dell'utente Unix che lo legge: le credenziali
// vivono nella home di devagent e hostagent, ma il piano dietro è lo stesso e
// va detto una volta sola. Il backend unisce le letture identiche; se due
// utenti leggessero valori diversi comparirebbero righe distinte, perché quel
// caso significherebbe due account davvero diversi.

const USAGE_STATE = {
  ok: ["letto", "sent", ""],
  unverified: ["non riletto ora", "idle",
    "sono mostrati gli ultimi valori noti: non è stato possibile riconfermarli"],
  unconfigured: ["nessuna credenziale", "failed",
    "nessuna credenziale locale per il provider: non c'è consumo da leggere"],
  never_checked: ["mai letto", "ended", "il giro periodico non ha ancora letto questo provider"],
};

function untilText(epoch) {
  const left = epoch * 1000 - Date.now();
  if (left <= 0) return "a momenti";
  const h = Math.floor(left / 3600000);
  const m = Math.round((left % 3600000) / 60000);
  if (h >= 24) return `fra ${Math.floor(h / 24)}g ${h % 24}h`;
  return h ? `fra ${h}h ${m}m` : `fra ${m}m`;
}

function usageWindow(w) {
  const pct = Math.max(0, Math.min(100, Number(w.percent) || 0));
  const cls = pct >= 90 ? "crit" : pct >= 70 ? "warn" : "ok";
  return `<div style="margin-top:7px">
    <div class="row spread"><span>${esc(w.label)}</span>
      <span class="mono">${pct}%${w.resets_at ? " · reset " + esc(untilText(w.resets_at)) : ""}</span></div>
    <div class="meter"><div class="meter-fill ${cls}" style="width:${pct}%"></div></div>
  </div>`;
}

// Nomi corti: nella barra laterale «Anthropic / Claude Code (OAuth)» andrebbe
// a capo tre volte senza dire nulla di più.
const USAGE_SHORT = { claude: "Claude Code", codex: "Codex", deepseek: "DeepSeek" };

function usagePanel(items, meta) {
  const rows = (items || []).map(u => {
    const [label, cls, why] = USAGE_STATE[u.status] || [u.status, "ended", ""];
    const bars = (u.windows || []).map(usageWindow).join("");
    const balance = u.balance
      ? `<div class="mono" style="margin-top:7px">${esc(String(u.balance.amount))}
         ${esc(u.balance.currency)} residui</div>` : "";
    // l'utente Unix si nomina solo quando la lettura non vale per entrambi:
    // altrimenti suggerirebbe una distinzione che non esiste
    const who = (u.users || []).length === 1 ? `credenziali di ${esc(u.users[0])}` : "";
    const note = [u.plan ? "piano " + esc(u.plan) : "", who, esc(u.error)].filter(Boolean).join(" · ");
    return `<div class="usage-item">
      <div class="row spread"><b>${esc(USAGE_SHORT[u.provider] || u.label)}</b>
        ${u.status === "ok" ? "" : `<span class="tag ${cls}">${esc(label)}</span>`}</div>
      ${note ? `<div class="muted">${note}</div>` : ""}
      ${bars + balance || `<div class="muted">${esc(u.status === "unverified"
        ? "nessun valore ancora letto con successo" : why || "nessun dato di consumo")}</div>`}
    </div>`;
  }).join("");
  return `<div class="card"><div class="row spread"><b>Consumo dei piani</b>
      <button class="ghost small" id="us-refresh" title="Interroga subito i provider">⟳</button></div>
    ${help("Percentuali lette dalla stessa sorgente che ogni CLI usa per il proprio indicatore: " +
           "nessuna stima locale e nessun conteggio di token. Un giro in background le rilegge " +
           "ogni pochi minuti; questo pannello rilegge il database ogni minuto e non attende " +
           "mai la rete.")}
    ${rows || '<div class="muted">Nessun provider configurato.</div>'}
    ${meta && meta.generated_at
      ? `<div class="muted" style="margin-top:12px">letto alle ${esc(hhmm(meta.generated_at))}</div>` : ""}
  </div>`;
}

// La barra laterale non deve mai poter rompere la pagina che affianca: un
// errore qui si ferma qui.
async function loadUsagePanel() {
  const box = $("#usage-panel");
  if (!box || document.body.classList.contains("usage-off")) return;
  try {
    const d = await api("/api/usage", {}, "consumo dei piani");
    box.innerHTML = usagePanel(d.usage, d.meta);
  } catch (e) {
    if (!box.innerHTML) box.innerHTML = '<div class="card muted">Consumo non disponibile.</div>';
    return;
  }
  const btn = $("#us-refresh", box);
  if (btn) {
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "…";
      try {
        await post("/api/usage/refresh", {}, "lettura consumo");
        toast("Consumo riletto");
      } catch (e) { showError(e); }
      loadUsagePanel();
    };
  }
}

function applyUsagePanel(on) {
  document.body.classList.toggle("usage-off", !on);
  localStorage.setItem("agenthub-usage", on ? "1" : "0");
  const b = $("#usage-toggle");
  if (b) b.textContent = on ? "consumo ◂" : "consumo ▸";
  if (on) loadUsagePanel();
  // Il terminale di una sessione si ridimensiona sull'evento `resize`, ma qui
  // cambia la larghezza disponibile senza che la finestra cambi: senza questo
  // evento tmux resterebbe alle colonne di prima, con il testo troncato.
  window.dispatchEvent(new Event("resize"));
}

// ------------------------------------------------------- salute del server

// OK resta verde: qui «va bene» non significa «in esecuzione».
const HEALTH_CLS = { OK: "sent", WARNING: "idle", CRITICAL: "failed" };

function healthCard(h) {
  const last = (h && h.last) || {};
  if (!last.checks) {
    return `<div class="card"><b>Salute del server</b>
      <div class="muted">Nessun controllo ancora eseguito. Il timer gira ogni 30 minuti; puoi
        eseguirlo subito con il pulsante.</div>
      <div class="row" style="margin-top:8px"><button class="small primary" id="hl-run">Esegui controllo</button></div></div>`;
  }
  const rows = last.checks.map(c => `<div><span class="tag ${HEALTH_CLS[c.status] || "ended"}">${esc(c.status)}</span></div>
    <div><b>${esc(c.label)}</b><div class="muted">${esc(c.detail)}</div></div>`).join("");
  const hist = (h.history || []).map(e =>
    `<div class="muted">${esc(ts(e.at))} — ${esc(e.previous || "?")} → <b>${esc(e.overall)}</b>${
      e.problems && e.problems.length ? " (" + esc(e.problems.map(p => p.name).join(", ")) + ")" : ""}</div>`).join("");
  return `<div class="card">
    <div class="row spread"><b>Salute del server</b>
      <span class="tag ${HEALTH_CLS[last.overall] || "ended"}">${esc(last.overall)}</span></div>
    ${help("Controllo deterministico eseguito da agent-hub-health: nessuna euristica, soglie in " +
           "/etc/agent-hub/controller.json. Nessuna notifica esterna: solo questa pagina e lo storico locale.")}
    <div class="muted">Ultimo controllo ${esc(ts(last.generated_at))} · ${last.duration_ms || 0} ms ·
      OK ${last.counts.OK} · WARNING ${last.counts.WARNING} · CRITICAL ${last.counts.CRITICAL}</div>
    <div class="muted mono">timer: ${esc(h.timer_active || "?")}</div>
    <div class="kv" style="margin-top:10px">${rows}</div>
    <div class="row" style="margin-top:10px">
      <button class="small primary" id="hl-run">Esegui controllo ora</button>
      <button class="small" id="hl-copy">Copia sintesi</button>
    </div>
    <details style="margin-top:8px"><summary>Cambi di stato registrati (${(h.history || []).length})</summary>
      ${hist || '<div class="muted">Nessun cambio di stato registrato.</div>'}</details>
    <details style="margin-top:4px"><summary>Dettaglio JSON</summary>
      <pre>${esc(JSON.stringify(last, null, 2))}</pre></details>
  </div>`;
}

function wireHealth(container, h) {
  const run = $("#hl-run");
  if (run) {
    run.onclick = async () => {
      run.disabled = true;
      run.textContent = "controllo in corso…";
      try {
        const fresh = await post("/api/health/run", {}, "controllo salute server");
        container.innerHTML = healthCard(fresh);
        wireHealth(container, fresh);
        toast("Controllo eseguito: " + fresh.last.overall, fresh.last.overall === "OK" ? "ok" : "err");
      } catch (e) { showError(e); run.disabled = false; run.textContent = "Esegui controllo ora"; }
    };
  }
  const cp = $("#hl-copy");
  if (cp) cp.onclick = () => copyText(((h && h.last) || {}).summary_text || "", "Sintesi copiata");
}

// Le tre letture sono indipendenti: in fila costavano la somma dei tempi.
async function fetchStatus() {
  const [s, dk, health] = await Promise.all([
    api("/api/status", {}, "stato sistema"),
    api("/api/status/docker", {}, "stato docker"),
    api("/api/health", {}, "salute server").catch(() => ({ last: {}, history: [] })),
  ]);
  const metas = [s.meta, dk.meta].filter(Boolean);
  return { s, dk, health, meta: {
    // la pagina è vecchia quanto il suo pezzo più vecchio
    generated_at: metas.map(m => m.generated_at).sort()[0] || "",
    stale: metas.some(m => m.stale),
    refreshing: metas.some(m => m.refreshing),
  } };
}

async function viewStatus() {
  await liveView("status", fetchStatus, renderStatus);
}

function renderStatus({ s, dk, health, meta }) {
  const unit = u => {
    const t = (s.tmux_units || {})[u] || {};
    return `${esc(t.ActiveState || t.error || "—")} · ${esc(t.ControlGroup || "")}`;
  };
  view().innerHTML = `<h2>Status</h2>
    ${freshness(meta)}
    <div id="health">${healthCard(health)}</div>
    <div class="card"><b>Agent Hub service</b>
      <div class="mono">${esc(s.service)}</div><pre>${esc(s.service_detail)}</pre></div>
    <div class="card"><b>Server tmux delle sessioni</b>
      ${help("Le sessioni girano dentro queste unit systemd utente, non dentro agent-hub.service: " +
             "per questo un riavvio del backend non le termina.")}
      <div class="kv">
        <div>${projectUser()}</div><div class="mono">${unit(projectUser())}</div>
        <div>${serverUser()}</div><div class="mono">${unit(serverUser())}</div>
      </div></div>
    <div class="grid2">
      <div class="card project"><b>Project Docker — rootless</b>
        <div class="muted mono">utente: devagent</div>
        <pre>${esc(dk.project_docker_rootless.info || "—")}</pre>
        <details><summary>Container</summary><pre>${esc(dk.project_docker_rootless.containers || "(nessuno)")}</pre></details></div>
      <div class="card server"><b>Server Docker — privileged</b>
        <div class="muted mono">utente: hostagent</div>
        <pre>${esc(dk.server_docker_privileged.info || "—")}</pre></div>
    </div>
    <div class="card"><b>Tailscale</b><pre>${esc(s.tailscale)}</pre>
      <b>Tailscale Serve</b><pre>${esc(s.tailscale_serve)}</pre></div>
    <div class="card"><b>Sessioni tmux per utente</b>
      <pre>devagent (${s.tmux_sessions.devagent.length}):\n${esc(s.tmux_sessions.devagent.join("\n") || "—")}\n\nhostagent (${s.tmux_sessions.hostagent.length}):\n${esc(s.tmux_sessions.hostagent.join("\n") || "—")}</pre></div>
    <div class="card"><b>Versioni harness e strumenti</b>
      <div class="kv">${Object.entries(s.versions).map(([k, v]) =>
        `<div>${esc(k)}</div><div class="mono">${esc(v)}</div>`).join("")}</div></div>
    <div class="card"><b>Spazio disco</b><pre>${esc(s.disk)}</pre></div>
    <div class="card"><b>Directory principali</b>
      <pre>${esc(Object.values(s.dirs).join("\n"))}</pre></div>`;

  wireHealth($("#health"), health);
}

// ------------------------------------------------------------------- riunioni

let _meetingPoll = null;

function stopMeetingPoll() {
  if (_meetingPoll) { clearInterval(_meetingPoll); _meetingPoll = null; }
}

// Mappatura da CATALOG_STATE/status a tag CSS.
const _MTG_TAG_CLS = { ok: "done", err: "failed", warn: "idle", muted: "ended" };

// Progetto scelto nella pagina Riunioni: sopravvive alla chiusura della tab.
const MTG_PROJECT_KEY = "agenthub-meetings-project";

function rememberedMeetingProject() {
  try { return localStorage.getItem(MTG_PROJECT_KEY) || ""; } catch (e) { return ""; }
}

function rememberMeetingProject(slug) {
  try {
    if (slug) localStorage.setItem(MTG_PROJECT_KEY, slug);
    else localStorage.removeItem(MTG_PROJECT_KEY);
  } catch (e) { /* storage non disponibile: la scelta vale per questa sessione */ }
}

// Valore di partenza per <input type="datetime-local">: adesso, ora locale.
function nowDatetimeLocal() {
  const d = new Date();
  d.setSeconds(0, 0);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
         `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function viewMeetings() {
  stopMeetingPoll();
  const [pjData, mtgData] = await Promise.all([
    api("/api/projects", {}, "elenco progetti"),
    api("/api/meetings", {}, "elenco riunioni"),
  ]);
  const projects = pjData.projects || [];
  const allMeetings = mtgData.meetings || [];

  // slug scelto: query param, poi l'ultimo usato, infine il primo progetto
  const qslug = qparams().get("project") || "";
  const saved = rememberedMeetingProject();
  const known = s => projects.some(p => p.slug === s);
  const selSlug = known(qslug) ? qslug
    : (known(saved) ? saved : (projects.length ? projects[0].slug : ""));
  rememberMeetingProject(selSlug);
  let mode = qparams().get("action") === "new" ? "new" : "history";

  // Data e ora sono precompilate con l'istante di apertura e restano
  // modificabili; il valore digitato sopravvive ai re-render del polling.
  let newMeetingDate = nowDatetimeLocal();

  let currentSlug = selSlug;
  let meetings = allMeetings.filter(m => m.project_slug === currentSlug);

  const profiles = BOOT.profiles || [];

  // --- filtri catalogo (PROJECT / devagent) ---
  const devProfiles = profiles.filter(p => (p.allowed_users || []).includes(projectUser()));
  const user = projectUser();

  const render = () => {
    const projOpts = projects.map(p =>
      `<option value="${esc(p.slug)}"${p.slug === currentSlug ? " selected" : ""}>${esc(p.slug)}</option>`).join("");
    const noProj = !projects.length;

    // --- selettore profilo / modello / effort per una nuova riunione ---
    const selProfile = () => {
      if (!devProfiles.length) return '<option value="">nessun profilo PROJECT disponibile</option>';
      return devProfiles.map(p =>
        `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("");
    };

    const mtgRows = meetings.map(m => meetingRow(m)).join("") ||
      '<div class="muted">Nessuna riunione per questo progetto.</div>';

    // --- HTML ---
    view().innerHTML = `<div class="row spread"><h2>Riunioni · ${esc(currentSlug || "nessun progetto")}</h2>
        <a class="plain" href="#/projects/${encodeURIComponent(currentSlug)}?tab=meetings"><button class="small" ${noProj ? "disabled" : ""}>← Scheda riunioni</button></a>
      </div>
      ${noProj ? '<div class="card muted">Nessun progetto registrato. Creane uno dalla pagina Progetti.</div>' : ""}

      <div class="row meeting-tabs">
        <button class="small ${mode === "history" ? "primary" : ""}" id="mtg-tab-history">Riunioni precedenti</button>
        <button class="small ${mode === "new" ? "primary" : ""}" id="mtg-tab-new" ${noProj ? "disabled" : ""}>Nuova riunione</button>
      </div>

      <div class="card">
        <label>Progetto</label>
        <select id="mtg-project">${projOpts}</select>
      </div>

      ${mode === "new" ? `<div class="card">
        <h3 style="margin-top:0">Nuova riunione</h3>
        ${noProj ? '<div class="muted">Registra un progetto per creare riunioni.</div>' : ""}
        <div id="mtg-new-fields" ${noProj ? 'style="display:none"' : ""}>
          <label>Titolo</label>
          <input id="mtg-title" placeholder="es. Sprint review 12">
          <label>Data e ora</label>
          <input type="datetime-local" id="mtg-date" value="${esc(newMeetingDate)}">
          <div class="muted">Precompilate con data e ora di adesso: modificale se la riunione è di un altro momento.</div>
          <label>Registrazione audio</label>
          <input type="file" id="mtg-audio" accept="audio/*,.m4a,.mp3,.wav,.ogg,.opus,.webm,.mp4,.aac,.flac">
          <div class="muted">Formati accettati: audio/*, m4a, mp3, wav, ogg, opus, webm, mp4, aac, flac</div>
          <label>Profilo harness</label>
          <select id="mtg-profile">${selProfile()}</select>
          <div id="mtg-model-wrap" style="display:none">
            <label>Modello</label>
            <select id="mtg-model"></select>
            <input id="mtg-model-other" placeholder="id del modello" style="display:none;margin-top:6px">
            <div class="muted" id="mtg-model-note" style="margin-top:4px"></div>
          </div>
          <div id="mtg-effort-wrap" style="display:none">
            <label>Effort di ragionamento</label>
            <select id="mtg-effort"></select>
            <div class="muted" id="mtg-effort-note" style="margin-top:4px"></div>
          </div>
          <label>Istruzioni aggiuntive post-approvazione (opzionali)</label>
          <textarea id="mtg-prompt" placeholder="Vincoli o indicazioni da riportare nella roadmap dopo l'approvazione (può restare vuoto)"></textarea>
          <label class="inline" style="margin-top:12px">
            <input type="checkbox" id="mtg-context" checked>
            <span>Aggiorna contesto dopo doppia approvazione</span>
          </label>
          ${help("Trascrizione, analisi e notifiche Telegram avvengono comunque. " +
                 "Dopo la doppia approvazione una sessione dedicata aggiorna solo roadmap e documentazione futura; " +
                 "non modifica il codice. Se la casella è spuntata, rigenera anche il package di contesto.")}
          <div class="row" style="margin-top:14px">
            <button class="primary" id="mtg-go">Crea riunione</button>
            <span id="mtg-progress" class="muted" style="display:none"></span>
          </div>
          <div id="mtg-err"></div>
        </div>
      </div>` : `<div class="card" id="mtg-list">${mtgRows}</div>`}`;

    // --- bindings ---
    wireMeetings();
  };

  async function refreshMeetings() {
    if (!currentSlug) { meetings = []; return; }
    try {
      const d = await api("/api/meetings?project=" + encodeURIComponent(currentSlug), {}, "elenco riunioni");
      meetings = d.meetings || [];
    } catch (e) {
      // non blocchiamo la view per un errore di refresh
    }
  }

  // Polling ogni 5 secondi se ci sono riunioni attive.
  function syncPoll() {
    stopMeetingPoll();
    if (mode === "history" && meetings.some(m => MEETING_ACTIVE.has(m.status))) {
      _meetingPoll = setInterval(async () => {
        await refreshMeetings();
        if (!_meetingPoll) return;   // cleanup intervenuto durante la fetch
        render();
        syncPoll();                    // rivaluta se serve ancora il polling
      }, 5000);
    }
  }

  async function switchProject(slug) {
    currentSlug = slug;
    rememberMeetingProject(slug);
    meetings = [];
    stopMeetingPoll();
    render();
    await refreshMeetings();
    render();
  }

  function wireMeetings() {
    const historyTab = $("#mtg-tab-history");
    const newTab = $("#mtg-tab-new");
    if (historyTab) historyTab.onclick = () => { mode = "history"; render(); };
    if (newTab) newTab.onclick = () => { mode = "new"; render(); };
    const projSel = $("#mtg-project");
    if (projSel) {
      projSel.onchange = () => switchProject(projSel.value);
    }

    // Data e ora: il valore digitato viene ricordato, cosi' un re-render del
    // polling non riporta il campo all'istante di apertura della pagina.
    const dateInput = $("#mtg-date");
    if (dateInput) {
      dateInput.value = newMeetingDate;
      dateInput.oninput = () => { newMeetingDate = dateInput.value; };
    }

    // --- profilo / modello / effort (riusa helper esistenti) ---
    const profSel = $("#mtg-profile");
    const modelSel = $("#mtg-model");
    const modelOther = $("#mtg-model-other");

    if (!profSel) { syncPoll(); return; } // nello storico il form non e' nel DOM

    const hasProfiles = devProfiles.length > 0;

    function selectedMtgModel() {
      if (!modelSel || !modelSel.value) return "";
      return modelSel.value === "__other__" ? (modelOther ? modelOther.value.trim() : "") : modelSel.value;
    }

    function syncMtgModel() {
      const pid = profSel ? profSel.value : "";
      const p = profiles.find(x => x.id === pid);
      if (!modelSel) return;
      const keep = selectedMtgModel();
      modelSel.innerHTML = modelOptions(user, pid, keep);
      modelSel.disabled = !(p && p.supports_model);
      // model note
      const note = $("#mtg-model-note");
      if (note && p) {
        const st = catalogState(user, pid);
        if (!p.supports_model) { note.textContent = `${p.harness} non permette di scegliere il modello.`; }
        else if (!st) { note.textContent = ""; }
        else {
          const n = (st.group.models || []).length;
          note.innerHTML = `Catalogo <b>${esc(st.label)}</b>${st.last_checked ? " · " + esc(ts(st.last_checked)) : ""} · ${
            n} modelli${st.error ? " · " + esc(st.error) : ""}`;
        }
      }
    }

    function syncMtgEffort() {
      const pid = profSel ? profSel.value : "";
      const p = profiles.find(x => x.id === pid);
      const sel = $("#mtg-effort");
      if (!sel || !p) return;
      const { levels, source } = effortLevelsFor(p, user, selectedMtgModel());
      const wanted = sel.value || p.default_effort || "medium";
      sel.innerHTML = levels.length
        ? `<option value="">(default dell'harness)</option>` + levels.map(l =>
            `<option value="${esc(l)}">${esc(l)}</option>`).join("")
        : `<option value="">non disponibile</option>`;
      sel.value = levels.includes(wanted) ? wanted : (levels.includes("medium") ? "medium" : "");
      sel.disabled = !levels.length;
      const note = $("#mtg-effort-note");
      if (note) {
        note.textContent = levels.length
          ? (source === "modello"
              ? `Livelli dichiarati dal modello: ${levels.join(", ")}.`
              : `Livelli supportati da ${p.harness}: ${levels.join(", ")}.`)
          : (source === "modello"
              ? "Il modello selezionato non espone livelli di effort."
              : `${p.harness} non espone un livello di effort.`);
      }
    }

    function syncMtgProfile() {
      const pid = profSel ? profSel.value : "";
      const p = profiles.find(x => x.id === pid);
      if (!$("#mtg-model-wrap")) return;
      const show = !!(p && p.supports_model);
      $("#mtg-model-wrap").style.display = show ? "" : "none";
      $("#mtg-effort-wrap").style.display = p ? "" : "none";
      if (show) syncMtgModel();
      if (p) syncMtgEffort();
    }

    if (profSel) profSel.onchange = syncMtgProfile;
    if (modelSel) {
      modelSel.onchange = () => {
        const other = modelSel.value === "__other__";
        if (modelOther) modelOther.style.display = other ? "" : "none";
        if (other && modelOther) modelOther.focus();
        syncMtgEffort();
      };
    }
    if (modelOther) modelOther.oninput = () => syncMtgEffort();

    syncMtgProfile();

    // --- creazione riunione ---
    const goBtn = $("#mtg-go");
    if (goBtn) {
      goBtn.onclick = async () => {
        const errBox = $("#mtg-err");
        if (errBox) errBox.innerHTML = "";
        const title = ($("#mtg-title") || {}).value || "";
        const dateVal = ($("#mtg-date") || {}).value || "";
        const audioInput = $("#mtg-audio");
        const file = audioInput && audioInput.files && audioInput.files.length ? audioInput.files[0] : null;

        if (!title.trim()) { toast("Inserisci un titolo", "err"); return; }
        if (!currentSlug) { toast("Seleziona un progetto", "err"); return; }
        if (!dateVal) { toast("Inserisci data e ora della riunione", "err"); return; }
        if (!file) { toast("Seleziona la registrazione audio", "err"); return; }

        goBtn.disabled = true;
        const prog = $("#mtg-progress");
        if (prog) { prog.style.display = ""; prog.textContent = "Invio in corso…"; }

        try {
          const fd = new FormData();
          fd.append("project_slug", currentSlug);
          fd.append("title", title);
          if (dateVal) fd.append("meeting_date", dateVal);
          fd.append("profile_id", (profSel && profSel.value) || (devProfiles.length ? devProfiles[0].id : ""));
          fd.append("model", selectedMtgModel());
          const effortSel = $("#mtg-effort");
          fd.append("effort", (effortSel && !effortSel.disabled) ? effortSel.value : "");
          fd.append("operational_prompt", ($("#mtg-prompt") || {}).value || "");
          const ctxCheck = $("#mtg-context");
          fd.append("context_on_approval", ctxCheck && ctxCheck.checked ? "1" : "0");
          if (file) fd.append("file", file, file.name);

          const r = await api("/api/meetings", { method: "POST", body: fd }, "creazione riunione");
          const mtg = r.meeting || r;
          toast("Riunione creata: " + (mtg.title || mtg.id));
          // Apre il dettaglio
          location.hash = "#/meetings/" + encodeURIComponent(mtg.id);
        } catch (e) {
          showError(e, errBox || undefined);
          goBtn.disabled = false;
          if (prog) prog.style.display = "none";
        }
      };
    }

    // --- polling ---
    syncPoll();
  }

  // Avvio
  render();
  await refreshMeetings();
  render();

  // cleanup
  const prev = cleanup;
  cleanup = () => {
    stopMeetingPoll();
    if (prev) try { prev(); } catch (e) { /* noop */ }
  };
}

// Dettaglio di una singola riunione.
async function viewMeeting(id) {
  id = decodeURIComponent(id);
  stopMeetingPoll();
  let d;
  try {
    d = await api("/api/meetings/" + encodeURIComponent(id), {}, "dettaglio riunione");
  } catch (e) {
    view().innerHTML = "";
    showError(e, view());
    return;
  }
  const m = d.meeting || {};
  const transcript = d.transcript || "";
  const proposal = m.proposal || {};
  const proposalList = (items, meta = []) => (Array.isArray(items) ? items : []).map(item => {
    if (item && typeof item === "object") {
      const title = item.title || item.decision || item.action || "";
      const detail = item.detail || item.description || "";
      const extras = meta.map(([key, label]) => item[key]
        ? `<div class="muted"><b>${esc(label)}:</b> ${esc(item[key])}</div>` : "").join("");
      return `<li>${title ? `<b>${esc(title)}</b>` : ""}${
        title && detail ? " — " : ""}${esc(detail)}${extras}</li>`;
    }
    return `<li>${esc(item)}</li>`;
  }).join("");
  const [statusLabel, statusCls] = MEETING_STATUS[m.status] || [m.status || "sconosciuto", "ended"];

  view().innerHTML = `<div class="row spread">
      <h2>${esc(m.title || "Riunione")}</h2>
      <a class="plain" href="#/projects/${encodeURIComponent(m.project_slug || "")}?tab=meetings"><button class="small">← Scheda riunioni</button></a></div>

    <div class="card meeting-detail">
      <div class="row">
        <span class="tag ${statusCls}">${esc(statusLabel)}</span>
        ${m.round != null ? `<span class="tag ended">Round ${esc(String(m.round))}</span>` : ""}
        <span class="tag ${(m.approval_count || 0) >= 2 ? "done" : "launching"}">Approvazioni ${m.approval_count || 0}/2</span>
        ${m.error ? `<span class="tag failed">Errore</span>` : ""}
      </div>
      <div class="kv" style="margin-top:10px">
        <div>Data</div><div>${esc(m.meeting_date ? ts(m.meeting_date) : "—")}</div>
        <div>Profilo</div><div class="mono">${esc(m.profile_id || "—")}</div>
        ${m.model ? `<div>Modello</div><div class="mono">${esc(m.model)}</div>` : ""}
        <div>Creata</div><div>${esc(ts(m.created_at))}</div>
        ${m.analysis_session_id ? `<div>Sessione analisi</div><div><a class="plain" href="#/session/${encodeURIComponent(m.analysis_session_id)}"><button class="small">Apri sessione</button></a></div>` : ""}
        ${m.implementation_session_id ? `<div>Sessione implementazione</div><div><a class="plain" href="#/session/${encodeURIComponent(m.implementation_session_id)}"><button class="small">Apri sessione</button></a></div>` : ""}
      </div>

      ${m.error ? `<div class="errbox" style="margin-top:10px">
        <b>Errore</b><div>${esc(m.error)}</div>
      </div>` : ""}

      ${m.status === "failed" ? `<div class="row" style="margin-top:10px">
        <button class="small primary" id="mtg-retry">Riprova</button>
      </div>` : ""}

      ${help("L'approvazione e le modifiche alla proposta di riunione avvengono via Telegram. " +
             "Servono due chat Telegram distinte e autorizzate perché la riunione passi allo stato «Approvato». " +
             "Dopo la doppia approvazione una sessione Agent Hub dedicata aggiorna roadmap e documentazione futura, non il codice.")}
    </div>

    ${proposal.summary || proposal.decisions || proposal.actions
      || proposal.open_questions || proposal.operational_prompt || proposal.target_architecture ? `
    <h3>Report da approvare</h3>
    <div class="card">
      ${proposal.summary ? `<div style="margin-bottom:10px"><b>Riepilogo</b><div>${esc(proposal.summary)}</div></div>` : ""}
      ${Array.isArray(proposal.decisions) && proposal.decisions.length
        ? `<div style="margin-bottom:10px"><b>Decisioni effettivamente prese</b><ul>${proposalList(
            proposal.decisions, [["rationale", "Razionale"], ["evidence", "Evidenza"]])}</ul></div>` : ""}
      ${Array.isArray(proposal.open_questions) && proposal.open_questions.length
        ? `<div style="margin-bottom:10px"><b>Questioni ancora aperte</b>
             <div class="muted">Sono informative: non diventano decisioni con l'approvazione del report.</div>
             <ul>${proposalList(proposal.open_questions)}</ul></div>` : ""}
      ${Array.isArray(proposal.actions) && proposal.actions.length
        ? `<div style="margin-bottom:10px"><b>Azioni concordate</b><ul>${proposalList(
            proposal.actions, [["owner", "Responsabile"], ["due_date", "Scadenza"]])}</ul></div>` : ""}
      ${proposal.operational_prompt
        ? `<div style="margin-bottom:10px"><b>Istruzioni documentali post-approvazione</b>
             <div class="muted">Saranno usate per aggiornare roadmap e documentazione futura.</div>
             <div class="proposal-text">${esc(proposal.operational_prompt)}</div></div>` : ""}
      ${proposal.target_architecture
        ? `<div><b>Architettura target consolidata</b>
             <div class="muted">Finirà nel contesto del progetto se l'aggiornamento automatico era attivo.</div>
             <div class="proposal-text">${esc(proposal.target_architecture)}</div></div>` : ""}
    </div>` : ""}

    <h3>Trascrizione</h3>
    <div class="card">
      ${transcript
        ? `<pre class="transcript" id="mtg-transcript"></pre>`
        : '<div class="muted">Trascrizione non ancora disponibile.</div>'}
    </div>`;

  // Imposta la trascrizione via textContent (nessuna interpolazione HTML)
  const trPre = $("#mtg-transcript");
  if (trPre && transcript) trPre.textContent = transcript;

  // Retry
  const retryBtn = $("#mtg-retry");
  if (retryBtn) {
    retryBtn.onclick = async () => {
      retryBtn.disabled = true;
      try {
        await api(`/api/meetings/${encodeURIComponent(id)}/retry`, { method: "POST", body: {} }, "retry riunione");
        toast("Retry avviato — ricarica in corso");
        setTimeout(() => route(), 1200);
      } catch (e) {
        showError(e);
        retryBtn.disabled = false;
      }
    };
  }

  // Polling se la riunione è ancora attiva
  if (MEETING_ACTIVE.has(m.status)) {
    _meetingPoll = setInterval(async () => {
      let fresh;
      try {
        fresh = await api("/api/meetings/" + encodeURIComponent(id), {}, "stato riunione");
      } catch (e) { return; }
      const fm = fresh.meeting || {};
      const st = fm.status;
      if (st !== m.status || fm.updated_at !== m.updated_at || !MEETING_ACTIVE.has(st)) {
        stopMeetingPoll();
        route();
      }
    }, 5000);
  }

  const prev = cleanup;
  cleanup = () => {
    stopMeetingPoll();
    if (prev) try { prev(); } catch (e) { /* noop */ }
  };
}

// -------------------------------------------------------------------- boot

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", mode);
  localStorage.setItem("agenthub-theme", mode);
  const btn = $("#theme-toggle");
  if (btn) btn.textContent = mode === "auto" ? "tema: auto" : (mode === "dark" ? "tema: scuro" : "tema: chiaro");
}

(async function boot() {
  applyTheme(localStorage.getItem("agenthub-theme") || "auto");
  if (localStorage.getItem("agenthub-help") === "1") document.body.classList.add("help-on");
  $("#theme-toggle").onclick = () => {
    const order = ["auto", "light", "dark"];
    const cur = localStorage.getItem("agenthub-theme") || "auto";
    applyTheme(order[(order.indexOf(cur) + 1) % order.length]);
  };
  $("#help-toggle").onclick = () => {
    const on = document.body.classList.toggle("help-on");
    localStorage.setItem("agenthub-help", on ? "1" : "0");
  };
  $("#usage-toggle").onclick = () =>
    applyUsagePanel(document.body.classList.contains("usage-off"));
  try {
    await refreshBoot();
  } catch (e) {
    document.body.innerHTML = `<main><div class="card alert"><b>Accesso negato</b>
      <p>${esc((e && e.detail) || (e && e.message) || "errore sconosciuto")}</p>
      <p class="muted">Agent Hub accetta richieste solo tramite Tailscale Serve.</p></div></main>`;
    return;
  }
  // Il consumo vale per tutta l'applicazione, non per una scheda: sta nella
  // barra laterale, resta visibile ovunque e si rilegge da solo. È una query
  // sul database (i numeri veri li porta il giro periodico del backend),
  // quindi ripeterla ogni minuto non costa nulla.
  applyUsagePanel(localStorage.getItem("agenthub-usage") !== "0");
  setInterval(loadUsagePanel, 60_000);

  ["pointerdown", "keydown", "touchstart", "wheel"].forEach(type =>
    document.addEventListener(type, () => recordActivity(), { passive: true, capture: true }));
  window.addEventListener("focus", () => recordActivity(true));
  recordActivity(true);

  window.addEventListener("hashchange", route);
  // Una SPA gia' aperta non richiede di nuovo index.html. Il confronto con
  // l'impronta servita dal backend la ricarica dopo il prossimo deploy.
  setInterval(() => refreshBoot().catch(() => {}), 60_000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { refreshBoot().catch(() => {}); loadUsagePanel(); recordActivity(true); }
  });
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }
  route();
})();
