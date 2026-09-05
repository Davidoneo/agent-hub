#!/usr/bin/env python3
"""Contratti del riconoscimento del limite d'uso e della ripresa dopo il reset."""
import re
import unittest
from pathlib import Path

import yaml

from app import usage_limit


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = yaml.safe_load(
    (ROOT / "roles" / "agent_hub" / "defaults" / "main.yml").read_text(encoding="utf-8"))


def compiled(patterns: dict) -> dict:
    return {state: [re.compile(p) for p in (patterns.get(state) or [])]
            for state in usage_limit.STATES}


# Schermate reali, copiate dal pane come lo legge il controller: `capture-pane
# -p -J` restituisce testo senza sequenze ANSI e con le righe spezzate riunite.
CODEX_BANNER = """\
• Ran agent-report COMPLETED --summary "..."
■ You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to
  purchase more credits or try again at Sep 7th, 2026 9:33 AM.

  › Ask Codex to do anything   gpt-5.6-sol max · ~
"""

CODEX_PARKED_GOAL = """\
  » Ask Codex to do anything   gpt-5.6-sol ultra · /srv/agent-workspace/projects/x
                                          Goal hit usage limits (/goal resume)
"""

CLAUDE_ARMED_WAIT = """\
  Continuing automatically when your limit resets at 3:00 PM
  esc to cancel · /rate-limit-options for other choices
"""

# Lo stesso testo, ma prodotto da un agente che ne sta parlando. Non e' un caso
# di scuola: la sessione che ha introdotto questi pattern si e' segnalata da
# sola appena li ha stampati, ed e' per questo che sono ancorati.
AGENT_PROSE = """\
Ho trovato nei log tre sessioni con "You've hit your usage limit" e ho spiegato
che il footer mostra Goal hit usage limits finche' non lo riprendi.
"""

AGENT_PATTERN_LIST = (
    "patterns = ['(?m)^\\\\s*■ You've hit your usage limit', "
    "'Continuing automatically when your limit resets']\n")


class ShippedPatterns(unittest.TestCase):
    """I pattern installati dal ruolo devono riconoscere le schermate vere."""

    def setUp(self):
        self.compiled = compiled(DEFAULTS["agent_hub_controller_patterns"])

    def test_codex_banner_and_parked_goal_are_a_usage_limit(self):
        self.assertEqual(usage_limit.blocked_state(self.compiled, CODEX_BANNER),
                         "USAGE_LIMIT")
        self.assertEqual(usage_limit.blocked_state(self.compiled, CODEX_PARKED_GOAL),
                         "USAGE_LIMIT")

    def test_claude_armed_wait_is_a_usage_limit(self):
        self.assertEqual(usage_limit.blocked_state(self.compiled, CLAUDE_ARMED_WAIT),
                         "USAGE_LIMIT")

    def test_an_agent_quoting_the_message_is_not_a_state(self):
        self.assertEqual(usage_limit.blocked_state(self.compiled, AGENT_PROSE), "")

    def test_an_agent_printing_the_patterns_is_not_a_state(self):
        self.assertEqual(
            usage_limit.blocked_state(self.compiled, AGENT_PATTERN_LIST), "")

    def test_auth_required_ships_without_unverified_patterns(self):
        self.assertEqual(DEFAULTS["agent_hub_controller_patterns"]["AUTH_REQUIRED"], [])


class BlockedState(unittest.TestCase):
    def test_no_patterns_means_no_state(self):
        self.assertEqual(usage_limit.blocked_state({}, CODEX_BANNER), "")

    def test_no_screen_means_no_state(self):
        self.assertEqual(usage_limit.blocked_state(compiled(
            {"USAGE_LIMIT": ["limit"]}), ""), "")

    def test_a_stale_login_wins_over_a_usage_limit(self):
        both = compiled({"AUTH_REQUIRED": ["please run /login"],
                         "USAGE_LIMIT": ["usage limit"]})
        screen = "please run /login — usage limit reached"
        self.assertEqual(usage_limit.blocked_state(both, screen), "AUTH_REQUIRED")


class UsageCohort(unittest.TestCase):
    def test_half_completed_refresh_keeps_only_the_fresh_reading(self):
        old = {"last_checked": "2026-09-05T18:20:00+00:00", "percent": 20}
        new = {"last_checked": "2026-09-05T18:25:00+00:00", "percent": 21}
        self.assertEqual(usage_limit.freshest_usage_cohort([old, new]), [new])

    def test_coeval_different_accounts_remain_visible(self):
        first = {"last_checked": "2026-09-05T18:25:00+00:00", "percent": 20}
        second = {"last_checked": "2026-09-05T18:25:02+00:00", "percent": 70}
        self.assertEqual(
            usage_limit.freshest_usage_cohort([first, second]),
            [first, second],
        )


class NextReset(unittest.TestCase):
    def test_windows_without_an_instant_say_nothing(self):
        self.assertEqual(usage_limit.next_reset(
            [{"label": "sessione (5 ore)", "percent": 0.0, "resets_at": 0}], 1000), 0)

    def test_an_instant_older_than_the_block_describes_the_past(self):
        self.assertEqual(usage_limit.next_reset([{"resets_at": 900}], 1000), 0)

    def test_the_first_reset_after_the_block_wins(self):
        windows = [{"resets_at": 5000}, {"resets_at": 2000}, {"resets_at": 900}]
        self.assertEqual(usage_limit.next_reset(windows, 1000), 2000)

    def test_a_malformed_window_is_ignored_instead_of_raising(self):
        self.assertEqual(usage_limit.next_reset(
            [{"resets_at": "domani"}, None, {"resets_at": 2000}], 1000), 2000)


class ResumeDue(unittest.TestCase):
    def call(self, **over):
        args = {"now": 0.0, "blocked_at": 1000.0, "windows": [{"resets_at": 2000}],
                "attempts": 0, "last_attempt_at": 0.0, "retry_seconds": 900,
                "blind_seconds": 3600, "max_attempts": 8}
        args.update(over)
        return usage_limit.resume_due(**args)

    def test_nothing_happens_before_the_announced_reset(self):
        self.assertFalse(self.call(now=1999.0))

    def test_the_announced_reset_alone_is_not_enough(self):
        # il margine lascia muovere per prima la ripresa nativa della harness
        self.assertFalse(self.call(now=2500.0))

    def test_after_the_reset_plus_the_margin_it_is_due(self):
        self.assertTrue(self.call(now=2900.0))

    def test_without_a_known_instant_it_waits_the_blind_delay(self):
        self.assertFalse(self.call(now=4000.0, windows=[{"resets_at": 0}]))
        self.assertTrue(self.call(now=5500.0, windows=[{"resets_at": 0}]))

    def test_a_recent_attempt_holds_the_next_one(self):
        self.assertFalse(self.call(now=3000.0, attempts=1, last_attempt_at=2900.0))
        self.assertTrue(self.call(now=3801.0, attempts=1, last_attempt_at=2900.0))

    def test_the_attempts_are_a_closed_number(self):
        self.assertFalse(self.call(now=100000.0, attempts=8))

    def test_zero_attempts_disables_the_resume(self):
        self.assertFalse(self.call(now=100000.0, max_attempts=0))


if __name__ == "__main__":
    unittest.main()
