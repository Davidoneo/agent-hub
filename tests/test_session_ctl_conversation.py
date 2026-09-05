#!/usr/bin/env python3
"""La conversazione della harness e' l'unica trascrizione completa.

Claude Code e Codex disegnano nel buffer alternativo del terminale, che non ha
scrollback: dal log PTY si recupera poco piu' dell'ultima schermata. Questi
test fissano il contratto della sorgente che legge invece i file di stato che
le due CLI scrivono a ogni turno.
"""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "session_ctl_conversation", str(ROOT / "libexec" / "session-ctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
session_ctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(session_ctl)


def write_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


CLAUDE = [
    {"type": "mode", "mode": "normal"},
    {"type": "user", "timestamp": "2026-09-05T09:00:00.000Z",
     "message": {"role": "user", "content":
                 "Sistema la trascrizione\n<system-reminder>promemoria</system-reminder>"}},
    {"type": "assistant", "timestamp": "2026-09-05T09:00:05.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "ragiono"},
         {"type": "text", "text": "Guardo i log."},
         {"type": "tool_use", "id": "t1", "name": "Bash",
          "input": {"command": "ls /tmp", "description": "Elenca /tmp"}}]}},
    {"type": "user", "timestamp": "2026-09-05T09:00:06.000Z",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "t1",
          "content": [{"type": "text", "text": "\n".join(f"riga {i}" for i in range(60))}]}]}},
    {"type": "assistant", "timestamp": "2026-09-05T09:00:09.000Z", "isSidechain": True,
     "message": {"role": "assistant", "content": [{"type": "text", "text": "sono un subagente"}]}},
]

CODEX = [
    {"timestamp": "2026-09-05T09:00:00.000Z", "type": "session_meta",
     "payload": {"cwd": "/srv/progetto"}},
    {"timestamp": "2026-09-05T09:00:01.000Z", "type": "event_msg",
     "payload": {"type": "item_completed", "item": {
         "type": "UserMessage", "content": [{"type": "text", "text": "Compila il progetto"}]}}},
    {"timestamp": "2026-09-05T09:00:04.000Z", "type": "event_msg",
     "payload": {"type": "item_completed", "item": {
         "type": "AgentMessage", "content": [{"type": "Text", "text": "Avvio la build."}]}}},
    {"timestamp": "2026-09-05T09:00:07.000Z", "type": "event_msg",
     "payload": {"type": "item_completed", "item": {
         "type": "CommandExecution", "command": ["/bin/bash", "-lc", "make all"],
         "exit_code": 2, "aggregated_output": "errore di compilazione"}}},
]


class ClaudeConversationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "claude.jsonl")
        write_jsonl(self.path, CLAUDE)
        self.addCleanup(self.dir.cleanup)

    def render(self, detail):
        return session_ctl._claude_conversation(self.path, detail)

    def test_text_detail_keeps_only_the_messages(self):
        out = self.render("text")
        self.assertIn("[Utente · ", out)
        self.assertIn("Sistema la trascrizione", out)
        self.assertIn("Guardo i log.", out)
        self.assertNotIn("[tool]", out)
        self.assertNotIn("[out]", out)
        # i promemoria di sistema non sono testo scritto dall'utente
        self.assertNotIn("promemoria", out)

    def test_full_detail_adds_tools_and_truncates_their_output(self):
        out = self.render("full")
        self.assertIn("[tool] Bash — Elenca /tmp", out)
        self.assertIn("ls /tmp", out)
        self.assertIn("[out]", out)
        self.assertIn("riga 0", out)
        self.assertIn(f"… (+{60 - session_ctl.CONV_OUT_LINES} righe)", out)
        self.assertNotIn("riga 59", out)
        # un subagente non e' la conversazione principale
        self.assertNotIn("sono un subagente", out)

    def test_verbose_detail_truncates_nothing(self):
        out = self.render("verbose")
        self.assertIn("riga 59", out)
        self.assertNotIn("righe)", out)
        self.assertIn("ragiono", out)
        self.assertIn("promemoria", out)
        self.assertIn("sono un subagente", out)

    def test_missing_transcript_is_empty_not_an_error(self):
        self.assertEqual(session_ctl._claude_jsonl("nessuna-sessione"), "")


class CodexConversationTests(unittest.TestCase):
    def test_rollout_becomes_readable_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rollout.jsonl")
            write_jsonl(path, CODEX)
            out = session_ctl._codex_conversation(path, "full")
        self.assertIn("Compila il progetto", out)
        self.assertIn("[Codex · ", out)
        self.assertIn("Avvio la build.", out)
        # il wrapper `bash -lc` non e' il comando che l'utente vuole leggere
        self.assertIn("[tool] exec — exit 2", out)
        self.assertIn("make all", out)
        self.assertNotIn("/bin/bash", out)
        self.assertIn("[err]", out)
        self.assertIn("errore di compilazione", out)


class TranscriptSourceContract(unittest.TestCase):
    def setUp(self):
        self.backend = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.app = (ROOT / "app/static/app.js").read_text(encoding="utf-8")

    def test_auto_prefers_the_harness_conversation(self):
        auto = self.backend.split("def build_transcript(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('native = native_transcript(row, detail)', auto)
        self.assertLess(auto.index("native_transcript"), auto.index("tmux_transcript"))

    def test_chrome_stripping_never_touches_the_harness_conversation(self):
        self.assertIn('stripped = chrome == "hide" and used != "native"', self.backend)

    def test_the_page_can_choose_source_and_detail(self):
        self.assertIn('<option value="native">', self.app)
        self.assertIn('id="tr-detail"', self.app)
        self.assertIn("&detail=", self.app)

    def test_the_terminal_offers_a_copy_that_needs_no_selection(self):
        # Con il mouse tracking attivo la selezione col puntatore richiede
        # Maiusc: senza un pulsante il testo non e' copiabile da telefono.
        self.assertIn('id="term-copy"', self.app)
        self.assertIn('$("#term-copy").onclick', self.app)


if __name__ == "__main__":
    unittest.main()
