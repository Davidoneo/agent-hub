#!/usr/bin/env python3
"""Contratti per le richieste di input mostrate dalle harness supportate."""
import unittest

from app import tui_state


class TuiStateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
