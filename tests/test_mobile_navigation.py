"""Browser regression: real UI assets, synthetic HTTP data, no live Hub.

Run with a Python environment containing Playwright and Chromium, e.g.:
  python -m unittest -v tests.test_mobile_navigation
CHROMIUM_PATH may override the system browser. No browser install is needed.
"""

import json
import os
from pathlib import Path
import shutil
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = os.environ.get("CHROMIUM_PATH") or shutil.which("chromium")
TRANSCRIPT = "<script>window.injected = true</script>\n" + "Riga di conversazione\n" * 120
DOCUMENT = {"id": "doc-test", "name": "nota è.txt", "path": "/synthetic/nota.txt",
            "size": 12, "project_slug": "demo"}
SESSION = {"id": "session-test", "name": "Sessione mobile", "environment": "PROJECT",
           "project_slug": "demo", "profile_id": "codex-openai", "lifecycle": "RUNNING",
           "alive": True, "unix_user": "devagent", "workdir": "/synthetic"}
PROJECT = {"slug": "demo", "path": "/synthetic", "source": "local"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.reply({})

    def reply(self, data, status=200, content_type="application/json", headers=None):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        fixtures = {
            "/api/bootstrap": {"user": "test", "csrf": "test", "profiles": []},
            "/api/sessions": {"sessions": [SESSION]},
            "/api/sessions/session-test": {"session": SESSION},
            "/api/sessions/session-test/state": SESSION,
            "/api/sessions/session-test/runtime": {},
            "/api/documents": {"documents": [DOCUMENT]},
            "/api/projects": {"projects": [PROJECT], "unregistered": []},
            "/api/projects/demo": {"project": PROJECT, "documents": [DOCUMENT]},
            "/api/meetings": {"meetings": []},
            "/api/projects/demo/context": {"context": {}, "package_ready": True},
            "/api/notifications": {"notifications": [], "unread_count": 0},
            "/api/push": {"enabled": False},
            "/api/usage": {"usage": []},
        }
        if path in fixtures:
            self.reply(fixtures[path])
        elif path.endswith("/transcript"):
            self.reply(TRANSCRIPT.encode(), content_type="text/plain; charset=utf-8")
        elif path.endswith(("/download", "/log/raw")):
            self.reply(b"file content", content_type="application/octet-stream", headers={
                "Content-Disposition": "attachment; filename*=UTF-8''nota%20%C3%A8.txt"})
        elif path == "/" or path.startswith("/static/"):
            asset = ROOT / "app/static" / ("index.html" if path == "/" else path[8:])
            if not asset.is_file():
                self.reply({}, 404)
                return
            mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
            self.reply(asset.read_bytes(), content_type=mime.get(asset.suffix, "image/png"))
        else:
            self.reply({}, 404)


@unittest.skipUnless(sync_playwright and CHROMIUM, "requires Playwright Python and Chromium")
class MobileNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844},
                                                is_mobile=True, has_touch=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(8000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def home(self):
        self.page.locator('#nav a[href="#/"]').tap()
        expect(self.page.locator("h2")).to_have_text("Dashboard")
        self.assertEqual(self.page.url, self.base + "/#/")

    def open_session(self):
        self.page.goto(self.base + "/#/")
        self.page.locator('#view a[href="#/session/session-test"]').tap()
        expect(self.page.locator("h2")).to_have_text("Sessione mobile")

    def test_transcript_keeps_navigation_and_selected_parameters(self):
        self.open_session()
        self.page.locator("#tr-source").select_option("native")
        self.page.locator("#tr-tail").select_option("500")
        self.page.locator("#tr-detail").select_option("text")
        self.page.locator("#tr-open").tap()
        expect(self.page.locator("#transcript-page")).to_have_text(TRANSCRIPT)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
        self.assertEqual(len(self.context.pages), 1)
        self.assertIn("source=native&tail=500&chrome=hide&detail=text", self.page.url)
        self.assertFalse(self.page.evaluate("Boolean(window.injected)"))
        self.page.locator("#transcript-page").evaluate("el => window.scrollTo(0, document.body.scrollHeight)")
        self.home()
        self.page.go_back()
        expect(self.page.locator("#transcript-page")).to_have_text(TRANSCRIPT)
        self.page.get_by_role("link", name="Torna alla sessione").tap()
        expect(self.page.locator("h2")).to_have_text("Sessione mobile")
        self.home()

    def test_compact_chrome_and_settings_remain_reachable(self):
        self.page.goto(self.base + "/#/")
        expect(self.page.locator("h2")).to_have_text("Dashboard")
        chrome = self.page.locator("#app-chrome").bounding_box()
        self.assertLessEqual(chrome["height"], 60)
        self.assertTrue(self.page.locator("#settings-menu > summary").is_visible())
        self.page.locator("#settings-menu > summary").tap()
        expect(self.page.locator("#settings-menu")).to_have_attribute("open", "")
        for selector in ('a[href="#/accounts"]', 'a[href="#/status"]', "#usage-toggle",
                         "#help-toggle", "#theme-toggle"):
            self.assertTrue(self.page.locator("#settings-menu " + selector).is_visible())
        panel = self.page.locator(".settings-panel").bounding_box()
        self.assertGreaterEqual(panel["x"], 0)
        self.assertLessEqual(panel["x"] + panel["width"], 390)
        self.page.locator("#help-toggle").tap()
        self.assertTrue(self.page.locator("body").evaluate("el => el.classList.contains('help-on')"))
        self.page.locator("#usage-toggle").tap()
        self.assertTrue(self.page.locator("body").evaluate("el => el.classList.contains('usage-off')"))
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        self.page.evaluate("syncAttentionToggle(2)")
        expect(self.page.locator("#settings-attention-count")).to_have_text("2")
        self.page.evaluate("""() => {
            const badge = document.querySelector('#nav-notice');
            badge.textContent = '2';
            badge.classList.add('visible');
        }""")
        self.assertFalse(self.page.locator("#nav-notice").is_visible())
        self.assertLessEqual(self.page.locator("#nav").evaluate("el => el.scrollWidth"),
                             self.page.locator("#nav").evaluate("el => el.clientWidth"))
        self.page.set_viewport_size({"width": 320, "height": 568})
        self.assertFalse(self.page.locator("#nav-notice").is_visible())
        self.assertLessEqual(self.page.locator("#nav").evaluate("el => el.scrollWidth"),
                             self.page.locator("#nav").evaluate("el => el.clientWidth"))
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 320)

    def test_document_download_keeps_project_and_home_accessible(self):
        self.page.goto(self.base + "/#/")
        self.page.locator('#nav a[href="#/projects"]').tap()
        self.page.locator('a.project-tile-main').tap()
        self.page.get_by_role("tab", name="Documenti (1)").tap()
        self.download('a[download]')
        self.assertIn("#/projects/demo?tab=documents", self.page.url)
        self.home()

    def download(self, selector):
        with self.page.expect_download() as captured:
            self.page.locator(selector).filter(visible=True).tap()
        result = captured.value
        self.assertIsNone(result.failure())
        self.assertEqual(result.suggested_filename, "nota è.txt")
        self.assertEqual(Path(result.path()).read_bytes(), b"file content")
        self.assertEqual(len(self.context.pages), 1)

    def test_analogous_raw_log_and_context_downloads(self):
        self.open_session()
        self.download('a[download][href$="/log/raw"]')
        self.home()
        self.page.goto(self.base + "/#/projects/demo?tab=context")
        self.download('a[download][href$="/context/download"]')
        self.home()

    def test_failed_download_stays_in_hub_with_error_and_can_retry(self):
        self.page.goto(self.base + "/#/projects/demo?tab=documents")
        pattern = "**/api/documents/doc-test/download"
        self.page.route(pattern, lambda route: route.fulfill(
            status=404, content_type="application/json", body='{"detail":"file non disponibile"}'))
        self.page.locator('a[download]:visible').tap()
        expect(self.page.locator("#banner")).to_contain_text("file non disponibile")
        self.page.unroute(pattern)
        self.download('a[download]')
        expect(self.page.locator("#banner")).to_be_empty()
        self.home()

    def test_direct_transcript_error_keeps_navigation_on_small_phone(self):
        self.page.set_viewport_size({"width": 320, "height": 568})
        self.page.route("**/api/sessions/missing/transcript?*", lambda route: route.fulfill(
            status=404, content_type="application/json", body='{"detail":"sessione inesistente"}'))
        self.page.goto(self.base + "/#/session/missing/transcript")
        expect(self.page.locator("#transcript-error")).to_contain_text("sessione inesistente")
        self.assertTrue(self.page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"))
        self.home()

    def test_leaving_transcript_during_fetch_does_not_replace_dashboard(self):
        pending = []
        self.page.route("**/api/sessions/session-test/transcript?*", lambda route: pending.append(route))
        self.page.goto(self.base + "/#/session/session-test/transcript")
        expect(self.page.locator("#transcript-page")).to_have_text("Caricamento…")
        self.home()
        self.assertEqual(len(pending), 1)
        with self.page.expect_response("**/api/sessions/session-test/transcript?*"):
            pending[0].fulfill(status=200, content_type="text/plain", body=TRANSCRIPT)
        expect(self.page.locator("h2")).to_have_text("Dashboard")
        expect(self.page.locator("#transcript-page")).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
