#!/usr/bin/env python3
"""Regressioni per gli avvisi Telegram che richiedono attenzione."""
import importlib.machinery
import importlib.util
import sqlite3
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "telegram_ctl", str(ROOT / "libexec" / "telegram-ctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
telegram_ctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(telegram_ctl)


class TelegramNeedsInputTests(unittest.TestCase):
    def test_repeated_questions_have_distinct_event_keys(self):
        first = {"id": "abc", "lifecycle": "NEEDS_INPUT", "lifecycle_at": "t1"}
        second = {"id": "abc", "lifecycle": "NEEDS_INPUT", "lifecycle_at": "t2"}
        self.assertNotEqual(
            telegram_ctl.lifecycle_event_key(first), telegram_ctl.lifecycle_event_key(second))

    def test_repeated_delivery_failures_have_distinct_event_keys(self):
        first = {"id": "abc", "lifecycle": "DELIVERY_FAILED", "lifecycle_at": "t1"}
        second = {"id": "abc", "lifecycle": "DELIVERY_FAILED", "lifecycle_at": "t2"}
        self.assertNotEqual(
            telegram_ctl.lifecycle_event_key(first), telegram_ctl.lifecycle_event_key(second))

    def test_needs_input_is_suppressed_while_ui_is_active(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE session_reports (
                session_id TEXT, status TEXT, summary TEXT, reported_at TEXT, unix_user TEXT
            );
            CREATE TABLE sessions (
                id TEXT, name TEXT, environment TEXT, profile_id TEXT, project_slug TEXT,
                lifecycle TEXT, lifecycle_at TEXT, exit_code TEXT, report_status TEXT
            );
            CREATE TABLE notifications (
                channel TEXT, kind TEXT, key TEXT, sent_at TEXT, detail TEXT,
                PRIMARY KEY(channel, kind, key)
            );
        """)
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", (
            "abc", "Scelta", "SERVER", "codex-openai", "", "NEEDS_INPUT",
            "2026-08-29T12:00:00+00:00", "", ""))
        conn.commit()
        sent = []
        cfg = {"notify": "lifecycle", "origin": "", "verbosity": "compact",
               "quiet": "0", "active_pause_seconds": "120"}
        with mock.patch.object(telegram_ctl, "db", return_value=conn), \
             mock.patch.object(telegram_ctl, "ui_is_active", return_value=True), \
             mock.patch.object(telegram_ctl, "send",
                               side_effect=lambda *a, **k: sent.append(a[2]) or True):
            count = telegram_ctl.deliver_pending(cfg, "chat")
        self.assertEqual(count, 0)
        self.assertEqual(sent, [])
        row = conn.execute(
            "SELECT detail FROM notifications WHERE channel='telegram'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("omesso: UI attiva", row["detail"])

    def test_delivery_failure_alerts_even_while_ui_is_active(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE session_reports (
                session_id TEXT, status TEXT, summary TEXT, reported_at TEXT, unix_user TEXT
            );
            CREATE TABLE sessions (
                id TEXT, name TEXT, environment TEXT, profile_id TEXT, project_slug TEXT,
                lifecycle TEXT, lifecycle_at TEXT, exit_code TEXT, report_status TEXT
            );
            CREATE TABLE notifications (
                channel TEXT, kind TEXT, key TEXT, sent_at TEXT, detail TEXT,
                PRIMARY KEY(channel, kind, key)
            );
        """)
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", (
            "abc", "Consegna", "SERVER", "claude-anthropic", "",
            "DELIVERY_FAILED", "2026-08-30T01:00:00+00:00", "", ""))
        conn.commit()
        sent = []
        cfg = {"notify": "lifecycle", "origin": "", "verbosity": "compact",
               "quiet": "0", "active_pause_seconds": "120"}
        with mock.patch.object(telegram_ctl, "db", return_value=conn), \
             mock.patch.object(telegram_ctl, "ui_is_active", return_value=True), \
             mock.patch.object(telegram_ctl, "send",
                               side_effect=lambda *a, **k: sent.append(a[2]) or True):
            count = telegram_ctl.deliver_pending(cfg, "chat")
        self.assertEqual(count, 1)
        self.assertIn("Messaggio non consegnato", sent[0])


if __name__ == "__main__":
    unittest.main()
