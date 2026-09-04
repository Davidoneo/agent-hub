import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "libexec" / "harness-update-ctl"
LOADER = importlib.machinery.SourceFileLoader("harness_update_ctl", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
harness_update = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(harness_update)

SESSION_LOADER = importlib.machinery.SourceFileLoader(
    "session_ctl_for_update_tests", str(ROOT / "libexec/session-ctl"))
SESSION_SPEC = importlib.util.spec_from_loader(SESSION_LOADER.name, SESSION_LOADER)
session_ctl = importlib.util.module_from_spec(SESSION_SPEC)
SESSION_LOADER.exec_module(session_ctl)


class HarnessUpdateTests(unittest.TestCase):
    def test_config_parser_does_not_execute_shell_syntax(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.env"
            path.write_text("A=plain\nB='quoted'\nC=$(not-executed)\n", encoding="utf-8")
            self.assertEqual(harness_update.load_config(path), {
                "A": "plain", "B": "quoted", "C": "$(not-executed)",
            })

    def test_update_detects_opencode_false_success(self):
        result = {"ok": True, "rc": 0, "output": "Upgrade failed for npm (exit code 243)."}
        with mock.patch.object(harness_update, "run", return_value=result):
            checked = harness_update.checked_update(["opencode", "upgrade"])
        self.assertFalse(checked["ok"])

    def test_unavailable_post_update_versions_fail_the_cycle(self):
        snapshots = {
            "devagent": {
                "user": "devagent", "claude": "2.1.260 (Claude Code)",
                "codex": "non disponibile: Permission denied", "opencode": "1.18.28",
            },
            "hostagent": {
                "user": "hostagent", "claude": "2.1.260 (Claude Code)",
                "codex": "codex-cli 0.154.0", "opencode": "1.18.28",
            },
        }
        self.assertEqual(harness_update.unavailable_versions(snapshots), ["codex (devagent)"])

    def test_audit_uses_requested_codex_model_and_effort(self):
        calls = []

        def fake_api(_socket, method, path, _headers, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return 200, {}, {"csrf": "token"}
            return 200, {}, {"session": {"id": "audit-session"}}

        args = mock.Mock(project="agent-hub", model="gpt-5.6-sol", effort="high")
        report = {"before": {}, "after": {}, "updates": []}
        config = {"AGENT_HUB_ALLOWED_USERS": "owner@example.test"}
        with mock.patch.object(harness_update, "api_request", side_effect=fake_api):
            result = harness_update.launch_audit(config, report, args)

        body = calls[1][2]
        self.assertEqual(result["session_id"], "audit-session")
        self.assertEqual(body["environment"], "PROJECT")
        self.assertEqual(body["profile_id"], "codex-openai")
        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["effort"], "high")
        self.assertIn("entrambi i\nruoli PROJECT e SERVER", body["prompt"])

    def test_nightly_contract_does_not_gate_audit_on_version_change(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('if audit_key != previous.get("last_audit_key")', source)
        self.assertNotIn("needs_audit = report", source)


class CodexMenuTests(unittest.TestCase):
    def test_numbered_menu_parser_tracks_selected_row(self):
        screen = """Select Model and Effort
  1. gpt-5.6-sol Reliable model
› 2. gpt-5.6-terra (current) Balanced model
  3. gpt-5.6-luna Fast model
"""
        self.assertEqual(session_ctl._menu_rows(screen), [
            (1, "gpt-5.6-sol Reliable model", False),
            (2, "gpt-5.6-terra (current) Balanced model", True),
            (3, "gpt-5.6-luna Fast model", False),
        ])

    def test_model_confirmation_waits_across_stale_and_empty_redraws(self):
        frames = iter([
            "model:     gpt-5.6-sol high",
            "",
            "model:     gpt-5.6-terra high",
        ])
        with mock.patch.object(session_ctl, "_screen_text",
                               side_effect=lambda *_args: next(frames)), \
             mock.patch.object(session_ctl.time, "sleep"):
            screen = session_ctl._wait_screen(
                "agenthub-abcdef", {}, "gpt-5.6-terra high", timeout=1)
        self.assertIn("gpt-5.6-terra high", screen)


if __name__ == "__main__":
    unittest.main()
