import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
    GOOD_STEP = {"ok": True, "rc": 0, "output": "ok", "stdout": "ok", "stderr": ""}

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

    def test_timeout_terminates_the_whole_process_group(self):
        process = mock.Mock(pid=4242, returncode=-signal.SIGTERM)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["codex"], 10), ("", "stopped")]
        with mock.patch.object(harness_update.subprocess, "Popen", return_value=process), \
             mock.patch.object(harness_update.os, "killpg") as killpg:
            result = harness_update.run(["codex"], timeout=10)
        killpg.assert_called_once_with(4242, signal.SIGTERM)
        self.assertFalse(result["ok"])
        self.assertEqual(result["rc"], -1)
        self.assertIn("intero process group terminato", result["stderr"])

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

    def test_audit_is_ephemeral_read_only_and_uses_requested_model(self):
        args = mock.Mock(project="agent-hub", model="gpt-5.6-sol", effort="high",
                         audit_timeout=321)
        report = {"before": {}, "after": {}, "updates": []}
        config = {"AGENT_HUB_PROJECT_USER": "project-agent",
                  "AGENT_HUB_PROJECTS": "/workspace/projects"}
        completed = {"ok": True, "rc": 0, "output": "compatibile",
                     "stdout": "compatibile", "stderr": ""}
        with mock.patch.object(harness_update, "as_user", return_value=completed) as run:
            result = harness_update.launch_audit(config, report, args)

        user, command = run.call_args.args
        self.assertEqual(user, "project-agent")
        self.assertEqual(run.call_args.kwargs["timeout"], 321)
        self.assertEqual(command[:5], [
            "/usr/bin/codex", "--search", "--ask-for-approval", "never", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--cd") + 1],
                         "/workspace/projects/agent-hub")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn("model_reasoning_effort=high", command)
        prompt = command[-1]
        self.assertIn("non modificare file", prompt)
        self.assertIn("non usare agent-report", prompt)
        self.assertIn("non avviare TUI o sessioni temporanee", prompt)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["summary"], "compatibile")

    def test_audit_runs_only_for_changes_or_errors(self):
        self.assertFalse(harness_update.needs_audit({"changed": False, "errors": []}))
        self.assertTrue(harness_update.needs_audit({"changed": True, "errors": []}))
        self.assertTrue(harness_update.needs_audit({"changed": False, "errors": ["codex"]}))

    def test_nightly_contract_uses_stable_claude_and_no_agent_hub_session(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('["claude", "install", "stable"]', source)
        self.assertNotIn('POST", "/api/sessions"', source)
        self.assertIn("if needs_audit(report) and audit_key", source)

    def test_report_is_group_readable_but_not_world_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            harness_update.write_report(path, {"ok": True})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)

    def test_unchanged_cycle_skips_ghost_and_persists_success(self):
        snapshot = {"user": "same", "claude": "1", "codex": "2", "opencode": "3"}
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            argv = ["harness-update-ctl", "--state", str(state)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(harness_update, "load_config", return_value={}), \
                 mock.patch.object(harness_update, "versions", return_value=snapshot), \
                 mock.patch.object(harness_update, "checked_update",
                                   return_value=dict(self.GOOD_STEP)), \
                 mock.patch.object(harness_update, "as_user",
                                   return_value=dict(self.GOOD_STEP)), \
                 mock.patch.object(harness_update, "launch_audit") as audit:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(harness_update.main(), 0)
            audit.assert_not_called()
            report = json.loads(state.read_text())
            self.assertEqual(report["audit"]["status"], "SKIPPED")
            self.assertIn("nessuna versione cambiata", report["audit"]["reason"])

    def test_changed_cycle_runs_ghost_and_records_stable_claude_commands(self):
        before = {"user": "role", "claude": "1", "codex": "2", "opencode": "3"}
        after = {**before, "claude": "1-stable"}
        snapshots = [before, before, after, after]
        calls = []

        def fake_as_user(user, command, timeout=900):
            calls.append((user, command, timeout))
            return dict(self.GOOD_STEP)

        ghost = {"status": "COMPLETED", "ok": True, "rc": 0,
                 "summary": "compatibile"}
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            argv = ["harness-update-ctl", "--state", str(state)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(harness_update, "load_config", return_value={}), \
                 mock.patch.object(harness_update, "versions", side_effect=snapshots), \
                 mock.patch.object(harness_update, "checked_update",
                                   return_value=dict(self.GOOD_STEP)), \
                 mock.patch.object(harness_update, "as_user", side_effect=fake_as_user), \
                 mock.patch.object(harness_update, "launch_audit", return_value=ghost) as audit:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(harness_update.main(), 0)
            audit.assert_called_once()
            self.assertEqual([command for _user, command, _timeout in calls], [
                ["claude", "install", "stable"], ["claude", "install", "stable"]])
            report = json.loads(state.read_text())
            self.assertTrue(report["changed"])
            self.assertEqual(report["audit"]["summary"], "compatibile")
            self.assertEqual(report["last_audit_key"], report["audit_key"])

    def test_audit_start_exception_is_reported_and_fails_cycle(self):
        before = {"user": "role", "claude": "1", "codex": "2", "opencode": "3"}
        after = {**before, "codex": "4"}
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            argv = ["harness-update-ctl", "--state", str(state)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(harness_update, "load_config", return_value={}), \
                 mock.patch.object(harness_update, "versions",
                                   side_effect=[before, before, after, after]), \
                 mock.patch.object(harness_update, "checked_update",
                                   return_value=dict(self.GOOD_STEP)), \
                 mock.patch.object(harness_update, "as_user",
                                   return_value=dict(self.GOOD_STEP)), \
                 mock.patch.object(harness_update, "launch_audit",
                                   side_effect=RuntimeError("auth non disponibile")):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(harness_update.main(), 1)
            report = json.loads(state.read_text())
            self.assertEqual(report["audit"]["status"], "FAILED")
            self.assertIn("auth non disponibile", report["audit"]["diagnostics"])


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
