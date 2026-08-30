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
        kill = '<button class="small danger" id="a-kill">Kill</button>'
        delete = '<button class="small danger" id="a-delete">Elimina</button>'
        self.assertIn(pause, self.app)
        self.assertIn(">Esc / Pausa</button>", self.app)
        self.assertLess(self.app.index(restart), self.app.index(kill))
        self.assertLess(self.app.index(kill), self.app.index(delete))
        self.assertIn('$("#a-pause").onclick = () => act("pause")', self.app)

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
        self.assertIn("const ATTENTION_LIFECYCLES", self.app)
        self.assertIn('"NEEDS_INPUT"', self.app)
        self.assertIn("function loadAttention()", self.app)
        self.assertIn("setInterval(loadAttention, 10_000)", self.app)
        self.assertIn(".attention-panel {", self.css)

    def test_delivery_failure_is_visible_attention(self):
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        notifier = (ROOT / "libexec/telegram-ctl").read_text(encoding="utf-8")
        self.assertIn('"DELIVERY_FAILED": "Messaggio non consegnato"', backend)
        self.assertIn('"DELIVERY_FAILED"', self.app)
        self.assertIn('"DELIVERY_FAILED"', notifier)
        self.assertIn('"DELIVERY_FAILED",', notifier.split("urgent =", 1)[1])

    def test_pwa_push_is_opt_in_and_opens_the_session(self):
        worker = (ROOT / "app/static/service-worker.js").read_text(encoding="utf-8")
        backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn('id="push-toggle"', self.index)
        self.assertIn("async function setupPush()", self.app)
        self.assertIn("Notification.requestPermission()", self.app)
        self.assertIn("pushManager.subscribe", self.app)
        self.assertIn('self.addEventListener("push"', worker)
        self.assertIn('self.addEventListener("notificationclick"', worker)
        self.assertIn("clients.openWindow(target)", worker)
        self.assertIn('CREATE TABLE IF NOT EXISTS push_subscriptions', backend)
        self.assertIn('@app.post("/api/push/subscriptions")', backend)
        self.assertIn('@app.post("/api/push/presence")', backend)
        self.assertIn("Finche' e' visibile basta la barra interna", backend)

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


if __name__ == "__main__":
    unittest.main()
