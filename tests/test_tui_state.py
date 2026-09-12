#!/usr/bin/env python3
"""Contratti per le richieste di input mostrate dalle harness supportate."""
import ast
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from app import tui_state


CAPACITY = "⚠ Selected model is at capacity. Please try a different model."
COMPOSER = "\n\n› Ask Codex to do anything\n\n  gpt-5.6-sol high · /workspace/example\n"


class TuiStateTests(unittest.TestCase):
    def test_capacity_stopped_at_composer_requires_input(self):
        for warning in (CAPACITY, CAPACITY.replace("Please try", "Please\n  try")):
            self.assertTrue(tui_state.needs_input("codex-openai", warning + COMPOSER))
        self.assertTrue(tui_state.needs_input(
            "codex-openai", CAPACITY + "\n› Retry\n• Working\n" + CAPACITY + COMPOSER))

    def test_capacity_recovered_or_retrying_does_not_block(self):
        screens = [
            "Tool returned: Selected model is at capacity." + COMPOSER,
            CAPACITY + "\n• Working (2s • esc to interrupt)" + COMPOSER,
            CAPACITY + "\n• Retried successfully; continuing analysis." + COMPOSER,
            CAPACITY + COMPOSER + "\n• Working (2s • esc to interrupt)",
            CAPACITY + "\n› Continue\n• Done." + COMPOSER,
            "", CAPACITY,
        ]
        for screen in screens:
            with self.subTest(screen=screen):
                self.assertFalse(tui_state.needs_input("codex-openai", screen))
        self.assertFalse(tui_state.needs_input("claude-anthropic", CAPACITY + COMPOSER))

    def test_codex_request_user_input(self):
        screen = """
        Question 1/3 (3 unanswered)
        Quale soluzione vuoi usare?
        option 1/4 | tab to add notes
        enter to submit answer
        ←/→ to navigate questions
        esc to interrupt
        """
        self.assertTrue(tui_state.needs_input("codex-openai", screen))

    def test_claude_ask_user_question(self):
        screen = """
        4. Type something.
        Enter to select · ↑/↓ to navigate · Esc to cancel
        """
        self.assertTrue(tui_state.needs_input("claude-anthropic", screen))

    def test_opencode_question(self):
        screen = """
        3. Type your own answer
        ↑↓ select   enter submit   esc dismiss
        """
        self.assertTrue(tui_state.needs_input("opencode-universal", screen))

    def test_output_that_only_mentions_controls_is_not_a_dialog(self):
        prose = "Documented tab to add notes and enter to submit for Codex."
        self.assertFalse(tui_state.needs_input("codex-openai", prose))
        self.assertFalse(tui_state.needs_input("claude-anthropic", "Type something."))
        self.assertFalse(tui_state.needs_input("opencode-universal", "Type your own answer"))


class CapacityLifecycleTests(unittest.TestCase):
    """Run real controller functions with SQLite and a fixture pane, no service.

    Load only the functions under test so the source check stays stdlib-only
    and cannot run FastAPI startup hooks or read the live database.
    """
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE sessions (id TEXT, lifecycle TEXT, lifecycle_at TEXT)")
        self.conn.execute("INSERT INTO sessions VALUES ('example', 'RUNNING', '')")
        self.screen = CAPACITY + COMPOSER
        self.row = dict(id="example", profile_id="codex-openai", alive=True,
                        status="running", kind="agent", lifecycle="RUNNING")
        self.env = dict(
            tui_state=tui_state, sqlite3=sqlite3, db=lambda: self.conn, now=lambda: "now",
            pane_screen_text=lambda row: self.screen, match_known_error=lambda row: "",
            inactivity_age=lambda row, last: 300,
            controller_config=lambda: dict(stall_seconds=900, nudge_seconds=240,
                                          resume_max_attempts=8),
            LIFECYCLE_LABEL={}, PUSH_LIFECYCLES=("NEEDS_INPUT",),
            dismiss_webpush_session_async=mock.Mock(),
            add_message=mock.Mock(), deliver_async=mock.Mock(),
            nudge_already_sent=lambda *args: False, SENT="sent", RESENT="resent",
            FAILED="failed",
        )
        source = Path(__file__).resolve().parents[1] / "app/main.py"
        names = {"apply_lifecycle", "report_is_current", "maybe_nudge", "maybe_resume_after_reset"}
        tree = ast.parse(source.read_text())
        subset = ast.Module(body=[node for node in tree.body
                                  if isinstance(node, ast.FunctionDef) and node.name in names],
                            type_ignores=[])
        exec(compile(subset, str(source), "exec"), self.env)

    def tearDown(self):
        self.conn.close()

    def test_capacity_persists_attention_without_nudge_or_automatic_resume(self):
        last = dict(status="sent", created_at="2026-09-01")
        self.assertEqual(self.env["apply_lifecycle"](self.row, last), "NEEDS_INPUT")
        self.assertEqual(self.conn.execute("SELECT lifecycle FROM sessions").fetchone()[0],
                         "NEEDS_INPUT")
        self.assertFalse(self.row["lifecycle_reported"])
        self.assertFalse(self.env["maybe_nudge"](self.row, last))
        self.assertFalse(self.env["maybe_resume_after_reset"](self.row))
        self.env["add_message"].assert_not_called()
        self.env["deliver_async"].assert_not_called()
        self.screen = CAPACITY + "\n• Working (2s • esc to interrupt)" + COMPOSER
        self.assertEqual(self.env["apply_lifecycle"](self.row, last), "RUNNING")
        self.env["dismiss_webpush_session_async"].assert_called_once_with("example")

    def test_report_and_process_exit_remain_authoritative(self):
        self.row.update(report_status="COMPLETED", reported_at="2026-09-02")
        self.assertEqual(self.env["apply_lifecycle"](self.row, {}), "COMPLETED")
        self.row.update(alive=False, report_status="", reported_at="", exit_code="1")
        self.assertEqual(self.env["apply_lifecycle"](self.row, {}), "CRASHED")


if __name__ == "__main__":
    unittest.main()
