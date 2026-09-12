import importlib.machinery
import importlib.util
import json
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


# Stringhe della CLI Codex installata; layout sintetico, nessun dato account.
RESET_MENU = """Usage limit resets
› 1. Redeem usage limit reset
  2. Cancel
Press enter to confirm or esc to go back
"""
RESET_CONFIRM = """Redeem usage limit reset
Reset your current usage limits.
› 1. Confirm
  2. Cancel
"""


class SessionDeliveryTests(unittest.TestCase):
    def test_automatic_transport_never_answers_reset_menus(self):
        for screen in (RESET_MENU, RESET_CONFIRM):
            commands = (
                ("send-keys", "-t", "agenthub-abcdef", "Enter"),
                ("send-keys", "-t", "agenthub-abcdef", "-l", "1"),
                ("send-keys", "-t", "agenthub-abcdef", "-l", "/model"),
                ("paste-buffer", "-d", "-p", "-b", "fixture", "-t",
                 "agenthub-abcdef"),
            )
            for command in commands:
                with self.subTest(screen=screen, command=command), \
                     mock.patch.object(session_ctl, "_screen_text", return_value=screen), \
                     mock.patch.object(session_ctl.subprocess, "run") as run:
                    result = session_ctl.tmux(*command, env={"TEST": "1"})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("consenso esplicito", result.stderr)
                    run.assert_not_called()

    def test_availability_notice_and_prose_do_not_block_normal_input(self):
        harmless = (
            "• You have 2 usage limit resets available. Run /usage to use one.",
            "Documented Redeem usage limit reset in the manual.",
        )
        for screen in harmless:
            with self.subTest(screen=screen), \
                 mock.patch.object(session_ctl, "_screen_text", return_value=screen), \
                 mock.patch.object(session_ctl.subprocess, "run", return_value=
                                   subprocess.CompletedProcess([], 0, "", "")) as run:
                result = session_ctl.tmux(
                    "send-keys", "-t", "agenthub-abcdef", "Enter", env={"TEST": "1"})
                self.assertEqual(result.returncode, 0)
                run.assert_called_once()

    def test_copy_mode_does_not_send_input_to_reset_menu(self):
        with mock.patch.object(session_ctl, "_screen_text", return_value=RESET_CONFIRM) as screen, \
             mock.patch.object(session_ctl.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, "", "")) as run:
            result = session_ctl.tmux(
                "send-keys", "-X", "-t", "agenthub-abcdef", "page-up", env={"TEST": "1"})
        self.assertEqual(result.returncode, 0)
        screen.assert_not_called()
        run.assert_called_once()

    def test_reset_appearing_during_paste_delay_prevents_first_enter(self):
        with mock.patch.object(session_ctl, "_screen_text", return_value=RESET_MENU), \
             mock.patch.object(session_ctl, "_pane_submit_state", return_value=(False, "idle")), \
             mock.patch.object(session_ctl.time, "sleep"), \
             mock.patch.object(session_ctl.subprocess, "run") as run:
            ok, error = session_ctl._submit_after_paste(
                "agenthub-abcdef", "ignored", {}, {"id": "codex-openai"})
        self.assertFalse(ok)
        self.assertIn("consenso esplicito", error)
        run.assert_not_called()

    def test_reset_appearing_during_retry_delay_prevents_second_enter(self):
        with mock.patch.object(session_ctl, "_screen_text", side_effect=[
                "composer", "composer", "composer", RESET_CONFIRM]), \
             mock.patch.object(session_ctl, "_pane_submit_state", return_value=(False, "idle")), \
             mock.patch.object(session_ctl, "_observe_submission", return_value=False), \
             mock.patch.object(session_ctl.time, "sleep"), \
             mock.patch.object(session_ctl.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, "", "")) as run:
            ok, error = session_ctl._submit_after_paste(
                "agenthub-abcdef", "ignored", {}, {"id": "codex-openai"})
        self.assertFalse(ok)
        self.assertIn("consenso esplicito", error)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], "Enter")

    def test_followup_and_initial_paste_stop_without_consuming_reset(self):
        with mock.patch.object(session_ctl, "_screen_text", return_value=RESET_MENU), \
             mock.patch.object(session_ctl.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, "0", "")) as run:
            ok, error = session_ctl._paste("agenthub-abcdef", "fixture", {})
        self.assertFalse(ok)
        self.assertIn("consenso esplicito", error)
        self.assertFalse(any("paste-buffer" in c.args[0] or "send-keys" in c.args[0]
                             for c in run.call_args_list))

    def test_explicit_terminal_confirmation_and_cancel_still_work(self):
        for key, expected in (("enter", "Enter"), ("escape", "Escape")):
            with self.subTest(key=key), \
                 mock.patch.object(session_ctl, "_screen_text", return_value=RESET_CONFIRM) as screen, \
                 mock.patch.object(session_ctl.subprocess, "run", return_value=
                                   subprocess.CompletedProcess([], 0, "", "")) as run, \
                 mock.patch("builtins.print"):
                session_ctl.cmd_keys(["agenthub-abcdef", key])
                screen.assert_not_called()
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][-1], expected)

    def test_command_confirmation_propagates_reset_guard_failure(self):
        profile = {"id": "codex-openai", "runtime": {"confirm_patterns": ["confirmation"]}}
        with mock.patch.object(session_ctl, "load_profiles", return_value={profile["id"]: profile}), \
             mock.patch.object(session_ctl, "_screen_text", side_effect=[
                 "composer", "composer", RESET_CONFIRM]), \
             mock.patch.object(session_ctl, "_pane_text", return_value="confirmation"), \
             mock.patch.object(session_ctl.time, "sleep"), \
             mock.patch.object(session_ctl.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, "", "")) as run, \
             mock.patch("builtins.print") as output, \
             self.assertRaises(SystemExit) as stopped:
            session_ctl.cmd_command(["agenthub-abcdef", profile["id"], "/model"])
        self.assertEqual(stopped.exception.code, 5)
        result = json.loads(output.call_args.args[0])
        self.assertFalse(result["ok"])
        self.assertIn("consenso esplicito", result["error"])
        self.assertEqual(sum(c.args[0][-1] == "Enter" for c in run.call_args_list), 1)

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
