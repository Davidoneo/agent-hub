#!/usr/bin/env python3
"""Small contracts for the navigation and two-axis session status UI."""

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NavParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_main_nav = False
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "nav" and values.get("id") == "nav":
            self.in_main_nav = True
        elif tag == "a" and self.in_main_nav:
            self.hrefs.append(values.get("href"))

    def handle_endtag(self, tag):
        if tag == "nav" and self.in_main_nav:
            self.in_main_nav = False


class StaticUiContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
        cls.index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

    def test_meetings_are_nested_under_projects(self):
        parser = NavParser()
        parser.feed(self.index)
        self.assertNotIn("#/meetings", parser.hrefs)
        self.assertIn('tabLink("meetings",', self.app)
        self.assertIn("?tab=meetings", self.app)

    def test_documents_are_nested_under_projects(self):
        parser = NavParser()
        parser.feed(self.index)
        self.assertNotIn("#/documents", parser.hrefs)
        self.assertNotIn("[/^\\/documents/", self.app)
        self.assertIn('tabLink("documents",', self.app)
        self.assertIn('uploadFiles(files, slug, null, "repository")', self.app)

    def test_backlog_is_a_primary_collection_and_session_prep_is_opt_in(self):
        parser = NavParser()
        parser.feed(self.index)
        self.assertIn("#/backlog", parser.hrefs)
        self.assertIn('[/^\\/backlog$/, viewBacklog]', self.app)
        self.assertIn("Una raccolta di idee, indipendente dalle sessioni operative", self.app)
        self.assertIn("Le idee arrivano dal bot Telegram dedicato", self.app)
        self.assertNotIn('id="backlog-text"', self.app)
        self.assertIn("Prepara sessione", self.app)
        self.assertIn('id="s-use-backlog-prompt" checked', self.app)
        self.assertIn("!$(\"#s-use-backlog-prompt\").checked ? \"\"", self.app)
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS backlog_ideas", backend)
        self.assertIn('@app.get("/api/backlog")', backend)
        self.assertNotIn('@app.post("/api/backlog")', backend)
        self.assertIn('@app.delete("/api/backlog/{bid}")', backend)
        self.assertIn("https://api.openai.com/v1/audio/transcriptions", backend)
        self.assertIn("BACKLOG_CTL", backend)

    def test_open_session_and_turn_outcome_are_separate(self):
        self.assertIn("function sessionIsOpen", self.app)
        self.assertIn('label = "Turno completato"', self.app)
        self.assertIn("const open = d.sessions.filter(sessionIsOpen)", self.app)
        self.assertIn("WAITING_SESSION", self.app)

    def test_project_tiles_and_nested_tabs_are_styled(self):
        self.assertIn(".project-tile {", self.css)
        self.assertIn(".project-tabs {", self.css)
        self.assertIn(".project-panel.active", self.css)

    def test_launch_failure_opens_the_persisted_session(self):
        self.assertIn('r.headers.get("X-Agent-Hub-Session-Id")', self.app)
        self.assertIn('e.code === "launch_failed"', self.app)
        self.assertIn('location.hash = "#/session/" + encodeURIComponent(e.sessionId)', self.app)
        self.assertIn("LAUNCH_FAILED:", self.app)

    def test_health_ui_summarizes_without_hiding_checks(self):
        for label in ("Hardware", "Rete / VPN", "Agent Hub backend", "Agent Hub frontend", "Generale"):
            self.assertIn(f'label: "{label}"', self.app)
        self.assertIn('class="health-summary"', self.app)
        self.assertIn('class="more health-details"', self.app)
        self.assertIn("Dettagli tecnici (${last.checks.length} controlli)", self.app)
        self.assertIn('class="card more status-details"', self.app)

    def test_session_actions_expose_pause_and_order_by_impact(self):
        pause = '<button class="key" id="a-pause"'
        restart = '<button class="small" id="a-restart">Restart</button>'
        kill = '<button class="small danger" id="a-kill">${escalationHost ? "Chiudi solo host" : "Kill"}</button>'
        delete = '<button class="small danger" id="a-delete">Elimina</button>'
        self.assertIn(pause, self.app)
        self.assertIn(">Esc / Pausa</button>", self.app)
        self.assertLess(self.app.index(restart), self.app.index(kill))
        self.assertLess(self.app.index(kill), self.app.index(delete))
        self.assertIn('$("#a-pause").onclick = () => act("pause")', self.app)

    def test_escalation_sessions_are_linked_and_can_close_as_a_pair(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("relation_type TEXT DEFAULT ''", backend)
        self.assertIn('"relations": session_relations(sid)', backend)
        self.assertIn('action == "kill_pair"', backend)
        self.assertIn('id="a-kill-pair"', self.app)
        self.assertIn("Chiudi entrambe", self.app)
        self.assertIn("Sessioni collegate", self.app)

    def test_failed_escalation_launch_is_not_marked_granted(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        failed = backend[backend.index("except HTTPException as exc:",
                                       backend.index("def grant_escalation")):
                         backend.index("finalize_escalation_grant(source_row, result",
                                       backend.index("def grant_escalation"))]
        self.assertIn("finalize_escalation_launch_failure", failed)
        self.assertNotIn("finalize_escalation_grant(source_row, host_row", failed)
        self.assertIn("Lo scope amministrativo resta da eseguire", backend)

    def test_mobile_tui_controls_cover_structured_answers_and_context(self):
        for control in ("a-left", "a-right", "a-space", "a-tab", "a-context"):
            self.assertIn(f'id="{control}"', self.app)
        self.assertIn('$("#a-tab").onclick = () => sendTuiKey("\\t")', self.app)
        self.assertIn('$("#a-context").onclick = () => sendTuiKey("\\x14")', self.app)
        self.assertIn("Tab per aggiungere note o una risposta libera", self.app)
        self.assertIn("Ctrl+T per leggere il contesto precedente", self.app)
        self.assertIn(".keypad-hint {", self.css)

    def test_terminal_keyboard_is_visually_separate_and_arrows_match(self):
        self.assertIn('class="terminal-keyboard"', self.app)
        self.assertIn("Tastiera terminale", self.app)
        self.assertNotIn("questi pulsanti inviano tasti alla TUI", self.app)
        self.assertIn('class="keypad-hint help"', self.app)
        self.assertIn('class="row session-actions"', self.app)
        for control, arrow in (("a-left", "←"), ("a-up", "↑"),
                               ("a-down", "↓"), ("a-right", "→")):
            self.assertIn(f'id="{control}"', self.app)
            self.assertIn(f'>{arrow}</button>', self.app)
        for old_arrow in ("◀", "▲", "▼", "▶"):
            self.assertNotIn(f'>{old_arrow}</button>', self.app)
        self.assertIn("grid-template-areas:", self.css)
        self.assertIn('"left down right"', self.css)
        self.assertIn(".dpad .arrow-key {", self.css)
        self.assertIn(".key-row-main {", self.css)

    def test_terminal_has_deterministic_tmux_scrolling_and_real_resize_observer(self):
        for control in ("term-page-up", "term-page-down", "term-live"):
            self.assertIn(f'id="{control}"', self.app)
        for action in ("scroll-up", "scroll-down", "scroll-bottom"):
            self.assertIn(f'scrollTerminal("{action}")', self.app)
        self.assertIn("new ResizeObserver(onResize)", self.app)
        self.assertIn('document.addEventListener("visibilitychange", onVisibility)', self.app)
        self.assertIn(".terminal-toolbar {", self.css)

    def test_session_uses_global_dashboard_tab_only(self):
        self.assertNotIn("← Dashboard", self.app)
        parser = NavParser()
        parser.feed(self.index)
        self.assertIn("#/", parser.hrefs)

    def test_internal_attention_is_persistent_and_polled(self):
        self.assertIn('id="attention" aria-live="polite"', self.index)
        self.assertIn('id="nav-notice"', self.index)
        self.assertIn("const ATTENTION_LIFECYCLES", self.app)
        self.assertIn('"NEEDS_INPUT"', self.app)
        self.assertIn("function loadAttention()", self.app)
        self.assertIn("setInterval(loadAttention, 10_000)", self.app)
        self.assertIn(".attention-panel {", self.css)
        self.assertIn("#nav-notice.visible", self.css)

    def test_attention_overlays_the_page_and_can_be_reopened(self):
        # Gli avvisi non stanno piu' nel flusso della pagina: scendono
        # dall'alto in sovraimpressione, si ritirano da soli e si riaprono
        # dal pulsante in testata o dal contatore accanto a «Dashboard».
        self.assertIn('id="notice-stack"', self.index)
        self.assertIn('id="attention-toggle"', self.index)
        self.assertIn("#notice-stack {", self.css)
        notice_rule = self.css.split("#notice-stack {", 1)[1].split("}", 1)[0]
        self.assertIn("position: fixed", notice_rule)
        self.assertIn("#notice-stack > .shown", self.css)
        self.assertIn("ATTENTION_AUTOHIDE_MS", self.app)
        self.assertIn("function toggleAttention()", self.app)
        self.assertIn('$("#attention-toggle").onclick = toggleAttention', self.app)
        self.assertIn('$("#nav-notice").onclick', self.app)

    def test_delivery_failure_is_visible_in_web_attention_only(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        notifier = (ROOT / "libexec/telegram-ctl").read_text(encoding="utf-8")
        self.assertIn('"DELIVERY_FAILED": "Messaggio non consegnato"', backend)
        self.assertIn('"DELIVERY_FAILED"', self.app)
        self.assertIn('"DELIVERY_FAILED"', notifier)
        self.assertNotIn("urgent =", notifier)
        self.assertIn("deliver_escalations", notifier)
        self.assertIn("deliver_shares", notifier)

    def test_pwa_push_is_opt_in_and_opens_the_session(self):
        worker = (ROOT / "app/static/service-worker.js").read_text(encoding="utf-8")
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn('id="push-toggle"', self.index)
        self.assertIn("async function setupPush()", self.app)
        self.assertIn("Notification.requestPermission()", self.app)
        self.assertIn("pushManager.subscribe", self.app)
        self.assertIn('self.addEventListener("push"', worker)
        self.assertIn('self.addEventListener("notificationclick"', worker)
        self.assertIn('data.type === "dismiss"', worker)
        self.assertIn('self.addEventListener("message"', worker)
        self.assertIn('type: "sync-notifications"', self.app)
        self.assertIn('type: "dismiss", session_id: sid, badge', self.app)
        worker_messages = worker[worker.index('self.addEventListener("message"'):
                                 worker.index('self.addEventListener("notificationclick"')]
        self.assertIn('data.type === "dismiss" && data.session_id', worker_messages)
        self.assertIn('closeResolvedNotifications(null, String(data.session_id))',
                      worker_messages)
        session_view = self.app[self.app.index("async function viewSession(sid)"):]
        self.assertIn("await markSessionNotificationRead(sid)", session_view)
        self.assertIn('const TRANSIENT_NOTICE_LIFECYCLES = new Set(["COMPLETED"])', self.app)
        self.assertIn("clients.openWindow(target)", worker)
        self.assertIn("function setBadge(count)", worker)
        self.assertIn("data.event_keys", worker)
        self.assertIn('lifecycle: data.lifecycle || ""', worker)
        self.assertIn('event_key: data.event_key || ""', worker)
        self.assertIn('CREATE TABLE IF NOT EXISTS push_subscriptions', backend)
        self.assertIn('CREATE TABLE IF NOT EXISTS lifecycle_receipts', backend)
        self.assertIn('@app.post("/api/push/subscriptions")', backend)
        self.assertIn('@app.post("/api/notifications/read")', backend)
        self.assertIn('@app.post("/api/push/presence")', backend)
        self.assertIn('"COMPLETED", "NEEDS_INPUT", "NEEDS_HOST_ACTION"', backend)
        self.assertIn("PUSH_TTL_SECONDS = 300", backend)
        self.assertEqual(backend.count("ttl=PUSH_TTL_SECONDS"), 2)
        self.assertIn("Una nuova installazione deve ricevere soltanto transizioni future", backend)
        self.assertIn('f"historical:{row[\'lifecycle\']}"', backend)
        self.assertNotIn("push_is_visible", backend)
        self.assertNotIn("reportPushPresence", self.app)

    def test_crash_wins_over_delivery_failure_and_is_explicit(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        dead_branch = backend[backend.index('elif not row["alive"]:'):
                              backend.index('elif row["status"] == "starting":',
                                            backend.index('elif not row["alive"]:'))]
        self.assertLess(dead_branch.index('row["exit_code"] not in'),
                        dead_branch.index('last.get("status") == FAILED'))
        self.assertIn("Sessione crashata", self.app)
        self.assertIn("Il prompt è rimasto salvato", self.app)
        self.assertIn("sostituito dal successivo Restart", backend)
        self.assertIn("lifecycle='STARTING'", backend)
        restart = backend[backend.index('elif action == "restart":'):
                          backend.index('elif action == "delete":')]
        self.assertLess(restart.index("register_initial_prompt(row, prompt, started)"),
                        restart.index("dismiss_webpush_session_async(sid)"))

    def test_explicit_close_suppresses_late_notifications_until_restart(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("notification_suppressed_at TEXT DEFAULT ''", backend)
        self.assertGreaterEqual(
            backend.count('not row.get("notification_suppressed_at")'), 2)
        self.assertIn('not r.get("notification_suppressed_at")', backend)
        close = backend[backend.index("def close_session(row: dict)"):
                        backend.index("def delete_session(row: dict)")]
        self.assertIn("notification_suppressed_at=?", close)
        self.assertIn('dismiss_webpush_session_async(row["id"])', close)
        restart = backend[backend.index('elif action == "restart":'):
                          backend.index('elif action == "delete":')]
        self.assertIn("notification_suppressed_at=''", restart)

    def test_iphone_chrome_respects_all_safe_areas(self):
        self.assertIn('<div id="app-chrome">', self.index)
        self.assertIn("#app-chrome {", self.css)
        for edge in ("top", "right", "bottom", "left"):
            self.assertIn(f"env(safe-area-inset-{edge})", self.css)
        self.assertNotIn("top: 51px", self.css)
        self.assertIn("calc(10px + env(safe-area-inset-bottom))", self.css)

    def test_mobile_terminal_is_readable_and_never_covered_by_controls(self):
        self.assertIn('window.matchMedia("(max-width: 520px)")', self.app)
        self.assertIn("fontSize: mobileTerminal ? 14 : 13", self.app)
        self.assertIn("lineHeight: mobileTerminal ? 1.1 : 1", self.app)
        self.assertIn("Il blocco arriva a oltre 400 px", self.css)
        self.assertIn("position: static; margin: 12px -10px 0", self.css)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", self.css)
        self.assertIn('btn.dataset.pushState = pushSubscription ? "on" : "off"', self.app)

    def test_usage_refresh_explains_expired_claude_oauth(self):
        self.assertIn("non rinnova le credenziali OAuth", self.app)
        self.assertIn("Il rinnovo automatico Claude non è riuscito", self.app)
        self.assertNotIn('if (left <= 0) return "a momenti"', self.app)
        self.assertIn('"previsto " + ago + "m fa"', self.app)

    def test_kill_and_delete_return_to_dashboard(self):
        self.assertIn(
            'if (action === "kill" || action === "delete") location.hash = "#/";',
            self.app,
        )

    def test_new_session_keeps_technical_controls_in_advanced_options(self):
        form_start = self.app.index("async function viewNewSession()")
        form_end = self.app.index("  const envSel =", form_start)
        form = self.app[form_start:form_end]
        advanced = form.index('<details class="more" id="s-opts">')
        permission = form.index('<select id="s-perm"></select>')
        self.assertEqual(form.count('<select id="s-perm"></select>'), 1)
        self.assertGreater(permission, advanced)
        self.assertIn("<summary>Opzioni avanzate</summary>", form)
        self.assertIn("Larghezza terminale (colonne)", form)
        self.assertIn("Altezza terminale (righe)", form)
        self.assertIn("quanti caratteri", form)
        self.assertIn("quante linee sono visibili", form)

    def test_account_defaults_drive_new_sessions(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn('"profile_id": "codex-openai"', backend)
        self.assertIn('"model": "gpt-5.6-sol"', backend)
        self.assertIn('"effort": "high"', backend)
        self.assertIn('CREATE TABLE IF NOT EXISTS account_session_defaults', backend)
        self.assertIn('@app.post("/api/accounts/defaults")', backend)
        self.assertIn('"account_defaults": account_session_defaults()', backend)
        self.assertIn("function sessionDefault(user)", self.app)
        self.assertIn('data-default-save', self.app)
        self.assertIn('post("/api/accounts/defaults"', self.app)

    def test_accounts_login_populates_every_explicit_insert_field(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        login = backend[backend.index("async def accounts_login"):
                        backend.index("def sh(", backend.index("async def accounts_login"))]
        for field in ("session_mode", "goal", "relation_type", "relation_request_id"):
            self.assertIn(f'"{field}": ""', login)

    def test_login_lifecycle_uses_process_exit_instead_of_agent_report(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        lifecycle = backend[backend.index('if row.get("kind") == "login":'):
                            backend.index('elif row["status"] == "failed":')]
        self.assertIn('row["exit_code"] == "0"', lifecycle)
        self.assertIn('life = "COMPLETED"', lifecycle)
        self.assertIn('life = "FAILED"', lifecycle)

    def test_new_session_prompt_draft_survives_navigation_until_persisted(self):
        self.assertIn("function newSessionPromptDraftKey()", self.app)
        self.assertIn('localStorage.getItem(newSessionPromptDraftKey())', self.app)
        self.assertIn('promptEl.oninput = () => writeNewSessionPromptDraft(promptEl.value)', self.app)
        self.assertIn('promptEl.value = readNewSessionPromptDraft()', self.app)
        self.assertEqual(self.app.count('writeNewSessionPromptDraft("")'), 2)
        launch_failed = self.app.index('e.code === "launch_failed"')
        self.assertIn('writeNewSessionPromptDraft("")', self.app[launch_failed:launch_failed + 300])

    def test_session_composer_survives_background_and_socket_reconnect(self):
        self.assertIn("function sessionMessageDraftKey(sid)", self.app)
        self.assertIn("msgEl.value = readSessionMessageDraft(sid)", self.app)
        self.assertIn("msgEl.oninput = () => writeSessionMessageDraft(sid, msgEl.value)", self.app)
        self.assertIn('writeSessionMessageDraft(sid, "")', self.app)
        self.assertIn("function connectTerminal()", self.app)
        self.assertIn("if (document.hidden) return;", self.app)
        visibility = self.app.index("const onVisibility = () =>", self.app.index("async function viewSession"))
        visibility_block = self.app[visibility:visibility + 500]
        self.assertIn("connectTerminal()", visibility_block)
        self.assertNotIn("route", visibility_block)

    def test_kill_returns_to_dashboard(self):
        self.assertIn('if (action === "kill" || action === "delete") location.hash = "#/";', self.app)

    def test_active_session_composer_owns_model_and_attachments(self):
        start = self.app.index("async function viewSession(sid)")
        session = self.app[start:self.app.index("// ---------------------------------------------------------------- accounts", start)]
        self.assertIn('id="attach">Allega</button>', session)
        self.assertIn('id="runtime-toggle"', session)
        self.assertIn("document_ids: Array.from(pendingDocIds)", session)
        self.assertNotIn("Allega e comunica i percorsi", session)
        self.assertIn("Gli allegati scelti non vengono comunicati", session)

    def test_session_advanced_options_hold_continuity_and_diagnostics_at_bottom(self):
        start = self.app.index("async function viewSession(sid)")
        session = self.app[start:self.app.index("// ---------------------------------------------------------------- accounts", start)]
        advanced = session.index('id="session-options"')
        continuity = session.index("<h3>Continuità</h3>")
        diagnostics = session.index("<h3>Diagnostica harness</h3>")
        log = session.index("<h3>Log</h3>")
        self.assertLess(log, advanced)
        self.assertLess(advanced, continuity)
        self.assertLess(continuity, diagnostics)

    def test_escalation_is_visible_only_for_agent_request(self):
        self.assertIn('s.lifecycle !== "NEEDS_HOST_ACTION"', self.app)
        self.assertIn('<div id="escalation-slot">', self.app)
        self.assertIn("paintEscalation(st);", self.app)


if __name__ == "__main__":
    unittest.main()
