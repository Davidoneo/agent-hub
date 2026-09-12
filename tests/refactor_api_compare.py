"""HTTP, persistent state and failures compared through the real FastAPI app."""
import concurrent.futures
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import sys

from fastapi.testclient import TestClient
from audit_backend_fixture import AuditBackend


def exercise(source):
    with tempfile.TemporaryDirectory() as work:
        backend = AuditBackend(source, work)
        app = backend.app
        observations, successful_sessions = [], []
        with TestClient(app.app, raise_server_exceptions=False) as client:
            def request(label, method, path, expected=(200,), **kw):
                for folder in ("uploads", "knowledge"):
                    for file in (Path(work) / folder).rglob("*"):
                        if file.is_file(): os.utime(file, (1789214400, 1789214400))
                headers = kw.pop("headers", {"x-csrf-token": "audit-csrf"})
                response = client.request(method, path, headers=headers, **kw)
                assert response.status_code in expected, (label, response.status_code, response.text[:400])
                if "application/json" in response.headers.get("content-type", ""):
                    body = response.json()
                    if path == "/api/bootstrap": body.pop("asset_version", None)
                else:
                    body = response.text
                selected_headers = {k: v for k, v in response.headers.items() if k.lower() in {
                    "content-type", "content-disposition", "x-agent-hub-error-code", "x-agent-hub-session-id"}}
                observations.append([label, method, path, response.status_code, body, selected_headers])
                return body

            request("bootstrap", "GET", "/api/bootstrap")
            for token in (None, "wrong", ""):
                request("csrf-" + str(token), "POST", "/api/activity", (403,),
                        headers={} if token is None else {"x-csrf-token": token})
            app.CONFIG["origin"] = "http://testserver"
            request("wrong-origin", "POST", "/api/activity", (403,),
                    headers={"x-csrf-token": "audit-csrf", "origin": "http://other.test"})
            app.CONFIG["require_tailscale"] = True
            app.CONFIG["allowed_users"] = ["audit@example.test"]
            request("identity-missing", "GET", "/api/projects", (403,))
            request("identity-wrong", "GET", "/api/projects", (403,), headers={"tailscale-user-login": "wrong"})
            request("identity-allowed", "GET", "/api/projects", headers={"tailscale-user-login": "audit@example.test"})
            app.CONFIG["require_tailscale"] = False
            request("activity", "POST", "/api/activity")
            for path in ("/api/projects", "/api/projects/demo", "/api/projects/second", "/api/documents",
                         "/api/backlog", "/api/meetings", "/api/push", "/api/ports", "/api/usage"):
                request("read-" + path, "GET", path)
            for path in ("/api/projects/absent", "/api/sessions/absent", "/api/documents/absent/download",
                         "/api/backlog/absent", "/api/meetings/absent"):
                request("missing-" + path, "GET", path, (404,))
            # Real multipart parsing, streaming, catalog insertion and file download.
            document_ids = []
            for scope, slug, name in itertools.product(["private", "repository"], ["", "demo"],
                                                       ["note.txt", "../caffè.txt", "windows\\note.md", "archive.tar.gz"]):
                result = request(f"upload-{scope}-{slug}-{name}", "POST", "/api/documents",
                                 (400,) if scope == "repository" and not slug else (200,),
                                 data={"scope": scope, "project_slug": slug}, files=[("files", (name, b"synthetic bytes"))])
                if "documents" in result:
                    document_ids.extend(d["id"] for d in result["documents"])
            for name, body, expected in [("empty.txt", b"", (200,)), ("exact.txt", b"x" * 1048576, (200,)),
                                         ("large.txt", b"x" * 1048577, (400,)), ("program.exe", b"x", (400,))]:
                request("upload-limit-" + name, "POST", "/api/documents", expected, files={"file": (name, body)})
            partial = request("upload-partial", "POST", "/api/documents",
                              files=[("files", ("good.txt", b"good")), ("files", ("bad.exe", b"bad"))])
            assert len(partial["documents"]) == len(partial["errors"]) == 1
            for data in ({}, {"scope": "bad"}, {"project_slug": "../escape"}, {"project_slug": "missing"}):
                request("upload-invalid-" + str(data), "POST", "/api/documents", (400,), data=data)
            for did in document_ids:
                assert request("download-" + did, "GET", f"/api/documents/{did}/download") == "synthetic bytes"
            # Creation across harness/environment/delivery modes, including absent fields.
            for env, profile, delivery in itertools.product(["PROJECT", "SERVER"],
                    ["codex-openai", "claude-anthropic", "minimal"], ["empty", "normal", "plan", "goal"]):
                body = {"environment": env, "project_slug": "demo", "profile_id": profile,
                        "prompt": "" if delivery == "empty" else "Obiettivo italiano\n日本語 <script>literal</script>"}
                if delivery == "plan": body["mode"] = "plan"
                if delivery == "goal": body["prompt_as_goal"] = True
                valid = not (env == "SERVER" and profile == "minimal") and (delivery in {"empty", "normal"} or profile == "codex-openai")
                result = request(f"create-{env}-{profile}-{delivery}", "POST", "/api/sessions",
                                 (200,) if valid else (400,), json=body)
                if "session" in result: successful_sessions.append(result["session"]["id"])
            for change in [{"environment": "UNKNOWN"}, {"project_slug": ""}, {"project_slug": "unknown"},
                           {"workdir": "relative"}, {"workdir": "/escape"}, {"profile_id": "missing"},
                           {"permission_mode": "invalid"}, {"cols": "bad"}, {"rows": []},
                           {"model": "bad model"}, {"effort": "ultra"}, {"mode": "bad"},
                           {"prompt_as_goal": True, "prompt": ""},
                           {"prompt_as_goal": True, "goal": "duplicate"}]:
                # Empty rows list is intentionally accepted as a falsy default by the baseline.
                request("create-invalid-" + str(change), "POST", "/api/sessions",
                        (200,) if change == {"rows": []} else (400,),
                        json={"project_slug": "demo", "prompt": "task", **change})
            backend.fail_start = True
            failed = request("launch-fails-after-persist", "POST", "/api/sessions", (400,),
                             json={"project_slug": "demo", "prompt": "Keep this prompt"})
            assert "synthetic launch failure" in failed["detail"]
            backend.fail_start = False
            # Empty/text/type/size and attachment-only message paths persist to real SQLite.
            sid = successful_sessions[1]
            for text in ["", "   ", "line one\nline two", "é 日本語 🧪", "<script>alert(1)</script>",
                         "x" * 500000, "x" * 500001, None, 7, True, [], {}]:
                valid = isinstance(text, str) and bool(text.strip()) and len(text) <= 500000
                request("message-" + hashlib.sha256(repr(text).encode()).hexdigest()[:12],
                        "POST", f"/api/sessions/{sid}/messages", (200,) if valid else (400,), json={"text": text})
            for ids in [[document_ids[0]], document_ids[:2], [document_ids[0]] * 2, ["missing"]]:
                request("message-docs-" + str(ids), "POST", f"/api/sessions/{sid}/messages",
                        (400,) if ids == ["missing"] else (200,), json={"text": "", "document_ids": ids})
            request("attach-note", "POST", f"/api/sessions/{sid}/documents",
                    json={"document_ids": document_ids[:2], "note": " Read these "})
            messages = request("messages-persisted", "GET", f"/api/sessions/{sid}/messages")["messages"]
            assert any(m["text"] == "é 日本語 🧪" for m in messages)
            request("resend", "POST", f"/api/sessions/{sid}/messages/{messages[-1]['id']}/resend")
            request("resend-missing", "POST", f"/api/sessions/{sid}/messages/missing/resend", (404,))
            # Regex boundary and effort policy in both account and runtime handlers.
            for model in ["", "a", "a" * 80, "a" * 81, "foo/bar:v1.2", "_bad", "bad model", "é", "custom"]:
                for effort in ["", "high", "invalid"]:
                    valid_model = not model or bool(app.MODEL_RE.fullmatch(model))
                    expected = (200,) if valid_model and effort != "invalid" and (model or effort) else (400,)
                    request("runtime-" + model + "/" + effort, "POST", f"/api/sessions/{sid}/runtime",
                            expected, json={"model": model, "effort": effort})
                    expected = (200,) if valid_model and effort != "invalid" else (400,)
                    request("defaults-" + model + "/" + effort, "POST", "/api/accounts/defaults", expected,
                            json={"unix_user": "devagent", "profile_id": "codex-openai", "model": model, "effort": effort})
            for action in ["up", "down", "enter", "pause", "scroll-up", "scroll-down", "scroll-bottom", "restart", "kill", "restart", "bad"]:
                request("action-" + action, "POST", f"/api/sessions/{sid}/action",
                        (400,) if action == "bad" else (200,), json={"action": action})
            request("read-runtime", "GET", f"/api/sessions/{sid}/runtime")
            # Compare complete persisted state before concurrency; concurrent arrival
            # order is nondeterministic, so assert exact contents and uniqueness there.
            state = backend.database()
            event_state = list(backend.events)
            count_before = len(app.session_messages(sid))
            texts = [f"concurrent-{i:02d}" for i in range(40)]
            def send(text):
                r = client.post(f"/api/sessions/{sid}/messages", json={"text": text}, headers={"x-csrf-token": "audit-csrf"})
                assert r.status_code == 200, r.text
                return r.json()["message"]["id"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                ids = list(pool.map(send, texts))
            assert len(set(ids)) == 40
            persisted = app.session_messages(sid)[count_before:]
            assert sorted(m["text"] for m in persisted) == sorted(texts)
            assert all(m["status"] == "pending" for m in persisted)
            concurrency = {"messages": len(persisted), "unique_ids": len(set(ids)), "texts": sorted(m["text"] for m in persisted)}
        payload = json.dumps({"http": observations, "database": state, "events": event_state,
                              "concurrency": concurrency}, ensure_ascii=False, sort_keys=True).replace(work, "<fixture>")
        return json.loads(payload)


if __name__ == "__main__":
    candidate = Path(__file__).resolve().parents[1]
    baseline = Path(sys.argv[1])
    left, right = exercise(baseline), exercise(candidate)
    if left != right:
        for key in left:
            if left[key] != right[key]:
                if isinstance(left[key], list):
                    for index, pair in enumerate(itertools.zip_longest(left[key], right[key])):
                        if pair[0] != pair[1]:
                            print("DIFFERENCE", key, index, repr(pair)[:2500]);break
                else: print("DIFFERENCE", key)
        raise AssertionError("Backend behavior differs")
    summary = {"equivalent": True, "http_cases_per_version": len(left["http"]),
               "status_counts": {str(code): sum(row[3] == code for row in left["http"]) for code in [200,400,403,404]},
               "database_tables_compared": len(left["database"]), "concurrent_messages_per_version": 40,
               "observation_sha256": hashlib.sha256(json.dumps(left, sort_keys=True).encode()).hexdigest()}
    print(json.dumps(summary, indent=2))
