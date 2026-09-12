#!/usr/bin/env python3
"""Backlog consumption at session launch, using real backend code and SQLite."""
import ast
import asyncio
import json
import re
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class HTTPException(Exception):
    def __init__(self, status_code, detail, headers=None):
        super().__init__(detail)
        self.status_code, self.detail, self.headers = status_code, detail, headers


class BacklogSessionTests(unittest.TestCase):
    def setUp(self):
        # Load the actual handlers without ASGI startup, host files or workers.
        tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        wanted = {
            "init_db", "migrate_db", "_create_session", "insert_session",
            "get_backlog_idea", "backlog_detail", "list_backlog", "backlog_payload",
            "remove_backlog_audio", "add_message", "set_message", "bump_attempt",
            "register_initial_prompt", "save_launch_info", "redact_argv",
        }
        selected = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in wanted]
        for node in selected:
            node.decorator_list = []
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        temp = tempfile.TemporaryDirectory(dir=ROOT / ".agent")
        self.addCleanup(temp.cleanup)
        self.audio = Path(temp.name) / "idea.ogg"
        self.audio.write_bytes(b"saved voice note")
        self.profile = {"id": "test", "allowed_users": ["projectagent", "serveragent"],
                        "permission_modes": {"full": {}}}
        self.backend = {
            "sqlite3": sqlite3, "Path": Path, "uuid": uuid, "json": json,
            "HTTPException": HTTPException, "PENDING": "pending",
            "FAILED": "delivery_failed", "LAUNCHING": "launching",
            "db": lambda: self.conn, "CONFIG": {"db": str(Path(temp.name) / "test.db")},
            "BACKLOG_AUDIO_DIR": Path(temp.name), "SECRET_RE": re.compile("secret"),
            "now": lambda: "2026-09-10T12:00:00+00:00",
            "UNIX_USERS": {"PROJECT": "projectagent", "SERVER": "serveragent"},
            "SLUG_RE": re.compile(r"^[a-z0-9-]+$"), "SERVER_HOME": temp.name,
            "account_session_default": lambda user: {
                "profile_id": "test", "model": "", "effort": ""},
            "profile_by_id": lambda pid: self.profile,
            "validate_workdir": lambda path, env: path,
            "validate_executor": lambda *args: (0, "", 0),
            "default_mode": lambda prof: "", "prompt_mode_for": lambda prof: "paste",
            "documents_by_ids": lambda ids: [],
            "with_contract": lambda row, prompt, **kwargs: prompt,
            "start_tmux_session": mock.Mock(return_value={}),
            "wake_session_delivery": mock.Mock(), "confirm_argv_async": mock.Mock(),
            "launch_extras_async": mock.Mock(), "GOAL_CONTRACT_RESERVE": 0,
        }
        for name in ("model", "effort", "mode", "goal"):
            self.backend["validate_" + name] = lambda prof, value, **kw: value or ""
        exec(compile(ast.Module(body=selected, type_ignores=[]), "app/main.py", "exec"),
             self.backend)
        self.backend["init_db"]()
        # Match an existing Hub database: receipt migration reads lifecycle
        # before adding its columns on a completely fresh database (out of scope).
        for column in ("lifecycle", "lifecycle_at"):
            self.conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT DEFAULT ''")
        self.backend["migrate_db"]()
        with self.conn:
            self.conn.execute("INSERT INTO projects(slug,path,source,created_at) "
                              "VALUES ('test',?,'local','now')", (temp.name,))
            for bid in ("chosen", "other"):
                self.conn.execute(
                    "INSERT INTO backlog_ideas(id,status,prepared_prompt,audio_path,"
                    "created_at,updated_at) VALUES (?,'ready','prepared',?,'now','now')",
                    (bid, str(self.audio) if bid == "chosen" else ""))
        self.body = {"backlog_id": "chosen", "project_slug": "test", "prompt": "edited"}

    def ideas(self):
        return [r[0] for r in self.conn.execute("SELECT id FROM backlog_ideas ORDER BY id")]

    def create(self, **changes):
        return self.backend["_create_session"](dict(self.body, **changes))

    def test_preparation_preserves_idea_and_does_not_launch(self):
        result = asyncio.run(self.backend["backlog_detail"]("chosen"))
        self.assertEqual(result["idea"]["prepared_prompt"], "prepared")
        self.assertEqual(self.ideas(), ["chosen", "other"])
        self.assertTrue(self.audio.exists())
        self.backend["start_tmux_session"].assert_not_called()

    def test_success_consumes_only_selected_idea_after_launch_and_keeps_prompt(self):
        def start(row, prompt, mode):
            self.assertEqual(self.ideas(), ["chosen", "other"])
            saved = self.conn.execute("SELECT * FROM sessions WHERE id=?", (row["id"],)).fetchone()
            self.assertEqual((saved["status"], saved["initial_prompt"]), ("starting", "edited"))
            self.assertTrue(self.audio.exists())
            return {}
        self.backend["start_tmux_session"].side_effect = start
        result = self.create()
        self.assertEqual(result["session"]["status"], "running")
        self.assertEqual(self.ideas(), ["other"])
        self.assertFalse(self.audio.exists())
        self.assertEqual(self.conn.execute("SELECT text FROM messages").fetchone()[0], "edited")
        self.assertEqual([r["id"] for r in asyncio.run(self.backend["list_backlog"]())["ideas"]],
                         ["other"])

    def test_failed_launch_preserves_idea_audio_and_saved_failed_session_for_retry(self):
        self.backend["start_tmux_session"].side_effect = HTTPException(400, "tmux failed")
        with self.assertRaises(HTTPException) as caught:
            self.create()
        self.assertEqual(caught.exception.headers["X-Agent-Hub-Error-Code"], "launch_failed")
        sid = caught.exception.headers["X-Agent-Hub-Session-Id"]
        saved = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        self.assertEqual((saved["status"], saved["initial_prompt"]), ("failed", "edited"))
        self.assertEqual(self.conn.execute("SELECT status FROM messages").fetchone()[0],
                         "delivery_failed")
        self.assertEqual(self.ideas(), ["chosen", "other"])
        self.assertTrue(self.audio.exists())
        self.backend["start_tmux_session"].side_effect = None
        self.create()
        self.assertEqual(self.ideas(), ["other"])

    def test_validation_failure_preserves_backlog(self):
        with self.assertRaises(HTTPException):
            self.create(project_slug="")
        self.assertEqual(self.ideas(), ["chosen", "other"])
        self.assertTrue(self.audio.exists())
        self.backend["start_tmux_session"].assert_not_called()

    def test_missing_or_unprepared_idea_cannot_launch(self):
        with self.assertRaises(HTTPException) as caught:
            self.create(backlog_id="gone")
        self.assertEqual(caught.exception.status_code, 404)
        with self.conn:
            self.conn.execute("UPDATE backlog_ideas SET status='processing' WHERE id='chosen'")
        with self.assertRaises(HTTPException) as caught:
            self.create()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.ideas(), ["chosen", "other"])
        self.backend["start_tmux_session"].assert_not_called()

    def test_fallback_idea_and_no_initial_prompt_are_consumed_on_server_launch(self):
        with self.conn:
            self.conn.execute("UPDATE backlog_ideas SET status='ready_fallback' WHERE id='chosen'")
        result = self.create(environment="SERVER", prompt="")
        self.assertEqual(result["delivery"], "none")
        self.assertEqual(self.ideas(), ["other"])

    def test_normal_session_does_not_consume_backlog(self):
        body = dict(self.body)
        body.pop("backlog_id")
        self.backend["_create_session"](body)
        self.assertEqual(self.ideas(), ["chosen", "other"])
        self.assertTrue(self.audio.exists())

    def test_goal_setup_also_consumes_idea_after_launch(self):
        result = self.create(prompt_as_goal=True)
        self.assertEqual(result["delivery"], "goal")
        self.assertEqual(self.ideas(), ["other"])
        self.assertEqual(result["session"]["goal"], "edited")
        self.backend["launch_extras_async"].assert_called_once()

    def test_later_delivery_failure_keeps_prompt_in_launched_session(self):
        result = self.create()
        self.backend["set_message"](result["message_id"], status="delivery_failed")
        self.assertEqual(self.ideas(), ["other"])
        saved = self.conn.execute("SELECT initial_prompt,status FROM sessions").fetchone()
        self.assertEqual(tuple(saved), ("edited", "running"))
        self.assertEqual(self.conn.execute("SELECT text FROM messages").fetchone()[0], "edited")

    def test_database_failure_rolls_back_consumption_and_running_state(self):
        self.conn.execute("CREATE TRIGGER fail_consume BEFORE DELETE ON backlog_ideas "
                          "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.create()
        self.assertEqual(self.ideas(), ["chosen", "other"])
        self.assertTrue(self.audio.exists())
        self.assertEqual(self.conn.execute("SELECT status FROM sessions").fetchone()[0], "starting")


if __name__ == "__main__":
    unittest.main()
