"""Agent Hub — servizio web privato per sessioni agentiche persistenti.

Gira come l'account di servizio, ascolta solo su loopback ed e' esposto nella tailnet
tramite Tailscale Serve. Le sessioni vivono in tmux sotto `devagent` o
`hostagent`, avviate attraverso i wrapper root-owned in
/usr/local/libexec/agent-hub/.

Persistenza delle sessioni
--------------------------
Il server tmux di ciascun utente Unix e' avviato dall'unit systemd *utente*
`agent-hub-tmux.service` (socket `agenthub`): le sessioni non appartengono
quindi al cgroup di agent-hub.service e sopravvivono al riavvio del servizio.
Lo stato reale di tmux prevale sempre su quello registrato in SQLite.

Prompt e messaggi
-----------------
Ogni testo destinato a una sessione (prompt iniziale o messaggio successivo)
viene prima salvato in SQLite con stato `pending`, poi consegnato, poi
aggiornato con l'esito reale del tentativo: nessun testo puo' andare perso a
causa di un errore di tmux o della TUI.
"""
from __future__ import annotations

import asyncio
import fcntl
import fnmatch
import hashlib
import json
import os
import pty
import re
import shutil
import signal
import sqlite3
import struct
import subprocess
import termios
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import transcript as tr

# ----------------------------------------------------------------- config

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

CONFIG = {
    "bind": os.environ.get("AGENT_HUB_BIND", "127.0.0.1"),
    "port": int(os.environ.get("AGENT_HUB_PORT", "8787")),
    "allowed_users": [u.strip() for u in os.environ.get("AGENT_HUB_ALLOWED_USERS", "").split(",") if u.strip()],
    "origin": os.environ.get("AGENT_HUB_ORIGIN", "").rstrip("/"),
    "require_tailscale": os.environ.get("AGENT_HUB_REQUIRE_TAILSCALE", "1") == "1",
    "db": os.environ.get("AGENT_HUB_DB", "/var/lib/agent-hub/agent-hub.sqlite3"),
    "profiles": os.environ.get("AGENT_HUB_PROFILES", "/etc/agent-hub/profiles.json"),
    "logs": os.environ.get("AGENT_HUB_LOGS", "/srv/agent-workspace/logs"),
    "projects": os.environ.get("AGENT_HUB_PROJECTS", "/srv/agent-workspace/projects"),
    "uploads": os.environ.get("AGENT_HUB_UPLOADS", "/srv/agent-workspace/uploads"),
    "knowledge": os.environ.get("AGENT_HUB_KNOWLEDGE", "/srv/agent-workspace/knowledge"),
    "max_upload": int(os.environ.get("AGENT_HUB_MAX_UPLOAD", str(50 * 1024 * 1024))),
    "controller": os.environ.get("AGENT_HUB_CONTROLLER", "/etc/agent-hub/controller.json"),
    "reports": os.environ.get("AGENT_HUB_REPORTS", "/srv/agent-workspace/reports"),
    "health_dir": os.environ.get("AGENT_HUB_HEALTH_DIR", "/var/lib/agent-hub/health"),
    "presence": os.environ.get("AGENT_HUB_PRESENCE", "/var/lib/agent-hub/ui-activity.json"),
}

SESSION_CTL = "/usr/local/libexec/agent-hub/session-ctl"
PROJECT_CTL = "/usr/local/libexec/agent-hub/project-ctl"
HEALTH_CTL = "/usr/local/libexec/agent-hub/health-ctl"
RUNTIME_DIR = Path("/run/agent-hub")
# Gli account Unix sono configurabili: l'installazione di riferimento usa
# devagent per i progetti e hostagent per l'host, ma il playbook di
# provisioning puo' sceglierne altri passandoli da config.env.
PROJECT_UNIX_USER = os.environ.get("AGENT_HUB_PROJECT_USER", "projectagent")
SERVER_UNIX_USER = os.environ.get("AGENT_HUB_SERVER_USER", "serveragent")
UNIX_USERS = {"PROJECT": PROJECT_UNIX_USER, "SERVER": SERVER_UNIX_USER}
AGENT_UNIX_USERS = (PROJECT_UNIX_USER, SERVER_UNIX_USER)
SERVER_HOME = os.environ.get("AGENT_HUB_SERVER_HOME", f"/home/{SERVER_UNIX_USER}")
CSRF_TOKEN = uuid.uuid4().hex
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# ------------------------------------------------------- meetings config

MEETING_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".webm", ".mp4", ".aac", ".flac"}
MEETING_AUDIO_MAX = int(os.environ.get("AGENT_HUB_MEETING_AUDIO_MAX", str(512 * 1024 * 1024)))
MEETING_WHISPER_MODEL = os.environ.get("AGENT_HUB_WHISPER_MODEL", "small")
MEETING_WHISPER_CACHE = os.environ.get("AGENT_HUB_WHISPER_CACHE", "/var/lib/agent-hub/whisper-models")
MEETING_WORKERS_ENABLED = os.environ.get("AGENT_HUB_MEETING_WORKERS", "1") != "0"
MEETING_REPORT_CTL = "/usr/local/libexec/agent-hub/meeting-report-ctl"
MEETING_CTL_USER = os.environ.get("AGENT_HUB_SERVICE_USER", "agenthub")

# ------------------------------------- perimetro della sessione post-riunione
#
# Una riunione decide, non implementa. Dopo la doppia approvazione il sistema
# aggiorna la documentazione dei prossimi passi; il codice non si tocca. E
# proprio perche' il codice non cambia, non puo' cambiare nulla di cio' che
# *descrive il presente*: l'architettura as-is e' la fotografia del sistema
# esistente ed e' la sorgente dei diagrammi pubblicati.
#
# Il confine e' espresso come regola, non come elenco: i file di un progetto
# crescono, e un elenco andrebbe aggiornato a mano ogni volta — cioe' verrebbe
# dimenticato. Dentro la cartella dell'architettura decide il suffisso del
# nome: `-target` e `-next` sono le versioni future e si aggiornano, ogni altro
# file di dati e' as-is e resta invariato, anche se aggiunto domani.
MEETING_DOC_ROOTS = ("docs/",)
MEETING_DOC_FILES = (".agent/HANDOFF.md",)
MEETING_ARCH_DIR = "docs/architecture/"
MEETING_ARCH_FUTURE = ("-target", "-next")
MEETING_ARCH_DATA_EXT = (".yaml", ".yml", ".json")


def meeting_arch_is_frozen(path: str) -> bool:
    """Vero per un file di dati che descrive l'architettura com'e' adesso.

    La prosa nella stessa cartella (README e simili) spiega il formato e le
    convenzioni: non e' la fotografia del sistema e resta modificabile.
    """
    if not path.startswith(MEETING_ARCH_DIR):
        return False
    stem, ext = os.path.splitext(path)
    if ext.lower() not in MEETING_ARCH_DATA_EXT:
        return False
    return not stem.endswith(MEETING_ARCH_FUTURE)


def meeting_doc_path_allowed(path: str) -> bool:
    """Vero se una sessione post-riunione puo' modificare questo percorso."""
    path = (path or "").strip()
    while path.startswith("./"):      # `lstrip("./")` mangerebbe `.agent/…`
        path = path[2:]
    if not path or meeting_arch_is_frozen(path):
        return False
    return path in MEETING_DOC_FILES or path.startswith(MEETING_DOC_ROOTS)


def meeting_arch_frozen_now(slug: str) -> list[str]:
    """I file as-is che esistono davvero adesso, per citarli come esempio.

    Serve solo a rendere concreto il prompt: la regola vale comunque, anche per
    i file che compariranno dopo che queste istruzioni sono state scritte.
    """
    root = Path(CONFIG["projects"]) / slug / MEETING_ARCH_DIR
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_file())
    except OSError:
        return []
    return [n for n in names if meeting_arch_is_frozen(MEETING_ARCH_DIR + n)]

# ZIP context limits
CTX_MAX_FILE_BYTES = 1 * 1024 * 1024      # 1 MiB per file
CTX_MAX_TOTAL_BYTES = 30 * 1024 * 1024    # 30 MiB totali
CTX_MAX_FILES = 2500

# Esclusioni ZIP
CTX_EXCLUDE_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "target",
                    "cache", "__pycache__", ".pytest_cache", ".mypy_cache"}
CTX_EXCLUDE_NAMES = {".env", "*.pem", "*.key", "id_rsa", "id_ed25519", "id_ecdsa"}
CTX_EXCLUDE_KINDS = {"database", "log", "backups", "secret", "token", "password",
                     "credential", "binary"}
# Nomi di sicurezza da non includere
CTX_EXCLUDE_PATTERNS = ["*.pem", "*.key", "id_rsa*", "id_ed25519*", "id_ecdsa*",
                        "*.db", "*.sqlite", "*.sqlite3",
                        "*.log", "*.bin", "*.exe", "*.dll", "*.so", "*.o", "*.a",
                        "*.pyc", "*.pyo", "*.class"]

# stati di consegna di un prompt/messaggio
PENDING, LAUNCHING, SENT, FAILED, RESENT = (
    "pending", "launching", "sent", "delivery_failed", "manually_resent")

# Stati finali dichiarati dall'agente stesso con `agent-report`.
REPORT_STATUSES = ("COMPLETED", "NEEDS_INPUT", "WAITING_SESSION",
                   "NEEDS_HOST_ACTION", "FAILED", "CANCELLED")
# Stati che il controller ricava da fatti osservabili, mai dal contenuto della TUI.
CONTROLLER_STATUSES = ("RUNNING", "STARTING", "CRASHED", "ENDED_UNREPORTED",
                       "POSSIBLY_STALLED", "AUTH_REQUIRED", "USAGE_LIMIT")

ALLOWED_DOC_EXT = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
    ".xsd": "application/xml",
    ".xsl": "application/xml",
    ".xslt": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".svg": "image/svg+xml",
    ".rst": "text/plain",
    ".tex": "text/plain",
    ".sql": "application/sql",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".schema": "application/octet-stream",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".log": "text/plain",
}

app = FastAPI(title="Agent Hub", docs_url=None, redoc_url=None, openapi_url=None)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_profiles() -> list[dict]:
    with open(CONFIG["profiles"]) as fh:
        return json.load(fh)["profiles"]


def profile_by_id(pid: str) -> dict:
    for p in load_profiles():
        if p["id"] == pid:
            return p
    raise HTTPException(400, f"profilo sconosciuto: {pid}")


# --------------------------------------------------- configurazione controller

CONTROLLER_DEFAULTS = {
    "stall_seconds": 900,
    "scan_bytes": 24000,
    "sweep_seconds": 30,
    # Turno consegnato, nessun report, log PTY fermo da questi secondi: il
    # controller sollecita una volta sola il contratto di stato finale.
    # 0 disattiva il sollecito.
    "nudge_seconds": 240,
    # Intervallo massimo fra due scritture nel log ancora considerato «lavoro»
    # continuo. Oltre questa soglia il silenzio e' attesa, non elaborazione.
    "work_gap_seconds": 120,
    "patterns": {"AUTH_REQUIRED": [], "USAGE_LIMIT": []},
}
_CONTROLLER: dict = {"mtime": -1.0, "data": {}, "compiled": {}}


def controller_config() -> dict:
    """Soglie e pattern del controller, riletti quando il file cambia.

    Nessun riavvio del servizio per cambiare una soglia: si confronta l'mtime
    di /etc/agent-hub/controller.json a ogni accesso.
    """
    path = CONFIG["controller"]
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0
    if _CONTROLLER["mtime"] == mtime and _CONTROLLER["data"]:
        return _CONTROLLER["data"]
    data = dict(CONTROLLER_DEFAULTS)
    try:
        with open(path) as fh:
            loaded = json.load(fh)
        for key, default in CONTROLLER_DEFAULTS.items():
            if key in loaded:
                data[key] = loaded[key]
        data["stall_seconds"] = max(60, int(data["stall_seconds"]))
        data["scan_bytes"] = max(1000, min(1_000_000, int(data["scan_bytes"])))
        data["sweep_seconds"] = max(10, min(600, int(data["sweep_seconds"])))
        nudge = int(data["nudge_seconds"])
        data["nudge_seconds"] = 0 if nudge <= 0 else max(60, nudge)
        data["work_gap_seconds"] = max(30, min(3600, int(data["work_gap_seconds"])))
    except (OSError, ValueError, TypeError):
        data = dict(CONTROLLER_DEFAULTS)
    compiled = {}
    for state, patterns in (data.get("patterns") or {}).items():
        if state not in ("AUTH_REQUIRED", "USAGE_LIMIT"):
            continue
        good = []
        for p in patterns or []:
            try:
                good.append(re.compile(p))
            except re.error:
                pass  # un pattern scritto male non deve rompere il controller
        compiled[state] = good
    _CONTROLLER.update({"mtime": mtime, "data": data, "compiled": compiled})
    return data


# --------------------------------------------------------------- database

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG["db"], timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    Path(CONFIG["db"]).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                slug TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                source TEXT NOT NULL,
                repo_url TEXT DEFAULT '',
                local_url TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tmux_name TEXT NOT NULL,
                project_slug TEXT DEFAULT '',
                workdir TEXT NOT NULL,
                environment TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                model TEXT DEFAULT '',
                executor_enabled INTEGER NOT NULL DEFAULT 0,
                executor_model TEXT DEFAULT '',
                executor_max_agents INTEGER NOT NULL DEFAULT 0,
                permission_mode TEXT NOT NULL,
                unix_user TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'agent',
                status TEXT NOT NULL,
                initial_prompt TEXT DEFAULT '',
                parent_session TEXT DEFAULT '',
                cols INTEGER DEFAULT 100,
                rows INTEGER DEFAULT 30,
                created_at TEXT NOT NULL,
                ended_at TEXT DEFAULT '',
                auto_trust INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS ports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_slug TEXT NOT NULL,
                port INTEGER NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(project_slug, port)
            );
            -- Prompt iniziali e messaggi successivi: il testo e' salvato
            -- PRIMA del tentativo di consegna e sopravvive a ogni errore.
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'user',   -- initial | user | controller | control
                text TEXT NOT NULL,
                status TEXT NOT NULL,                -- pending|launching|sent|delivery_failed|manually_resent
                method TEXT DEFAULT '',              -- cli-arg | paste | menu-choice
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                delivered_at TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                note TEXT DEFAULT ''                 -- consegnato, ma con una riserva da dire
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                path TEXT NOT NULL,
                project_slug TEXT DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'private',
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT DEFAULT '',
                content_type TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_documents (
                session_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, document_id)
            );
            -- Stati finali dichiarati dagli agenti con `agent-report`. Lo
            -- storico e' append-only; la sessione porta l'ultimo valore.
            CREATE TABLE IF NOT EXISTS session_reports (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,       -- COMPLETED|NEEDS_INPUT|WAITING_SESSION|NEEDS_HOST_ACTION|FAILED|CANCELLED
                summary TEXT NOT NULL DEFAULT '',
                waiting_for_session TEXT NOT NULL DEFAULT '',
                reported_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'agent-report',
                unix_user TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_reports_session
                ON session_reports(session_id, reported_at);
            CREATE TABLE IF NOT EXISTS model_catalog_status (
                unix_user TEXT NOT NULL,
                provider TEXT NOT NULL,
                last_checked TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'never_checked',
                error TEXT DEFAULT '',
                PRIMARY KEY (unix_user, provider)
            );
            CREATE TABLE IF NOT EXISTS model_catalog (
                unix_user TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                origin TEXT NOT NULL CHECK(origin IN ('verified','curated')),
                last_checked TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'available',
                error TEXT DEFAULT '',
                PRIMARY KEY (unix_user, provider, model_id)
            );
            CREATE INDEX IF NOT EXISTS idx_model_catalog_user_provider
                ON model_catalog(unix_user, provider);
            -- Consumo dei piani, riletto periodicamente dal wrapper. E' uno
            -- stato remoto e costoso da leggere: sta qui perche' la pagina
            -- possa mostrare sempre l'ultimo valore noto con la sua data,
            -- senza mai attendere una chiamata di rete per aprirsi.
            CREATE TABLE IF NOT EXISTS provider_usage (
                unix_user TEXT NOT NULL,
                provider TEXT NOT NULL,
                last_checked TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'never_checked',
                error TEXT DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (unix_user, provider)
            );
            -- Notifiche gia' consegnate. La chiave e' l'evento, non il
            -- messaggio: qualunque notificatore puo' ripartire da zero senza
            -- inondare di duplicati chi legge.
            CREATE TABLE IF NOT EXISTS notifications (
                channel TEXT NOT NULL,      -- telegram, …
                kind TEXT NOT NULL,         -- report | lifecycle | health
                key TEXT NOT NULL,          -- identita' dell'evento
                sent_at TEXT NOT NULL,
                detail TEXT DEFAULT '',
                PRIMARY KEY (channel, kind, key)
            );
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL,
                title TEXT NOT NULL,
                meeting_date TEXT NOT NULL,
                audio_path TEXT DEFAULT '',
                audio_name TEXT DEFAULT '',
                audio_size INTEGER DEFAULT 0,
                transcript_path TEXT DEFAULT '',
                proposal_json TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                error TEXT DEFAULT '',
                round INTEGER NOT NULL DEFAULT 1,
                analysis_session_id TEXT DEFAULT '',
                implementation_session_id TEXT DEFAULT '',
                profile_id TEXT DEFAULT '',
                model TEXT DEFAULT '',
                effort TEXT DEFAULT '',
                operational_prompt TEXT DEFAULT '',
                context_on_approval INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meeting_approvals (
                meeting_id TEXT NOT NULL,
                round INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (meeting_id, round, chat_id)
            );
            CREATE TABLE IF NOT EXISTS meeting_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                processed_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS project_contexts (
                project_slug TEXT PRIMARY KEY,
                target_architecture TEXT DEFAULT '',
                package_path TEXT DEFAULT '',
                package_sha256 TEXT DEFAULT '',
                package_size INTEGER DEFAULT 0,
                file_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT '',
                trigger TEXT DEFAULT '',
                meeting_id TEXT DEFAULT ''
            );
            """
        )


def migrate_db() -> None:
    """Aggiunge le colonne mancanti ai database creati da versioni precedenti."""
    with db() as conn:
        have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "auto_trust" not in have:
            conn.execute("ALTER TABLE sessions ADD COLUMN auto_trust INTEGER NOT NULL DEFAULT 1")
        if "launch_info" not in have:
            # comando e cgroup reali dell'ultimo avvio, per la diagnostica
            conn.execute("ALTER TABLE sessions ADD COLUMN launch_info TEXT DEFAULT ''")
        # contratto di stato finale e stato deterministico del controller.
        # `effort`: livello richiesto all'avvio. `harness_session_id`:
        # identificativo passato alla CLI con --session-id, che rende
        # deterministico il file di stato da cui si rilegge il modello reale.
        for col in ("report_status", "report_summary", "reported_at",
                    "exit_code", "lifecycle", "lifecycle_at",
                    "effort", "harness_session_id",
                    # dipendenza strutturata usata da WAITING_SESSION
                    "waiting_for_session",
                    # ultimo mtime del log gia' contabilizzato come lavoro
                    "work_probe_mtime", "work_probe_at"):
            if col not in have:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT DEFAULT ''")
        # secondi in cui l'harness ha davvero scritto sul PTY: e' il tempo di
        # elaborazione, non l'attesa di un input umano
        if "work_seconds" not in have:
            conn.execute("ALTER TABLE sessions ADD COLUMN work_seconds INTEGER NOT NULL DEFAULT 0")
        have_reports = {r["name"] for r in conn.execute("PRAGMA table_info(session_reports)")}
        if "waiting_for_session" not in have_reports:
            conn.execute("ALTER TABLE session_reports ADD COLUMN waiting_for_session TEXT NOT NULL DEFAULT ''")
        # Executor esterni opzionali: configurazione per-sessione passata al
        # wrapper, senza credenziali nel database o negli argomenti della CLI.
        if "executor_enabled" not in have:
            conn.execute("ALTER TABLE sessions ADD COLUMN executor_enabled INTEGER NOT NULL DEFAULT 0")
        if "executor_model" not in have:
            conn.execute("ALTER TABLE sessions ADD COLUMN executor_model TEXT DEFAULT ''")
        if "executor_max_agents" not in have:
            conn.execute("ALTER TABLE sessions ADD COLUMN executor_max_agents INTEGER NOT NULL DEFAULT 0")
        # livelli di effort realmente supportati dal singolo modello, come
        # dichiarati dal provider: e' cosi' che la pagina evita di offrire un
        # effort a un modello che non ne ha (Haiku, per esempio)
        have_cat = {r["name"] for r in conn.execute("PRAGMA table_info(model_catalog)")}
        if "efforts" not in have_cat:
            conn.execute("ALTER TABLE model_catalog ADD COLUMN efforts TEXT DEFAULT ''")
        # `alias` distingue le scorciatoie della CLI (opus, sonnet, …) dai
        # modelli del catalogo: un alias non dichiara i propri effort perche'
        # non si sa in anticipo su quale modello si risolvera'.
        if "kind" not in have_cat:
            conn.execute("ALTER TABLE model_catalog ADD COLUMN kind TEXT DEFAULT 'model'")
        # Nota non allarmante su un messaggio comunque consegnato: serve al
        # prompt passato in argv, dove la TUI puo' non confermare la propria
        # prontezza senza che questo tolga nulla alla consegna. Tenerla in
        # `last_error` avrebbe mostrato «Errore» su un testo davvero arrivato.
        have_msg = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "note" not in have_msg:
            conn.execute("ALTER TABLE messages ADD COLUMN note TEXT DEFAULT ''")
        # sha del repository quando parte la sessione post-riunione: senza un
        # punto di partenza non si puo' dire che cosa ha toccato davvero
        have_mtg = {r["name"] for r in conn.execute("PRAGMA table_info(meetings)")}
        if "doc_baseline" not in have_mtg:
            conn.execute("ALTER TABLE meetings ADD COLUMN doc_baseline TEXT DEFAULT ''")
        have_docs = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        if "scope" not in have_docs:
            conn.execute("ALTER TABLE documents ADD COLUMN scope TEXT NOT NULL DEFAULT 'private'")
            conn.execute("UPDATE documents SET scope='repository' WHERE path LIKE '/srv/agent-workspace/projects/%'")


# Il catalogo e' per utente Unix: OpenCode legge le credenziali solo dalla
# home dell'utente che lo esegue. I fallback OAuth sono intenzionalmente
# separati e non vengono mai spacciati per disponibili nell'account.
CATALOG_TTL = 12 * 60 * 60
# Gli errori transitori (token OAuth appena scaduto, rete) vanno ritentati
# rapidamente: Claude Code puo' rinnovare il token all'avvio di una sessione
# e tenere il vecchio catalogo per 12 ore renderebbe invisibile il rinnovo.
CATALOG_RETRY_TTL = 5 * 60
# Tutti e tre i provider hanno una sorgente di verita' interrogabile con le
# credenziali dell'utente: /v1/models per Claude, `codex debug models` per
# Codex, `opencode models` per i provider API. Non esistono piu' liste
# «curate» scritte a mano: un elenco compilato altrove invecchia in silenzio
# e finisce per offrire modelli deprecati o mai disponibili.
# `usage: False` distingue i provider che non espongono un consumo leggibile:
# il gateway OpenCode Zen non ha ne' finestre ne' saldo interrogabili, e una
# riga permanentemente vuota nella pagina Accounts direbbe «non lo sappiamo»
# su qualcosa che semplicemente non esiste.
CATALOG_PROVIDERS = {
    "deepseek": {"label": "DeepSeek", "aliases": []},
    "opencode": {"label": "OpenCode Zen (gateway multi-provider)", "aliases": [],
                 "usage": False},
    "codex": {"label": "OpenAI / Codex (OAuth)", "aliases": []},
    "claude": {"label": "Anthropic / Claude Code (OAuth)",
               # alias stabili accettati dalla CLI: non sono modelli del
               # catalogo ma restano scelte valide e vanno offerte
               "aliases": [("opus", "Claude Opus (alias: ultimo Opus)"),
                           ("sonnet", "Claude Sonnet (alias: ultimo Sonnet)"),
                           ("haiku", "Claude Haiku (alias: ultimo Haiku)"),
                           ("fable", "Claude Fable (alias: ultimo Fable)")]},
}


# Messaggi che il wrapper costruisce da sé, campo per campo: non contengono
# testo esterno e quindi possono raggiungere la pagina così come sono. Tutto
# ciò che non è in questa lista viene ricondotto a un messaggio generico.
CATALOG_KNOWN_ERRORS = {
    "provider non valido",
    "provider non configurato",
    "account non autenticato",
    "nessuna credenziale locale",
    "credenziale rifiutata",
    "token locale scaduto: Claude Code lo rinnova al prossimo avvio, "
    "catalogo non verificabile ora",
    "provider non raggiungibile",
    "risposta non interpretabile",
    "discovery fallita",
    "timeout",
    "non disponibile",
}


def safe_catalog_error(value: str) -> str:
    """Errore mostrabile: i messaggi noti passano, gli sconosciuti si riducono.

    Un errore utile vale molto piu' di uno generico — «token scaduto» dice cosa
    fare, «verifica non riuscita» no — ma solo i messaggi che sappiamo costruiti
    dal wrapper, e quindi privi di testo esterno, passano senza riscrittura.
    """
    if (value or "").strip() in CATALOG_KNOWN_ERRORS:
        return value.strip()
    text = (value or "").lower()
    if "provider not found" in text or "not configured" in text:
        return "provider non configurato per questo utente"
    if "timeout" in text or "network" in text or "fetch" in text:
        return "rete o provider non raggiungibile"
    if "permission" in text or "access" in text:
        return "accesso locale al provider non disponibile"
    return "verifica del provider non riuscita"


def _catalog_due(user: str, provider: str, force: bool) -> bool:
    if force:
        return True
    with db() as conn:
        row = conn.execute("SELECT last_checked,status FROM model_catalog_status WHERE unix_user=? AND provider=?",
                           (user, provider)).fetchone()
    if not row or not row["last_checked"]:
        return True
    try:
        checked = datetime.fromisoformat(row["last_checked"]).timestamp()
    except ValueError:
        return True
    ttl = CATALOG_RETRY_TTL if row["status"] == "unverified" else CATALOG_TTL
    return time.time() - checked >= ttl


def refresh_model_catalog(user: str, provider: str | None = None, force: bool = False) -> None:
    """Riallinea il catalogo di un utente a ciò che il provider offre davvero.

    Tre esiti distinti, perché confonderli è come si finisce per proporre
    modelli inesistenti:

    - `available`  — elenco verificato: sostituisce il precedente per intero,
      quindi un modello ritirato dal provider sparisce anche qui. È la
      rimozione automatica: nessuna riga sopravvive a un refresh riuscito.
    - `unavailable` — il provider non è utilizzabile da questo utente (nessuna
      credenziale). Il catalogo viene svuotato: offrire modelli che non si
      possono chiamare è peggio che non offrirne.
    - `unverified` — la verifica non è possibile adesso (rete, token scaduto,
      timeout). L'ultimo elenco noto resta, marcato come non verificato: è
      probabilmente ancora giusto, ma non lo si spaccia per confermato.
    """
    targets = [provider] if provider else list(CATALOG_PROVIDERS)
    for name in targets:
        spec = CATALOG_PROVIDERS.get(name)
        if not spec or not _catalog_due(user, name, force):
            continue
        checked, err = now(), ""
        models: list[tuple[str, str, str, str]] = []
        try:
            result = wrapper_json(user, SESSION_CTL, "models", name, timeout=90)
            for m in result.get("models", []):
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]{1,127}", mid):
                    continue
                efforts = ",".join(str(e) for e in (m.get("efforts") or [])
                                   if re.fullmatch(r"[a-z]{1,16}", str(e)))
                models.append((mid, str(m.get("name", "")).strip() or mid, efforts, "model"))
            if models:
                state = "available"
            elif result.get("configured") is False:
                state = "unavailable"
                err = safe_catalog_error(str(result.get("error", "")))
            else:
                state = "unverified"
                err = safe_catalog_error(str(result.get("error", "")))
        except (OSError, subprocess.SubprocessError, HTTPException) as exc:
            state, err = "unverified", safe_catalog_error(str(getattr(exc, "detail", exc)))

        # gli alias della CLI non vivono nel catalogo del provider ma restano
        # scelte valide: si aggiungono solo quando l'account è utilizzabile
        if state == "available":
            known = {mid for mid, _l, _e, _k in models}
            models += [(a, label, "", "alias") for a, label in spec.get("aliases", []) if a not in known]

        with db() as conn:
            if state != "unverified":
                # sostituzione integrale: è qui che i modelli spariti spariscono
                conn.execute("DELETE FROM model_catalog WHERE unix_user=? AND provider=?",
                             (user, name))
                conn.executemany(
                    "INSERT INTO model_catalog (unix_user,provider,model_id,display_name,"
                    "origin,last_checked,status,error,efforts,kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(user, name, mid, label, "verified", checked, state, err, eff, kind)
                     for mid, label, eff, kind in models])
            else:
                # elenco precedente conservato, ma non più spacciato per verificato
                conn.execute("UPDATE model_catalog SET status=?, error=? "
                             "WHERE unix_user=? AND provider=?", (state, err, user, name))
            conn.execute(
                "INSERT INTO model_catalog_status (unix_user,provider,last_checked,status,error) "
                "VALUES (?,?,?,?,?) ON CONFLICT(unix_user,provider) DO UPDATE SET "
                "last_checked=excluded.last_checked,status=excluded.status,error=excluded.error",
                (user, name, checked, state, err))


def model_catalog_payload(user: str | None = None) -> dict:
    """Catalogo per utente Unix, con lo stato di verifica di ogni provider.

    Ogni modello porta con sé i livelli di effort che il provider dichiara di
    supportare: la pagina non deve indovinarli né offrirne di inesistenti.
    """
    users = [user] if user else list(AGENT_UNIX_USERS)
    out = {}
    with db() as conn:
        for u in users:
            rows = conn.execute(
                "SELECT c.provider,c.model_id,c.display_name,c.origin,c.last_checked,"
                "c.status,c.error,c.efforts,c.kind,"
                "s.status AS refresh_status,s.error AS refresh_error,s.last_checked AS refresh_checked "
                "FROM model_catalog c LEFT JOIN model_catalog_status s "
                "ON s.unix_user=c.unix_user AND s.provider=c.provider "
                "WHERE c.unix_user=? ORDER BY c.provider,c.model_id", (u,)).fetchall()
            grouped = {}
            for row in rows:
                item = dict(row)
                grouped.setdefault(item["provider"], {"provider": item["provider"],
                    "label": CATALOG_PROVIDERS.get(item["provider"], {}).get("label", item["provider"]),
                    "last_checked": item.pop("refresh_checked"), "status": item.pop("refresh_status"),
                    "error": item.pop("refresh_error"), "models": []})["models"].append({
                        "model_id": item["model_id"], "display_name": item["display_name"],
                        "origin": item["origin"], "last_checked": item["last_checked"],
                        "status": item["status"], "error": item["error"],
                        "efforts": [e for e in (item.get("efforts") or "").split(",") if e],
                        "kind": item.get("kind") or "model"})
            # Un provider senza modelli ha comunque uno stato, e va letto dalla
            # tabella di stato: un catalogo vuoto perche' il provider non e'
            # utilizzabile non e' la stessa cosa di uno mai controllato.
            checked = {r["provider"]: r for r in conn.execute(
                "SELECT provider,last_checked,status,error FROM model_catalog_status "
                "WHERE unix_user=?", (u,))}
            for name, spec in CATALOG_PROVIDERS.items():
                st = checked.get(name)
                grouped.setdefault(name, {"provider": name, "label": spec["label"],
                    "last_checked": st["last_checked"] if st else "",
                    "status": st["status"] if st else "never_checked",
                    "error": st["error"] if st else "", "models": []})
            out[u] = list(grouped.values())
    return out


# ------------------------------------------------------- consumo dei piani
#
# Quanto e' stato consumato di ciascun piano lo sa solo il provider, e chiederlo
# costa una chiamata di rete per provider e per utente. Viene quindi letto da un
# giro periodico e conservato in SQLite: la pagina Accounts mostra sempre
# l'ultimo valore noto con la sua data, e non attende mai la rete per aprirsi.

# Provider che un consumo ce l'hanno davvero: gli altri non vanno interrogati
# ne' mostrati, perche' una riga vuota non e' un'informazione.
USAGE_PROVIDERS = {n: s for n, s in CATALOG_PROVIDERS.items() if s.get("usage", True)}

USAGE_TTL = 5 * 60          # ogni quanto rileggere un consumo gia' noto
USAGE_RETRY_TTL = 60        # riprova piu' spesso dopo una lettura fallita
USAGE_TICK = 60             # cadenza del giro periodico


def _usage_due(user: str, provider: str, force: bool) -> bool:
    if force:
        return True
    with db() as conn:
        row = conn.execute("SELECT last_checked,status FROM provider_usage "
                           "WHERE unix_user=? AND provider=?", (user, provider)).fetchone()
    if not row or not row["last_checked"]:
        return True
    try:
        checked = datetime.fromisoformat(row["last_checked"]).timestamp()
    except ValueError:
        return True
    return time.time() - checked >= (USAGE_TTL if row["status"] == "ok" else USAGE_RETRY_TTL)


def refresh_usage(user: str, provider: str | None = None, force: bool = False) -> None:
    """Riallinea il consumo noto a quello che il provider dichiara adesso.

    Tre esiti, come per il catalogo:

    - `ok`           — lettura riuscita: i numeri sostituiscono i precedenti.
    - `unconfigured` — nessuna credenziale per questo utente: non c'e' consumo
      da mostrare e i valori vecchi vengono azzerati.
    - `unverified`   — lettura impossibile adesso (rete, token da rinnovare):
      gli ultimi numeri noti restano, ma con la loro data, che li smentisce da
      sola quando invecchiano.
    """
    for name in ([provider] if provider else list(USAGE_PROVIDERS)):
        if name not in USAGE_PROVIDERS or not _usage_due(user, name, force):
            continue
        checked = now()
        try:
            data = wrapper_json(user, SESSION_CTL, "usage", name, timeout=90)
        except (OSError, subprocess.SubprocessError, HTTPException) as exc:
            data = {"error": str(getattr(exc, "detail", exc))}
        # `safe_catalog_error` riscrive ciò che non riconosce: va chiamata solo
        # quando un errore c'è davvero, altrimenti trasformerebbe il successo
        # (errore vuoto) in un fallimento generico
        raw = str(data.get("error") or "").strip()
        err = safe_catalog_error(raw) if raw else ""
        state = "ok" if not err else ("unconfigured" if data.get("configured") is False
                                      else "unverified")
        with db() as conn:
            if state == "unverified":
                # nessun payload nuovo: si conserva l'ultimo noto, e la data
                # aggiornata dice fino a quando lo si e' potuto confermare
                conn.execute("INSERT INTO provider_usage (unix_user,provider,last_checked,"
                             "status,error) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(unix_user,provider) DO UPDATE SET "
                             "last_checked=excluded.last_checked,status=excluded.status,"
                             "error=excluded.error",
                             (user, name, checked, state, err))
                continue
            payload = {
                "windows": [{"label": str(w.get("label") or "")[:40],
                             "percent": float(w.get("percent") or 0),
                             "resets_at": int(w.get("resets_at") or 0)}
                            for w in (data.get("windows") or [])
                            if isinstance(w, dict)][:12],
                "balance": data.get("balance") if isinstance(data.get("balance"), dict) else None,
                "plan": str(data.get("plan") or "")[:40],
            }
            conn.execute("INSERT INTO provider_usage (unix_user,provider,last_checked,"
                         "status,error,payload) VALUES (?,?,?,?,?,?) "
                         "ON CONFLICT(unix_user,provider) DO UPDATE SET "
                         "last_checked=excluded.last_checked,status=excluded.status,"
                         "error=excluded.error,payload=excluded.payload",
                         (user, name, checked, state, err, json.dumps(payload)))


def usage_payload(user: str | None = None) -> dict:
    """Ultimo consumo noto per utente Unix, un elemento per provider.

    Ogni provider compare sempre, anche quando non e' mai stato letto: una
    riga assente non e' «nessun consumo», e' «non lo sappiamo».
    """
    out = {}
    with db() as conn:
        for u in ([user] if user else list(AGENT_UNIX_USERS)):
            rows = {r["provider"]: r for r in conn.execute(
                "SELECT provider,last_checked,status,error,payload FROM provider_usage "
                "WHERE unix_user=?", (u,))}
            items = []
            for name, spec in USAGE_PROVIDERS.items():
                row = rows.get(name)
                try:
                    payload = json.loads(row["payload"]) if row else {}
                except (TypeError, ValueError):
                    payload = {}
                items.append({
                    "provider": name, "label": spec["label"],
                    "last_checked": row["last_checked"] if row else "",
                    "status": row["status"] if row else "never_checked",
                    "error": row["error"] if row else "",
                    "windows": payload.get("windows") or [],
                    "balance": payload.get("balance"),
                    "plan": payload.get("plan") or "",
                })
            out[u] = items
    return out


def usage_overview() -> list[dict]:
    """Consumo per piano, non per utente Unix.

    Le credenziali vivono nella home di ciascun utente, quindi la lettura si fa
    per utente — ma il piano consumato e' uno solo, e va detto una volta sola.
    Le letture riuscite identiche vengono percio' unite; se due utenti leggono
    valori diversi restano righe distinte, perche' quel caso significa due
    account davvero diversi e nasconderne uno sarebbe falso.
    """
    per_user = usage_payload()
    out = []
    for name, spec in USAGE_PROVIDERS.items():
        found = [dict(item, users=[user]) for user, items in per_user.items()
                 for item in items if item["provider"] == name]
        good = [i for i in found if i["status"] == "ok"]
        if not good:
            # nessuna lettura riuscita: si mostra la piu' recente comunque
            # tentata, cosi' la riga dice *perche'* non c'e' un numero
            latest = max(found, key=lambda i: i["last_checked"], default=None)
            if latest:
                out.append(latest)
            continue
        merged: dict[str, dict] = {}
        for item in good:
            # l'istante di reset non entra nella firma: alcuni provider lo
            # ricalcolano a ogni lettura, quindi due letture dello stesso piano
            # differiscono di un secondo e si sdoppierebbero in due righe.
            # Contano i numeri: finestre, piano e saldo.
            sig = json.dumps([[[w["label"], w["percent"]] for w in item["windows"]],
                              item["balance"], item["plan"]], sort_keys=True)
            same = merged.get(sig)
            if not same:
                merged[sig] = item
            else:
                same["users"] += item["users"]
                same["last_checked"] = max(same["last_checked"], item["last_checked"])
        out.extend(merged.values())
    return out


def usage_loop() -> None:
    """Giro periodico: e' l'unico posto da cui parte una lettura di consumo."""
    while True:
        for user in AGENT_UNIX_USERS:
            try:
                refresh_usage(user)
            except Exception:  # noqa: BLE001
                pass  # un provider irraggiungibile non deve fermare il giro
        time.sleep(USAGE_TICK)


def recover_pending() -> None:
    """Consegne interrotte da un riavvio del backend: stato onesto, non 'sent'."""
    try:
        with db() as conn:
            conn.execute(
                "UPDATE messages SET status=?, last_error=? WHERE status IN (?,?)",
                (FAILED, "backend riavviato durante la consegna: usa «Invia ora»",
                 LAUNCHING, PENDING))
    except sqlite3.Error:
        pass


def sweep_runtime() -> None:
    """File di input rimasti orfani da una consegna interrotta."""
    try:
        for p in RUNTIME_DIR.glob("input-*.txt"):
            try:
                if time.time() - p.stat().st_mtime > 3600:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


# ------------------------------------------------------------ wrapper I/O

def wrapper(user: str, script: str, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n", "-u", user, script, *[str(a) for a in args]]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def wrapper_json(user: str, script: str, *args: str, timeout: int = 900) -> dict:
    r = wrapper(user, script, *args, timeout=timeout)
    if r.returncode != 0:
        raise HTTPException(400, (r.stderr or r.stdout).strip() or "comando fallito")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"output": r.stdout}


def tmux_sessions(user: str) -> dict[str, dict]:
    r = wrapper(user, SESSION_CTL, "list", timeout=30)
    out: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            out[parts[0]] = {
                "created": parts[1], "attached": parts[2] != "0", "size": parts[3],
                "pane_pid": parts[4] if len(parts) > 4 else "",
                "command": parts[5] if len(parts) > 5 else "",
                # pane «dead»: il comando e' uscito ma tmux conserva il pane e
                # il suo codice di uscita (remain-on-exit). E' l'unico modo per
                # distinguere una conclusione normale da un crash.
                "dead": (parts[6] if len(parts) > 6 else "") == "1",
                "dead_status": parts[7] if len(parts) > 7 else "",
                "dead_signal": parts[8] if len(parts) > 8 else "",
            }
    return out


def live_sessions() -> dict[str, dict]:
    live: dict[str, dict] = {}
    for u in AGENT_UNIX_USERS:
        for name, info in tmux_sessions(u).items():
            info["unix_user"] = u
            live[name] = info
    return live


def reap_dead_pane(row: dict, info: dict) -> None:
    """Pane terminato: registra il codice di uscita, poi chiude la sessione tmux.

    `remain-on-exit` tiene in vita il pane proprio per questo istante: senza
    raccoglierlo, la differenza fra un harness uscito con 0 e uno andato in
    crash sarebbe persa per sempre. Dopo la raccolta la sessione tmux viene
    chiusa dal wrapper, che ne salva la trascrizione.
    """
    code = (info.get("dead_status") or "").strip()
    signal_num = (info.get("dead_signal") or "").strip()
    if code == "" and signal_num.isdigit():
        # ucciso da un segnale: nessun exit status, ma non e' una fine normale.
        # Si registra la convenzione della shell (128 + segnale), che rende il
        # crash distinguibile e leggibile.
        code = str(128 + int(signal_num))
    with db() as conn:
        conn.execute("UPDATE sessions SET exit_code=?, status='ended', ended_at=? WHERE id=?",
                     (code if code != "" else "?", now(), row["id"]))
    try:
        wrapper(row["unix_user"], SESSION_CTL, "kill", row["tmux_name"], timeout=30)
    except (subprocess.SubprocessError, OSError):
        pass  # alla prossima sincronizzazione il pane risultera' comunque assente


def sync_status() -> dict[str, dict]:
    """Lo stato reale di tmux prevale su quello registrato nel database."""
    live = live_sessions()
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, tmux_name, unix_user, status, kind FROM sessions")]
    reaped = []
    for row in rows:
        info = live.get(row["tmux_name"])
        if info and info.get("dead"):
            reap_dead_pane(row, info)
            reaped.append(row["tmux_name"])
    for name in reaped:
        live.pop(name, None)
    finished_login_users = set()
    with db() as conn:
        for row in rows:
            if row["tmux_name"] in reaped:
                continue
            alive = row["tmux_name"] in live
            if alive and row["status"] != "running":
                conn.execute("UPDATE sessions SET status='running', ended_at='' WHERE id=?", (row["id"],))
            elif not alive and row["status"] in ("running", "starting"):
                conn.execute("UPDATE sessions SET status='ended', ended_at=? WHERE id=?", (now(), row["id"]))
                if row.get("kind") == "login":
                    finished_login_users.add(row["unix_user"])
    # La chiusura del flusso interattivo è il solo segnale disponibile di una
    # configurazione conclusa: tenta una discovery, ma conserva il fallback
    # anche se l'utente ha annullato il login o la rete non risponde.
    for user in finished_login_users:
        refresh_model_catalog(user, force=True)
    return live


# ----------------------------------------------------------- autorizzazione

def identity(request: Request) -> str:
    login = request.headers.get("tailscale-user-login", "")
    if not login:
        if CONFIG["require_tailscale"]:
            raise HTTPException(403, "richiesta priva degli header Tailscale attesi")
        return "local-debug"
    if CONFIG["allowed_users"] and login not in CONFIG["allowed_users"]:
        raise HTTPException(403, f"identita' non autorizzata: {login}")
    return login


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return  # richiesta non cross-origin dal browser
    expected = CONFIG["origin"]
    if expected and origin.rstrip("/") != expected:
        raise HTTPException(403, f"origin non consentito: {origin}")


class CsrfError(HTTPException):
    """403 riconoscibile dal frontend, che rinnova il bootstrap e riprova una volta."""


def check_csrf(request: Request) -> None:
    check_origin(request)
    if request.headers.get("x-csrf-token") != CSRF_TOKEN:
        raise CsrfError(403, "token CSRF mancante o non valido")


def error_body(exc: HTTPException, path: str = "") -> dict:
    return {"error": exc.detail, "detail": exc.detail, "status": exc.status_code,
            "code": "csrf" if isinstance(exc, CsrfError) else "", "path": path}


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    try:
        if not path.startswith("/static/"):
            identity(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            check_csrf(request)
    except HTTPException as exc:
        return JSONResponse(error_body(exc, path), status_code=exc.status_code)
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Corpo JSON uniforme: il frontend mostra sempre un testo, mai un riquadro vuoto."""
    return JSONResponse(error_body(exc, request.url.path), status_code=exc.status_code)


# ------------------------------------------------------------------- API

def asset_version() -> str:
    """Impronta degli asset statici: invalida la cache del browser a ogni deploy."""
    h = hashlib.sha256()
    for name in ("app.js", "style.css", "index.html", "manifest.webmanifest", "service-worker.js"):
        p = STATIC / name
        try:
            st = p.stat()
            h.update(f"{name}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            pass
    return h.hexdigest()[:12]


@app.get("/api/bootstrap")
async def bootstrap(request: Request):
    for unix_user in AGENT_UNIX_USERS:
        refresh_model_catalog(unix_user)
    return JSONResponse({
        "user": identity(request),
        "csrf": CSRF_TOKEN,
        "profiles": load_profiles(),
        "catalog": model_catalog_payload(),
        # Il frontend non deve piu' cablare i nomi degli account Unix.
        "unix_users": {"project": PROJECT_UNIX_USER, "server": SERVER_UNIX_USER,
                        "service": MEETING_CTL_USER},
        "server_home": SERVER_HOME,
        "projects_root": CONFIG["projects"],
        "uploads_root": CONFIG["uploads"],
        "knowledge_root": CONFIG["knowledge"] + "/projects",
        "max_upload": CONFIG["max_upload"],
        "allowed_doc_ext": sorted(ALLOWED_DOC_EXT),
        "origin": CONFIG["origin"],
        "asset_version": asset_version(),
        "started_at": STARTED_AT,
    }, headers={"Cache-Control": "no-store"})


@app.post("/api/activity")
async def record_ui_activity(request: Request):
    """Registra una vera interazione con la UI per silenziare gli avvisi ordinari.

    Il frontend limita gia' la frequenza. Il file, condiviso col notificatore,
    contiene soltanto istante e identita' Tailscale autorizzata.
    """
    path = Path(CONFIG["presence"])
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"at": now(), "user": identity(request)}) + "\n")
        os.replace(tmp, path)
        path.chmod(0o640)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(503, f"presenza UI non registrabile: {exc}") from exc
    return {"active": True, "at": now()}


@app.get("/api/sessions")
async def list_sessions():
    live = sync_status()
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions ORDER BY (status='running') DESC, created_at DESC")]
        pend = {r["session_id"]: r["n"] for r in conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM messages "
            "WHERE kind != 'control' AND status IN (?,?,?) GROUP BY session_id",
            (PENDING, LAUNCHING, FAILED))}
        total = {r["session_id"]: r["n"] for r in conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM messages "
            "WHERE kind != 'control' GROUP BY session_id")}
    for r in rows:
        decorate(r, live.get(r["tmux_name"]))
        r["undelivered"] = pend.get(r["id"], 0)
        r["messages_total"] = total.get(r["id"], 0)
    return {"sessions": rows}


IDLE_OUTPUT_SECONDS = 120  # oltre questa soglia: «nessun output recente»


def decorate(row: dict, info: dict | None, last: dict | None = None) -> dict:
    """Aggiunge lo stato *osservato*, senza interpretare la semantica della TUI.

    Nessun tentativo di indovinare se il modello «sta pensando»: si espongono
    soltanto fatti verificabili — processo vivo secondo tmux, eta' dell'ultimo
    byte scritto nel log PTY, ultimo testo registrato e ultimo testo consegnato.
    """
    row["attached"] = bool(info and info["attached"])
    row["size"] = info["size"] if info else ""
    row["pane_command"] = info["command"] if info else ""
    row["alive"] = bool(info)
    # una shell nel pane significa che l'harness e' uscito; il nome dell'harness
    # significa che il processo agente e' effettivamente in esecuzione
    row["agent_busy"] = bool(info and info["command"] not in ("bash", "sh", "zsh", ""))
    row["duration"] = duration_of(row)
    row["work_seconds"] = int(row.get("work_seconds") or 0)
    row["work_label"] = human_duration(row["work_seconds"])

    # ultimo output osservabile: mtime del log PTY, che tmux aggiorna a ogni byte
    age = None
    try:
        age = int(time.time() - log_path(row).stat().st_mtime)
    except OSError:
        pass
    row["output_age"] = age
    row["output_age_label"] = human_age(age)

    last = last_message_of(row["id"]) if last is None else last
    row["last_message"] = last
    row["last_input_at"] = last.get("created_at", "")
    row["last_delivered_at"] = last.get("delivered_at", "")
    row["state"] = observed_state(row, last)
    apply_lifecycle(row, last)
    row["waiting_for_name"] = ""
    if row.get("waiting_for_session"):
        with db() as conn:
            blocker = conn.execute(
                "SELECT name FROM sessions WHERE id=?", (row["waiting_for_session"],)).fetchone()
        row["waiting_for_name"] = blocker["name"] if blocker else "Sessione non trovata"
    return row


def human_age(secs: int | None) -> str:
    if secs is None:
        return "—"
    secs = max(0, secs)
    if secs < 60:
        return f"{secs}s fa"
    if secs < 3600:
        return f"{secs // 60}m fa"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m fa"
    return f"{secs // 86400}g fa"


def observed_state(row: dict, last: dict) -> str:
    """Etichetta unica e verificabile: nessuna inferenza sul contenuto."""
    if row["status"] == "failed":
        return "failed"
    if not row["alive"]:
        return "starting" if row["status"] == "starting" else "ended"
    if row["status"] == "starting":
        return "starting"
    if last:
        if last["status"] in (PENDING, LAUNCHING):
            return "prompt_pending"
        if last["status"] == FAILED:
            return "delivery_failed"
    if row["output_age"] is not None and row["output_age"] > IDLE_OUTPUT_SECONDS:
        return "no_recent_output"
    return "running"


# ------------------------------------------- stato deterministico di sessione
#
# Due sorgenti, mai mescolate:
#   * il *report* lo dichiara l'agente stesso con `agent-report`;
#   * il *lifecycle* lo deduce il controller da fatti verificabili — codice di
#     uscita del pane, eta' dell'ultimo byte scritto nel log, pattern noti di
#     login scaduto o limite d'uso.
# Il report, quando c'e' ed e' piu' recente dell'ultimo input, prevale sempre.

LIFECYCLE_LABEL = {
    "COMPLETED": "Completato",
    "NEEDS_INPUT": "Attende input",
    "WAITING_SESSION": "In attesa di un'altra sessione",
    "NEEDS_HOST_ACTION": "Richiede azione host",
    "FAILED": "Fallito (dichiarato)",
    "CANCELLED": "Annullato",
    "CRASHED": "Crash",
    "ENDED_UNREPORTED": "Concluso senza report",
    "POSSIBLY_STALLED": "Forse bloccato",
    "AUTH_REQUIRED": "Login richiesto",
    "USAGE_LIMIT": "Limite d'uso",
    "RUNNING": "Al lavoro",
    "STARTING": "Avvio",
}


def log_tail_text(row: dict, limit: int) -> str:
    """Coda del log PTY, ripulita dalle sequenze ANSI: solo per i pattern."""
    try:
        path = log_path(row)
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            data = fh.read()
    except OSError:
        return ""
    return tr.clean_capture(data)


def match_known_error(row: dict) -> str:
    """AUTH_REQUIRED / USAGE_LIMIT dai pattern configurabili. '' se nessuno."""
    cfg = controller_config()
    compiled = _CONTROLLER.get("compiled") or {}
    if not any(compiled.values()):
        return ""
    text = log_tail_text(row, cfg["scan_bytes"])
    if not text:
        return ""
    for state in ("AUTH_REQUIRED", "USAGE_LIMIT"):
        for rx in compiled.get(state, []):
            if rx.search(text):
                return state
    return ""


def report_is_current(row: dict, last: dict) -> bool:
    """Un report vale finche' non arriva nuovo input: dopo, la sessione riparte."""
    reported = row.get("reported_at") or ""
    if not reported:
        return False
    newer_input = (last or {}).get("created_at") or ""
    return not (newer_input and newer_input > reported)


def inactivity_age(row: dict, last: dict) -> int | None:
    """Secondi dall'ultimo fatto osservabile: output PTY o input consegnato.

    Un messaggio appena consegnato riapre un turno anche se il terminale era
    fermo da ore. Guardare soltanto il log lo classificava per pochi secondi
    come POSSIBLY_STALLED, abbastanza per generare un falso avviso Telegram.
    """
    ages = []
    output_age = row.get("output_age")
    if output_age is not None:
        ages.append(max(0, int(output_age)))
    if last and last.get("status") in (SENT, RESENT):
        delivered = last.get("delivered_at") or last.get("created_at") or ""
        try:
            ages.append(max(0, int(time.time() - datetime.fromisoformat(delivered).timestamp())))
        except (TypeError, ValueError):
            pass
    return min(ages) if ages else None


def apply_lifecycle(row: dict, last: dict) -> str:
    """Calcola e memorizza lo stato deterministico della sessione."""
    row["report_status"] = row.get("report_status") or ""
    row["report_summary"] = row.get("report_summary") or ""
    row["reported_at"] = row.get("reported_at") or ""
    row["waiting_for_session"] = row.get("waiting_for_session") or ""
    row["exit_code"] = row.get("exit_code") or ""
    stored = row.get("lifecycle") or ""
    current_report = report_is_current(row, last)

    if row.get("kind") == "login":
        life = "RUNNING" if row["alive"] else "ENDED_UNREPORTED"
    elif not row["alive"]:
        if row["status"] == "starting":
            life = "STARTING"
        elif row["report_status"]:
            # 1. processo concluso con report → si usa il report
            life = row["report_status"]
        elif row["exit_code"] not in ("", "0", "?"):
            # 3. exit code diverso da zero → crash
            life = "CRASHED"
        else:
            # 2. exit code 0 (o sconosciuto) senza report
            life = "ENDED_UNREPORTED"
    elif row["status"] == "starting":
        life = "STARTING"
    elif current_report:
        life = row["report_status"]
    else:
        known = match_known_error(row)
        stall = controller_config()["stall_seconds"]
        age = inactivity_age(row, last)
        if known:
            life = known
        elif age is not None and age > stall:
            # 4. viva, nessun output o input recente: segnalata, mai terminata
            life = "POSSIBLY_STALLED"
        else:
            life = "RUNNING"

    row["lifecycle"] = life
    row["lifecycle_label"] = LIFECYCLE_LABEL.get(life, life)
    row["lifecycle_reported"] = bool(current_report and life == row["report_status"])
    if stored != life:   # una scrittura solo quando lo stato cambia davvero
        try:
            with db() as conn:
                conn.execute("UPDATE sessions SET lifecycle=?, lifecycle_at=? WHERE id=?",
                             (life, now(), row["id"]))
        except sqlite3.Error:
            pass
    return life


# -------------------------------------------- sollecito del contratto finale
#
# Il contratto di stato finale e' l'unico input semplice che il controller
# deterministico riceve dall'agente. Se l'agente conclude il turno e si
# dimentica di registrarlo, il controller non ha alcun fatto da cui dedurre
# l'esito: dopo la soglia di stallo la sessione finiva marcata «forse
# bloccata» pur avendo lavorato benissimo — ed era il caso piu' frequente,
# non l'eccezione.
#
# Invece di indovinare, il controller chiede: turno consegnato, nessun report
# piu' recente, log fermo da `nudge_seconds` → un solo sollecito per turno,
# scritto nella cronologia come messaggio di tipo `controller` perche' resti
# visibile. Se anche il sollecito resta senza risposta fino a `stall_seconds`,
# allora POSSIBLY_STALLED e' un'informazione vera.

NUDGE_KIND = "controller"


def nudge_already_sent(sid: str, last: dict) -> bool:
    """True se questo turno ha gia' ricevuto il suo unico sollecito."""
    created = last.get("created_at") or ""
    if not created:
        return False
    with db() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM messages WHERE session_id=? AND kind=? AND created_at>=? "
            "LIMIT 1", (sid, NUDGE_KIND, created)).fetchone())


def nudge_text(row: dict) -> str:
    return "\n".join([
        CONTRACT_HEADER,
        "Il turno risulta concluso ma non hai registrato lo stato finale: senza",
        "di esso il controller non sa come e' andata e la sessione viene",
        "segnalata come bloccata.",
        "",
        "Esegui adesso soltanto questo comando, con il riepilogo del lavoro:",
        f'agent-report COMPLETED --summary "..." --session {row["id"]}',
        "",
        "Usa NEEDS_INPUT se attendi una mia risposta; WAITING_SESSION con",
        "--waiting-for <session-id> se dipendi da un'altra sessione;",
        "NEEDS_HOST_ACTION se serve un intervento sull'host, FAILED se non sei",
        "riuscito, CANCELLED se hai abbandonato. Non serve altro output.",
        "-" * len(CONTRACT_HEADER),
    ])


def maybe_nudge(row: dict, last: dict) -> bool:
    """Sollecita una volta sola il report per il turno in corso."""
    cfg = controller_config()
    if not cfg["nudge_seconds"] or row.get("kind") != "agent":
        return False
    if not row["alive"] or row["status"] != "running":
        return False
    if row["lifecycle"] in ("WAITING_SESSION", "AUTH_REQUIRED", "USAGE_LIMIT", "STARTING"):
        return False               # l'agente e' fermo per un motivo noto
    age = inactivity_age(row, last)
    if age is None or age < cfg["nudge_seconds"] or age > cfg["stall_seconds"]:
        return False
    if not last:
        return False               # niente da concludere
    if last.get("status") not in (SENT, RESENT):
        return False               # il turno non e' nemmeno arrivato all'agente
    if report_is_current(row, last):
        return False               # ha gia' registrato lo stato di questo turno
    if nudge_already_sent(row["id"], last):
        return False               # uno solo per turno, anche se il log si muove
    text = nudge_text(row)
    mid = add_message(row["id"], text, kind=NUDGE_KIND)
    deliver_async(row, mid, text, wait_ready=False)
    return True


def controller_sweep() -> int:
    """Ricalcola il lifecycle di tutte le sessioni, senza che nessuno guardi.

    Serve perche' gli stati devono essere veri anche a browser chiuso: e' qui
    che i pane morti vengono raccolti, che il tempo di lavoro viene sommato e
    che POSSIBLY_STALLED, AUTH_REQUIRED e USAGE_LIMIT compaiono in tempo utile
    perche' un notificatore li veda.
    """
    live = sync_status()
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
    by_id = {}
    for row in rows:
        decorate(row, live.get(row["tmux_name"]))
        by_id[row["id"]] = row
        accumulate_work(row)
        try:
            maybe_nudge(row, row.get("last_message") or {})
        except Exception:  # noqa: BLE001
            pass       # un sollecito non riuscito non deve fermare lo sweep
    for row in rows:
        try:
            release_dependency(row, by_id)
        except Exception:  # noqa: BLE001
            pass       # una ripresa non riuscita verra' ritentata o restera' visibile
    return len(rows)


DEPENDENCY_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "CRASHED", "ENDED_UNREPORTED"}


def release_dependency(row: dict, sessions: dict[str, dict]) -> bool:
    """Riprende una sessione quando la sessione da cui dipende e' terminata.

    La creazione del messaggio invalida immediatamente il report
    WAITING_SESSION; questo rende l'operazione idempotente anche mentre la
    consegna asincrona e' ancora in corso.
    """
    if row.get("lifecycle") != "WAITING_SESSION" or not row.get("alive"):
        return False
    blocker_id = row.get("waiting_for_session") or ""
    blocker = sessions.get(blocker_id)
    if not blocker or blocker.get("lifecycle") not in DEPENDENCY_TERMINAL:
        return False
    outcome = blocker["lifecycle"]
    summary = blocker.get("report_summary") or "Nessun riepilogo disponibile."
    text = (f"La sessione da cui dipendevi, {blocker.get('name') or blocker_id} "
            f"({blocker_id}), ha raggiunto lo stato {outcome}.\n\n"
            f"Riepilogo: {summary}\n\nRiprendi ora il lavoro rimasto in sospeso.")
    mid = add_message(row["id"], text, kind="dependency")
    deliver_async(row, mid, with_contract(row, text, initial=False), wait_ready=False)
    return True


def sweep_loop() -> None:
    while True:
        time.sleep(controller_config()["sweep_seconds"])
        try:
            controller_sweep()
        except Exception:  # noqa: BLE001
            pass  # un errore transitorio non deve fermare il controller


def last_report_of(sid: str) -> dict:
    with db() as conn:
        r = conn.execute("SELECT status, summary, waiting_for_session, reported_at, source, unix_user "
                         "FROM session_reports WHERE session_id=? "
                         "ORDER BY reported_at DESC, rowid DESC LIMIT 1", (sid,)).fetchone()
    return dict(r) if r else {}


def session_reports(sid: str) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT status, summary, waiting_for_session, reported_at, source, unix_user FROM session_reports "
            "WHERE session_id=? ORDER BY reported_at, rowid", (sid,))]


def last_message_of(sid: str) -> dict:
    with db() as conn:
        r = conn.execute(
            "SELECT id,kind,status,method,attempts,created_at,delivered_at,last_error,note,"
            "length(text) AS length FROM messages "
            "WHERE session_id=? AND kind != 'control' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (sid,)).fetchone()
    return dict(r) if r else {}


def human_duration(secs) -> str:
    h, rem = divmod(max(0, int(secs or 0)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


def duration_of(row: dict) -> str:
    """Tempo trascorso dall'avvio: comprende le attese, non e' tempo di lavoro."""
    try:
        start = datetime.fromisoformat(row["created_at"])
    except ValueError:
        return ""
    end = datetime.now(timezone.utc)
    if row["status"] != "running" and row["ended_at"]:
        try:
            end = datetime.fromisoformat(row["ended_at"])
        except ValueError:
            pass
    return human_duration((end - start).total_seconds())


# ------------------------------------------------------- tempo di lavoro
#
# La durata da avvio a chiusura diceva soltanto da quanto la sessione era
# aperta: una sessione che ha lavorato due minuti e poi e' rimasta ferma
# un giorno in attesa di un input risultava «durata 24h». Qui si misura
# invece il tempo in cui l'harness ha scritto sul PTY, che e' l'unico fatto
# osservabile che distingue l'elaborazione dall'attesa: mentre il modello
# lavora la TUI ridisegna di continuo, quando ha finito il log resta fermo.
#
# L'accumulo avviene solo nello sweep del controller (un solo thread) e la
# scrittura e' un compare-and-swap sull'ultimo mtime contabilizzato: un
# doppio conteggio richiederebbe due sweep simultanei con la stessa lettura.

def accumulate_work(row: dict) -> None:
    try:
        mtime = log_path(row).stat().st_mtime
    except OSError:
        return
    seen = row.get("work_probe_mtime") or ""
    try:
        prev = float(seen)
    except ValueError:
        prev = 0.0
    if prev and mtime <= prev:
        return                       # nessuna scrittura nuova: niente da contare
    delta = mtime - prev if prev else 0.0
    # un salto piu' lungo della soglia e' silenzio, non elaborazione: si
    # riparte dal nuovo mtime senza sommare l'attesa
    added = int(delta) if 0 < delta <= controller_config()["work_gap_seconds"] else 0
    total = int(row.get("work_seconds") or 0) + added
    try:
        with db() as conn:
            conn.execute(
                "UPDATE sessions SET work_seconds=?, work_probe_mtime=?, work_probe_at=? "
                "WHERE id=? AND COALESCE(work_probe_mtime,'')=?",
                (total, f"{mtime:.3f}", now(), row["id"], seen))
    except sqlite3.Error:
        return
    row["work_seconds"], row["work_probe_mtime"] = total, f"{mtime:.3f}"
    row["work_label"] = human_duration(total)


def get_session(sid: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "sessione inesistente")
    return dict(row)


def session_messages(sid: str) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid", (sid,))]


def session_documents(sid: str) -> list[dict]:
    with db() as conn:
        rows = list(conn.execute(
            "SELECT d.* FROM documents d JOIN session_documents s ON s.document_id=d.id "
            "WHERE s.session_id=? ORDER BY d.created_at", (sid,)))
    return existing_document_rows(rows)


def message_counters(sid: str) -> dict:
    with db() as conn:
        tot = conn.execute("SELECT COUNT(*) FROM messages "
                           "WHERE session_id=? AND kind != 'control'", (sid,)).fetchone()[0]
        und = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=? "
                           "AND kind != 'control' AND status IN (?,?,?)",
                           (sid, PENDING, LAUNCHING, FAILED)).fetchone()[0]
    return {"messages_total": tot, "undelivered": und}


@app.get("/api/sessions/{sid}")
async def session_detail(sid: str):
    live = sync_status()
    row = get_session(sid)
    decorate(row, live.get(row["tmux_name"]))
    row.update(message_counters(sid))
    return {"session": row, "messages": session_messages(sid),
            "documents": session_documents(sid),
            "reports": session_reports(sid),
            "diagnostics": launch_diagnostics(row)}


@app.get("/api/sessions/{sid}/state")
async def session_state(sid: str):
    """Stato osservato della sola sessione richiesta.

    Interroga tmux per il solo utente Unix interessato (una sudo invece di due)
    e non restituisce il testo dei messaggi: e' l'endpoint pensato per il
    polling periodico della pagina di sessione.
    """
    row = get_session(sid)
    info = tmux_sessions(row["unix_user"]).get(row["tmux_name"])
    if info and info.get("dead"):
        reap_dead_pane(row, info)
        row = get_session(sid)
        info = None
    alive = info is not None
    if alive and row["status"] != "running":
        with db() as conn:
            conn.execute("UPDATE sessions SET status='running', ended_at='' WHERE id=?", (sid,))
        row["status"] = "running"
    elif not alive and row["status"] in ("running", "starting"):
        with db() as conn:
            conn.execute("UPDATE sessions SET status='ended', ended_at=? WHERE id=?", (now(), sid))
        row["status"], row["ended_at"] = "ended", now()
    decorate(row, info)
    counters = message_counters(sid)
    return {"id": sid, "status": row["status"], "state": row["state"],
            "alive": row["alive"], "agent_busy": row["agent_busy"],
            "pane_command": row["pane_command"], "duration": row["duration"],
            "work_seconds": row["work_seconds"], "work_label": row["work_label"],
            "output_age": row["output_age"], "output_age_label": row["output_age_label"],
            "last_message": row["last_message"],
            "lifecycle": row["lifecycle"], "lifecycle_label": row["lifecycle_label"],
            "lifecycle_reported": row["lifecycle_reported"],
            "report_status": row["report_status"], "report_summary": row["report_summary"],
            "reported_at": row["reported_at"], "waiting_for_session": row["waiting_for_session"],
            "waiting_for_name": row["waiting_for_name"],
            "exit_code": row["exit_code"],
            **counters}


# ------------------------------------------- modello ed effort in esercizio
#
# La pagina non deve mostrare «il modello che abbiamo chiesto» ma quello che
# l'harness sta davvero usando: il primo e' una intenzione registrata a
# database, il secondo un fatto scritto dalla CLI a ogni risposta. I due
# valori vengono tenuti distinti e mostrati come tali, perche' possono
# divergere legittimamente (un `/model` digitato a mano nel terminale, un
# fallback del provider, una sessione ripresa).

MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,79}$")
EXECUTOR_MODEL_RE = MODEL_NAME_RE
EXECUTOR_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
EXECUTOR_MAX_LIMIT = 4
# I sub agent passano sempre da `agent-executor`, che gira come devagent e
# instrada il modello alla CLI del suo provider: `opencode run` per DeepSeek e
# OpenCode Zen, `claude -p` per Claude Code, `codex exec` per Codex. Delegabile
# e' quindi esattamente cio' che devagent puo' gia' usare come coordinatore,
# ne' piu' ne' meno: e' lo stesso catalogo, letto per gli stessi provider.
EXECUTOR_UNIX_USER = PROJECT_UNIX_USER
EXECUTOR_PROVIDERS = ("claude", "codex", "deepseek", "opencode")
# Gli alias della CLI (opus, sonnet, …) sono scelte valide solo dove la CLI li
# risolve: `claude -p --model opus` funziona, `opencode run --model opus` no.
EXECUTOR_ALIAS_PROVIDERS = ("claude",)


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def runtime_capabilities(prof: dict) -> dict:
    rt = prof.get("runtime") or {}
    return {
        "profile_id": prof.get("id", ""),
        "supports_model": bool(prof.get("supports_model")),
        "supports_effort": bool(prof.get("supports_effort")),
        "effort_levels": list(prof.get("effort_levels") or []),
        "default_effort": prof.get("default_effort", ""),
        "model_suggestions": list(prof.get("model_suggestions") or []),
        # cambio a caldo: possibile solo se il profilo dichiara il comando TUI
        "live_model_change": bool(rt.get("model_command")),
        "live_effort_change": bool(rt.get("effort_command")),
        "readable_state": bool(rt.get("source")),
        "note": rt.get("note", ""),
    }


def session_runtime_state(row: dict) -> dict:
    """Stato verificato dell'harness, letto dal wrapper come l'utente Unix."""
    try:
        prof = profile_by_id(row["profile_id"])
    except HTTPException:
        return {"state": "unsupported", "reason": "profilo non piu' configurato"}
    if not (prof.get("runtime") or {}).get("source"):
        return {"state": "unsupported",
                "reason": "questo harness non espone uno stato leggibile per sessione"}
    if not row.get("alive", True) and row["status"] != "running":
        # niente processo: l'ultimo stato scritto resta comunque valido da leggere
        pass
    try:
        return wrapper_json(row["unix_user"], SESSION_CTL, "runtime", row["profile_id"],
                            row["workdir"], row.get("harness_session_id") or "",
                            str(_epoch(row["created_at"])), timeout=45)
    except (HTTPException, OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "detail", str(exc))
        return {"state": "error", "reason": str(detail)[:300]}


def runtime_payload(row: dict) -> dict:
    try:
        prof = profile_by_id(row["profile_id"])
    except HTTPException:
        prof = {"id": row["profile_id"]}
    return {
        "id": row["id"],
        # ciò che è stato chiesto all'avvio, così com'è registrato in SQLite
        "configured": {"model": row.get("model") or "",
                       "effort": row.get("effort") or ""},
        # ciò che l'harness sta davvero usando, letto dai suoi file di stato
        "live": session_runtime_state(row),
        "capabilities": runtime_capabilities(prof),
        "harness_session_id": row.get("harness_session_id") or "",
        # il catalogo modelli e' per utente Unix: la pagina deve sapere quale
        "unix_user": row.get("unix_user") or "",
        "alive": bool(row.get("alive")),
    }


@app.get("/api/sessions/{sid}/runtime")
async def session_runtime(sid: str):
    row = get_session(sid)
    info = tmux_sessions(row["unix_user"]).get(row["tmux_name"])
    row["alive"] = bool(info) and not (info or {}).get("dead")
    return runtime_payload(row)


@app.post("/api/sessions/{sid}/runtime")
async def set_session_runtime(sid: str, request: Request):
    """Cambia modello ed effort della sessione, davvero.

    Due effetti distinti, entrambi reali e dichiarati come tali nella
    risposta: il valore viene registrato in SQLite (e quindi vale al prossimo
    avvio), e — se l'harness espone un comando per farlo e il processo e' vivo
    — viene applicato subito alla sessione in corso. Quando il cambio a caldo
    non e' possibile la risposta lo dice con `restart_required`, invece di
    lasciar credere che sia bastato salvare.
    """
    row = get_session(sid)
    prof = profile_by_id(row["profile_id"])
    body = await request.json()
    rt = prof.get("runtime") or {}

    model = str(body.get("model") or "").strip()
    if model and not prof.get("supports_model"):
        raise HTTPException(400, f"il profilo {prof['id']} non supporta la selezione del modello")
    if model and not MODEL_NAME_RE.match(model):
        raise HTTPException(400, f"nome modello non valido: {model}")
    effort = validate_effort(prof, body.get("effort"))
    if not model and not effort:
        raise HTTPException(400, "indica almeno un modello o un livello di effort")

    info = tmux_sessions(row["unix_user"]).get(row["tmux_name"])
    alive = bool(info) and not (info or {}).get("dead")

    applied, pending, errors = [], [], []
    for field, value, template in (("model", model, rt.get("model_command")),
                                   ("effort", effort, rt.get("effort_command"))):
        if not value:
            continue
        if not template:
            pending.append(field)
            continue
        if not alive:
            pending.append(field)
            continue
        try:
            res = wrapper_json(row["unix_user"], SESSION_CTL, "command", row["tmux_name"],
                               row["profile_id"], template.replace("{value}", value),
                               timeout=90)
        except HTTPException as exc:
            errors.append(f"{field}: {exc.detail}")
            continue
        if res.get("ok"):
            applied.append({"field": field, "value": value,
                            "command": res.get("command", ""),
                            "confirmed": int(res.get("confirmed") or 0),
                            "pane_tail": res.get("pane_tail", "")})
        else:
            errors.append(f"{field}: {res.get('error', 'comando rifiutato')}")

    # il valore registrato vale comunque al prossimo avvio della sessione
    sets, params = [], []
    if model:
        sets.append("model=?")
        params.append(model)
    if effort:
        sets.append("effort=?")
        params.append(effort)
    if sets:
        with db() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", (*params, sid))

    row = get_session(sid)
    row["alive"] = alive
    return {"ok": not errors, "applied": applied, "errors": errors,
            "restart_required": pending,
            "detail": _runtime_detail(applied, pending, errors, alive),
            "runtime": runtime_payload(row)}


def _runtime_detail(applied: list, pending: list, errors: list, alive: bool) -> str:
    parts = []
    if applied:
        parts.append("applicato subito alla sessione: " +
                     ", ".join(f"{a['field']}={a['value']}" for a in applied))
    if pending:
        why = "la sessione non è attiva" if not alive else "questo harness non permette il cambio a caldo"
        parts.append(f"registrato per il prossimo avvio ({', '.join(pending)}): {why} — usa «Restart»")
    if errors:
        parts.append("errori: " + "; ".join(errors))
    return ". ".join(parts) or "nessuna modifica"


# --------------------------------------------- contratto di stato finale
#
# Il blocco viene aggiunto a ogni testo *consegnato* alla sessione, mai al
# testo dell'utente registrato in SQLite: la cronologia degli input resta
# esattamente quella scritta dall'operatore. La pagina della sessione lo
# mostra come blocco separato nella diagnostica.

CONTRACT_HEADER = "--- Agent Hub · contratto di stato finale (blocco automatico) ---"
EXECUTOR_HEADER = "--- Agent Hub · executor (configurazione automatica) ---"


def executor_block(row: dict, *, compact: bool = False) -> str:
    """Istruzioni verificabili per usare il bridge configurato dalla UI."""
    if not row.get("executor_enabled"):
        return ""
    model = row.get("executor_model") or EXECUTOR_DEFAULT_MODEL
    maximum = int(row.get("executor_max_agents") or 1)
    server_note = (" Da SERVER gli executor sono rilanciati come devagent e confinati sotto "
                   f"{CONFIG['projects']}; usa `agent-executor --dir {CONFIG['projects']}/<progetto>` "
                   "per scegliere un progetto.") if row.get("environment") == "SERVER" else ""
    if compact:
        return ("\n\n" + EXECUTOR_HEADER + "\n"
                f"Delega il lavoro esecutivo delimitabile con `agent-executor` "
                f"({model}, massimo {maximum} simultanei) per contenere i token del coordinatore. "
                "Passa contesto minimo e criteri di accettazione; usa nel thread principale solo "
                "la sintesi finale, poi verifica personalmente integrazione e risultato." + server_note)
    return "\n".join([
        "",
        EXECUTOR_HEADER,
        f"Executor configurato: {model}",
        f"Concorrenza massima: {maximum}",
        "",
        "## Strategia obbligatoria di delega efficiente",
        "Sei il coordinatore. Mantieni nel tuo contesto requisiti, decisioni, integrazione e verifica finale; "
        "delega invece all'executor il maggior lavoro esecutivo delimitabile possibile quando preserva la qualita'.",
        "Sono buoni candidati: esplorazione del codice, implementazioni circoscritte, test, analisi di log, "
        "refactoring locali e raccolta di evidenze. Non delegare decisioni irreversibili, push, deploy, gestione "
        "di segreti o la valutazione finale di correttezza.",
        "Dai a ogni executor soltanto i percorsi, i vincoli e i criteri di accettazione necessari. Non passare "
        "l'intera conversazione. Chiedi una risposta concisa con file modificati, verifiche, rischi e blocchi; "
        "non riversare nel tuo contesto log grezzi o ragionamenti estesi.",
        "Per modifiche concorrenti assegna file o aree disgiunte; altrimenti usa gli executor in sequenza.",
        "",
        "## Comando disponibile",
        "Invia il task su standard input:",
        server_note.strip(),
        "",
        "agent-executor <<'EXECUTOR_TASK'",
        "Descrivi qui un solo task delimitato, con percorsi e criteri di accettazione.",
        "EXECUTOR_TASK",
        "",
        "Per parallelizzare puoi avviare piu' invocazioni in background, senza superare il limite configurato, "
        "e poi attendere. Il comando applica modello, permessi, directory e limite della sessione; non sovrascriverli.",
        "-" * len(EXECUTOR_HEADER),
    ])


def contract_block(row: dict) -> str:
    """Blocco completo, aggiunto al prompt iniziale di ogni sessione."""
    env = row["environment"]
    who = UNIX_USERS.get(env, row.get("unix_user", ""))
    lines = [
        "",
        CONTRACT_HEADER,
        f"SESSION_ID: {row['id']}",
        f"Ambiente: {env} (utente Unix {who})",
        f"Directory di lavoro: {row['workdir']}",
        "",
        "## Contesto Agent Hub",
        "Sei eseguito dentro Agent Hub sul server Debian. L'ambiente PROJECT indica il repository "
        "del progetto; l'ambiente SERVER indica il meta-progetto e l'infrastruttura host.",
        "I documenti associati possono essere knowledge privata fuori da Git oppure documenti "
        "repository versionabili. Non copiare knowledge privata nel worktree e non modificarla "
        "come se fosse codice: usa il catalogo Documenti di Agent Hub e `agent-document` per "
        "registrare file generati.",
        "",
        "Alla fine di OGNI turno, come ultima azione prima di restituire il controllo, "
        "registra uno stato finale usando:",
        "",
        'agent-report COMPLETED --summary "..."',
        'agent-report NEEDS_INPUT --summary "..."',
        'agent-report WAITING_SESSION --waiting-for <session-id> --summary "..."',
        'agent-report NEEDS_HOST_ACTION --summary "..."',
        'agent-report FAILED --summary "..."',
        'agent-report CANCELLED --summary "..."',
        "",
        "WAITING_SESSION indica una dipendenza fra agenti e riprende automaticamente "
        "questa sessione quando quella indicata termina; NEEDS_INPUT e' riservato a una "
        "risposta dell'utente.",
        "Non concludere un turno senza registrare uno di questi stati: il riepilogo "
        "che scrivi e' il risultato che arriva a chi ha chiesto il lavoro, e senza "
        "di esso la sessione viene segnalata come bloccata.",
        "Il comando ricava da solo il SESSION_ID dall'ambiente della sessione; "
        "in caso di dubbio aggiungi --session " + row["id"] + ".",
        "-" * len(CONTRACT_HEADER),
    ]
    executor = executor_block(row)
    return (executor + "\n\n" if executor else "") + "\n".join(lines)


def contract_reminder(row: dict) -> str:
    """Promemoria aggiunto ai messaggi successivi al primo.

    Vale per *ogni* turno, non solo per il primo: e' la fine del turno che il
    controller deve poter leggere, e un turno senza report lo lascia cieco.
    Il promemoria e' esplicito sul fatto che si tratta dell'ultima azione da
    compiere, perche' la versione compatta precedente veniva ignorata di
    frequente e la sessione finiva marcata come «forse bloccata».
    """
    return ("\n\n" + CONTRACT_HEADER + "\n"
            f"SESSION_ID: {row['id']}\n"
            "Obbligatorio: come ultima azione di QUESTO turno, prima di restituire "
            "il controllo, registra lo stato finale con\n"
            'agent-report COMPLETED --summary "cosa hai fatto"\n'
            "(oppure NEEDS_INPUT se attendi una risposta; WAITING_SESSION "
            "--waiting-for <session-id> se attendi un'altra sessione; "
            "NEEDS_HOST_ACTION se serve un intervento sull'host, FAILED, CANCELLED).\n"
            "Vale a ogni turno, anche per richieste brevi: senza questo comando il "
            "risultato del lavoro non arriva a chi lo ha chiesto.")


def with_contract(row: dict, text: str, *, initial: bool) -> str:
    """Testo da consegnare: quello dell'utente piu' il contratto, mai al posto."""
    if initial:
        return (text.rstrip() + "\n\n" + contract_block(row)) if text.strip() \
            else contract_block(row).lstrip("\n")
    return text.rstrip() + executor_block(row, compact=True) + contract_reminder(row)


# ------------------------------------------------------ prompt e messaggi

def write_input_file(text: str) -> str:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_DIR / f"input-{uuid.uuid4().hex}.txt"
    path.write_text(text)
    path.chmod(0o644)
    return str(path)


def add_message(sid: str, text: str, kind: str = "user", status: str = PENDING) -> str:
    mid = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (id,session_id,kind,text,status,method,attempts,created_at,"
            "delivered_at,last_error) VALUES (?,?,?,?,?,'',0,?,'','')",
            (mid, sid, kind, text, status, now()))
        if kind != "control":
            # Un nuovo turno, automatico o umano, scioglie il vincolo corrente.
            # Lo storico del report conserva comunque quale sessione si attendeva.
            conn.execute("UPDATE sessions SET waiting_for_session='' WHERE id=?", (sid,))
    return mid


def set_message(mid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE messages SET {cols} WHERE id=?", (*fields.values(), mid))


def bump_attempt(mid: str) -> None:
    with db() as conn:
        conn.execute("UPDATE messages SET attempts = attempts + 1 WHERE id=?", (mid,))


def message_row(mid: str) -> dict:
    with db() as conn:
        r = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    return dict(r) if r else {}


def _wrapper_data(r: subprocess.CompletedProcess) -> dict:
    """Payload JSON del wrapper, o dizionario vuoto se non ne ha prodotto uno."""
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _wrapper_result(r: subprocess.CompletedProcess) -> tuple[bool, str, str]:
    """Estrae (ok, errore, metodo) dall'output JSON del wrapper."""
    data = _wrapper_data(r)
    method = str(data.get("method") or "")
    if method not in ("cli-arg", "paste", "menu-choice"):
        method = ""
    if r.returncode == 0 and data.get("ok", True):
        return True, "", method
    err = data.get("error") or (r.stderr or r.stdout).strip() or f"rc={r.returncode}"
    return False, str(err)[:2000], method


# Un messaggio incollato mentre l'agente sta gia' elaborando un turno finisce
# nel campo di composizione della TUI, e l'Invio non apre un turno nuovo perche'
# uno e' in corso. `paste-buffer` e `send-keys` restituiscono comunque 0, quindi
# la consegna risultava riuscita e il testo spariva senza lasciare traccia: e'
# cosi' che un messaggio di questa sessione e' andato perso due volte di fila.
#
# L'attesa usa l'unico fatto osservabile gia' impiegato altrove nel controller:
# mentre il modello lavora la TUI ridisegna e il log del PTY cresce; quando ha
# finito il log resta fermo. Si aspetta che sia fermo da qualche secondo, poi si
# incolla. Scaduto il tetto si consegna comunque — mai peggio di prima — ma il
# messaggio porta una nota che dice che la sessione era occupata.
QUIET_SECONDS = 3.0
BUSY_WAIT_CAP = 900.0
BUSY_NOTE = ("consegnato mentre la sessione era ancora occupata: se l'agente "
             "non risponde, il testo puo' non essere stato letto")


def _log_fingerprint(row: dict) -> tuple:
    try:
        st = log_path(row).stat()
    except OSError:
        return ()
    return (st.st_size, round(st.st_mtime, 3))


def wait_until_quiet(row: dict, quiet: float = QUIET_SECONDS,
                     cap: float = BUSY_WAIT_CAP) -> bool:
    """Attende che l'harness smetta di scrivere sul PTY. Vero se ci e' riuscito.

    Senza log leggibile non si inventa un'attesa: si procede come prima.
    """
    if not _log_fingerprint(row):
        return True
    deadline = time.time() + cap
    last, still_since = _log_fingerprint(row), time.time()
    while time.time() < deadline:
        time.sleep(0.5)
        current = _log_fingerprint(row)
        if current != last:
            last, still_since = current, time.time()
            continue
        if time.time() - still_since >= quiet:
            return True
    return False


def deliver_text(row: dict, mid: str, text: str, *, wait_ready: bool,
                 final_status: str = SENT, count_attempt: bool = True,
                 busy_cap: float = BUSY_WAIT_CAP) -> tuple[bool, str]:
    """Consegna un testo alla sessione e registra l'esito reale del tentativo."""
    if count_attempt:
        bump_attempt(mid)
    set_message(mid, status=LAUNCHING, method="paste")
    # niente incollaggi sopra un turno in corso: il testo andrebbe perso
    quiet = wait_until_quiet(row, cap=busy_cap)
    path = write_input_file(text)
    try:
        if wait_ready:
            r = wrapper(row["unix_user"], SESSION_CTL, "deliver", row["tmux_name"],
                        path, row["profile_id"], "",
                        "1" if row.get("auto_trust", 1) else "0", timeout=300)
        else:
            r = wrapper(row["unix_user"], SESSION_CTL, "send", row["tmux_name"],
                        path, timeout=90)
    except subprocess.TimeoutExpired:
        set_message(mid, status=FAILED, last_error="timeout del wrapper di consegna")
        return False, "timeout del wrapper di consegna"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    ok, err, wrapper_method = _wrapper_result(r)
    if ok:
        fields = {"status": final_status, "method": wrapper_method or "paste",
                  "delivered_at": now(), "last_error": "",
                  "note": "" if quiet else BUSY_NOTE}
        # Una scelta di menu controlla la TUI ma non avvia un turno del
        # modello: non deve invalidare l'ultimo agent-report ne' generare un
        # sollecito automatico quattro minuti dopo.
        if wrapper_method == "menu-choice":
            fields["kind"] = "control"
        set_message(mid, **fields)
    else:
        set_message(mid, status=FAILED, method="paste", last_error=err)
    return ok, err


def deliver_async(row: dict, mid: str, text: str, *, wait_ready: bool = True,
                  count_attempt: bool = False) -> None:
    """Consegna in background.

    La richiesta HTTP termina appena il testo e' al sicuro in SQLite: la pagina
    conferma subito la registrazione e segue lo stato di consegna dal
    successivo aggiornamento di stato. Nessuna attesa della TUI nel ciclo
    request/response, che era la causa della latenza percepita all'invio.
    """
    def worker():
        try:
            deliver_text(row, mid, text, wait_ready=wait_ready, count_attempt=count_attempt)
        except Exception as exc:  # noqa: BLE001
            set_message(mid, status=FAILED, last_error=f"errore interno: {exc}"[:2000])
    threading.Thread(target=worker, daemon=True).start()


def confirm_argv_async(row: dict, mid: str) -> None:
    """Il prompt e' gia' negli argomenti della CLI: resta da sapere se l'harness
    sia partito, non se il testo sia arrivato.

    Il testo entra nella argv al momento della exec. Dedurre la consegna dai
    marcatori della TUI significava dedurla dal disegno dello schermo: bastava
    che il profilo restasse indietro di una versione — o che l'agente riempisse
    il pane di output prima del controllo — per marcare «non consegnato» un
    prompt su cui l'agente stava gia' lavorando.

    Quindi: marcatori riconosciuti -> consegnato e basta. Marcatori non
    riconosciuti ma harness vivo e schermo disegnato -> consegnato con una nota
    che dice cosa non e' stato confermato, perche' quel silenzio della TUI resta
    un fatto da mostrare. Solo pane morto, schermo vuoto o ostacolo riconosciuto
    («trust this folder», che attende davvero una risposta umana) sono un
    fallimento di consegna.
    """
    def worker():
        try:
            r = wrapper(row["unix_user"], SESSION_CTL, "await-ready", row["tmux_name"],
                        row["profile_id"], "",
                        "1" if row.get("auto_trust", 1) else "0", timeout=300)
            ok, err, _method = _wrapper_result(r)
            if ok:
                set_message(mid, status=SENT, method="cli-arg", delivered_at=now(),
                            last_error="", note="")
            elif _wrapper_data(r).get("started"):
                set_message(mid, status=SENT, method="cli-arg", delivered_at=now(),
                            last_error="",
                            note=("prompt passato nella riga di comando della CLI e harness "
                                  "avviato, ma la TUI non ha esposto i marcatori di prontezza "
                                  f"del profilo: {err}")[:2000])
            else:
                set_message(mid, status=FAILED, method="cli-arg", last_error=err)
        except Exception as exc:  # noqa: BLE001
            set_message(mid, status=FAILED, method="cli-arg", last_error=f"errore interno: {exc}"[:2000])
    threading.Thread(target=worker, daemon=True).start()


def validate_effort(prof: dict, value) -> str:
    """Livello di effort accettato dal profilo, oppure stringa vuota.

    Vuoto significa «non passare l'opzione»: l'harness usa il proprio default.
    Un valore non previsto dal profilo e' un errore esplicito, non un silenzioso
    ripiego sul default: chi ha chiesto `max` deve sapere se non l'ha ottenuto.
    """
    effort = (str(value or "")).strip().lower()
    if not effort:
        return ""
    if not prof.get("supports_effort"):
        raise HTTPException(400, f"il profilo {prof['id']} non supporta la selezione dell'effort")
    levels = prof.get("effort_levels") or []
    if effort not in levels:
        raise HTTPException(400, f"livello di effort non valido: {effort} — "
                                 f"ammessi: {', '.join(levels) or '(nessuno)'}")
    return effort


def executor_model_id(provider: str, model_id: str) -> str:
    """Id di un sub agent: `provider/modello`, che e' anche il suo instradamento.

    I provider OpenCode nominano gia' cosi' i propri modelli
    (`deepseek/deepseek-v4-flash`); Claude e Codex no, e il prefisso glielo
    mettiamo qui. E' quel prefisso che dice ad `agent-executor` quale CLI
    lanciare, quindi non e' decorazione: e' il contratto fra le due parti.
    """
    return model_id if model_id.startswith(provider + "/") else f"{provider}/{model_id}"


def executor_model_ids() -> list[str]:
    """Modelli davvero delegabili a un sub agent, dal catalogo di devagent.

    Non e' una lista scritta a mano: viene dal catalogo verificato leggendo i
    provider che `agent-executor` sa raggiungere. Un id che non e' qui dentro
    o non esiste piu' o non e' chiamabile con quelle credenziali, e in
    entrambi i casi non va offerto ne' accettato.
    """
    placeholders = ",".join("?" * len(EXECUTOR_PROVIDERS))
    with db() as conn:
        rows = conn.execute(
            "SELECT provider,model_id,kind FROM model_catalog WHERE unix_user=? "
            f"AND provider IN ({placeholders}) ORDER BY provider,model_id",
            (EXECUTOR_UNIX_USER, *EXECUTOR_PROVIDERS)).fetchall()
    return [executor_model_id(r["provider"], r["model_id"]) for r in rows
            if r["kind"] != "alias" or r["provider"] in EXECUTOR_ALIAS_PROVIDERS]


def validate_executor(body: dict, environment: str, prof: dict) -> tuple[int, str, int]:
    """Valida la delega a sub agent per una sessione.

    I sub agent girano sempre come devagent e restano confinati all'albero
    dei progetti. Da SERVER il bridge attraversa esplicitamente il confine
    utente senza creare agenti privilegiati. Il modello viene passato
    direttamente a OpenCode, quindi deve essere uno di quelli che OpenCode
    sa chiamare per devagent: la verifica sta qui e non solo nella pagina,
    perche' un id inesistente fallirebbe alla prima delega, a sessione
    ormai avviata.
    """
    enabled = 1 if body.get("executor_enabled") is True else 0
    if not enabled:
        return 0, "", 0
    known = executor_model_ids()
    if not known:
        raise HTTPException(400, "nessun modello delegabile: il catalogo dei sub agent per "
                                 "devagent e' vuoto (provider OpenCode non configurato o mai "
                                 "verificato). Aggiornalo dalla pagina Accounts.")
    requested = str(body.get("executor_model") or "").strip()
    model = requested or (EXECUTOR_DEFAULT_MODEL if EXECUTOR_DEFAULT_MODEL in known else known[0])
    if not EXECUTOR_MODEL_RE.fullmatch(model):
        raise HTTPException(400, "modello sub agent non valido")
    if model not in known:
        raise HTTPException(400, f"modello sub agent non disponibile per devagent: {model}")
    try:
        maximum = int(body.get("executor_max_agents") or 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "numero massimo sub agent non valido")
    if not 1 <= maximum <= EXECUTOR_MAX_LIMIT:
        raise HTTPException(400, f"numero massimo sub agent fuori intervallo: 1-{EXECUTOR_MAX_LIMIT}")
    return 1, model, maximum


def prompt_mode_for(prof: dict) -> str:
    """`argv` se la CLI installata accetta il prompt iniziale come argomento."""
    spec = prof.get("prompt_arg") or {}
    if spec.get("mode") == "positional":
        return "argv"
    if spec.get("mode") == "flag" and spec.get("flag"):
        return "argv"
    return "paste"


def start_tmux_session(row: dict, prompt: str, mode: str) -> dict:
    prompt_file = ""
    if prompt.strip():
        prompt_file = write_input_file(prompt)
    # Identificativo nuovo a ogni avvio: le CLI rifiutano di riusare un
    # --session-id gia' esistente, quindi un restart non puo' riciclare
    # quello precedente (e la trascrizione della run passata resta intatta).
    harness_sid = str(uuid.uuid4())
    args = [
        "start", row["tmux_name"], row["environment"], row["workdir"], row["profile_id"],
        row["permission_mode"], str(row["cols"]), str(row["rows"]),
        row["model"] or "", prompt_file or "", mode,
        row.get("effort") or "", harness_sid,
        "1" if row.get("executor_enabled") else "0",
        row.get("executor_model") or "", str(row.get("executor_max_agents") or 0),
    ]
    try:
        r = wrapper(row["unix_user"], SESSION_CTL, *args, timeout=120)
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass
    if r.returncode != 0:
        raise HTTPException(400, (r.stderr or r.stdout).strip() or "avvio sessione fallito")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


SECRET_RE = re.compile(r"(?i)(key|token|secret|password|passwd|auth|credential)")


def redact_argv(argv: list) -> list:
    """Argomenti pronti da mostrare: i valori che sembrano segreti spariscono.

    I comandi costruiti dai profili non contengono credenziali, ma la lista
    finisce in pagina: meglio non fidarsi di un profilo modificato a mano.
    """
    out, hide_next = [], False
    for a in argv:
        a = str(a)
        if hide_next:
            out.append("«rimosso»")
            hide_next = False
            continue
        if "=" in a and SECRET_RE.search(a.split("=", 1)[0]):
            out.append(a.split("=", 1)[0] + "=«rimosso»")
            continue
        if a.startswith("-") and SECRET_RE.search(a):
            out.append(a)
            hide_next = True
            continue
        out.append(a)
    return out


def save_launch_info(sid: str, started: dict, row: dict | None = None) -> None:
    """Registra com'e' andato l'avvio e l'id di sessione accettato dall'harness.

    L'id viene salvato solo se il wrapper conferma di averlo passato alla CLI:
    se il profilo non dichiara `session_id_arg` resta vuoto, e la pagina lo
    dira' invece di far credere che lo stato sia leggibile.
    """
    harness_sid = str(started.get("harness_session_id") or "")
    with db() as conn:
        conn.execute("UPDATE sessions SET launch_info=?, harness_session_id=? WHERE id=?",
                     (json.dumps({
                         "argv": redact_argv(started.get("argv") or []),
                         "cgroup": started.get("cgroup", ""),
                         "argv_prompt": bool(started.get("argv_prompt")),
                         "at": now(),
                     }), harness_sid, sid))
    if row is not None:
        row["harness_session_id"] = harness_sid


def launch_diagnostics(row: dict) -> dict:
    """Che cosa è stato realmente eseguito per questa sessione."""
    try:
        info = json.loads(row.get("launch_info") or "{}")
    except json.JSONDecodeError:
        info = {}
    try:
        prof = profile_by_id(row["profile_id"])
    except HTTPException:
        prof = {}
    perm = row["permission_mode"]
    return {
        "harness": prof.get("harness", "?"),
        "profile_id": row["profile_id"],
        "profile_label": prof.get("label", ""),
        "permission_mode": perm,
        "permission_args": prof.get("permission_modes", {}).get(perm, []),
        "model": row["model"] or "(default dell'harness)",
        "effort": row.get("effort") or "(default dell'harness)",
        "unix_user": row["unix_user"],
        "workdir": row["workdir"],
        "argv": info.get("argv", []),
        "cgroup": info.get("cgroup", ""),
        "prompt_as_argv": info.get("argv_prompt", False),
        "launched_at": info.get("at", ""),
        "trust_note": prof.get("trust_note", ""),
        # blocco aggiunto in coda a ogni testo consegnato, mai al testo salvato
        "contract": contract_block(row).strip(),
    }


def register_initial_prompt(row: dict, prompt: str, started: dict) -> str:
    """Crea il messaggio iniziale e ne aggiorna lo stato dopo il tentativo reale.

    In SQLite finisce il testo dell'utente; alla sessione arriva quel testo piu'
    il contratto di stato finale.
    """
    if not prompt.strip():
        return ""
    mid = add_message(row["id"], prompt, kind="initial")
    bump_attempt(mid)
    if started.get("argv_prompt"):
        set_message(mid, status=LAUNCHING, method="cli-arg")
        confirm_argv_async(row, mid)
    else:
        deliver_async(row, mid, with_contract(row, prompt, initial=True))
    return mid


@app.post("/api/sessions")
async def create_session(request: Request):
    body = await request.json()
    return _create_session(body)


def validate_workdir(path: str, environment: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/") or "\x00" in path:
        raise HTTPException(400, f"Percorso non valido: {path or '(vuoto)'} — "
                                 "deve essere un percorso assoluto che inizia con /")
    real = os.path.realpath(path)
    if environment == "PROJECT":
        root = os.path.realpath(CONFIG["projects"])
        if real != root and not real.startswith(root + os.sep):
            raise HTTPException(400, f"Directory inesistente o non autorizzata: {path} — "
                                     f"le sessioni PROJECT restano sotto {root}")
    r = wrapper(UNIX_USERS[environment], PROJECT_CTL, "ls", real, timeout=30)
    if r.returncode != 0:
        raise HTTPException(400, f"Directory inesistente o non autorizzata: {path} "
                                 f"({(r.stderr or r.stdout).strip()[:200]})")
    return real


def _create_session(body: dict) -> dict:
    name = (body.get("name") or "").strip() or "sessione"
    environment = body.get("environment", "PROJECT")
    if environment not in UNIX_USERS:
        raise HTTPException(400, "ambiente non valido: usare PROJECT o SERVER")
    unix_user = UNIX_USERS[environment]
    prof = profile_by_id(body.get("profile_id", ""))
    if unix_user not in prof.get("allowed_users", []):
        raise HTTPException(400, f"il profilo {prof['id']} non e' consentito per {unix_user}")

    slug = (body.get("project_slug") or "").strip()
    if environment == "PROJECT":
        if not slug or not SLUG_RE.match(slug):
            raise HTTPException(400, "progetto obbligatorio per le sessioni PROJECT")
        with db() as conn:
            p = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        if not p:
            raise HTTPException(400, f"progetto non registrato: {slug}")
        workdir = validate_workdir(body.get("workdir") or p["path"], environment)
    else:
        workdir = validate_workdir(body.get("workdir") or SERVER_HOME, environment)
        slug = ""

    perm = body.get("permission_mode") or prof.get("default_permission_mode", "full")
    if perm not in prof.get("permission_modes", {}):
        raise HTTPException(400, f"modalita' permessi non valida: {perm}")

    model = (body.get("model") or "").strip()
    effort = validate_effort(prof, body.get("effort"))
    executor_enabled, executor_model, executor_max_agents = validate_executor(
        body, environment, prof)
    try:
        cols = max(40, min(400, int(body.get("cols") or 100)))
        rows = max(10, min(200, int(body.get("rows") or 30)))
    except (TypeError, ValueError):
        raise HTTPException(400, "colonne e righe devono essere numeri")

    # I documenti associati restano nella knowledge area privata per default;
    # solo quelli marcati repository vengono copiati nel worktree Git.
    docs = [materialize_document(d, slug)
            for d in documents_by_ids([str(d) for d in (body.get("document_ids") or [])])]
    prompt = body.get("prompt") or ""
    if docs:
        prompt = (prompt.rstrip() + "\n\n" + documents_block(docs)).strip()

    sid = str(uuid.uuid4())
    row = {
        "id": sid, "name": name, "tmux_name": f"agenthub-{sid}", "project_slug": slug,
        "workdir": workdir, "environment": environment, "profile_id": prof["id"],
        "model": model, "effort": effort, "harness_session_id": "",
        "executor_enabled": executor_enabled, "executor_model": executor_model,
        "executor_max_agents": executor_max_agents,
        "permission_mode": perm, "unix_user": unix_user, "kind": "agent",
        "status": "starting", "initial_prompt": prompt,
        "parent_session": body.get("parent_session") or "", "cols": cols, "rows": rows,
        "created_at": now(), "ended_at": "",
        # La conferma della directory non e' piu' una scelta: la directory di
        # lavoro viene scelta qui dentro, quindi il dialogo dell'harness
        # («I trust this folder» di Claude Code) non aggiunge alcuna decisione
        # e, senza risposta, blocca la TUI prima ancora del prompt iniziale.
        # La colonna resta per le sessioni gia' registrate; le nuove sono
        # sempre a conferma automatica, qualunque sia il profilo.
        "auto_trust": 1,
    }
    # 1. sessione e prompt sono persistiti PRIMA di avviare l'harness
    insert_session(row)
    if docs:
        with db() as conn:
            for d in docs:
                conn.execute("INSERT OR IGNORE INTO session_documents "
                             "(session_id,document_id,created_at) VALUES (?,?,?)",
                             (sid, d["id"], now()))

    # 2. avvio dell'harness; alla CLI arriva il prompt con il contratto in coda
    mode = prompt_mode_for(prof) if prompt.strip() else "paste"
    launch_text = with_contract(row, prompt, initial=True) if prompt.strip() else ""
    try:
        started = start_tmux_session(row, launch_text, mode)
    except HTTPException as exc:
        with db() as conn:
            conn.execute("UPDATE sessions SET status='failed', ended_at=? WHERE id=?", (now(), sid))
        if prompt.strip():
            mid = add_message(sid, prompt, kind="initial")
            set_message(mid, status=FAILED, last_error=str(exc.detail)[:2000])
        raise
    with db() as conn:
        conn.execute("UPDATE sessions SET status='running' WHERE id=?", (sid,))
    row["status"] = "running"
    save_launch_info(sid, started, row)

    # 3. consegna del prompt; lo stato cambia solo dopo il tentativo reale
    mid = register_initial_prompt(row, prompt, started)
    return {"session": row, "message_id": mid,
            "delivery": ("cli-arg" if started.get("argv_prompt") else ("paste" if mid else "none")),
            "cgroup": started.get("cgroup", "")}


def insert_session(row: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (id,name,tmux_name,project_slug,workdir,environment,"
            "profile_id,model,effort,harness_session_id,executor_enabled,executor_model,"
            "executor_max_agents,permission_mode,unix_user,kind,"
            "status,initial_prompt,parent_session,cols,rows,created_at,ended_at,auto_trust) VALUES "
            "(:id,:name,:tmux_name,:project_slug,:workdir,:environment,:profile_id,:model,"
            ":effort,:harness_session_id,:executor_enabled,:executor_model,:executor_max_agents,"
            ":permission_mode,:unix_user,:kind,:status,"
            ":initial_prompt,:parent_session,:cols,:rows,:created_at,:ended_at,:auto_trust)", row)


@app.get("/api/sessions/{sid}/messages")
async def list_messages(sid: str):
    get_session(sid)
    return {"messages": session_messages(sid)}


@app.post("/api/sessions/{sid}/messages")
async def post_message(sid: str, request: Request):
    row = get_session(sid)
    body = await request.json()
    text = body.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "testo mancante: scrivi il messaggio da inviare")
    if len(text) > 500_000:
        raise HTTPException(400, "testo troppo lungo (massimo 500 000 caratteri)")
    docs = attach_docs_to_session(row, [str(d) for d in (body.get("document_ids") or [])])
    if docs:
        text = text.rstrip() + "\n\n" + documents_block(docs)
    # il testo e' salvato prima del tentativo: un errore di tmux non lo perde
    mid = add_message(sid, text, kind="user")
    bump_attempt(mid)
    deliver_async(row, mid, with_contract(row, text, initial=False), wait_ready=False)
    return {"ok": True, "registered": True, "message": message_row(mid),
            "detail": "Messaggio registrato: la consegna è in corso."}


@app.post("/api/sessions/{sid}/messages/{mid}/resend")
async def resend_message(sid: str, mid: str):
    row = get_session(sid)
    with db() as conn:
        m = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?",
                         (mid, sid)).fetchone()
    if not m:
        raise HTTPException(404, "messaggio inesistente")
    if m["kind"] == "control":
        raise HTTPException(400, "una scelta TUI non puo' essere reinviata come messaggio")
    # il reinvio risponde dentro la richiesta HTTP: qui l'attesa che la
    # sessione si liberi resta corta, quanto basta a scavalcare un tool call
    ok, err = deliver_text(row, mid,
                           with_contract(row, m["text"], initial=(m["kind"] == "initial")),
                           wait_ready=False, final_status=RESENT, busy_cap=20.0)
    if not ok:
        raise HTTPException(400, f"Reinvio fallito: {err}")
    return {"ok": True, "message": message_row(mid)}


@app.post("/api/sessions/{sid}/input")
async def session_input(sid: str, request: Request):
    """Alias storico di POST /messages."""
    return await post_message(sid, request)


@app.post("/api/sessions/{sid}/action")
async def session_action(sid: str, request: Request):
    row = get_session(sid)
    action = (await request.json()).get("action", "")
    u, t = row["unix_user"], row["tmux_name"]
    if action in ("pause", "enter", "up", "down"):
        # Il tastierino della UI: Pause ferma la generazione restando dentro la
        # TUI, Invio conferma, Su/Giu' scelgono un'altra voce del dialogo.
        # Ctrl-C non e' piu' esposto qui: resta raggiungibile dal terminale.
        key = {"pause": "escape", "enter": "enter", "up": "up", "down": "down"}[action]
        r = wrapper(u, SESSION_CTL, "keys", t, key, timeout=30)
        if r.returncode != 0:
            raise HTTPException(400, (r.stderr or r.stdout).strip() or "invio tasto fallito")
    elif action == "kill":
        wrapper(u, SESSION_CTL, "kill", t, timeout=30)
        with db() as conn:
            conn.execute("UPDATE sessions SET status='ended', ended_at=? WHERE id=?", (now(), sid))
    elif action == "restart":
        wrapper(u, SESSION_CTL, "kill", t, timeout=30)
        time.sleep(1)
        prof = profile_by_id(row["profile_id"])
        prompt = row["initial_prompt"]
        started = start_tmux_session(
            row, with_contract(row, prompt, initial=True) if prompt.strip() else "",
            prompt_mode_for(prof) if prompt.strip() else "paste")
        with db() as conn:
            conn.execute("UPDATE sessions SET status='running', ended_at='', created_at=? WHERE id=?",
                         (now(), sid))
        row["created_at"] = now()
        save_launch_info(sid, started, row)
        register_initial_prompt(row, prompt, started)
    elif action == "delete":
        delete_session(row)
    else:
        raise HTTPException(400, f"azione non valida: {action}")
    return {"ok": True, "action": action}


def delete_session(row: dict) -> None:
    wrapper(row["unix_user"], SESSION_CTL, "kill", row["tmux_name"], timeout=30)
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
        conn.execute("DELETE FROM messages WHERE session_id=?", (row["id"],))
        conn.execute("DELETE FROM session_documents WHERE session_id=?", (row["id"],))
    _TRANSCRIPT_CACHE.pop(row["tmux_name"], None)
    logs = Path(CONFIG["logs"])
    for f in (logs / f"{row['tmux_name']}.log", logs / f"{row['tmux_name']}.transcript.txt"):
        try:
            f.unlink()
        except OSError:
            pass


@app.post("/api/sessions/cleanup")
async def sessions_cleanup(request: Request):
    """Elimina in blocco sessioni concluse: server personale, nessun vincolo di audit."""
    body = await request.json()
    ids = [str(i) for i in (body.get("ids") or [])]
    live = sync_status()
    with db() as conn:
        if ids:
            marks = ",".join("?" * len(ids))
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM sessions WHERE id IN ({marks})", ids)]
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM sessions WHERE status != 'running'")]
    removed = []
    for r in rows:
        if r["tmux_name"] in live and not body.get("force"):
            continue  # una sessione viva non sparisce per errore
        delete_session(r)
        removed.append(r["id"])
    return {"ok": True, "removed": removed, "count": len(removed)}


# ------------------------------------------------------------------- log
#
# Due rappresentazioni distinte dello stesso materiale:
#   - il log raw (flusso PTY integrale) resta intatto per la diagnostica ed e'
#     servito solo come allegato da scaricare, mai come testo nel browser;
#   - la trascrizione e' testo semplice, senza ANSI/OSC/controlli, ricavata da
#     `tmux capture-pane -p -J -S -` oppure emulando lo schermo sul log raw.

MAX_RAW_SCAN = 8_000_000          # byte di log raw analizzati per la trascrizione
MAX_TRANSCRIPT_BYTES = 1_000_000  # tetto della risposta
DEFAULT_TAIL_LINES = 3000
MAX_TAIL_LINES = 50_000

_TRANSCRIPT_CACHE: dict[str, tuple[tuple, str]] = {}


def log_path(row: dict) -> Path:
    return Path(CONFIG["logs"]) / f"{row['tmux_name']}.log"


def snapshot_path(row: dict) -> Path:
    """Trascrizione salvata alla chiusura della sessione tmux."""
    return Path(CONFIG["logs"]) / f"{row['tmux_name']}.transcript.txt"


def wrapper_bytes(user: str, script: str, *args: str, timeout: int = 60) -> bytes:
    """Come `wrapper` ma senza decodifica: il testo puo' contenere UTF-8 misto."""
    cmd = ["sudo", "-n", "-u", user, script, *[str(a) for a in args]]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout if r.returncode == 0 else b""


def tmux_transcript(row: dict) -> str:
    """Trascrizione del pane vivo. Vuota se la sessione non esiste piu'."""
    return tr.clean_capture(wrapper_bytes(row["unix_user"], SESSION_CTL, "capture",
                                          row["tmux_name"], "all", timeout=60))


def raw_transcript(row: dict) -> str:
    """Emula lo schermo sul log raw: ricostruisce l'intero storico."""
    path = log_path(row)
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_size, st.st_mtime_ns, row["cols"], row["rows"])
    hit = _TRANSCRIPT_CACHE.get(row["tmux_name"])
    if hit and hit[0] == key:
        return hit[1]
    with open(path, "rb") as fh:
        if st.st_size > MAX_RAW_SCAN:
            fh.seek(st.st_size - MAX_RAW_SCAN)
        data = fh.read()
    text = tr.sanitize(data, int(row["cols"] or 100), int(row["rows"] or 30))
    _TRANSCRIPT_CACHE.clear()  # una sola sessione alla volta in cache: basta
    _TRANSCRIPT_CACHE[row["tmux_name"]] = (key, text)
    return text


def build_transcript(row: dict, source: str) -> tuple[str, str]:
    """Restituisce (testo, sorgente effettiva) secondo la strategia richiesta."""
    running = row["status"] == "running"
    if source == "tmux":
        if running:
            return tmux_transcript(row), "tmux"
        snap = snapshot_path(row)
        if snap.exists():
            return tr.clean_capture(snap.read_bytes()), "snapshot"
        return "", "tmux"
    if source == "log":
        return raw_transcript(row), "log"

    # auto: si preferisce tmux quando offre davvero uno scrollback. Le TUI che
    # vivono nello schermo alternativo (Claude Code) non ne hanno, quindi in
    # quel caso il log raw emulato e' l'unica fonte con lo storico completo.
    if running:
        cap = tmux_transcript(row)
        if cap.count("\n") + 1 > int(row["rows"] or 30):
            return cap, "tmux"
        raw = raw_transcript(row)
        if len(raw) > len(cap):
            return raw, "log"
        return cap, "tmux"
    raw = raw_transcript(row)
    snap = snapshot_path(row)
    if snap.exists():
        snap_text = tr.clean_capture(snap.read_bytes())
        if len(snap_text) > len(raw):
            return snap_text, "snapshot"
    return raw, "log"


@app.get("/api/sessions/{sid}/transcript", response_class=PlainTextResponse)
async def session_transcript(sid: str, source: str = "auto", tail: int = DEFAULT_TAIL_LINES,
                             chrome: str = "hide"):
    row = get_session(sid)
    if source not in ("auto", "tmux", "log"):
        raise HTTPException(400, "sorgente non valida: usa auto, tmux oppure log")
    if chrome not in ("hide", "show"):
        raise HTTPException(400, "chrome non valido: usa hide oppure show")
    try:
        tail = max(50, min(MAX_TAIL_LINES, int(tail)))
    except (TypeError, ValueError):
        tail = DEFAULT_TAIL_LINES
    text, used = build_transcript(row, source)
    # La cornice si toglie prima del taglio, cosi' `tail` conta righe di
    # contenuto e non separatori.
    if chrome == "hide" and text.strip():
        text = tr.strip_chrome(text)
    if not text.strip():
        text = "(nessuna trascrizione disponibile per questa sessione)"
        truncated = False
    else:
        text, truncated = tr.tail(text, tail, MAX_TRANSCRIPT_BYTES)
    # text/plain esplicito piu' nosniff: il contenuto non viene mai
    # interpretato come HTML dal browser
    return PlainTextResponse(text, headers={
        "Content-Type": "text/plain; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
        "X-Transcript-Source": used,
        "X-Transcript-Chrome": chrome,
        "X-Transcript-Lines": str(text.count("\n") + 1),
        "X-Transcript-Truncated": "1" if truncated else "0",
        "Cache-Control": "no-store",
    })


@app.get("/api/sessions/{sid}/log/raw")
async def session_log_raw(sid: str):
    """Log PTY integrale, servito come allegato: mai renderizzato in pagina."""
    row = get_session(sid)
    path = log_path(row)
    if not path.exists():
        raise HTTPException(404, "nessun log raw per questa sessione")
    return FileResponse(path, media_type="application/octet-stream",
                        filename=f"{row['tmux_name']}.log",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/sessions/{sid}/log", response_class=PlainTextResponse)
async def session_log(sid: str, tail: int = DEFAULT_TAIL_LINES):
    """Alias storico: rimanda alla trascrizione, mai al PTY grezzo."""
    return await session_transcript(sid, "auto", tail)


# ------------------------------------------------------------- documenti

# Deve restare allineata a UNVERIFIED_SUBDIR in project-ctl, che e' l'unico
# punto autorizzato a creare la cartella.
DOC_UNVERIFIED_SUBDIR = "license-unverified"


def doc_subdir(path: str) -> str:
    """Sottocartella di docs/input in cui vive il documento, se e' una nota."""
    return DOC_UNVERIFIED_SUBDIR if Path(path or "").parent.name == DOC_UNVERIFIED_SUBDIR else ""


def safe_name(name: str) -> str:
    """Nome file normalizzato: niente traversal, niente caratteri esotici."""
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    name = re.sub(r"_{2,}", "_", name).strip(" ._-")
    return (name or "documento")[:120]


def doc_ext(name: str) -> str:
    low = name.lower()
    if low.endswith(".tar.gz"):
        return ".gz"
    return os.path.splitext(low)[1]


def documents_by_ids(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    with db() as conn:
        marks = ",".join("?" * len(ids))
        rows = list(conn.execute(f"SELECT * FROM documents WHERE id IN ({marks})", ids))
        return existing_document_rows(rows)


def document_payload(row: sqlite3.Row | dict) -> dict:
    """Aggiunge metadati filesystem senza esporre record ormai orfani."""
    item = dict(row)
    try:
        path = Path(item["path"])
        if not path.is_file():
            raise FileNotFoundError(item["path"])
        stat = path.stat()
    except OSError:
        item["exists"] = False
        item["modified_at"] = ""
    else:
        item["exists"] = True
        item["modified_at"] = datetime.fromtimestamp(
            stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
    return item


def existing_document_rows(rows) -> list[dict]:
    """La GUI mostra solo file realmente leggibili nel filesystem gestito."""
    return [item for item in (document_payload(row) for row in rows) if item["exists"]]


def materialize_document(doc: dict, slug: str) -> dict:
    """Porta un documento nella sua destinazione dichiarata, fuori o dentro Git."""
    if not slug or doc["project_slug"] == slug or not os.path.isfile(doc["path"]):
        return doc
    try:
        command = "doc-import" if doc.get("scope", "private") == "repository" else "knowledge-import"
        res = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, command, slug,
                           doc["path"], doc["name"], timeout=300)
    except HTTPException:
        return doc  # il percorso attuale resta valido e viene comunicato com'e'
    old_parent = Path(doc["path"]).parent
    if doc["project_slug"]:
        old_command = "doc-delete" if doc.get("scope", "private") == "repository" else "knowledge-delete"
        wrapper(PROJECT_UNIX_USER, PROJECT_CTL, old_command, doc["project_slug"], doc["name"], timeout=60)
    elif str(old_parent).startswith(CONFIG["uploads"] + "/"):
        shutil.rmtree(old_parent, ignore_errors=True)
    with db() as conn:
        conn.execute("UPDATE documents SET project_slug=?, path=?, name=? WHERE id=?",
                     (slug, res["path"], res["name"], doc["id"]))
    return get_document(doc["id"])


def promote_document(doc: dict, slug: str) -> dict:
    """Compatibilità interna: i vecchi chiamanti usano la nuova destinazione."""
    return materialize_document(doc, slug)


def attach_docs_to_session(row: dict, ids: list[str]) -> list[dict]:
    """Associa documenti a una sessione.

    Se la sessione appartiene a un progetto, un documento ancora libero viene
    prima spostato dentro il progetto: il percorso comunicato all'agente e'
    quello definitivo, non quello temporaneo dell'area di upload.
    """
    docs = documents_by_ids(ids)
    if not docs:
        return []
    slug = row.get("project_slug") or ""
    docs = [materialize_document(d, slug) for d in docs]
    with db() as conn:
        for d in docs:
            conn.execute("INSERT OR IGNORE INTO session_documents "
                         "(session_id,document_id,created_at) VALUES (?,?,?)",
                         (row["id"], d["id"], now()))
    return docs


def documents_block(docs: list[dict]) -> str:
    lines = ["## Documenti allegati (percorsi completi sul server)",
             "I documenti private sono nella knowledge area di Agent Hub e non fanno parte del repository; "
             "i documenti repository sono versionabili e richiedono una scelta esplicita."]
    for d in docs:
        lines.append(f"- {d['path']}  [{d.get('scope', 'private')}] ({d['size']} byte, sha256 {d['sha256'][:16]})")
    lines.append("")
    lines.append("Leggi i file dai percorsi indicati quando ti servono.")
    return "\n".join(lines)


@app.get("/api/documents")
async def list_documents(project: str = ""):
    with db() as conn:
        if project:
            rows = conn.execute(
                "SELECT * FROM documents WHERE project_slug=? ORDER BY created_at DESC", (project,))
        else:
            rows = conn.execute("SELECT * FROM documents ORDER BY created_at DESC")
        rows = existing_document_rows(rows)
    return {"documents": rows, "max_upload": CONFIG["max_upload"],
            "allowed_ext": sorted(ALLOWED_DOC_EXT)}


@app.post("/api/documents")
async def upload_documents(request: Request):
    """Upload multiplo in streaming. Nessun file viene mai eseguito."""
    form = await request.form()
    slug = (form.get("project_slug") or "").strip()
    if slug and not SLUG_RE.match(slug):
        raise HTTPException(400, f"slug progetto non valido: {slug}")
    if slug:
        with db() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                raise HTTPException(400, f"progetto non registrato: {slug}")
    files = [v for _k, v in form.multi_items() if hasattr(v, "filename") and v.filename]
    if not files:
        raise HTTPException(400, "nessun file ricevuto")
    saved, errors = [], []
    for up in files:
        try:
            saved.append(store_upload(up, slug))
        except HTTPException as exc:
            errors.append(f"{up.filename}: {exc.detail}")
        finally:
            await up.close()
    if not saved and errors:
        raise HTTPException(400, "; ".join(errors))
    return {"documents": saved, "errors": errors}


def store_upload(up, slug: str) -> dict:
    name = safe_name(up.filename)
    ext = doc_ext(name)
    if ext not in ALLOWED_DOC_EXT:
        raise HTTPException(400, f"estensione non ammessa: {ext or '(nessuna)'} — "
                                 f"ammesse: {', '.join(sorted(ALLOWED_DOC_EXT))}")
    did = str(uuid.uuid4())
    staging = Path(CONFIG["uploads"]) / did
    staging.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(staging, 0o2775)
    except OSError:
        pass
    dest = staging / name
    sha = hashlib.sha256()
    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = up.file.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > CONFIG["max_upload"]:
                    raise HTTPException(400, "file troppo grande: limite "
                                             f"{CONFIG['max_upload'] // (1024 * 1024)} MiB")
                sha.update(chunk)
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        os.chmod(dest, 0o664)
    except OSError:
        pass
    path = str(dest)
    scope = "private"
    if slug:
        res = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "knowledge-import", slug, path, name, timeout=300)
        path, name = res["path"], res["name"]
        shutil.rmtree(staging, ignore_errors=True)
    doc = {
        "id": did, "name": name, "original_name": (up.filename or "")[:200], "path": path,
        "project_slug": slug, "scope": scope, "size": size, "sha256": sha.hexdigest(),
        "content_type": ALLOWED_DOC_EXT.get(ext, "application/octet-stream"),
        "created_at": now(),
    }
    with db() as conn:
        conn.execute(
            "INSERT INTO documents (id,name,original_name,path,project_slug,scope,size,sha256,"
            "content_type,created_at) VALUES (:id,:name,:original_name,:path,:project_slug,:scope,"
            ":size,:sha256,:content_type,:created_at)", doc)
    return doc


def get_document(did: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    if not row:
        raise HTTPException(404, "documento inesistente")
    return dict(row)


@app.get("/api/documents/{did}/download")
async def download_document(did: str):
    d = get_document(did)
    if not os.path.isfile(d["path"]):
        raise HTTPException(404, f"file non piu' presente sul disco: {d['path']}")
    return FileResponse(d["path"], media_type="application/octet-stream",
                        filename=d["name"],
                        headers={"X-Content-Type-Options": "nosniff"})


@app.post("/api/documents/{did}/assign")
async def assign_document(did: str, request: Request):
    d = get_document(did)
    body = await request.json()
    slug = (body.get("project_slug") or "").strip()
    scope = (body.get("scope") or d.get("scope") or "private").strip()
    if scope not in ("private", "repository"):
        raise HTTPException(400, "scope deve essere private oppure repository")
    # Diritti di redistribuzione non ancora verificati: il file entra comunque
    # nel repository, ma in una sottocartella che lo dichiara, cosi' la review
    # che precede il rilascio non puo' non vederlo.
    subdir = "" if body.get("license_verified", True) else DOC_UNVERIFIED_SUBDIR
    if not SLUG_RE.match(slug):
        raise HTTPException(400, f"slug progetto non valido: {slug}")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
            raise HTTPException(400, f"progetto non registrato: {slug}")
    if (d["project_slug"] == slug and d.get("scope", "private") == scope
            and doc_subdir(d["path"]) == subdir):
        return {"ok": True, "document": d}
    if not os.path.isfile(d["path"]):
        raise HTTPException(400, f"file non piu' presente sul disco: {d['path']}")
    command = "doc-import" if scope == "repository" else "knowledge-import"
    args = [slug, d["path"], d["name"]] + ([subdir] if scope == "repository" and subdir else [])
    res = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, command, *args, timeout=300)
    if d["project_slug"]:
        old_repo = d.get("scope", "private") == "repository"
        old_command = "doc-delete" if old_repo else "knowledge-delete"
        old_args = [d["project_slug"], d["name"]]
        if old_repo and doc_subdir(d["path"]):
            old_args.append(doc_subdir(d["path"]))
        wrapper(PROJECT_UNIX_USER, PROJECT_CTL, old_command, *old_args, timeout=60)
    old_parent = Path(d["path"]).parent
    if str(old_parent).startswith(CONFIG["uploads"] + "/"):
        shutil.rmtree(old_parent, ignore_errors=True)
    with db() as conn:
        conn.execute("UPDATE documents SET project_slug=?, scope=?, path=?, name=? WHERE id=?",
                     (slug, scope, res["path"], res["name"], d["id"]))
    moved = get_document(did)
    if moved["project_slug"] != slug:
        raise HTTPException(400, f"spostamento in {slug} fallito: controlla i permessi di "
                                 f"{CONFIG['knowledge']}/{slug}")
    return {"ok": True, "document": moved}


@app.delete("/api/documents/{did}")
async def delete_document(did: str):
    d = get_document(did)
    if d["project_slug"]:
        command = "doc-delete" if d.get("scope", "private") == "repository" else "knowledge-delete"
        wrapper(PROJECT_UNIX_USER, PROJECT_CTL, command, d["project_slug"], d["name"], timeout=60)
    else:
        parent = Path(d["path"]).parent
        if str(parent).startswith(CONFIG["uploads"] + "/"):
            shutil.rmtree(parent, ignore_errors=True)
    with db() as conn:
        conn.execute("DELETE FROM documents WHERE id=?", (did,))
        conn.execute("DELETE FROM session_documents WHERE document_id=?", (did,))
    return {"ok": True}


@app.post("/api/sessions/{sid}/documents")
async def attach_documents(sid: str, request: Request):
    """Aggiunge documenti a una sessione gia' attiva e ne comunica i percorsi."""
    row = get_session(sid)
    body = await request.json()
    docs = attach_docs_to_session(row, [str(d) for d in (body.get("document_ids") or [])])
    if not docs:
        raise HTTPException(400, "nessun documento selezionato")
    note = (body.get("note") or "").strip()
    text = (note + "\n\n" if note else "") + documents_block(docs)
    mid = add_message(sid, text, kind="user")
    bump_attempt(mid)
    deliver_async(row, mid, with_contract(row, text, initial=False), wait_ready=False)
    return {"ok": True, "registered": True, "documents": docs, "message": message_row(mid),
            "detail": "Percorsi registrati: la consegna è in corso."}


# ------------------------------------------------- handoff deterministico

def project_context(slug: str) -> dict:
    if not slug:
        return {}
    try:
        return wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "status", slug, timeout=120)
    except HTTPException:
        return {}


def compose_handoff(row: dict, *, to_host: bool) -> str:
    ctx = project_context(row["project_slug"])
    parts = []
    if to_host:
        parts.append("# Handoff amministrativo da devagent a hostagent\n")
        parts.append("Completa la parte host di questo lavoro. Al termine lascia i file del "
                     "progetto di proprieta' devagent:agentprojects.\n")
    else:
        parts.append("# Prosecuzione del lavoro con un altro harness\n")
    parts.append(f"## Sessione precedente\n- id: {row['id']}\n- nome: {row['name']}\n"
                 f"- harness: {row['profile_id']}  modello: {row['model'] or '(default)'}\n"
                 f"- ambiente: {row['environment']} ({row['unix_user']})\n"
                 f"- directory: {row['workdir']}\n")
    if row["project_slug"]:
        parts.append(f"- progetto: {row['project_slug']}  ({row['workdir']})")
        parts.append(f"- dati deployment: /srv/agent-workspace/data/{row['project_slug']}")
    if row["initial_prompt"].strip():
        parts.append("\n## Obiettivo originale\n" + row["initial_prompt"].strip())
    docs = session_documents(row["id"])
    if docs:
        parts.append("\n" + documents_block(docs))
    if ctx.get("handoff"):
        parts.append("\n## .agent/HANDOFF.md corrente\n```\n" + ctx["handoff"].strip() + "\n```")
    else:
        parts.append("\n## .agent/HANDOFF.md corrente\n(assente)")
    parts.append("\n## git status --short\n```\n" + (ctx.get("short") or "(pulito o non un repo git)")
                 + "\n```")
    if to_host:
        try:
            docker = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "docker-status", timeout=60).get("output", "")
        except HTTPException:
            docker = "(non disponibile)"
        parts.append("\n## Docker rootless di devagent\n```\n" + (docker or "(nessun container)") + "\n```")
        parts.append("\n## Cosa fare\nLeggi AGENTS.md e .agent/HANDOFF.md del progetto, esegui le "
                     "operazioni amministrative necessarie e aggiorna .agent/HANDOFF.md con "
                     "l'azione host eseguita, il risultato e gli eventuali passi residui.")
    else:
        parts.append("\n## Cosa fare\nRiprendi il lavoro da questo stato. Leggi AGENTS.md e "
                     ".agent/HANDOFF.md, poi prosegui verso l'obiettivo indicato.")
    return "\n".join(parts)


@app.post("/api/sessions/{sid}/continue-with")
async def continue_with(sid: str, request: Request):
    row = get_session(sid)
    body = await request.json()
    prompt = compose_handoff(row, to_host=False)
    return _create_session({
        "name": f"{row['name']} → {body.get('profile_id')}",
        "environment": row["environment"],
        "project_slug": row["project_slug"],
        "workdir": row["workdir"],
        "profile_id": body.get("profile_id"),
        "model": body.get("model", ""),
        "effort": body.get("effort", ""),
        "executor_enabled": bool(row.get("executor_enabled")),
        "executor_model": row.get("executor_model") or "",
        "executor_max_agents": row.get("executor_max_agents") or 0,
        "prompt": prompt,
        "parent_session": sid,
        "cols": row["cols"], "rows": row["rows"],
        "auto_trust": bool(row.get("auto_trust", 1)),
    })


@app.post("/api/sessions/{sid}/escalate")
async def escalate(sid: str, request: Request):
    row = get_session(sid)
    if row["environment"] != "PROJECT":
        raise HTTPException(400, "l'escalation e' disponibile solo per sessioni PROJECT")
    body = await request.json()
    prompt = compose_handoff(row, to_host=True)
    if body.get("note"):
        prompt += "\n\n## Nota dell'operatore\n" + str(body["note"])
    return _create_session({
        "name": f"ESCALATION · {row['name']}",
        "environment": "SERVER",
        "workdir": row["workdir"],
        "profile_id": body.get("profile_id") or row["profile_id"],
        "model": body.get("model", ""),
        "effort": body.get("effort", ""),
        "prompt": prompt,
        "parent_session": sid,
        "cols": row["cols"], "rows": row["rows"],
        "auto_trust": bool(row.get("auto_trust", 1)),
    })


@app.get("/api/sessions/{sid}/handoff-preview")
async def handoff_preview(sid: str, to_host: int = 0):
    return {"prompt": compose_handoff(get_session(sid), to_host=bool(to_host))}


# --------------------------------------------------------------- progetti

@app.get("/api/fs")
async def browse_fs(path: str = "", environment: str = "SERVER"):
    """Elenco delle sottodirectory: alimenta il selettore di percorso della UI."""
    if environment not in UNIX_USERS:
        raise HTTPException(400, "ambiente non valido")
    if not path:
        path = CONFIG["projects"] if environment == "PROJECT" else f"/home/{UNIX_USERS[environment]}"
    if not path.startswith("/"):
        raise HTTPException(400, f"Percorso non valido: {path} — deve iniziare con /")
    real = os.path.realpath(path)
    root = os.path.realpath(CONFIG["projects"])
    if environment == "PROJECT" and real != root and not real.startswith(root + os.sep):
        raise HTTPException(400, f"Directory inesistente o non autorizzata: {path} — "
                                 f"le sessioni PROJECT restano sotto {root}")
    r = wrapper(UNIX_USERS[environment], PROJECT_CTL, "ls", real, timeout=30)
    if r.returncode != 0:
        raise HTTPException(400, f"Directory inesistente o non autorizzata: {path} "
                                 f"({(r.stderr or r.stdout).strip()[:200]})")
    data = json.loads(r.stdout)
    if environment == "PROJECT" and data["path"] == root:
        data["parent"] = root  # la root PROJECT e' invalicabile verso l'alto
    return data


@app.get("/api/projects")
async def list_projects():
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY slug")]
        for r in rows:
            r["ports"] = [dict(p) for p in conn.execute(
                "SELECT port, description FROM ports WHERE project_slug=? ORDER BY port", (r["slug"],))]
            paths = conn.execute(
                "SELECT path FROM documents WHERE project_slug=?", (r["slug"],))
            r["documents"] = sum(Path(item["path"]).is_file() for item in paths)
            r["meetings"] = conn.execute(
                "SELECT COUNT(*) FROM meetings WHERE project_slug=?", (r["slug"],)
            ).fetchone()[0]
            context = conn.execute(
                "SELECT updated_at,package_path FROM project_contexts WHERE project_slug=?",
                (r["slug"],)).fetchone()
            r["context_updated_at"] = (context["updated_at"] if context else "") or ""
            r["context_ready"] = bool(
                context and context["package_path"] and Path(context["package_path"]).is_file())
    known = {r["slug"] for r in rows}
    unregistered = []
    root = Path(CONFIG["projects"])
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and d.name not in known:
                unregistered.append(d.name)
    return {"projects": rows, "unregistered": unregistered}


@app.get("/api/projects/{slug}")
async def project_detail(slug: str):
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug non valido")
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        ports = [dict(p) for p in conn.execute(
            "SELECT id, port, description FROM ports WHERE project_slug=? ORDER BY port", (slug,))]
        docs = existing_document_rows(conn.execute(
            "SELECT * FROM documents WHERE project_slug=? ORDER BY created_at DESC", (slug,)))
    if not row:
        raise HTTPException(404, "progetto non registrato")
    return {"project": dict(row), "status": project_context(slug), "ports": ports,
            "documents": docs}


def register_project(slug: str, path: str, source: str, repo_url: str = "") -> dict:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO projects (slug,path,source,repo_url,local_url,created_at) "
            "VALUES (?,?,?,?,COALESCE((SELECT local_url FROM projects WHERE slug=?),''),?)",
            (slug, path, source, repo_url, slug, now()))
    return {"slug": slug, "path": path}


@app.post("/api/projects")
async def create_project(request: Request):
    body = await request.json()
    action = body.get("action")
    slug = (body.get("slug") or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug non valido: usa minuscole, cifre, punto, trattino")
    if action == "new":
        res = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "new", slug, timeout=300)
        return register_project(slug, res["path"], "new") | {"created": res.get("created", [])}
    if action == "clone":
        url = (body.get("repo_url") or "").strip()
        if not url:
            raise HTTPException(400, "URL repository mancante")
        key = (body.get("ssh_key") or "").strip()
        args = ["clone", slug, url] + ([key] if key else [])
        res = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, *args, timeout=1800)
        return register_project(slug, res["path"], "clone", url)
    if action == "register":
        res = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "adopt", slug, timeout=60)
        return register_project(slug, res["path"], "registered")
    if action == "init-instructions":
        res = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "init-instructions", slug, timeout=120)
        return {"slug": slug, "created": res.get("created", [])}
    if action == "deploykey":
        return wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "deploykey", slug, timeout=120)
    if action == "set-local-url":
        with db() as conn:
            conn.execute("UPDATE projects SET local_url=? WHERE slug=?",
                         ((body.get("local_url") or "").strip(), slug))
        return {"ok": True}
    if action == "add-port":
        try:
            port = int(body.get("port"))
        except (TypeError, ValueError):
            raise HTTPException(400, "porta non valida")
        if not 1024 <= port <= 65535:
            raise HTTPException(400, "porta fuori intervallo (1024-65535)")
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO ports (project_slug,port,description,created_at) "
                         "VALUES (?,?,?,?)", (slug, port, (body.get("description") or "").strip(), now()))
        return {"ok": True}
    if action == "remove-port":
        with db() as conn:
            conn.execute("DELETE FROM ports WHERE project_slug=? AND port=?", (slug, int(body.get("port", 0))))
        return {"ok": True}
    if action == "unregister":
        with db() as conn:
            conn.execute("DELETE FROM projects WHERE slug=?", (slug,))
        return {"ok": True}
    raise HTTPException(400, f"azione progetto non valida: {action}")


@app.post("/api/projects/{slug}/compose")
async def compose_action(slug: str, request: Request):
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug non valido")
    action = (await request.json()).get("action", "")
    return wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "compose", slug, action, timeout=1800)


@app.get("/api/ports")
async def ports_registry():
    with db() as conn:
        return {"ports": [dict(r) for r in conn.execute(
            "SELECT project_slug, port, description FROM ports ORDER BY port")]}


# ------------------------------------------------------- accounts / status
#
# Accounts e Status descrivono stato che vive fuori da questo processo: sudo
# verso i wrapper, docker, systemd, tailscale. Costruirlo dura secondi, e farlo
# dentro la richiesta significava una pagina vuota a ogni apertura e — poiche'
# subprocess.run blocca — l'intero servizio fermo per quei secondi.
#
# Ogni vista e' quindi uno `Snapshot`: l'ultimo risultato noto viene restituito
# sempre subito, con la sua data; quando e' piu' vecchio del TTL il ricalcolo
# parte in un thread e la risposta lo dichiara, cosi' la pagina mostra i dati
# vecchi e si aggiorna da sola quando i nuovi sono pronti. Solo la primissima
# richiesta, quando non c'e' ancora niente da mostrare, aspetta.

SNAPSHOT_TTL = 60


class Snapshot:
    """Risultato caro da costruire, servito subito e rinfrescato in background."""

    def __init__(self, builder, ttl: int = SNAPSHOT_TTL):
        self.builder, self.ttl = builder, ttl
        self.lock = threading.Lock()        # protegge i campi qui sotto
        self.build_lock = threading.Lock()  # una sola costruzione alla volta
        self.value: dict | None = None
        self.built_at = 0.0
        self.error = ""
        self.refreshing = False

    def _build(self, only_if_missing: bool = False) -> None:
        with self.build_lock:
            if only_if_missing and self.value is not None:
                return
            try:
                value, error = self.builder(), ""
            except Exception as exc:  # noqa: BLE001
                value, error = None, f"{type(exc).__name__}: {exc}"[:300]
            with self.lock:
                self.refreshing = False
                self.error = error
                if value is not None:
                    self.value, self.built_at = value, time.time()

    def invalidate(self) -> None:
        """Il prossimo lettore serve il valore vecchio e ne chiede uno nuovo."""
        with self.lock:
            self.built_at = 0.0

    def get(self) -> tuple[dict, dict]:
        with self.lock:
            value, built_at = self.value, self.built_at
            stale = value is None or time.time() - built_at >= self.ttl
            if stale and not self.refreshing and value is not None:
                self.refreshing = True
                threading.Thread(target=self._build, daemon=True).start()
            refreshing = self.refreshing
        if value is None:
            self._build(only_if_missing=True)
            with self.lock:
                value, built_at, refreshing = self.value, self.built_at, self.refreshing
            if value is None:
                raise HTTPException(503, f"stato non disponibile: {self.error}")
        age = max(0, int(time.time() - built_at))
        return value, {"generated_at": datetime.fromtimestamp(built_at, timezone.utc)
                                              .isoformat(timespec="seconds"),
                       "age_seconds": age, "stale": age >= self.ttl,
                       "refreshing": refreshing, "error": self.error}


def session_info(user: str) -> dict:
    """`session-ctl info` di un utente: la leggono sia Accounts sia Status."""
    r = wrapper(user, SESSION_CTL, "info", timeout=120)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"user": user, "error": (r.stderr or r.stdout).strip()[:2000]}


INFO_SNAPSHOTS = {u: Snapshot(lambda u=u: session_info(u)) for u in AGENT_UNIX_USERS}


def build_accounts() -> dict:
    out = {}
    for u in AGENT_UNIX_USERS:
        out[u] = INFO_SNAPSHOTS[u].get()[0]
        # Un login o una configurazione riusciti si vedono qui, al primo
        # ricalcolo dopo l'evento; il TTL del catalogo evita di richiedere il
        # catalogo al provider a ogni giro.
        refresh_model_catalog(u)
    return {"accounts": out, "profiles": load_profiles(),
            "catalog": model_catalog_payload()}


ACCOUNTS_SNAPSHOT = Snapshot(build_accounts)


@app.get("/api/accounts")
async def accounts(refresh: int = 0):
    if refresh:
        for snap in (*INFO_SNAPSHOTS.values(), ACCOUNTS_SNAPSHOT):
            snap.invalidate()
    data, meta = await asyncio.to_thread(ACCOUNTS_SNAPSHOT.get)
    return data | {"meta": meta}


@app.post("/api/accounts/models/refresh")
async def refresh_accounts_models(request: Request):
    body = await request.json()
    user = body.get("unix_user")
    provider = body.get("provider") or None
    if user not in AGENT_UNIX_USERS:
        raise HTTPException(400, "utente non valido")
    if provider is not None and provider not in CATALOG_PROVIDERS:
        raise HTTPException(400, "provider non valido")
    await asyncio.to_thread(refresh_model_catalog, user, provider, True)
    ACCOUNTS_SNAPSHOT.invalidate()
    return {"catalog": model_catalog_payload(user)[user]}


@app.post("/api/accounts/login")
async def accounts_login(request: Request):
    body = await request.json()
    user = body.get("unix_user")
    if user not in AGENT_UNIX_USERS:
        raise HTTPException(400, "utente non valido")
    prof = profile_by_id(body.get("profile_id", ""))
    if not prof.get("login"):
        raise HTTPException(400, "il profilo non definisce un flusso di login")
    sid = str(uuid.uuid4())
    tmux_name = f"agenthub-{sid}"
    r = wrapper(user, SESSION_CTL, "login", tmux_name, prof["id"], timeout=200)
    if r.returncode != 0:
        raise HTTPException(400, (r.stderr or r.stdout).strip() or "avvio login fallito")
    row = {
        "id": sid, "name": f"LOGIN {prof['label']} ({user})", "tmux_name": tmux_name,
        "project_slug": "", "workdir": f"/home/{user}", "environment":
            "SERVER" if user == SERVER_UNIX_USER else "PROJECT",
        "profile_id": prof["id"], "model": "", "effort": "", "harness_session_id": "",
        "executor_enabled": 0, "executor_model": "", "executor_max_agents": 0,
        "permission_mode": "n/a", "unix_user": user,
        "kind": "login", "status": "running", "initial_prompt": "", "parent_session": "",
        "cols": 120, "rows": 34, "created_at": now(), "ended_at": "", "auto_trust": 1,
    }
    # Il pane tmux esiste gia': se la riga non entra nel registro il login
    # resterebbe vivo e invisibile all'Hub, impossibile da chiudere dalla UI e
    # rilevabile solo dal controllo di coerenza. Si annulla quindi il pane e si
    # riporta l'errore, per non lasciare mai un pane senza la sua riga.
    try:
        insert_session(row)
    except Exception as exc:
        wrapper(user, SESSION_CTL, "kill", tmux_name, timeout=60)
        raise HTTPException(500, f"registrazione del login fallita: {exc}") from exc
    INFO_SNAPSHOTS[user].invalidate()
    ACCOUNTS_SNAPSHOT.invalidate()
    return {"session": row, "note": prof["login"].get("note", "")}


def sh(cmd: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return f"non disponibile: {exc}"


def harness_versions() -> dict:
    """Versioni delle CLI viste dagli utenti che le eseguono davvero.

    Claude Code sta in ~/.local/bin di devagent/hostagent, home a 0700: il
    servizio, che gira come l'account di servizio, non le vede. Le chiede al wrapper.
    """
    for user in (SERVER_UNIX_USER, PROJECT_UNIX_USER):
        r = wrapper(user, SESSION_CTL, "version", timeout=60)
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        return {k: f"{v} ({user})" for k, v in data.items() if k != "user" and v}
    return {}


STATUS_DIRS = ["/srv/agent-workspace/projects", "/srv/agent-workspace/data",
               "/srv/agent-workspace/deployments", "/srv/agent-workspace/logs",
               "/srv/agent-workspace/uploads", "/srv/agent-workspace/knowledge",
               "/srv/agent-workspace/templates",
               "/opt/agent-hub", "/etc/agent-hub", "/var/lib/agent-hub"]


def tmux_unit_status(user: str) -> dict:
    r = wrapper(user, SESSION_CTL, "server", "status", timeout=30)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": (r.stderr or r.stdout).strip()[:500]}


def build_status() -> dict:
    """Una trentina di comandi indipendenti: eseguirli in fila non serve."""
    live = sync_status()
    per_user = {u: sorted(n for n, i in live.items() if i["unix_user"] == u)
                for u in AGENT_UNIX_USERS}
    cmds = {
        "service": ["systemctl", "is-active", "agent-hub.service"],
        "service_detail": ["systemctl", "show", "agent-hub.service", "-p",
                           "ActiveState,SubState,ExecMainStartTimestamp,NRestarts,KillMode"],
        "tailscale": ["tailscale", "status", "--peers=false"],
        "tailscale_serve": ["tailscale", "serve", "status"],
        "disk": ["df", "-h", "/", "/srv"],
    }
    tools = {"tmux": ["tmux", "-V"], "git": ["git", "--version"],
             "docker": ["docker", "--version"], "python": ["python3", "--version"],
             "node": ["node", "--version"]}
    with ThreadPoolExecutor(max_workers=12) as pool:
        out = {k: pool.submit(sh, cmd) for k, cmd in cmds.items()}
        tool_jobs = {k: pool.submit(sh, cmd) for k, cmd in tools.items()}
        dir_jobs = {p: pool.submit(sh, ["ls", "-ld", p]) for p in STATUS_DIRS}
        unit_jobs = {u: pool.submit(tmux_unit_status, u) for u in AGENT_UNIX_USERS}
        harness = pool.submit(harness_versions)
        result = {k: f.result() for k, f in out.items()}
        result["versions"] = {**harness.result(), **{k: f.result() for k, f in tool_jobs.items()}}
        result["dirs"] = {p: f.result() for p, f in dir_jobs.items()}
        result["tmux_units"] = {u: f.result() for u, f in unit_jobs.items()}
    result["tmux_sessions"] = per_user
    return result


STATUS_SNAPSHOT = Snapshot(build_status)


@app.get("/api/status")
async def status():
    data, meta = await asyncio.to_thread(STATUS_SNAPSHOT.get)
    return data | {"meta": meta}


@app.get("/api/usage")
async def usage():
    """Solo lettura del database: i numeri li porta il giro periodico."""
    items = usage_overview()
    checked = [i["last_checked"] for i in items if i["last_checked"]]
    return {"usage": items, "meta": {"generated_at": min(checked) if checked else "",
                                     "next_in_seconds": USAGE_TTL}}


@app.post("/api/usage/refresh")
async def refresh_usage_now():
    """Rilettura immediata di tutti i provider, per entrambi gli utenti Unix."""
    for user in AGENT_UNIX_USERS:
        await asyncio.to_thread(refresh_usage, user, None, True)
    return {"usage": usage_overview()}


# ------------------------------------------------- controllo di salute host
#
# Il controllo vero sta in /usr/local/libexec/agent-hub/health-ctl, root-owned.
# Il servizio web (che gira con il proprio account) legge l'ultimo risultato salvato dal
# timer e puo' chiederne uno nuovo: nessuna logica di monitoraggio qui dentro.

HEALTH_TIMEOUT = 300


def health_last() -> dict:
    path = Path(CONFIG["health_dir"]) / "last.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def health_history(limit: int = 50) -> list[dict]:
    """Solo i cambi di stato: il file e' scritto dal controllo, non da qui."""
    path = Path(CONFIG["health_dir"]) / "history.jsonl"
    try:
        lines = path.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return list(reversed(out))


@app.get("/api/health")
async def health(history: int = 20):
    last = health_last()
    timer = sh(["systemctl", "show", "agent-hub-health.timer", "-p",
                "ActiveState,LastTriggerUSec,NextElapseUSecRealtime"])
    return {"last": last, "history": health_history(max(0, min(200, history))),
            "timer": timer,
            "timer_active": sh(["systemctl", "is-active", "agent-hub-health.timer"]),
            "controller": controller_config()}


@app.post("/api/health/run")
async def health_run():
    """Esecuzione manuale dalla pagina Status: stesso codice del timer."""
    try:
        r = subprocess.run(["sudo", "-n", HEALTH_CTL, "--json", "--save"],
                           capture_output=True, text=True, timeout=HEALTH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HTTPException(400, f"il controllo non è terminato entro {HEALTH_TIMEOUT}s")
    except OSError as exc:
        raise HTTPException(400, f"controllo non eseguibile: {exc}")
    if r.returncode == 3 or not (r.stdout or "").strip():
        raise HTTPException(400, (r.stderr or r.stdout).strip()[:2000] or
                            "il controllo non ha prodotto risultati")
    try:
        return {"last": json.loads(r.stdout), "history": health_history(20)}
    except json.JSONDecodeError:
        raise HTTPException(400, "output del controllo non interpretabile: "
                                 + (r.stdout or "")[:500])


def build_docker() -> dict:
    # stessa `session-ctl info` che serve ad Accounts: una sola lettura per
    # utente, condivisa, invece di due chiamate sudo identiche per pagina
    dev = INFO_SNAPSHOTS[PROJECT_UNIX_USER].get()[0]
    host = INFO_SNAPSHOTS[SERVER_UNIX_USER].get()[0]
    try:
        containers = wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "docker-status", timeout=60).get("output", "")
    except HTTPException as exc:
        containers = str(exc.detail)
    return {
        "project_docker_rootless": {"user": PROJECT_UNIX_USER, "info": dev.get("docker", ""),
                                    "containers": containers},
        "server_docker_privileged": {"user": SERVER_UNIX_USER, "info": host.get("docker", "")},
    }


DOCKER_SNAPSHOT = Snapshot(build_docker)


@app.get("/api/status/docker")
async def status_docker():
    data, meta = await asyncio.to_thread(DOCKER_SNAPSHOT.get)
    return data | {"meta": meta}


# -------------------------------------------------------------- WebSocket

@app.websocket("/ws/session/{sid}")
async def ws_session(ws: WebSocket, sid: str):
    origin = ws.headers.get("origin")
    expected = CONFIG["origin"]
    if expected and origin and origin.rstrip("/") != expected:
        await ws.close(code=4403)
        return
    login = ws.headers.get("tailscale-user-login", "")
    if CONFIG["require_tailscale"] and not login:
        await ws.close(code=4403)
        return
    if login and CONFIG["allowed_users"] and login not in CONFIG["allowed_users"]:
        await ws.close(code=4403)
        return
    try:
        row = get_session(sid)
    except HTTPException:
        await ws.close(code=4404)
        return

    await ws.accept()
    pid, fd = pty.fork()
    if pid == 0:  # figlio: diventa il client tmux
        os.execvp("sudo", ["sudo", "-n", "-u", row["unix_user"], SESSION_CTL,
                           "attach", row["tmux_name"]])
        os._exit(1)

    loop = asyncio.get_running_loop()
    os.set_blocking(fd, False)
    set_winsize(fd, row["rows"], row["cols"])
    closed = asyncio.Event()

    def on_readable():
        try:
            data = os.read(fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            loop.call_soon_threadsafe(closed.set)
            return
        if not data:
            loop.call_soon_threadsafe(closed.set)
            return
        asyncio.ensure_future(safe_send(ws, data))

    async def safe_send(sock: WebSocket, data: bytes):
        try:
            await sock.send_bytes(data)
        except Exception:  # noqa: BLE001
            closed.set()

    loop.add_reader(fd, on_readable)

    async def reader_ws():
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    os.write(fd, msg["bytes"])
                elif msg.get("text") is not None:
                    txt = msg["text"]
                    if txt.startswith("{"):
                        try:
                            obj = json.loads(txt)
                        except json.JSONDecodeError:
                            obj = None
                        if isinstance(obj, dict) and obj.get("type") == "resize":
                            c = max(40, min(400, int(obj.get("cols", 100))))
                            r = max(10, min(200, int(obj.get("rows", 30))))
                            set_winsize(fd, r, c)
                            wrapper(row["unix_user"], SESSION_CTL, "resize",
                                    row["tmux_name"], str(c), str(r), timeout=20)
                            with db() as conn:
                                conn.execute("UPDATE sessions SET cols=?, rows=? WHERE id=?",
                                             (c, r, sid))
                            continue
                    os.write(fd, txt.encode())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            closed.set()

    task = asyncio.ensure_future(reader_ws())
    try:
        await closed.wait()
    finally:
        loop.remove_reader(fd)
        task.cancel()
        # stacca il client tmux: la sessione agente resta viva
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


# ------------------------------------------------------ meetings helpers

def _meetings_root(slug: str) -> Path:
    return Path(CONFIG["knowledge"]) / "projects" / slug / "meetings"


def _context_pkg_dir(slug: str) -> Path:
    return Path(CONFIG["knowledge"]) / "projects" / slug / "context-packages"


def get_meeting(mid: str) -> dict:
    with db() as conn:
        r = conn.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404, "riunione inesistente")
    return dict(r)


def _decode_proposal(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _approval_count(meeting_id: str, round_num: int) -> int:
    with db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) FROM meeting_approvals "
            "WHERE meeting_id=? AND round=? AND decision='approved'",
            (meeting_id, round_num)).fetchone()
    return r[0] if r else 0


def _mask_chat(chat_id: str) -> str:
    if not chat_id:
        return "?"
    s = str(chat_id)
    if len(s) <= 4:
        return s
    return s[:2] + "…" + s[-2:]


def _meeting_transcript(mid: str) -> str:
    m = get_meeting(mid)
    tp = m.get("transcript_path") or ""
    if tp and os.path.isfile(tp):
        try:
            return Path(tp).read_text()
        except OSError:
            return ""
    return ""


# -------------------------------------------------- meetings API routes

@app.get("/api/meetings")
async def list_meetings(project: str = ""):
    with db() as conn:
        if project:
            if not SLUG_RE.match(project):
                raise HTTPException(400, "slug progetto non valido")
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM meetings WHERE project_slug=? ORDER BY created_at DESC", (project,))]
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM meetings ORDER BY created_at DESC")]
    out = []
    for m in rows:
        m["proposal"] = _decode_proposal(m.get("proposal_json") or "")
        m["approval_count"] = _approval_count(m["id"], m.get("round") or 0)
        m.pop("proposal_json", None)
        m.pop("operational_prompt", None)
        m.pop("context_on_approval", None)
        out.append(m)
    return {"meetings": out}


@app.post("/api/meetings")
async def create_meeting(request: Request):
    form = await request.form()
    slug = (form.get("project_slug") or "").strip()
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug progetto non valido")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
            raise HTTPException(400, f"progetto non registrato: {slug}")
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "titolo mancante")
    meeting_date = (form.get("meeting_date") or "").strip()
    if not meeting_date:
        raise HTTPException(400, "data riunione mancante")
    profile_id = (form.get("profile_id") or "").strip()
    if not profile_id:
        raise HTTPException(400, "profilo PROJECT mancante")
    prof = profile_by_id(profile_id)
    if PROJECT_UNIX_USER not in prof.get("allowed_users", []):
        raise HTTPException(400, "il profilo scelto non e' disponibile per PROJECT")
    model = (form.get("model") or "").strip()
    effort = validate_effort(prof, (form.get("effort") or "").strip())
    operational_prompt = (form.get("operational_prompt") or "").strip()
    context_on_approval = (form.get("context_on_approval") or "").strip()
    # audio file
    audio_files = [v for _k, v in form.multi_items() if hasattr(v, "filename") and v.filename]
    if not audio_files:
        raise HTTPException(400, "file audio mancante")
    up = audio_files[0]
    ext = os.path.splitext((up.filename or "").lower() or ".tmp")[1]
    if ext not in MEETING_AUDIO_EXT:
        raise HTTPException(400, f"estensione audio non ammessa: {ext} — "
                                 f"ammesse: {', '.join(sorted(MEETING_AUDIO_EXT))}")
    mid = str(uuid.uuid4())
    meeting_dir = _meetings_root(slug) / mid
    meeting_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(meeting_dir, 0o2775)
    except OSError:
        pass
    audio_name = (up.filename or "audio")[:200]
    audio_dest = meeting_dir / f"audio{ext}"
    size = 0
    try:
        with open(audio_dest, "wb") as out:
            while True:
                chunk = await up.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MEETING_AUDIO_MAX:
                    shutil.rmtree(str(meeting_dir), ignore_errors=True)
                    raise HTTPException(400, f"audio troppo grande: limite "
                                              f"{MEETING_AUDIO_MAX // (1024 * 1024)} MiB")
                out.write(chunk)
    except HTTPException:
        raise
    finally:
        await up.close()
    try:
        os.chmod(audio_dest, 0o664)
    except OSError:
        pass
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO meetings (id,project_slug,title,meeting_date,audio_path,audio_name,"
            "audio_size,status,round,profile_id,model,effort,operational_prompt,"
            "context_on_approval,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, slug, title[:500], meeting_date, str(audio_dest), audio_name, size,
             "queued", 1, profile_id, model, effort, operational_prompt[:20000],
             1 if context_on_approval.lower() in ("1", "true", "yes", "on") else 0,
             ts, ts))
    return {"meeting": {"id": mid, "project_slug": slug, "title": title,
                        "meeting_date": meeting_date, "status": "queued",
                        "audio_name": audio_name, "audio_size": size,
                        "created_at": ts}}


@app.get("/api/meetings/{mid}")
async def meeting_detail(mid: str):
    m = get_meeting(mid)
    m["proposal"] = _decode_proposal(m.get("proposal_json") or "")
    m["approval_count"] = _approval_count(m["id"], m.get("round") or 1)
    m.pop("proposal_json", None)
    m.pop("operational_prompt", None)
    m.pop("context_on_approval", None)
    with db() as conn:
        approvals = [dict(r) for r in conn.execute(
            "SELECT * FROM meeting_approvals WHERE meeting_id=? ORDER BY created_at", (mid,))]
    for a in approvals:
        a["chat_label"] = _mask_chat(a.get("chat_id", ""))
        a.pop("chat_id", None)
    transcript = _meeting_transcript(mid)
    return {"meeting": m, "approvals": approvals, "transcript": transcript}


@app.post("/api/meetings/{mid}/retry")
async def retry_meeting(mid: str):
    m = get_meeting(mid)
    if not m.get("transcript_path"):
        # nessuna trascrizione → rimetti in coda per ri-tascrivere
        with db() as conn:
            conn.execute(
                "UPDATE meetings SET status='queued',error='',updated_at=? WHERE id=?",
                (now(), mid))
        return {"ok": True, "action": "requeued"}
    # ha gia' trascrizione → avvia nuova analisi
    with db() as conn:
        conn.execute(
            "UPDATE meetings SET status='queued',error='',updated_at=? WHERE id=?",
            (now(), mid))
    return {"ok": True, "action": "reanalyze"}


# ----------------------------------------------- project context API

@app.get("/api/projects/{slug}/context")
async def project_context_get(slug: str):
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug progetto non valido")
    with db() as conn:
        r = conn.execute(
            "SELECT * FROM project_contexts WHERE project_slug=?", (slug,)).fetchone()
    ctx = dict(r) if r else {}
    pkg_ready = bool(ctx.get("package_path") and os.path.isfile(ctx["package_path"]))
    return {"context": ctx, "package_ready": pkg_ready}


@app.post("/api/projects/{slug}/context")
async def project_context_create(slug: str, request: Request):
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug progetto non valido")
    body = await request.json() if await request.body() else {}
    target_arch = (body.get("target_architecture") or "").strip()
    if not target_arch:
        with db() as conn:
            current = conn.execute(
                "SELECT target_architecture FROM project_contexts WHERE project_slug=?",
                (slug,)).fetchone()
        target_arch = (current["target_architecture"] if current else "") or ""
    result = _build_context_zip(slug, target_arch)
    return result


@app.get("/api/projects/{slug}/context/download")
async def project_context_download(slug: str):
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug progetto non valido")
    with db() as conn:
        r = conn.execute(
            "SELECT package_path FROM project_contexts WHERE project_slug=?", (slug,)).fetchone()
    if not r or not r["package_path"]:
        raise HTTPException(404, "nessun pacchetto di contesto disponibile")
    path = r["package_path"]
    if not os.path.isfile(path):
        raise HTTPException(404, "file pacchetto non piu' presente")
    return FileResponse(path, media_type="application/zip",
                        filename=f"{slug}-context.zip",
                        headers={"X-Content-Type-Options": "nosniff",
                                 "Cache-Control": "no-store"})


# --------------------------------------------------------- ZIP context

_CONTEXT_BUILD_LOCK = threading.Lock()


def _build_context_zip(slug: str, target_architecture: str = "") -> dict:
    with _CONTEXT_BUILD_LOCK:
        return _build_context_zip_locked(slug, target_architecture)


def _build_context_zip_locked(slug: str, target_architecture: str = "") -> dict:
    """Genera ZIP sicuro del contesto progetto e salva metadati in project_contexts."""
    import zipfile
    project_dir = Path(CONFIG["projects"]) / slug
    with db() as conn:
        registered = conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone()
    if not registered or not project_dir.is_dir():
        raise HTTPException(400, f"directory progetto non trovata: {project_dir}")
    pkg_dir = _context_pkg_dir(slug)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(pkg_dir, 0o2775)
    except OSError:
        pass
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    pkg_name = f"{slug}-{ts_str}.zip"
    pkg_tmp = pkg_dir / f".{pkg_name}.tmp"
    pkg_final = pkg_dir / pkg_name
    sha = hashlib.sha256()
    included, skipped, total_bytes = 0, 0, 0
    manifest_files: list[dict] = []
    manifest_skipped: list[str] = []
    manifest_redacted: list[str] = []
    # raccogli meeting approvati per il progetto
    with db() as conn:
        meetings_approved = [dict(r) for r in conn.execute(
            "SELECT m.* FROM meetings m WHERE m.project_slug=? AND "
            "(SELECT COUNT(*) FROM meeting_approvals a WHERE a.meeting_id=m.id "
            "AND a.round=m.round AND a.decision='approved') >= 2 "
            "ORDER BY m.created_at DESC", (slug,))]
    try:
        with zipfile.ZipFile(pkg_tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            # CONTEXT.md
            context_md = _build_context_md(slug, target_architecture, meetings_approved)
            zf.writestr("CONTEXT.md", context_md)
            included += 1
            zf.writestr("LLM_BRIEF.md", _context_llm_brief(slug))
            included += 1
            # MANIFEST.json (scritto alla fine)
            # project/ subtree. os.walk top-down permette di potare .git,
            # node_modules, build e cache prima di visitarli: rglob li avrebbe
            # enumerati per intero prima che il filtro potesse intervenire.
            candidates = []
            for walk_root, dirnames, filenames in os.walk(project_dir, topdown=True,
                                                            followlinks=False):
                root_path = Path(walk_root)
                keep_dirs = []
                for dirname in sorted(dirnames):
                    dir_path = root_path / dirname
                    dir_rel = str(dir_path.relative_to(project_dir))
                    if _should_exclude_context(dir_path, dir_rel):
                        manifest_skipped.append(dir_rel + "/")
                        skipped += 1
                    else:
                        keep_dirs.append(dirname)
                dirnames[:] = keep_dirs
                candidates.extend(root_path / name for name in sorted(filenames))
            for file_path in candidates:
                rel = str(file_path.relative_to(project_dir))
                if _should_exclude_context(file_path, rel):
                    manifest_skipped.append(rel)
                    skipped += 1
                    continue
                if not file_path.is_file():
                    continue
                try:
                    fsize = file_path.stat().st_size
                except OSError:
                    manifest_skipped.append(rel)
                    skipped += 1
                    continue
                if fsize > CTX_MAX_FILE_BYTES:
                    manifest_skipped.append(f"{rel} (troppo grande: {fsize})")
                    skipped += 1
                    continue
                if total_bytes + fsize > CTX_MAX_TOTAL_BYTES:
                    manifest_skipped.append(f"{rel} (superato limite totale)")
                    skipped += 1
                    continue
                if included >= CTX_MAX_FILES:
                    manifest_skipped.append(f"{rel} (limite file raggiunto)")
                    skipped += 1
                    continue
                try:
                    data = file_path.read_bytes()
                except OSError:
                    manifest_skipped.append(rel)
                    skipped += 1
                    continue
                if b"\x00" in data[:8192]:
                    manifest_skipped.append(f"{rel} (binario)")
                    skipped += 1
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    manifest_skipped.append(f"{rel} (non testuale)")
                    skipped += 1
                    continue
                if "-----BEGIN PRIVATE KEY-----" in text or "-----BEGIN OPENSSH PRIVATE KEY-----" in text:
                    manifest_skipped.append(f"{rel} (chiave privata)")
                    skipped += 1
                    continue
                text, redacted = _redact_context_secrets(text)
                if redacted:
                    data = text.encode("utf-8")
                    fsize = len(data)
                    manifest_redacted.append(rel)
                arcname = f"project/{rel}"
                zf.writestr(arcname, data)
                sha.update(data)
                total_bytes += fsize
                included += 1
                manifest_files.append({"path": arcname, "size": fsize})
            # MANIFEST.json
            manifest = {
                "project_slug": slug, "generated_at": now(),
                "target_architecture": target_architecture,
                "included_files": manifest_files,
                "included_count": len(manifest_files),
                "skipped": manifest_skipped, "skipped_count": skipped,
                "redacted_files": manifest_redacted,
                "total_size": total_bytes,
            }
            zf.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        # scrittura atomica
        os.replace(pkg_tmp, pkg_final)
        try:
            os.chmod(pkg_final, 0o664)
        except OSError:
            pass
    except Exception:
        try:
            pkg_tmp.unlink()
        except OSError:
            pass
        raise HTTPException(500, "generazione pacchetto contesto fallita")
    pkg_size = pkg_final.stat().st_size
    sha_hex = hashlib.sha256(pkg_final.read_bytes()).hexdigest()
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_contexts "
            "(project_slug,target_architecture,package_path,package_sha256,package_size,"
            "file_count,skipped_count,updated_at,trigger) VALUES (?,?,?,?,?,?,?,?,?)",
            (slug, target_architecture, str(pkg_final), sha_hex, pkg_size,
             included, skipped, now(), "manual"))
    return {"package_path": str(pkg_final), "package_sha256": sha_hex,
            "package_size": pkg_size, "file_count": included,
            "skipped_count": skipped}


_SENSITIVE_RE = re.compile(
    r"(?i)(^|[._-])(secret|secrets|token|tokens|password|passwd|credential|credentials|"
    r"private[_-]?key|api[_-]?key)([._-]|$)")
_CONTEXT_SECRET_VALUE_RE = re.compile(
    r"(?im)((?:[\"']?)(?:api[_-]?key|secret|token|password|passwd|credential)"
    r"(?:[\"']?)\s*[:=]\s*)([\"'])(.*?)(\2)")
_CONTEXT_SECRET_BARE_RE = re.compile(
    r"(?im)^(\s*(?:api[_-]?key|secret|token|password|passwd|credential)\s*[:=]\s*)"
    r"([^\s#\"']{6,})")
_CONTEXT_TOKEN_RE = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"[0-9]{7,12}:[A-Za-z0-9_-]{25,})\b")

_CTX_EXCLUDE_DIR_NAMES = {".git", ".venv", "venv", "node_modules", "build", "dist",
                           "target", "cache", "__pycache__", ".pytest_cache",
                           ".mypy_cache", "backups"}


def _redact_context_secrets(text: str) -> tuple[str, bool]:
    original = text
    text = _CONTEXT_SECRET_VALUE_RE.sub(r"\1\2<REDACTED>\4", text)
    text = _CONTEXT_SECRET_BARE_RE.sub(r"\1<REDACTED>", text)
    text = _CONTEXT_TOKEN_RE.sub("<REDACTED>", text)
    return text, text != original


def _should_exclude_context(file_path: Path, rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    for p in parts:
        if p in _CTX_EXCLUDE_DIR_NAMES:
            return True
    name = file_path.name.lower()
    # escludi .env tranne example/sample
    if name == ".env" or name.endswith(".env"):
        parent_dir = file_path.parent.name.lower()
        if "example" not in parent_dir and "sample" not in parent_dir:
            return True
    # pattern sicurezza
    for pat in CTX_EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(name.lower(), pat.lower()):
            return True
    # file con nome sensibile
    if _SENSITIVE_RE.search(file_path.name):
        return True
    # saltare symlink
    if file_path.is_symlink():
        return True
    # saltare binary (primo approccio: estensioni note)
    binary_exts = {".bin", ".exe", ".dll", ".so", ".o", ".a", ".pyc", ".pyo",
                   ".class", ".db", ".sqlite", ".sqlite3"}
    if file_path.suffix.lower() in binary_exts:
        return True
    # saltare log e database
    log_db_paths = {"database", "log", "logs", "backups", "backup"}
    for p in parts:
        if p.lower() in log_db_paths:
            return True
    return False


def _build_context_md(slug: str, target_architecture: str,
                      meetings_approved: list[dict]) -> str:
    project_dir = Path(CONFIG["projects"]) / slug
    lines = [f"# Contesto progetto: {slug}", "",
             f"Generato: {now()}", "",
             "## Target architecture"]
    if target_architecture:
        lines.append(target_architecture)
    else:
        lines.append("(nessuna architettura target specificata)")
    lines.append("")
    # git status
    try:
        r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True,
                           timeout=30, cwd=str(project_dir))
        short = r.stdout.strip() or "(pulito o non un repo git)"
    except Exception:
        short = "(non disponibile)"
    lines.extend(["## Git status --short", "```", short, "```", ""])
    # AGENTS.md
    agents_path = project_dir / "AGENTS.md"
    if agents_path.is_file():
        try:
            content = agents_path.read_text()
            lines.extend(["## AGENTS.md", "```", content[:5000], "```", ""])
        except OSError:
            lines.append("## AGENTS.md (non leggibile)")
            lines.append("")
    # HANDOFF.md
    handoff_path = project_dir / ".agent" / "HANDOFF.md"
    if handoff_path.is_file():
        try:
            content = handoff_path.read_text()
            lines.extend(["## .agent/HANDOFF.md", "```", content[:5000], "```", ""])
        except OSError:
            lines.append("## .agent/HANDOFF.md (non leggibile)")
            lines.append("")
    # meetings approvati
    if meetings_approved:
        lines.extend(["## Riunioni approvate", ""])
        for m in meetings_approved[:10]:
            prop = _decode_proposal(m.get("proposal_json") or "")
            lines.append(f"- **{m['title']}** ({m['meeting_date']})")
            if prop.get("summary"):
                lines.append(f"  Riepilogo: {str(prop['summary'])[:300]}")
            for decision in prop.get("decisions", [])[:20]:
                if isinstance(decision, dict):
                    lines.append(f"  Decisione: {decision.get('title', '')} — "
                                 f"{decision.get('detail', '')}")
            for question in prop.get("open_questions", [])[:20]:
                if isinstance(question, dict):
                    lines.append(f"  Questione aperta: {question.get('title', '')} — "
                                 f"{question.get('detail', '')}")
            for action in prop.get("actions", [])[:20]:
                if isinstance(action, dict):
                    lines.append(f"  Azione: {action.get('title', '')} — "
                                 f"{action.get('detail', '')}")
            lines.append(f"  Round: {m.get('round', 0)}")
            lines.append("")
    lines.extend([
        "## Istruzioni per l'LLM",
        "",
        "Segui il processo in `LLM_BRIEF.md`: prima discuti e rivedi il target; ",
        "solo quando l'utente lo dichiara accettato produci la roadmap operativa.", "",
        "I sorgenti filtrati del progetto sono nella directory `project/`.", "",
    ])
    return "\n".join(lines)


def _context_llm_brief(slug: str) -> str:
    return f"""# Brief per la sessione LLM — {slug}

Usa `CONTEXT.md`, `MANIFEST.json` e i file sotto `project/` come unica fonte
del contesto corrente. Distingui sempre fatti osservati, inferenze e proposte;
quando descrivi il codice cita i percorsi rilevanti.

## Fase 1 — revisione del target

Discuti criticamente l'architettura target: verifica coerenza con lo stato
attuale, requisiti, vincoli, rischi, migrazioni, sicurezza e operabilita'.
Evidenzia lacune e domande aperte. Non produrre ancora la roadmap definitiva.

## Fase 2 — solo dopo accettazione esplicita

Quando l'utente dichiara esplicitamente che il target e' accettato, produci una
roadmap operativa ordinata per dipendenze. Includi fasi, work package concreti,
file/componenti coinvolti, criteri di accettazione, test, migrazione dati,
rollout, osservabilita', rischi e rollback. Mantieni separati passi applicativi
e passi host/infrastruttura e segnala le decisioni ancora necessarie.
"""


# ------------------------------------------------- meeting worker daemon

_MEETING_WORKER_TICK = 10
_MEETING_ACTION_TICK = 10
# Nei primi secondi dopo l'avvio la liveness di una sessione puo' oscillare (il
# poller elenca le sessioni tmux mentre l'harness sta ancora partendo), e il
# lifecycle scivola per un attimo su ENDED_UNREPORTED. Senza questa attesa la
# riunione veniva dichiarata fallita mentre l'analisi girava davvero.
_MEETING_SESSION_GRACE = 180
_WHISPER_MODEL = None
_WHISPER_MODEL_LOCK = threading.Lock()


def _recover_meetings() -> None:
    """All'avvio: transcribing→queued, action running→pending. Non duplicare sessioni."""
    try:
        with db() as conn:
            conn.execute(
                "UPDATE meetings SET status='queued', error='backend riavviato durante la trascrizione' "
                "WHERE status='transcribing'")
            conn.execute(
                "UPDATE meeting_actions SET status='pending', error='backend riavviato', "
                "processed_at='' WHERE status='running'")
            # non duplicare sessioni per analyzing/revising: gia' attive
    except sqlite3.Error:
        pass


def _session_outcome_is_settled(row) -> bool:
    """Vero solo per una sessione davvero finita, non per un lampo di liveness.

    Un `report_status` e' una dichiarazione dell'agente e vale subito. Un
    lifecycle dedotto (CRASHED/ENDED_UNREPORTED) vale invece solo se la
    sessione non e' tornata viva e ha superato il periodo di grazia iniziale.
    """
    if (row["report_status"] or "") in ("COMPLETED", "FAILED", "CANCELLED"):
        return True
    if (row["session_status"] or "") == "running":
        return False
    try:
        started = datetime.fromisoformat(row["session_created_at"])
    except (TypeError, ValueError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() >= _MEETING_SESSION_GRACE


def _doc_perimeter_violations(slug: str, baseline: str) -> list[str]:
    """Percorsi fuori perimetro toccati dalla sessione post-riunione.

    Il prompt dichiara il confine, questa funzione lo verifica: senza controllo
    resterebbe un'istruzione, non una garanzia. Se il repository non e'
    interrogabile non si inventa una violazione — si lascia passare e lo si
    dice nel log, perche' bloccare su un dato mancante e' peggio.
    """
    state = _repo_state(slug, baseline)
    if not state:
        return []
    return [p for p in (state.get("changed") or []) if not meeting_doc_path_allowed(p)]


def _settle_doc_sessions() -> None:
    """Chiude le riunioni la cui sessione documentale ha dichiarato COMPLETED.

    La verifica del perimetro gira fuori dalla transazione: interroga git in un
    sottoprocesso e non deve tenere aperta una scrittura sul database.
    """
    with db() as conn:
        candidates = [dict(r) for r in conn.execute(
            "SELECT m.id,m.project_slug,m.doc_baseline FROM meetings m "
            "JOIN sessions s ON s.id=m.implementation_session_id "
            "WHERE m.status='implementing' AND s.report_status='COMPLETED'")]
    for row in candidates:
        violations = _doc_perimeter_violations(
            row["project_slug"], row.get("doc_baseline") or "")
        with db() as conn:
            if violations:
                shown = ", ".join(violations[:10])
                extra = f" e altri {len(violations) - 10}" if len(violations) > 10 else ""
                conn.execute(
                    "UPDATE meetings SET status='failed',error=?,updated_at=? WHERE id=?",
                    (f"la sessione ha modificato file fuori dalla documentazione: "
                     f"{shown}{extra}", now(), row["id"]))
            else:
                conn.execute(
                    "UPDATE meetings SET status='completed',updated_at=? WHERE id=?",
                    (now(), row["id"]))


def _sync_meeting_sessions() -> None:
    """Propaga alle riunioni soltanto gli esiti dichiarati delle sessioni collegate."""
    _settle_doc_sessions()
    with db() as conn:
        impl_failed = conn.execute(
            "SELECT m.id,COALESCE(NULLIF(s.report_status,''),s.lifecycle) AS outcome,"
            "s.report_status,s.status AS session_status,s.created_at AS session_created_at "
            "FROM meetings m JOIN sessions s ON s.id=m.implementation_session_id "
            "WHERE m.status='implementing' AND (s.report_status IN ('FAILED','CANCELLED') "
            "OR s.lifecycle IN ('CRASHED','ENDED_UNREPORTED'))").fetchall()
        for row in impl_failed:
            if not _session_outcome_is_settled(row):
                continue
            conn.execute(
                "UPDATE meetings SET status='failed',error=?,updated_at=? WHERE id=?",
                (f"sessione operativa conclusa: {row['outcome']}", now(), row["id"]))
        failed = conn.execute(
            "SELECT m.id,s.report_status,s.lifecycle,s.status AS session_status,"
            "s.created_at AS session_created_at FROM meetings m JOIN sessions s "
            "ON s.id=m.analysis_session_id WHERE m.status IN ('analyzing','revising') "
            "AND (s.report_status IN ('COMPLETED','FAILED','CANCELLED') "
            "OR s.lifecycle IN ('CRASHED','ENDED_UNREPORTED'))").fetchall()
        for row in failed:
            if not _session_outcome_is_settled(row):
                continue
            # COMPLETED senza agent-meeting-report significa che non esiste una
            # proposta da approvare; non lasciamo la riunione bloccata in eterno.
            outcome = row["report_status"] or row["lifecycle"]
            detail = ("sessione conclusa senza proposta strutturata"
                      if outcome == "COMPLETED"
                      else f"sessione di analisi conclusa: {outcome}")
            conn.execute(
                "UPDATE meetings SET status='failed',error=?,updated_at=? WHERE id=?",
                (detail, now(), row["id"]))


def _meeting_worker() -> None:
    """Ciclo principale: recupera meeting queued, trascrive, avvia analisi."""
    _recover_meetings()
    while True:
        try:
            _sync_meeting_sessions()
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM meetings WHERE status='queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
            if row:
                _process_meeting(dict(row))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_MEETING_WORKER_TICK)


def _action_worker() -> None:
    """Ciclo: consuma meeting_actions pending."""
    while True:
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM meeting_actions WHERE status='pending' "
                    "ORDER BY created_at LIMIT 1").fetchone()
            if row:
                _process_action(dict(row))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_MEETING_ACTION_TICK)


def _process_meeting(m: dict) -> None:
    """Trascrivi e avvia analisi per un meeting queued."""
    mid = m["id"]
    transcript_path = m.get("transcript_path") or ""
    if transcript_path and os.path.isfile(transcript_path):
        _start_analysis_session(m)
        return
    # 1. marcare transcribing
    with db() as conn:
        conn.execute(
            "UPDATE meetings SET status='transcribing', updated_at=? WHERE id=? AND status='queued'",
            (now(), mid))
    if not _transcribe_audio(m):
        return
    # 2. marcare transcribed
    with db() as conn:
        conn.execute(
            "UPDATE meetings SET status='transcribed', updated_at=? WHERE id=?",
            (now(), mid))
    # 3. avviare sessione di analisi
    _start_analysis_session(get_meeting(mid))


def _transcribe_audio(m: dict) -> bool:
    """Trascrizione locale con faster_whisper. Scrive transcript.txt."""
    mid = m["id"]
    audio_path = m.get("audio_path") or ""
    if not audio_path or not os.path.isfile(audio_path):
        _meeting_error(mid, "file audio non trovato")
        return False
    # lazy import
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        _meeting_error(mid, "dipendenza faster_whisper non installata")
        return False
    # verifica ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10, check=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        _meeting_error(mid, "ffmpeg non disponibile: necessario per la trascrizione")
        return False
    # modello
    cache = MEETING_WHISPER_CACHE
    Path(cache).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cache, 0o755)
    except OSError:
        pass
    global _WHISPER_MODEL
    try:
        with _WHISPER_MODEL_LOCK:
            if _WHISPER_MODEL is None:
                _WHISPER_MODEL = WhisperModel(
                    MEETING_WHISPER_MODEL, device="cpu", compute_type="int8",
                    download_root=cache)
            model = _WHISPER_MODEL
    except Exception as exc:
        _meeting_error(mid, f"caricamento modello whisper fallito: {exc}")
        return False
    # trascrivi
    transcript_path = str(Path(m["audio_path"]).parent / "transcript.txt")
    try:
        segments, _info = model.transcribe(audio_path, language="it",
                                            vad_filter=True, beam_size=5)
        lines = []
        for seg in segments:
            ts = f"[{seg.start:06.1f} - {seg.end:06.1f}]"
            lines.append(f"{ts}  {seg.text.strip()}")
        text = "\n".join(lines) + "\n"
        Path(transcript_path).write_text(text)
        os.chmod(transcript_path, 0o664)
    except Exception as exc:
        _meeting_error(mid, f"trascrizione fallita: {exc}")
        return False
    with db() as conn:
        conn.execute(
            "UPDATE meetings SET transcript_path=?, updated_at=? WHERE id=?",
            (transcript_path, now(), mid))
    return True


def _meeting_error(mid: str, msg: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE meetings SET status='failed', error=?, updated_at=? WHERE id=?",
            (msg[:2000], now(), mid))


def _transcript_document(m: dict) -> dict | None:
    """Registra la trascrizione nel catalogo Documenti e la riusa a ogni round.

    Una riunione di un'ora produce decine di migliaia di caratteri: inlinearli
    nel prompt superava il limite di lunghezza del comando con cui la sessione
    viene avviata e l'avvio falliva con «command too long». La trascrizione
    viaggia quindi come documento allegato e all'agente arriva il percorso da
    leggere. Il file resta dov'e' (knowledge area, dentro la cartella della
    riunione): il record punta al file esistente, non ne crea una copia.
    """
    path = (m.get("transcript_path") or "").strip()
    if not path or not os.path.isfile(path):
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()
    if row:
        return dict(row)
    sha, size = hashlib.sha256(), 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                size += len(chunk)
                sha.update(chunk)
    except OSError:
        return None
    title = safe_name(m.get("title") or "riunione") or "riunione"
    doc = {
        "id": str(uuid.uuid4()), "name": f"trascrizione-{title}.txt"[:120],
        "original_name": os.path.basename(path), "path": path,
        "project_slug": m.get("project_slug") or "", "scope": "private",
        "size": size, "sha256": sha.hexdigest(), "content_type": "text/plain",
        "created_at": now(),
    }
    with db() as conn:
        conn.execute(
            "INSERT INTO documents (id,name,original_name,path,project_slug,scope,size,sha256,"
            "content_type,created_at) VALUES (:id,:name,:original_name,:path,:project_slug,:scope,"
            ":size,:sha256,:content_type,:created_at)", doc)
    return doc


def _meeting_report_prompt(m: dict, *, feedback: str = "",
                           previous: dict | None = None) -> str:
    """Build one consistent decision-report contract for analysis and revision."""
    mid = m["id"]
    slug = m["project_slug"]
    transcript_path = m.get("transcript_path") or ""
    suggested_prompt = (m.get("operational_prompt") or "").strip()
    with db() as conn:
        row = conn.execute(
            "SELECT target_architecture FROM project_contexts WHERE project_slug=?",
            (slug,)).fetchone()
    current_target = ((row["target_architecture"] if row else "") or "").strip()

    prompt = (
        "# Prepare a meeting decision report for human approval\n\n"
        f"Meeting: {m['title']}\n"
        f"Project: {slug}\n\n"
        "This is a decision report, not generic minutes. Read the attached transcript "
        "in full before writing anything. Write the report in the language used by the "
        "meeting.\n\n"
        "## Evidence and classification rules\n\n"
        "- The transcript is the only authority for what participants discussed, agreed, "
        "rejected, assigned, or left unresolved. Project files may clarify names and the "
        "current state, but they must never be promoted into meeting decisions.\n"
        "- A `decision` requires explicit convergence, approval, or commitment in the "
        "transcript. Write one atomic, self-contained outcome per item. Do not include "
        "brainstorming, alternatives, status updates, assumptions, or follow-up tasks.\n"
        "- Put unresolved alternatives, contradictions, missing confirmations, and topics "
        "deferred to later in `open_questions`, never in `decisions`. If later speech clearly "
        "resolves an earlier ambiguity, keep only the final outcome.\n"
        "- Put concrete follow-up commitments in `actions`; an action is not a decision. "
        "Record owner and due date only when explicitly stated, otherwise use empty strings.\n"
        "- Deduplicate semantically equivalent items. Never invent consensus, rationale, "
        "owners, deadlines, requirements, or architecture. An empty array is correct when "
        "the meeting contains none.\n"
        "- For each decision, `detail` states exactly what was chosen, `rationale` contains "
        "only reasons actually stated, and `evidence` cites the shortest supporting "
        "transcript timestamp range (for example `[012.0 - 038.5]`).\n\n"
        "## Target architecture rule\n\n"
    )
    if current_target:
        prompt += (
            "The current target architecture is included below. If the meeting contains no "
            "explicit architecture decision, return it unchanged. If it does, return the "
            "complete consolidated target by applying only those explicit decisions; do not "
            "rewrite or fill gaps speculatively.\n\n"
            f"```text\n{current_target[:50000]}\n```\n\n"
        )
    else:
        prompt += (
            "No current target architecture is recorded. Return an empty string unless the "
            "meeting explicitly establishes target architecture content; never reconstruct a "
            "complete architecture from incidental discussion.\n\n"
        )
    prompt += (
        "## Required JSON\n\n"
        "Submit exactly one JSON object with:\n"
        "- `summary`: concise context needed to review the outcomes;\n"
        "- `decisions`: objects with `title`, `detail`, `rationale`, `evidence`;\n"
        "- `open_questions`: objects with `title`, `detail`;\n"
        "- `actions`: objects with `title`, `detail`, `owner`, `due_date`;\n"
        "- `operational_prompt`: approved post-meeting documentation/roadmap instructions;\n"
        "- `target_architecture`: the consolidated target governed by the rule above.\n\n"
        "Do not create note, summary, or minutes files: Agent Hub generates the minutes from "
        "the submitted JSON. Before your final status, submit the proposal with:\n\n"
        f"`agent-meeting-report --meeting {mid}`\n\n"
        "Example shape (replace every value; do not copy placeholders):\n\n"
        "```json\n"
        '{"summary":"...","decisions":[{"title":"...","detail":"...",'
        '"rationale":"...","evidence":"[000.0 - 000.0]"}],'
        '"open_questions":[{"title":"...","detail":"..."}],'
        '"actions":[{"title":"...","detail":"...","owner":"","due_date":""}],'
        '"operational_prompt":"","target_architecture":""}\n'
        "```\n\n"
    )
    if suggested_prompt:
        prompt += (
            "## Requester-supplied post-approval instructions\n\n"
            "These are explicit requester constraints, not evidence of a meeting decision. "
            "Preserve them in `operational_prompt` unless the transcript explicitly amends or "
            "rejects them; never duplicate them in `decisions`.\n\n"
            f"{suggested_prompt[:20000]}\n\n"
        )
    if feedback:
        prompt += (
            "## Reviewer feedback for this revision\n\n"
            f"{feedback[:6000]}\n\n"
            "Apply the feedback while rechecking every changed item against the original "
            "transcript. Reviewer feedback may request presentation changes, but it is not by "
            "itself evidence that the meeting made a new decision.\n\n"
        )
    if previous:
        prompt += (
            "## Previous proposal\n\n"
            "Revise this proposal; do not preserve an item merely because it appeared here.\n\n"
            f"```json\n{json.dumps(previous, ensure_ascii=False, indent=2)[:12000]}\n```\n\n"
        )
    prompt += (
        "## Transcript\n\n"
        "The full text is the attached document. Open and read it completely.\n"
        f"Path: {transcript_path or '(unavailable)'}\n"
    )
    return prompt


def _start_analysis_session(m: dict) -> None:
    """Crea sessione PROJECT per analizzare la trascrizione."""
    mid = m["id"]
    slug = m["project_slug"]
    profile = m.get("profile_id") or ""
    model = m.get("model") or ""
    effort = m.get("effort") or ""
    if not profile:
        _meeting_error(mid, "profilo non specificato per l'analisi")
        return
    # la trascrizione arriva come documento allegato, mai dentro il prompt
    doc = _transcript_document(m)
    prompt = _meeting_report_prompt(m)
    try:
        body = {
            "name": f"Analisi riunione: {m['title'][:50]}",
            "environment": "PROJECT",
            "project_slug": slug,
            "profile_id": profile,
            "model": model,
            "effort": effort,
            "prompt": prompt,
            "document_ids": [doc["id"]] if doc else [],
        }
        result = _create_session(body)
    except HTTPException as exc:
        _meeting_error(mid, f"creazione sessione analisi fallita: {exc.detail}")
        return
    sid = result["session"]["id"]
    with db() as conn:
        conn.execute(
            "UPDATE meetings SET status='analyzing', analysis_session_id=?, updated_at=? WHERE id=?",
            (sid, now(), mid))


def _process_action(act: dict) -> None:
    """Consuma una meeting_action pending."""
    act_id = act["id"]
    mid = act["meeting_id"]
    kind = act["kind"]
    # marca running atomicamente
    with db() as conn:
        conn.execute(
            "UPDATE meeting_actions SET status='running', processed_at=? WHERE id=? AND status='pending'",
            (now(), act_id))
    try:
        m = get_meeting(mid)
    except HTTPException:
        _action_error(act_id, "meeting non trovato")
        return
    try:
        if kind == "revise":
            _action_revise(m, act)
        elif kind == "implement":
            _action_implement(m, act)
        elif kind == "context":
            _action_context(m, act)
        else:
            _action_error(act_id, f"kind azione sconosciuto: {kind}")
            return
    except Exception as exc:  # noqa: BLE001
        _action_error(act_id, f"azione {kind} fallita: {exc}")
        return
    with db() as conn:
        conn.execute(
            "UPDATE meeting_actions SET status='completed', processed_at=? "
            "WHERE id=? AND status='running'", (now(), act_id))


def _action_error(act_id: int, msg: str) -> None:
    with db() as conn:
        action = conn.execute(
            "SELECT meeting_id,kind FROM meeting_actions WHERE id=?", (act_id,)).fetchone()
        conn.execute(
            "UPDATE meeting_actions SET status='failed', error=?, processed_at=? WHERE id=?",
            (msg[:2000], now(), act_id))
        if action and action["kind"] in ("revise", "implement"):
            conn.execute(
                "UPDATE meetings SET status='failed',error=?,updated_at=? WHERE id=?",
                (msg[:2000], now(), action["meeting_id"]))


def _action_revise(m: dict, act: dict) -> None:
    """Incrementa round e avvia nuova sessione di analisi con feedback."""
    mid = m["id"]
    raw_payload = act.get("payload") or ""
    payload = _decode_proposal(raw_payload)
    feedback = payload.get("feedback", "") if isinstance(payload, dict) else ""
    if not feedback:
        feedback = raw_payload[:6000]
    new_round = (m.get("round") or 0) + 1
    with db() as conn:
        conn.execute(
            "UPDATE meetings SET status='revising', round=?, updated_at=? WHERE id=?",
            (new_round, now(), mid))
    # avvia nuova sessione con feedback
    slug = m["project_slug"]
    profile = m.get("profile_id") or ""
    model = m.get("model") or ""
    effort = m.get("effort") or ""
    doc = _transcript_document(m)
    prev_proposal = _decode_proposal(m.get("proposal_json") or "")
    prompt = _meeting_report_prompt(m, feedback=feedback, previous=prev_proposal)
    if not profile:
        _action_error(act["id"], "profilo non configurato per la revisione")
        return
    try:
        body = {
            "name": f"Revisione riunione R{new_round}: {m['title'][:50]}",
            "environment": "PROJECT",
            "project_slug": slug,
            "profile_id": profile,
            "model": model,
            "effort": effort,
            "prompt": prompt,
            "document_ids": [doc["id"]] if doc else [],
        }
        result = _create_session(body)
        sid = result["session"]["id"]
        with db() as conn:
            conn.execute(
                "UPDATE meetings SET analysis_session_id=?, updated_at=? WHERE id=?",
                (sid, now(), mid))
    except HTTPException as exc:
        _action_error(act["id"], f"creazione sessione revisione fallita: {exc.detail}")


def _repo_state(slug: str, base: str = "") -> dict:
    """Sha corrente e percorsi toccati dal repository del progetto."""
    try:
        return wrapper_json(PROJECT_UNIX_USER, PROJECT_CTL, "changed", slug, base, timeout=120)
    except HTTPException:
        return {}


def _action_implement(m: dict, act: dict) -> None:
    """Avvia la sessione post-riunione: aggiorna la documentazione, non il codice.

    Il nome dell'azione resta `implement` per non rompere le righe gia' in coda,
    ma cio' che viene eseguito e' un aggiornamento documentale. Una riunione
    decide: trasformare le decisioni in codice e' un lavoro separato, che passa
    dalla roadmap e da una revisione umana.
    """
    mid = m["id"]
    slug = m["project_slug"]
    profile = m.get("profile_id") or ""
    model = m.get("model") or ""
    effort = m.get("effort") or ""
    proposal = _decode_proposal(m.get("proposal_json") or "")
    op_prompt = proposal.get("operational_prompt", "") if isinstance(proposal, dict) else ""
    op_prompt = op_prompt or (m.get("operational_prompt") or "")
    decisions = proposal.get("decisions", []) if isinstance(proposal, dict) else []
    open_questions = proposal.get("open_questions", []) if isinstance(proposal, dict) else []
    actions = proposal.get("actions", []) if isinstance(proposal, dict) else []
    frozen_now = meeting_arch_frozen_now(slug)
    esempi = (", ".join(f"`{n}`" for n in frozen_now) if frozen_now
              else "(nessun file di dati as-is presente al momento)")
    prompt = (
        "## Aggiornamento della documentazione dalla riunione approvata\n\n"
        f"Riunione: {m['title']} ({m['meeting_date']})\n\n"
        "Questa sessione **non implementa nulla**. Il tuo compito e' aggiornare "
        "**tutta e sola la documentazione che riguarda i prossimi passi**: "
        "quello che il progetto ha deciso di fare, non quello che il progetto "
        "e' oggi.\n\n"
        "### Il criterio\n"
        "Aggiorna un documento se parla del **futuro**: obiettivi, priorita', "
        "ordine del lavoro, milestone, criteri di accettazione previsti, "
        "versioni bersaglio dell'architettura, piani. Cercali tutti: parti "
        "dalla roadmap e segui i rimandi. Nessun elenco in queste istruzioni e' "
        "esaustivo, e possono esistere documenti creati dopo che sono state "
        "scritte — se un documento di pianificazione e' reso obsoleto dalle "
        "decisioni, rientra nel tuo compito anche se nessuno te l'ha indicato.\n\n"
        "Lascia stare un documento se fotografa il **presente**: come e' fatto "
        "il sistema adesso, che cosa e' gia' implementato, che cosa passa oggi. "
        "Questa sessione non cambia una riga di codice, quindi niente di cio' "
        "che descrive il presente puo' essere diventato falso.\n\n"
        "### Il confine, in concreto\n"
        "- Scrivi solo dentro `docs/` e in `.agent/HANDOFF.md`. Fuori non si "
        "tocca nulla: codice, test, migrazioni, script, dipendenze, "
        "configurazione.\n"
        f"- Dentro `{MEETING_ARCH_DIR}` distingue il nome del file: quelli che "
        f"finiscono in `-target` o `-next` sono le versioni future e sono il "
        "posto giusto dove far atterrare una decisione; **ogni altro file di "
        "dati e' l'architettura as-is e resta invariato** — oggi " + esempi +
        ", ma la regola vale per qualunque file con lo stesso ruolo, compresi "
        "quelli aggiunti in futuro. La prosa nella stessa cartella (README e "
        "simili) spiega il formato: quella si puo' aggiornare.\n"
        "- Nessun `git push`.\n\n"
        "Se una decisione richiede lavoro sul codice, mettila nella roadmap "
        "come attivita' da fare, con il contesto necessario a chi la prendera' "
        "in carico. Non eseguirla.\n\n"
        "### Come si scrive\n"
        "- **Scrivi il piano, non la sua storia.** La documentazione dice cosa "
        "si fa, non cosa e' stato scartato. Niente sezioni o note su proposte "
        "respinte, esperimenti chiusi, rami abbandonati, alternative valutate e "
        "lasciate cadere: nessun «rejected X», «discarded Y», «non piu' Z». Se "
        "una decisione rende obsoleto un contenuto, **cancellalo**; non "
        "annotarlo come rimosso e non lasciarne la lapide.\n"
        "- **Non ripetere cio' che e' gia' la norma.** Le convenzioni gia' in "
        "vigore — la lingua degli artefatti, lo stile dei commit, il flusso di "
        "lavoro consolidato — non sono obiettivi: continuare a rispettarle non "
        "e' un passo da pianificare. Non vanno fra le decisioni, ne' nei "
        "target, ne' in roadmap. Li' ci va solo cio' che cambia.\n"
        "- **Resta snello.** Chi legge deve vedere le decisioni e la roadmap a "
        "livello di architettura, non il verbale della riunione: quello esiste "
        "gia' altrove. Se un aggiornamento allunga un documento senza "
        "aggiungere una decisione o un passo, e' cronaca e va tolto. A parita' "
        "di contenuto vince la versione piu' corta.\n\n")
    if decisions:
        prompt += "## Decisioni approvate\n"
        for d in decisions:
            if isinstance(d, dict):
                prompt += f"- {d.get('title', '?')}: {d.get('detail', '')}\n"
        prompt += "\n"
    if actions:
        prompt += (
            "## Azioni concordate\n"
            "Sono work item da riportare nella roadmap, non decisioni aggiuntive. "
            "Conserva responsabile e scadenza solo quando presenti.\n")
        for action in actions:
            if isinstance(action, dict):
                metadata = ", ".join(
                    value for value in (
                        f"responsabile: {action.get('owner')}" if action.get("owner") else "",
                        f"scadenza: {action.get('due_date')}" if action.get("due_date") else "",
                    ) if value)
                suffix = f" ({metadata})" if metadata else ""
                prompt += f"- {action.get('title', '?')}: {action.get('detail', '')}{suffix}\n"
        prompt += "\n"
    if open_questions:
        prompt += (
            "## Questioni ancora aperte\n"
            "Non sono decisioni approvate. Se la documentazione di pianificazione "
            "tiene un decision backlog o dei blocker, registrale li' come irrisolte; "
            "non scegliere una risposta e non inserirle nell'architettura target.\n")
        for question in open_questions:
            if isinstance(question, dict):
                prompt += f"- {question.get('title', '?')}: {question.get('detail', '')}\n"
        prompt += "\n"
    if op_prompt:
        prompt += ("## Prompt operativo approvato\n"
                   "E' il lavoro deciso in riunione. Qui serve come contenuto da "
                   "riportare nella roadmap, non da eseguire.\n\n"
                   f"{op_prompt}\n\n")
    prompt += ("Committa i soli file di documentazione che hai cambiato, con un "
               "messaggio che cita la riunione. Al termine registra lo stato con "
               "agent-report, dicendo quali file hai toccato.\n")
    if not profile:
        _action_error(act["id"], "profilo non configurato per l'aggiornamento documentale")
        return
    # punto di partenza per la verifica del perimetro a sessione conclusa
    baseline = (_repo_state(slug).get("head") or "")
    try:
        body = {
            "name": f"Documentazione riunione: {m['title'][:40]}",
            "environment": "PROJECT",
            "project_slug": slug,
            "profile_id": profile,
            "model": model,
            "effort": effort,
            "prompt": prompt,
        }
        result = _create_session(body)
        sid = result["session"]["id"]
        with db() as conn:
            conn.execute(
                "UPDATE meetings SET status='implementing', implementation_session_id=?, "
                "doc_baseline=?, updated_at=? WHERE id=?",
                (sid, baseline, now(), mid))
    except HTTPException as exc:
        _action_error(act["id"], f"creazione sessione documentale fallita: {exc.detail}")


def _action_context(m: dict, act: dict) -> None:
    """Applica target_architecture della proposal a project_contexts e genera ZIP."""
    slug = m["project_slug"]
    proposal = _decode_proposal(m.get("proposal_json") or "")
    target_arch = proposal.get("target_architecture", "") if isinstance(proposal, dict) else ""
    if not target_arch:
        with db() as conn:
            current = conn.execute(
                "SELECT target_architecture FROM project_contexts WHERE project_slug=?",
                (slug,)).fetchone()
        target_arch = (current["target_architecture"] if current else "") or ""
    try:
        _build_context_zip(slug, target_arch)
    except HTTPException as exc:
        _action_error(act["id"], f"generazione ZIP contesto fallita: {exc.detail}")
        return
    with db() as conn:
        conn.execute(
            "UPDATE project_contexts SET trigger='approved_meeting', meeting_id=? "
            "WHERE project_slug=?", (m["id"], slug))


# ------------------------------------------------------- pagine statiche

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/manifest.webmanifest")
async def web_manifest():
    return FileResponse(
        STATIC / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/service-worker.js")
async def service_worker():
    # Lo scope radice permette alla PWA di coprire tutte le viste della SPA.
    return FileResponse(
        STATIC / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/")
@app.get("/projects")
@app.get("/sessions")
@app.get("/meetings")
@app.get("/accounts")
@app.get("/documents")
@app.get("/status")
@app.get("/session/{sid}")
async def index(sid: str = ""):
    """index.html con asset versionati: mai un frontend vecchio su backend nuovo."""
    html = (STATIC / "index.html").read_text()
    html = html.replace("__ASSET_VERSION__", asset_version())
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


STARTED_AT = now()
init_db()
migrate_db()
recover_pending()
sweep_runtime()
threading.Thread(target=sweep_loop, daemon=True).start()
threading.Thread(target=usage_loop, daemon=True).start()
if MEETING_WORKERS_ENABLED:
    threading.Thread(target=_meeting_worker, daemon=True).start()
    threading.Thread(target=_action_worker, daemon=True).start()


def prime_snapshots() -> None:
    """Scalda Accounts e Status subito dopo l'avvio.

    Senza questo la prima apertura dopo un riavvio del servizio sarebbe l'unica
    a pagare per intero la costruzione: farlo qui la rende immediata come tutte
    le altre.
    """
    for snap in (STATUS_SNAPSHOT, DOCKER_SNAPSHOT, ACCOUNTS_SNAPSHOT):
        try:
            snap.get()
        except Exception:  # noqa: BLE001
            pass  # una vista non costruibile ora si ricostruira' alla richiesta


threading.Thread(target=prime_snapshots, daemon=True).start()
