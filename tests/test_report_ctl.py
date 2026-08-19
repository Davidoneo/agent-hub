#!/usr/bin/env python3
"""Regression tests for the agent-report privilege boundary."""

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / "libexec" / "report-ctl"


class ReportCtlBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db = root / "agent-hub.sqlite3"
        self.reports = root / "reports"
        self.reports.mkdir()
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, unix_user TEXT NOT NULL,
                    report_status TEXT DEFAULT '', report_summary TEXT DEFAULT '',
                    reported_at TEXT DEFAULT '', waiting_for_session TEXT DEFAULT ''
                );
                CREATE TABLE session_reports (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                    summary TEXT NOT NULL, waiting_for_session TEXT NOT NULL,
                    reported_at TEXT NOT NULL, source TEXT NOT NULL, unix_user TEXT NOT NULL
                );
                INSERT INTO sessions (id,name,unix_user) VALUES
                    ('aaaaaa','project','devagent'),
                    ('bbbbbb','server','hostagent');
            """)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_ctl(self, caller, *args):
        env = os.environ.copy()
        env.update({
            "AGENT_HUB_DB": str(self.db),
            "AGENT_HUB_REPORTS": str(self.reports),
        })
        if caller is None:
            env.pop("SUDO_USER", None)
        else:
            env["SUDO_USER"] = caller
        return subprocess.run(
            [str(CTL), *args], env=env, text=True, capture_output=True,
            check=False,
        )

    def test_owner_can_report_and_identity_is_derived(self):
        result = self.run_ctl(
            "devagent", "report", "aaaaaa", "COMPLETED", "  lavoro   finito  ", "",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["unix_user"], "devagent")
        self.assertEqual(payload["source"], "agent-report")
        self.assertEqual(payload["summary"], "lavoro finito")
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT sessions.report_status,session_reports.unix_user,"
                "session_reports.source FROM sessions "
                "JOIN session_reports ON sessions.id=session_reports.session_id "
                "WHERE sessions.id='aaaaaa'"
            ).fetchone()
        self.assertEqual(row, ("COMPLETED", "devagent", "agent-report"))
        self.assertTrue((self.reports / "aaaaaa.json").is_file())

    def test_project_cannot_report_server_session(self):
        result = self.run_ctl(
            "devagent", "report", "bbbbbb", "COMPLETED", "falso", "",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("non appartenente", result.stderr)
        with sqlite3.connect(self.db) as conn:
            status = conn.execute(
                "SELECT report_status FROM sessions WHERE id='bbbbbb'"
            ).fetchone()[0]
        self.assertEqual(status, "")

    def test_report_requires_sudo_identity(self):
        result = self.run_ctl(
            None, "report", "aaaaaa", "COMPLETED", "senza identita'", "",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("chiamante sudo", result.stderr)

    def test_legacy_spoofable_arguments_are_rejected(self):
        result = self.run_ctl(
            "devagent", "report", "aaaaaa", "COMPLETED", "test",
            "fake-source", "hostagent", "",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("uso:", result.stderr)


if __name__ == "__main__":
    unittest.main()
