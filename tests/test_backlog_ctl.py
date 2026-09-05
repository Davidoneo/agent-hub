#!/usr/bin/env python3
"""Contratti del preparatore effimero per le idee del Backlog."""
import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "backlog_ctl", str(ROOT / "libexec" / "backlog-ctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
backlog_ctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(backlog_ctl)


class BacklogCtlTests(unittest.TestCase):
    def test_schema_requires_metadata_and_unix_environment(self):
        self.assertEqual(set(backlog_ctl.SCHEMA["required"]),
                         {"title", "description", "prepared_prompt", "environment"})
        self.assertEqual(backlog_ctl.SCHEMA["properties"]["environment"]["enum"],
                         ["PROJECT", "SERVER"])
        self.assertFalse(backlog_ctl.SCHEMA["additionalProperties"])

    def test_codex_is_ephemeral_read_only_and_receives_idea_on_stdin(self):
        payload = {"title": "Titolo", "description": "Descrizione",
                   "prepared_prompt": "Prompt operativo", "environment": "SERVER"}

        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertEqual(command[-1], "-")
            self.assertIn("<idea>\nprova idea\n</idea>", kwargs["input"])
            self.assertIn("SERVER soltanto", kwargs["input"])
            self.assertIn("ogni caso ambiguo", kwargs["input"])
            return mock.Mock(returncode=0, stderr="")

        with mock.patch("sys.argv", ["backlog-ctl", "prepare"]), \
             mock.patch("sys.stdin.read", return_value="prova idea"), \
             mock.patch.object(backlog_ctl.subprocess, "run", side_effect=fake_run), \
             mock.patch("sys.stdout") as stdout:
            self.assertEqual(backlog_ctl.main(), 0)
        rendered = "".join(str(c.args[0]) for c in stdout.write.call_args_list if c.args)
        self.assertIn('"title": "Titolo"', rendered)
        self.assertIn('"environment": "SERVER"', rendered)

    def test_rejects_an_environment_outside_the_privilege_boundary(self):
        payload = {"title": "Titolo", "description": "Descrizione",
                   "prepared_prompt": "Prompt operativo", "environment": "hostagent"}

        def fake_run(command, **_kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(payload), encoding="utf-8")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch("sys.argv", ["backlog-ctl", "prepare"]), \
             mock.patch("sys.stdin.read", return_value="prova idea"), \
             mock.patch.object(backlog_ctl.subprocess, "run", side_effect=fake_run), \
             mock.patch("sys.stderr") as stderr:
            self.assertEqual(backlog_ctl.main(), 1)
        rendered = "".join(str(c.args[0]) for c in stderr.write.call_args_list if c.args)
        self.assertIn("environment non valido", rendered)


if __name__ == "__main__":
    unittest.main()
