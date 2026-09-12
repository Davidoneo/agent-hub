#!/usr/bin/env python3
"""Contratti dei lettori usage OAuth di Claude e Codex."""
import importlib.machinery
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "session_ctl_claude", str(ROOT / "libexec" / "session-ctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
session_ctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(session_ctl)


class ClaudeRefreshTests(unittest.TestCase):
    def credentials(self, home, expires, refresh=True):
        path = Path(home) / ".claude" / ".credentials.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "old-token", "refreshToken": "refresh" if refresh else "",
            "expiresAt": expires,
        }}), encoding="utf-8")
        return path

    def test_expired_renewable_token_uses_safe_refresh_result(self):
        with tempfile.TemporaryDirectory() as home:
            path = self.credentials(home, 1)

            def renew(_path, _previous):
                path.write_text(json.dumps({"claudeAiOauth": {
                    "accessToken": "new-token", "refreshToken": "refresh",
                    "expiresAt": int((time.time() + 3600) * 1000),
                }}), encoding="utf-8")
                return True

            with mock.patch.object(session_ctl, "me", return_value=SimpleNamespace(pw_dir=home)), \
                 mock.patch.object(session_ctl, "CLAUDE_AUTO_REFRESH", True), \
                 mock.patch.object(session_ctl, "_refresh_claude_oauth", side_effect=renew) as refresh:
                token, renewable, error = session_ctl._claude_oauth()
            self.assertEqual(token, "new-token")
            self.assertTrue(renewable)
            self.assertEqual(error, "")
            refresh.assert_called_once()

    def test_token_without_refresh_credential_never_starts_claude(self):
        with tempfile.TemporaryDirectory() as home:
            self.credentials(home, 1, refresh=False)
            with mock.patch.object(session_ctl, "me", return_value=SimpleNamespace(pw_dir=home)), \
                 mock.patch.object(session_ctl, "CLAUDE_AUTO_REFRESH", True), \
                 mock.patch.object(session_ctl, "_refresh_claude_oauth") as refresh:
                token, renewable, error = session_ctl._claude_oauth()
            self.assertEqual(token, "")
            self.assertFalse(renewable)
            self.assertIn("token locale scaduto", error)
            refresh.assert_not_called()

    def test_successful_older_refresh_does_not_block_the_next_expiry(self):
        # Il tentativo che ha creato il token corrente precede per definizione
        # la sua scadenza: non e' un fallimento recente da limitare.
        self.assertFalse(session_ctl._claude_refresh_throttled(
            last_attempt=1000, current_expiry=2000 * 1000, moment=2100))

    def test_recent_attempt_after_expiry_is_throttled(self):
        self.assertTrue(session_ctl._claude_refresh_throttled(
            last_attempt=2100, current_expiry=2000 * 1000, moment=2200))


class CodexUsageTests(unittest.TestCase):
    def test_model_specific_primary_and_secondary_windows_are_preserved(self):
        with tempfile.TemporaryDirectory() as home:
            auth = Path(home) / ".codex" / "auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(json.dumps({"tokens": {
                "access_token": "token", "account_id": "account",
            }}), encoding="utf-8")
            payload = {
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 18000, "used_percent": 10,
                        "reset_at": time.time() + 3600,
                    },
                    "secondary_window": {
                        "limit_window_seconds": 604800, "used_percent": 20,
                        "reset_at": time.time() + 86400,
                    },
                },
                "additional_rate_limits": [{
                    "limit_name": "GPT-Spark",
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 18000, "used_percent": 30,
                            "reset_at": time.time() + 3600,
                        },
                        "secondary_window": {
                            "limit_window_seconds": 604800, "used_percent": 40,
                            "reset_at": time.time() + 86400,
                        },
                    },
                }],
                "plan_type": "pro",
            }
            with mock.patch.object(session_ctl, "me",
                                   return_value=SimpleNamespace(pw_dir=home)), \
                 mock.patch.object(session_ctl, "_usage_get",
                                   return_value=(payload, "", True)):
                result = session_ctl._usage_codex()

        self.assertEqual([w["label"] for w in result["windows"]], [
            "5 ore", "7 giorni", "GPT-Spark (5 ore)", "GPT-Spark (7 giorni)",
        ])
        self.assertEqual([w["percent"] for w in result["windows"]],
                         [10.0, 20.0, 30.0, 40.0])


if __name__ == "__main__":
    unittest.main()
