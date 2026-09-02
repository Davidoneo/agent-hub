#!/usr/bin/env python3
"""Telegram is on-demand except escalation, meetings and explicit shares."""
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


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, name TEXT, environment TEXT, profile_id TEXT,
            project_slug TEXT, lifecycle TEXT, lifecycle_at TEXT, exit_code TEXT,
            report_status TEXT, status TEXT, unix_user TEXT
        );
        CREATE TABLE session_reports (
            session_id TEXT, status TEXT, summary TEXT, reported_at TEXT, unix_user TEXT
        );
        CREATE TABLE notifications (
            channel TEXT, kind TEXT, key TEXT, sent_at TEXT, detail TEXT,
            PRIMARY KEY(channel, kind, key)
        );
        CREATE TABLE escalation_requests (
            id TEXT PRIMARY KEY, session_id TEXT, report_id TEXT, scope TEXT,
            status TEXT, host_session_id TEXT, requested_at TEXT, decided_at TEXT,
            decision_chat_id TEXT, error TEXT
        );
        CREATE TABLE telegram_outbox (
            id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, text TEXT,
            file_path TEXT, file_name TEXT, status TEXT, attempts INTEGER,
            last_error TEXT, requested_by TEXT, created_at TEXT, sent_at TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, text TEXT,
            status TEXT, method TEXT, attempts INTEGER, created_at TEXT
        );
        INSERT INTO sessions VALUES (
            'aaaaaa','Progetto','PROJECT','codex-openai','demo','NEEDS_HOST_ACTION',
            '2026-09-01T20:00:00+00:00','','NEEDS_HOST_ACTION','running','devagent'
        );
    """)
    return conn


CFG = {"notify": "reports,lifecycle,health", "origin": "", "verbosity": "compact",
       "quiet": "0", "active_pause_seconds": "120", "chat_id": "primary"}


class TelegramFlowTests(unittest.TestCase):
    def test_legacy_automatic_events_are_never_sent(self):
        conn = database()
        conn.execute("INSERT INTO session_reports VALUES (?,?,?,?,?)", (
            "aaaaaa", "NEEDS_HOST_ACTION", "scope", "2026-09-01", "devagent"))
        sent = []
        with mock.patch.object(telegram_ctl, "db", return_value=conn), \
             mock.patch.object(telegram_ctl, "send",
                               side_effect=lambda *a, **k: sent.append(a[2]) or True):
            count = telegram_ctl.deliver_pending(CFG, "primary")
        self.assertEqual(count, 0)
        self.assertEqual(sent, [])

    def test_escalation_request_has_yes_no_buttons(self):
        conn = database()
        conn.execute("INSERT INTO escalation_requests VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "req-1", "aaaaaa", "rep-1", "installare nft rule limitata", "pending",
            "", "2026-09-01T20:00:00+00:00", "", "", ""))
        captured = []
        with mock.patch.object(telegram_ctl, "db", return_value=conn), \
             mock.patch.object(telegram_ctl, "send_long",
                               side_effect=lambda *a, **k: captured.append((a, k)) or True):
            count = telegram_ctl.deliver_pending(CFG, "primary")
        self.assertEqual(count, 1)
        buttons = captured[0][1]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in buttons], ["ey:req-1", "en:req-1"])

    def test_explicit_message_is_delivered_and_marked(self):
        conn = database()
        conn.execute("INSERT INTO telegram_outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            "share-1", "aaaaaa", "message", "testo richiesto", "", "", "pending",
            0, "", "devagent", "2026-09-01T20:00:00+00:00", ""))
        sent = []
        with mock.patch.object(telegram_ctl, "db", return_value=conn), \
             mock.patch.object(telegram_ctl, "send_long",
                               side_effect=lambda *a, **k: sent.append(a[2]) or True):
            count = telegram_ctl.deliver_pending(CFG, "primary")
        self.assertEqual(count, 1)
        self.assertIn("testo richiesto", sent[0])
        self.assertEqual(conn.execute(
            "SELECT status FROM telegram_outbox WHERE id='share-1'").fetchone()[0], "sent")

    def test_denial_returns_a_durable_message_to_calling_session(self):
        conn = database()
        conn.execute("INSERT INTO escalation_requests VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "req-2", "aaaaaa", "rep-2", "scope limitato", "pending", "",
            "2026-09-01T20:00:00+00:00", "", "", ""))
        conn.commit()
        with mock.patch.object(telegram_ctl, "db", return_value=conn), \
             mock.patch.object(telegram_ctl, "send", return_value=True):
            telegram_ctl.handle_escalation_decision(CFG, {}, "primary", "req-2", False)
        self.assertEqual(conn.execute(
            "SELECT status FROM escalation_requests WHERE id='req-2'").fetchone()[0], "denied")
        message = conn.execute("SELECT kind,text,status FROM messages").fetchone()
        self.assertEqual(message["kind"], "escalation")
        self.assertIn("rifiutata", message["text"])
        self.assertEqual(message["status"], "pending")
        self.assertEqual(conn.execute(
            "SELECT lifecycle FROM sessions WHERE id='aaaaaa'").fetchone()[0], "RUNNING")

    def test_approval_is_queued_for_unprivileged_backend_processing(self):
        conn = database()
        conn.execute("INSERT INTO escalation_requests VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "req-3", "aaaaaa", "rep-3", "scope", "pending", "",
            "2026-09-01T20:00:00+00:00", "", "", ""))
        conn.commit()
        with mock.patch.object(telegram_ctl, "db", return_value=conn), \
             mock.patch.object(telegram_ctl, "send", return_value=True):
            telegram_ctl.handle_escalation_decision(CFG, {}, "primary", "req-3", True)
        row = conn.execute(
            "SELECT status,decision_chat_id FROM escalation_requests WHERE id='req-3'"
        ).fetchone()
        self.assertEqual(tuple(row), ("approved_queued", "primary"))


if __name__ == "__main__":
    unittest.main()
