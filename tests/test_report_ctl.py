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
                    environment TEXT NOT NULL,
                    report_status TEXT DEFAULT '', report_summary TEXT DEFAULT '',
                    reported_at TEXT DEFAULT '', waiting_for_session TEXT DEFAULT ''
                );
                CREATE TABLE session_reports (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                    summary TEXT NOT NULL, waiting_for_session TEXT NOT NULL,
                    reported_at TEXT NOT NULL, source TEXT NOT NULL, unix_user TEXT NOT NULL
                );
                INSERT INTO sessions (id,name,unix_user,environment) VALUES
                    ('aaaaaa','project','devagent','PROJECT'),
                    ('bbbbbb','server','serveragent','SERVER');
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

    def test_project_host_action_creates_telegram_approval_request(self):
        result = self.run_ctl(
            "devagent", "report", "aaaaaa", "NEEDS_HOST_ACTION",
            "Installare il pacchetto host X, senza modificare la rete", "",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["escalation_request_id"])
        with sqlite3.connect(self.db) as conn:
            request = conn.execute(
                "SELECT session_id,scope,status FROM escalation_requests"
            ).fetchone()
        self.assertEqual(request, (
            "aaaaaa", "Installare il pacchetto host X, senza modificare la rete", "pending"))

    def test_server_host_action_does_not_request_another_escalation(self):
        result = self.run_ctl(
            "serveragent", "report", "bbbbbb", "NEEDS_HOST_ACTION", "serve presenza fisica", "",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["escalation_request_id"], "")

    def test_waiting_session_records_structured_dependency(self):
        result = self.run_ctl(
            "devagent", "report", "aaaaaa", "WAITING_SESSION",
            "attendo la sessione server", "bbbbbb",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["waiting_for_session"], "bbbbbb")
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT report_status,waiting_for_session FROM sessions "
                "WHERE id='aaaaaa'"
            ).fetchone()
        self.assertEqual(row, ("WAITING_SESSION", "bbbbbb"))

    def test_waiting_session_rejects_missing_target(self):
        result = self.run_ctl(
            "devagent", "report", "aaaaaa", "WAITING_SESSION",
            "attendo una sessione assente", "cccccc",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("sessione attesa inesistente", result.stderr)

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
