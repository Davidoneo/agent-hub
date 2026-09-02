#!/usr/bin/env python3
"""Privilege and staging contracts for explicit Telegram shares."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / "libexec" / "telegram-outbox-ctl"


class TelegramOutboxBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "hub.sqlite3"
        self.outbox = root / "outbox"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, unix_user TEXT)")
            conn.executemany("INSERT INTO sessions VALUES (?,?)", [
                ("aaaaaa", "devagent"), ("bbbbbb", "hostagent"),
            ])

    def tearDown(self):
        self.tmp.cleanup()

    def run_ctl(self, caller, sid, header, body=b""):
        env = os.environ.copy()
        env.update({"SUDO_USER": caller, "AGENT_HUB_DB": str(self.db),
                    "AGENT_HUB_TELEGRAM_OUTBOX": str(self.outbox)})
        payload = (json.dumps(header) + "\n").encode() + body
        return subprocess.run([str(CTL), "queue", sid], input=payload,
                              capture_output=True, env=env, check=False)

    def test_owner_can_queue_message_without_network(self):
        result = self.run_ctl("devagent", "aaaaaa", {"kind": "message", "text": "ciao"})
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT session_id,kind,text,status,requested_by FROM telegram_outbox"
            ).fetchone()
        self.assertEqual(row, ("aaaaaa", "message", "ciao", "pending", "devagent"))

    def test_project_cannot_queue_for_server_session(self):
        result = self.run_ctl("devagent", "bbbbbb", {"kind": "message", "text": "no"})
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"non appartenente", result.stderr)

    def test_file_bytes_are_copied_to_private_staging(self):
        result = self.run_ctl(
            "devagent", "aaaaaa", {"kind": "file", "filename": "report.pdf",
                                    "text": "richiesto"}, b"%PDF-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.db) as conn:
            path, name = conn.execute(
                "SELECT file_path,file_name FROM telegram_outbox").fetchone()
        self.assertEqual(name, "report.pdf")
        self.assertEqual(Path(path).read_bytes(), b"%PDF-test")
        self.assertEqual(path, str(Path(path).resolve()))


if __name__ == "__main__":
    unittest.main()
