"""Real ASGI app/SQLite/files; replace only host/provider and background work.

Never run against live data. The caller must use the audit filesystem/network
namespace. The app is imported with its real startup, on a synthetic existing DB.
"""
import ast
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch
import uuid

STAMP = "2026-09-12T12:00:00+00:00"
PROFILES = [{
    "id": "codex-openai", "label": "Codex", "harness": "codex", "command": "synthetic",
    "allowed_users": ["devagent", "hostagent"], "supports_model": True,
    "supports_effort": True, "effort_levels": ["low", "medium", "high"],
    "default_effort": "high", "permission_modes": {"full": [], "standard": []},
    "default_permission_mode": "full", "prompt_arg": {"mode": "positional"},
    "modes": {"default": "default", "options": [{"id": "default", "label": "Normale"},
                                                     {"id": "plan", "label": "Plan"}]},
    "goal": {"command": "/goal", "max_length": 10000},
}, {
    "id": "claude-anthropic", "label": "Claude", "harness": "claude", "command": "synthetic",
    "allowed_users": ["devagent", "hostagent"], "supports_model": True,
    "supports_effort": True, "effort_levels": ["low", "medium", "high"],
    "default_effort": "medium", "permission_modes": {"full": []},
    "prompt_arg": {"mode": "none"}, "modes": {"options": []},
}, {"id": "minimal", "label": "Minimal", "harness": "other", "command": "synthetic",
    "allowed_users": ["devagent"], "supports_model": False, "supports_effort": False,
    "permission_modes": {"full": []}, "prompt_arg": {"mode": "none"}}]


class AuditBackend:
    def __init__(self, source, work):
        self.source, self.work = Path(source), Path(work)
        self.events, self.alive, self.fail_start = [], {}, False
        self.work.mkdir(parents=True, exist_ok=True)
        for name in ("projects/demo", "projects/second", "uploads", "logs", "knowledge", "server"):
            (self.work / name).mkdir(parents=True, exist_ok=True)
        profile_file = self.work / "profiles.json"
        profile_file.write_text(json.dumps({"profiles": PROFILES}))
        env = {"AGENT_HUB_" + key: str(value) for key, value in {
            "DB": self.work / "audit.sqlite3", "PROFILES": profile_file,
            "PROJECTS": self.work / "projects", "UPLOADS": self.work / "uploads",
            "LOGS": self.work / "logs", "KNOWLEDGE": self.work / "knowledge",
            "REPORTS": self.work / "reports", "PRESENCE": self.work / "presence.json",
            "CONTROLLER": self.work / "controller.json", "HEALTH_DIR": self.work / "health",
            "HARNESS_UPDATE_STATE": self.work / "harness.json",
            "BACKLOG_AUDIO_DIR": self.work / "audio", "WHISPER_CACHE": self.work / "whisper",
            "PROJECT_USER": "devagent", "SERVER_USER": "hostagent", "SERVICE_USER": "sandbox",
            "SERVER_HOME": self.work / "server", "REQUIRE_TAILSCALE": "0",
            "MEETING_WORKERS": "0", "BACKLOG_WORKERS": "0", "WEBPUSH_ENABLED": "0",
            "MAX_UPLOAD": 1048576,
        }.items()}
        # Emulate an existing installation. This schema prerequisite is also
        # present in test_backlog_sessions: receipt migration predates lifecycle.
        tree = ast.parse((self.source / "app/main.py").read_text())
        init = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "init_db")
        ns = {"Path": Path, "CONFIG": {"db": env["AGENT_HUB_DB"]},
              "db": lambda: sqlite3.connect(env["AGENT_HUB_DB"])}
        exec(compile(ast.Module(body=[init], type_ignores=[]), "init_db", "exec"), ns)
        ns["init_db"]()
        with sqlite3.connect(env["AGENT_HUB_DB"]) as db:
            for col in ("lifecycle", "lifecycle_at"):
                db.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT DEFAULT ''")
        spec = importlib.util.spec_from_file_location("audit_application", self.source / "app/main.py")
        self.app = mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(self.source / "app"))
        sys.modules[spec.name] = mod
        with patch.dict(os.environ, env), patch.object(threading.Thread, "start"), \
                patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess during import")):
            spec.loader.exec_module(mod)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls.fromisoformat(STAMP)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        mod.datetime = FixedDateTime
        mod.now = lambda: STAMP
        mod.STARTED_AT = STAMP
        mod.CSRF_TOKEN = "audit-csrf"
        self.sequence = 0
        mod.uuid = SimpleNamespace(uuid4=self.next_uuid)
        mod.wrapper = self.wrapper
        mod.start_tmux_session = self.start
        mod.refresh_model_catalog = lambda *a, **kw: None
        mod.queue_session_delivery = lambda sid: self.events.append(["queue", sid])
        mod.confirm_argv_async = lambda row, mid: self.events.append(["confirm", row["id"], mid])
        mod.launch_extras_async = lambda row, mid, prompt, **kw: self.events.append(
            ["setup", row["id"], mid, prompt, kw])
        mod.dismiss_webpush_session_async = lambda *a: None
        mod.INPUT_RUNTIME_DIR = self.work / "inputs"
        mod.time = SimpleNamespace(**{k: getattr(mod.time, k) for k in dir(mod.time) if not k.startswith("_")})
        mod.time.sleep = lambda _: None
        mod.time.time = lambda: 1789214400
        with mod.db() as db:
            for slug in ("demo", "second"):
                db.execute("INSERT INTO projects(slug,path,source,created_at) VALUES (?,?,'local',?)",
                           (slug, str(self.work / "projects" / slug), STAMP))
            for user in ("devagent", "hostagent"):
                db.execute("INSERT INTO account_session_defaults VALUES (?,?,?,?,?)",
                           (user, "codex-openai", "gpt-test", "high", STAMP))

    def next_uuid(self):
        self.sequence += 1
        return uuid.UUID(f"00000000-0000-4000-8000-{self.sequence:012d}")

    def start(self, row, text, mode):
        self.events.append(["start", row["id"], text, mode])
        if self.fail_start:
            raise self.app.HTTPException(400, "synthetic launch failure")
        self.alive[row["tmux_name"]] = row["unix_user"]
        return {"argv_prompt": bool(text) and mode == "argv", "argv": ["synthetic"], "at": STAMP}

    def wrapper(self, user, script, *args, **kwargs):
        command = args[0]
        result = {"ok": True}
        if script == self.app.SESSION_CTL:
            if command == "list":
                lines = [f"{name}\t0\t0\t100x30\t0\tsynthetic\t0\t\t" for name, owner in self.alive.items() if owner == user]
                return subprocess.CompletedProcess([], 0, "\n".join(lines), "")
            if command == "kill":
                self.alive.pop(args[1], None)
            elif command in {"model", "mode", "runtime", "capture", "status", "info", "up", "down", "enter", "pause",
                             "scroll-up", "scroll-down", "scroll-bottom", "key", "keys", "scroll", "command", "input", "paste", "ready"}:
                result.update({"state": "live", "model": "gpt-test", "effort": "high", "mode": "default"})
            else:
                raise AssertionError(f"Unmodelled session wrapper: {command}")
        elif script == self.app.PROJECT_CTL:
            if command == "ls":
                p = Path(args[1])
                if not p.is_dir():
                    return subprocess.CompletedProcess([], 1, "", "directory missing")
                result = {"path": str(p), "parent": str(p.parent), "dirs": sorted(x.name for x in p.iterdir() if x.is_dir())}
            elif command == "status":
                result = {"branch": "main", "compose": False, "dirty": False, "path": str(self.work / "projects" / args[1])}
            elif command in {"doc-import", "knowledge-import"}:
                slug, source, name = args[1:4]
                base = self.work / "projects" / slug / "docs/input" if command == "doc-import" else self.work / "knowledge/projects" / slug
                base.mkdir(parents=True, exist_ok=True)
                dest = base / name
                if Path(source).resolve() != dest.resolve():
                    stem, suffix, index = dest.stem, dest.suffix, 1
                    while dest.exists():
                        index += 1; dest = base / f"{stem}-{index}{suffix}"
                    shutil.copyfile(source, dest)
                os.utime(dest, (1789214400, 1789214400))
                result = {"path": str(dest), "name": dest.name}
            elif command in {"doc-delete", "knowledge-delete"}:
                base = self.work / "projects" / args[1] / "docs/input" if command == "doc-delete" else self.work / "knowledge/projects" / args[1]
                (base / args[2]).unlink(missing_ok=True)
            else:
                raise AssertionError(f"Unmodelled project wrapper: {command}")
        else:
            raise AssertionError(f"Unmodelled wrapper: {script}")
        self.events.append(["wrapper", Path(script).name, user, *args])
        return subprocess.CompletedProcess([], 0, json.dumps(result), "")

    def database(self):
        with self.app.db() as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [dict(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                    for name in names}


if __name__ == "__main__":
    import uvicorn
    source, work, port = sys.argv[1:]
    backend = AuditBackend(source, work)
    from fastapi import Request, WebSocket, WebSocketDisconnect
    web = backend.app.app
    web.router.routes = [r for r in web.router.routes if getattr(r, "path", "") != "/ws/session/{sid}"]
    @web.websocket("/ws/session/{sid}")
    async def terminal(ws: WebSocket, sid: str):
        await ws.accept()
        await ws.send_bytes(b"Synthetic terminal ready\r\n")
        try:
            while True:
                text = await ws.receive_text()
                if text.startswith("{"): continue  # viewport resize is nondeterministic
                backend.events.append(["terminal-input", text])
                await ws.send_bytes(text.encode())
        except WebSocketDisconnect:
            pass
    @web.get("/_audit/state")
    async def audit_state():
        return {"database": backend.database(), "events": backend.events}
    @web.post("/_audit/control")
    async def audit_control(request: Request):
        data = await request.json()
        backend.fail_start = data.get("fail_start", False)
        if "csrf" in data: backend.app.CSRF_TOKEN = data["csrf"]
        with backend.app.db() as db:
            db.execute("DROP TRIGGER IF EXISTS reject_message")
            if data.get("reject_message"):
                db.execute("CREATE TRIGGER reject_message BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'synthetic database failure'); END")
        return {"ok": True}
    uvicorn.run(web, host="127.0.0.1", port=int(port), log_level="critical")
