import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "session_ctl_delivery", str(ROOT / "libexec" / "session-ctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
session_ctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(session_ctl)


class SessionDeliveryTests(unittest.TestCase):
    def test_claude_login_uses_direct_auth_command_without_waiting_for_composer(self):
        calls = []
        user = session_ctl.me().pw_name
        profile = {
            "id": "claude-anthropic",
            "harness": "claude",
            "command": "claude",
            "allowed_users": [user],
            "login": {"args": ["auth", "login"]},
        }

        def fake_tmux(*args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(session_ctl, "load_profiles",
                               return_value={profile["id"]: profile}), \
             mock.patch.object(session_ctl, "ensure_server"), \
             mock.patch.object(session_ctl, "tmux", side_effect=fake_tmux), \
             mock.patch.object(session_ctl, "_wait_ready") as wait_ready, \
             mock.patch("builtins.print"):
            session_ctl.cmd_login(["agenthub-abcdef", profile["id"]])

        launch = next(call for call in calls if call[0] == "new-session")
        self.assertEqual(launch[-1], "exec claude auth login")
        self.assertIn(
            ("set-option", "-t", "agenthub-abcdef", "remain-on-exit", "on"), calls)
        wait_ready.assert_not_called()

    def test_private_launch_spec_preserves_and_removes_a_long_prompt(self):
        prompt = "handoff lungo\n" + ("x" * 20000)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(session_ctl, "_launch_spec_dir", return_value=directory):
            path = session_ctl._write_launch_spec(["codex", "--model", "gpt-5.6-sol", prompt])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            argv = session_ctl._consume_launch_spec(path)
            self.assertEqual(argv[-1], prompt)
            self.assertFalse(os.path.exists(path))

    def test_claude_trust_selects_yes_before_enter(self):
        sent = []

        def fake_tmux(*args, **_kwargs):
            if args[0] == "send-keys":
                sent.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(session_ctl, "tmux", side_effect=fake_tmux), \
             mock.patch.object(session_ctl, "_screen_text", return_value=(
                 "  No, exit\n❯ Yes, I trust this folder\n")), \
             mock.patch.object(session_ctl.time, "sleep"):
            ok, err = session_ctl._confirm_trust_prompt(
                "agenthub-abcdef", {"key": "Enter"}, {},
                "❯ No, exit\n  Yes, I trust this folder\n")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(sent, ["Down", "Enter"])

    def test_claude_trust_never_confirms_an_unknown_selection(self):
        with mock.patch.object(session_ctl, "tmux") as mocked:
            ok, err = session_ctl._confirm_trust_prompt(
                "agenthub-abcdef", {"key": "Enter"}, {},
                "Do you trust the files in this folder?\n")
        self.assertFalse(ok)
        self.assertIn("non risulta selezionata", err)
        mocked.assert_not_called()

    def test_backend_passes_profile_to_followup_transport(self):
        backend = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('path, "submit", row["profile_id"], timeout=120)', backend)

    def test_codex_delay_covers_observed_eight_second_failure(self):
        with mock.patch.object(session_ctl.os.path, "getsize", return_value=3072):
            self.assertGreaterEqual(session_ctl._codex_paste_delay("ignored"), 12.0)

    def test_codex_submit_is_verified(self):
        sent = []

        def fake_tmux(*args, **_kwargs):
            if args[0] == "send-keys":
                sent.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(session_ctl, "tmux", side_effect=fake_tmux), \
             mock.patch.object(session_ctl, "_pane_submit_state",
                               return_value=(False, "hostagent")), \
             mock.patch.object(session_ctl, "_screen_text", return_value="prompt invariato"), \
             mock.patch.object(session_ctl, "_codex_paste_delay", return_value=0), \
             mock.patch.object(session_ctl, "_observe_submission", return_value=True), \
             mock.patch.object(session_ctl.time, "sleep"):
            ok, err = session_ctl._submit_after_paste(
                "agenthub-abcdef", "/tmp/ignored", {}, {"id": "codex-openai"})
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(sent, ["Enter"])

    def test_codex_retries_once_then_reports_honest_failure(self):
        sent = []

        def fake_tmux(*args, **_kwargs):
            if args[0] == "send-keys":
                sent.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(session_ctl, "tmux", side_effect=fake_tmux), \
             mock.patch.object(session_ctl, "_pane_submit_state",
                               return_value=(False, "hostagent")), \
             mock.patch.object(session_ctl, "_screen_text", return_value="prompt invariato"), \
             mock.patch.object(session_ctl, "_codex_paste_delay", return_value=0), \
             mock.patch.object(session_ctl, "_observe_submission", return_value=False), \
             mock.patch.object(session_ctl.time, "sleep"):
            ok, err = session_ctl._submit_after_paste(
                "agenthub-abcdef", "/tmp/ignored", {}, {"id": "codex-openai"})
        self.assertFalse(ok)
        self.assertIn("due submit", err)
        self.assertEqual(sent, ["Enter", "Enter"])

    def test_codex_does_not_retry_after_screen_changes(self):
        sent = []

        def fake_tmux(*args, **_kwargs):
            if args[0] == "send-keys":
                sent.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")

        screens = iter(("prompt originale", "domanda interattiva nuova"))
        with mock.patch.object(session_ctl, "tmux", side_effect=fake_tmux), \
             mock.patch.object(session_ctl, "_pane_submit_state",
                               return_value=(False, "hostagent")), \
             mock.patch.object(session_ctl, "_screen_text", side_effect=lambda *_: next(screens)), \
             mock.patch.object(session_ctl, "_codex_paste_delay", return_value=0), \
             mock.patch.object(session_ctl, "_observe_submission", return_value=False), \
             mock.patch.object(session_ctl.time, "sleep"):
            ok, err = session_ctl._submit_after_paste(
                "agenthub-abcdef", "/tmp/ignored", {}, {"id": "codex-openai"})
        self.assertFalse(ok)
        self.assertIn("retry automatico omesso", err)
        self.assertEqual(sent, ["Enter"])

    def test_claude_submit_is_observed_not_assumed_from_tmux(self):
        sent = []

        def fake_tmux(*args, **_kwargs):
            if args[0] == "send-keys":
                sent.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(session_ctl, "tmux", side_effect=fake_tmux), \
             mock.patch.object(session_ctl, "_settle"), \
             mock.patch.object(session_ctl, "_pane_submit_state",
                               return_value=(False, "claude")), \
             mock.patch.object(session_ctl, "_screen_text", return_value="composer"), \
             mock.patch.object(session_ctl, "_observe_submission", return_value=False), \
             mock.patch.object(session_ctl.time, "sleep"):
            ok, err = session_ctl._submit_after_paste(
                "agenthub-abcdef", "/tmp/ignored", {}, {"id": "claude-anthropic"})
        self.assertFalse(ok)
        self.assertIn("due submit", err)
        self.assertEqual(sent, ["Enter", "Enter"])

    def test_opencode_accepts_observed_screen_transition(self):
        with mock.patch.object(session_ctl, "_pane_submit_state",
                               return_value=(False, "opencode")), \
             mock.patch.object(session_ctl, "_screen_text",
                               return_value="esc to interrupt"), \
             mock.patch.object(session_ctl.time, "sleep"):
            self.assertTrue(session_ctl._observe_submission(
                "agenthub-abcdef", {}, "opencode", "composer", timeout=1))


if __name__ == "__main__":
    unittest.main()
