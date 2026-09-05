#!/usr/bin/env python3
"""Contratti del bot Telegram dedicato al Backlog."""
import importlib.machinery
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "backlog_telegram_ctl", str(ROOT / "libexec" / "backlog-telegram-ctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
bot = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(bot)


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE backlog_ideas (
            id TEXT PRIMARY KEY, title TEXT DEFAULT '', description TEXT DEFAULT '',
            prepared_prompt TEXT DEFAULT '', raw_text TEXT DEFAULT '', source TEXT,
            source_ref TEXT UNIQUE, audio_path TEXT DEFAULT '', audio_name TEXT DEFAULT '',
            status TEXT, processor TEXT DEFAULT '', error TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
    """)
    return conn


CFG = {
    "token": "test-token", "chat_id": "primary", "pairing_code": "pair-secret",
    "origin": "https://hub.example", "poll_timeout": "1",
}


class BacklogTelegramTests(unittest.TestCase):
    def test_text_is_idempotent_per_telegram_update(self):
        conn = database()
        self.addCleanup(conn.close)
        with mock.patch.object(bot, "db", return_value=conn):
            first = bot.backlog_text("una nuova idea", 44)
            second = bot.backlog_text("una nuova idea", 44)
        self.assertEqual(first, second)
        row = conn.execute(
            "SELECT raw_text,source,status,source_ref FROM backlog_ideas").fetchone()
        self.assertEqual(tuple(row), (
            "una nuova idea", "telegram_text", "pending", "backlog-telegram:44"))

    def test_voice_audio_and_audio_documents_are_recognized(self):
        self.assertEqual(bot.audio_descriptor(
            {"voice": {"file_id": "v1", "file_size": 123}}),
            ("v1", "nota-vocale.ogg", 123))
        self.assertEqual(bot.audio_descriptor(
            {"audio": {"file_id": "a1", "file_name": "nota.m4a"}}),
            ("a1", "nota.m4a", 0))
        self.assertEqual(bot.audio_descriptor(
            {"document": {"file_id": "d1", "file_name": "idea.opus",
                          "mime_type": "audio/ogg"}}),
            ("d1", "idea.opus", 0))
        self.assertEqual(bot.audio_descriptor(
            {"document": {"file_id": "pdf", "mime_type": "application/pdf"}}),
            ("", "", 0))

    def test_pairing_requires_exact_start_code(self):
        self.assertTrue(bot._pairing_request("/start pair-secret", "pair-secret"))
        self.assertFalse(bot._pairing_request("pair-secret", "pair-secret"))
        self.assertFalse(bot._pairing_request("/start xxpair-secret", "pair-secret"))

        state = {}
        update = {"update_id": 1, "message": {
            "text": "/start pair-secret", "chat": {"id": "new-chat"}}}
        cfg = dict(CFG, chat_id="")
        with mock.patch.object(bot, "save_state"), mock.patch.object(bot, "send") as sent:
            bot.handle_update(cfg, state, update)
        self.assertEqual(state["chat_id"], "new-chat")
        sent.assert_called_once()

    def test_primary_text_and_voice_route_to_separate_collector(self):
        text_update = {"update_id": 71, "message": {
            "text": "idea libera", "chat": {"id": "primary"}}}
        with mock.patch.object(bot, "backlog_text", return_value="abcd1234") as queued, \
             mock.patch.object(bot, "send"):
            bot.handle_update(CFG, {}, text_update)
        queued.assert_called_once_with("idea libera", 71)

        voice_update = {"update_id": 72, "message": {
            "voice": {"file_id": "voice-72", "file_size": 100},
            "chat": {"id": "primary"}}}
        with mock.patch.object(bot, "download_audio", return_value="efgh5678") as downloaded, \
             mock.patch.object(bot, "send"):
            bot.handle_update(CFG, {}, voice_update)
        downloaded.assert_called_once_with(CFG, "voice-72", "nota-vocale.ogg", 100, 72)

    def test_unauthorized_chat_cannot_queue_an_idea(self):
        update = {"update_id": 9, "message": {
            "text": "idea ostile", "chat": {"id": "stranger"}}}
        with mock.patch.object(bot, "backlog_text") as queued, \
             mock.patch.object(bot, "send") as sent:
            bot.handle_update(CFG, {}, update)
        queued.assert_not_called()
        sent.assert_not_called()

    def test_audio_is_staged_privately_and_queued(self):
        conn = database()
        self.addCleanup(conn.close)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = [b"fake-ogg", b""]
        with tempfile.TemporaryDirectory() as tempdir, \
             mock.patch.object(bot, "AUDIO_DIR", Path(tempdir)), \
             mock.patch.object(bot, "db", return_value=conn), \
             mock.patch.object(bot, "call", return_value={"file_path": "voice/file.ogg"}), \
             mock.patch.object(bot.urllib.request, "urlopen", return_value=response):
            idea_id = bot.download_audio(CFG, "voice", "nota.ogg", 8, 88)
            row = conn.execute("SELECT * FROM backlog_ideas WHERE id=?", (idea_id,)).fetchone()
            staged = Path(row["audio_path"])
            self.assertEqual(staged.read_bytes(), b"fake-ogg")
            self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
            self.assertEqual(row["status"], "pending_transcription")
            self.assertEqual(row["source_ref"], "backlog-telegram:88")

    def test_config_initialization_defaults_to_free_local_transcription(self):
        with tempfile.TemporaryDirectory() as tempdir, \
             mock.patch.object(bot, "CONFIG_FILE", str(Path(tempdir) / "backlog.env")), \
             mock.patch.dict(os.environ, {"AGENT_HUB_BACKLOG_TG_ORIGIN":
                                          "https://hub.example"}, clear=False):
            self.assertEqual(bot.init_config(), 0)
            text = Path(bot.CONFIG_FILE).read_text(encoding="utf-8")
        self.assertIn("AGENT_HUB_BACKLOG_TG_TOKEN=", text)
        self.assertIn("AGENT_HUB_BACKLOG_TRANSCRIBE_PROVIDER=local", text)
        self.assertIn("AGENT_HUB_BACKLOG_OPENAI_API_KEY=", text)
        self.assertNotIn("AGENT_HUB_BACKLOG_TG_PAIRING_CODE=\n", text)

    def test_status_bot_has_no_backlog_capture_code(self):
        existing = (ROOT / "libexec" / "telegram-ctl").read_text(encoding="utf-8")
        self.assertNotIn("backlog_text", existing)
        self.assertNotIn("telegram_audio", existing)
        self.assertNotIn('(\"backlog\",', existing)

    def test_backend_uses_dedicated_config_and_never_auto_enables_paid_api(self):
        backend = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"/etc/agent-hub/backlog-telegram.env"', backend)
        self.assertIn('values = {"provider": "local"', backend)
        self.assertIn('if values["provider"] == "openai" and not values["api_key"]', backend)
        self.assertNotIn('AGENT_HUB_TELEGRAM_CONFIG", "/etc/agent-hub/telegram.env"', backend)

    def test_unit_is_unprivileged_and_has_narrow_writes(self):
        unit = (ROOT / "roles/agent_hub/templates/agent-hub-backlog-telegram.service.j2"
                ).read_text(encoding="utf-8")
        self.assertIn("User={{ agent_hub_service_user }}", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ReadWritePaths={{ agent_hub_state_dir }}", unit)


if __name__ == "__main__":
    unittest.main()
