#!/usr/bin/env python3
"""Regression tests for the persistent, per-session delivery queue."""

import importlib.util
import ast
import asyncio
import sqlite3
import unittest
from contextlib import contextmanager
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


class DeliveryCounterTests(unittest.TestCase):
    """Exercise the API queries with SQLite, without starting the live service."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE sessions (id TEXT, tmux_name TEXT, status TEXT,
                                   created_at TEXT, lifecycle TEXT);
            INSERT INTO sessions VALUES ('s1','pane1','running','1','RUNNING');
            INSERT INTO sessions VALUES ('s2','pane2','running','2','RUNNING');
            CREATE TABLE messages (session_id TEXT, kind TEXT, status TEXT);
        """)

        @contextmanager
        def db():
            yield self.conn

        self.api = dict(db=db, PENDING=queue.PENDING, LAUNCHING=queue.LAUNCHING,
                        FAILED=queue.FAILED, Request=object, sync_status=lambda: {},
                        decorate=lambda *_: None, identity=lambda _: "test",
                        ensure_notification_identity=lambda *_: None,
                        lifecycle_receipt_keys=lambda _: set(), PUSH_LIFECYCLES=set())
        tree = ast.parse((ROOT / "app/main.py").read_text())
        functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name in ("message_counters", "list_sessions")]
        for node in functions:
            node.decorator_list = []
        exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), self.api)

    def tearDown(self):
        self.conn.close()

    def counters(self):
        detail = self.api["message_counters"]("s1")
        listed = asyncio.run(self.api["list_sessions"](None))["sessions"]
        row = next(s for s in listed if s["id"] == "s1")
        self.assertEqual({k: row[k] for k in detail}, detail)
        return detail

    def test_queue_confirmation_failure_and_retry_are_distinct(self):
        self.assertEqual(self.counters(), dict(messages_total=0, undelivered=0, delivery_failed=0))
        self.conn.execute("INSERT INTO messages VALUES ('s1','user','pending')")
        for status, undelivered, failed in [
            ("pending", 1, 0), ("launching", 1, 0), ("sent", 0, 0),
            ("delivery_failed", 1, 1), ("pending", 1, 0),
            ("launching", 1, 0), ("manually_resent", 0, 0),
        ]:
            with self.subTest(status=status):
                self.conn.execute("UPDATE messages SET status=?", (status,))
                self.assertEqual(self.counters(), dict(
                    messages_total=1, undelivered=undelivered, delivery_failed=failed))

    def test_failed_predecessor_and_pending_successor_keep_separate_counts(self):
        self.conn.executemany("INSERT INTO messages VALUES (?,?,?)", [
            ("s1", "user", "delivery_failed"), ("s1", "user", "pending"),
            ("s1", "initial", "sent"), ("s1", "control", "delivery_failed"),
            ("s2", "user", "delivery_failed"),
        ])
        self.assertEqual(self.counters(), dict(messages_total=3, undelivered=2, delivery_failed=1))


if __name__ == "__main__":
    unittest.main()
