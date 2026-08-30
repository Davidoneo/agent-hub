#!/usr/bin/env python3
"""Regression tests for the persistent, per-session delivery queue."""

import importlib.util
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_hub_delivery_queue", ROOT / "app" / "delivery_queue.py")
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


class DeliveryQueueTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, text TEXT,
                status TEXT, method TEXT, attempts INTEGER, created_at TEXT,
                delivered_at TEXT, last_error TEXT, note TEXT
            );
            INSERT INTO sessions VALUES ('session-a', 'queue test');
        """)

    def tearDown(self):
        self.conn.close()

    def add(self, mid, stamp, status="pending", method=""):
        self.conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mid, "session-a", "user", mid, status, method, 0, stamp, "", "", ""))
        self.conn.commit()

    def test_restart_requeues_paste_but_not_argv_prompt(self):
        self.add("paste", "1", queue.LAUNCHING, "paste")
        self.add("argv", "2", queue.LAUNCHING, "cli-arg")
        queue.recover_pending(self.conn)
        self.conn.commit()
        states = dict(self.conn.execute("SELECT id,status FROM messages"))
        self.assertEqual(states, {"paste": queue.PENDING, "argv": queue.SENT})

    def test_one_message_per_session_is_claimed_in_order(self):
        self.add("first", "1")
        self.add("second", "2")
        _row, claimed = queue.claim_next(self.conn, "session-a")
        self.conn.commit()
        self.assertEqual(claimed["id"], "first")
        self.assertEqual(queue.claim_next(self.conn, "session-a"), (None, None))
        self.conn.execute("UPDATE messages SET status=? WHERE id='first'", (queue.SENT,))
        self.conn.commit()
        _row, claimed = queue.claim_next(self.conn, "session-a")
        self.assertEqual(claimed["id"], "second")

    def test_failed_predecessor_blocks_later_message(self):
        self.add("first", "1", queue.FAILED)
        self.add("second", "2")
        self.assertEqual(queue.claim_next(self.conn, "session-a"), (None, None))
        state = self.conn.execute(
            "SELECT status FROM messages WHERE id='second'").fetchone()[0]
        self.assertEqual(state, queue.PENDING)
        self.assertEqual(queue.pending_sessions(self.conn), [])

    def test_special_launch_job_blocks_normal_queue_until_done(self):
        self.add("setup", "1", queue.PENDING, "setup")
        self.add("normal", "2")
        self.assertEqual(queue.pending_sessions(self.conn), [])
        self.conn.execute("UPDATE messages SET status=? WHERE id='setup'", (queue.SENT,))
        self.conn.commit()
        self.assertEqual(queue.pending_sessions(self.conn), ["session-a"])


if __name__ == "__main__":
    unittest.main()
