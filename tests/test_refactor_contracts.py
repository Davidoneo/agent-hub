"""Exercise changed producers with real temporary files/SQLite and fake providers.

Set AGENT_HUB_BASELINE to compare each observation with another source tree.
Functions are compiled from the source AST to avoid starting the ASGI workers.
"""
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(os.environ.get("AGENT_HUB_BASELINE", ROOT))


def functions(root, relative, names, namespace):
    tree = ast.parse((root / relative).read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), relative, "exec"), namespace)
    return namespace


def observe(root, scenario):
    with tempfile.TemporaryDirectory() as folder:
        result = scenario(root, Path(folder))
        return json.loads(json.dumps(result).replace(folder, "<temporary>"))


def document_scenario(root, folder):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE documents (id TEXT PRIMARY KEY,name,original_name,path,"
                 "project_slug,scope,size,sha256,content_type,created_at)")
    counter, imported = [], []

    def new_id():
        counter.append(1)
        return f"document-{len(counter)}"

    def import_file(user, ctl, command, slug, source, name, **kwargs):
        target = folder / command / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        imported.append([user, command, slug, name])
        return {"path": str(target), "name": name}

    ns = dict(Path=Path, os=os, re=re, unicodedata=unicodedata, hashlib=hashlib,
              shutil=shutil, HTTPException=HTTPException, db=lambda: conn,
              uuid=SimpleNamespace(uuid4=new_id), now=lambda: "fixed-time",
              CONFIG={"uploads": str(folder / "uploads"), "max_upload": 8},
              ALLOWED_DOC_EXT={".txt": "text/plain"}, PROJECT_UNIX_USER="devagent",
              PROJECT_CTL="synthetic-project-ctl", wrapper_json=import_file)
    functions(root, "app/main.py", {"safe_name", "doc_ext", "store_upload",
              "insert_document", "_transcript_document"}, ns)
    records, failures = [], []
    for slug, scope in [("", "private"), ("demo", "private"), ("demo", "repository")]:
        up = SimpleNamespace(filename="../caffè.txt", file=io.BytesIO(b"example"))
        doc = ns["store_upload"](up, slug, scope)
        records.append([doc, Path(doc["path"]).read_text()])
    for name, content in [("code.exe", b"x"), ("large.txt", b"x" * 9)]:
        try:
            ns["store_upload"](SimpleNamespace(filename=name, file=io.BytesIO(content)), "")
        except HTTPException as exc:
            failures.append([exc.status_code, exc.detail])
    transcript = folder / "transcript.txt"
    transcript.write_text("[0000.0 - 0001.0]  riunione\n")
    meeting = {"title": "Riunione prova", "transcript_path": str(transcript), "project_slug": "demo"}
    first = ns["_transcript_document"](meeting)
    second = ns["_transcript_document"](meeting)
    missing = ns["_transcript_document"]({"transcript_path": str(folder / "missing.txt")})
    rows = [dict(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]
    conn.close()
    return {"records": records, "imports": imported, "failures": failures, "rows": rows,
            "reused": first == second, "missing": missing,
            "staged": sorted(p.name for p in (folder / "uploads").iterdir())}


def transcription_scenario(root, folder):
    constructors, calls, errors = [], [], []
    mode = {"value": "ok"}

    class Model:
        def __init__(self, name, **kwargs):
            constructors.append([name, kwargs])
            if mode["value"] == "load-error":
                raise RuntimeError("synthetic load failure")

        def transcribe(self, path, **kwargs):
            calls.append([path, kwargs])
            if mode["value"] == "error":
                raise RuntimeError("synthetic inference failure")
            segments = [] if mode["value"] == "empty" else [
                SimpleNamespace(start=0.0, end=1.2, text=" Prima "),
                SimpleNamespace(start=1.2, end=2.5, text=" seconda ")]
            return iter(segments), None

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meetings (id,transcript_path,updated_at)")
    conn.execute("INSERT INTO meetings VALUES ('m1','','')")
    ns = dict(Path=Path, os=os, subprocess=subprocess, _WHISPER_MODEL=None,
              _WHISPER_MODEL_LOCK=threading.Lock(), MEETING_WHISPER_MODEL="tiny",
              MEETING_WHISPER_CACHE=str(folder / "cache"), db=lambda: conn,
              now=lambda: "fixed-time", _meeting_error=lambda mid, error: errors.append([mid, error]))
    functions(root, "app/main.py", {"local_transcription_model", "_local_transcribe", "_transcribe_audio"}, ns)
    audio = folder / "audio.wav"
    audio.write_bytes(b"synthetic audio; inference is mocked")
    meeting = {"id": "m1", "audio_path": str(audio)}
    results = []
    with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=Model)}), \
            patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)):
        results.append(ns["_local_transcribe"](str(audio)))
        results.append(ns["_transcribe_audio"](meeting))
        results.append((folder / "transcript.txt").read_text())
        for case in ("empty", "error"):
            mode["value"] = case
            try:
                ns["_local_transcribe"](str(audio))
            except RuntimeError as exc:
                results.append(str(exc))
            results.append(ns["_transcribe_audio"](meeting))
        ns["_WHISPER_MODEL"] = None
        mode["value"] = "load-error"
        results.append(ns["_transcribe_audio"](meeting))
        results.append(ns["_transcribe_audio"]({"id": "m1", "audio_path": ""}))
    with patch.dict(sys.modules, {"faster_whisper": None}):
        try:
            ns["_local_transcribe"](str(audio))
        except RuntimeError as exc:
            results.append(str(exc))
        results.append(ns["_transcribe_audio"](meeting))
    row = conn.execute("SELECT * FROM meetings").fetchone()
    conn.close()
    return {"results": results, "constructors": constructors, "calls": calls,
            "errors": errors, "row": row}


def import_scenario(root, folder):
    uploads, projects = folder / "uploads", folder / "projects"
    uploads.mkdir(); projects.mkdir()

    def directory(kind):
        target = projects / kind
        target.mkdir(exist_ok=True)
        return str(target)

    def die(message):
        raise ValueError(message)

    ns = dict(os=os, shutil=shutil, json=json, die=die, UPLOAD_ROOT=str(uploads),
              PROJECT_ROOT=str(projects), SLUG_RE=re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$"),
              DOC_NAME_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}$"),
              docs_dir=lambda slug, subdir: directory("repository"),
              knowledge_dir=lambda slug: directory("knowledge"))
    functions(root, "libexec/project-ctl", {"document_source", "cmd_doc_import",
              "cmd_knowledge_import", "check_slug", "check_doc_name"}, ns)
    source = uploads / "source.txt"
    source.write_text("original")
    outsider = folder / "outside.txt"
    outsider.write_text("outside")
    link = uploads / "escape.txt"
    link.symlink_to(outsider)
    prefix = folder / "uploads-neighbor"
    prefix.mkdir(); (prefix / "file.txt").write_text("neighbor")
    results = []
    for command in ("cmd_doc_import", "cmd_knowledge_import"):
        for file, name in [(source, "copy.txt"), (source, "copy.txt"),
                           (uploads / "missing.txt", "x.txt"), (link, "x.txt"),
                           (prefix / "file.txt", "x.txt"), (source, "../invalid.txt")]:
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    ns[command](["demo", str(file), name])
                result = json.loads(output.getvalue())
                results.append([command, result, Path(result["path"]).read_text()])
            except ValueError as exc:
                results.append([command, str(exc)])
    same = projects / "knowledge" / "copy.txt"
    with contextlib.redirect_stdout(output := io.StringIO()):
        ns["cmd_knowledge_import"](["demo", str(same), "copy.txt"])
    results.append(json.loads(output.getvalue()))
    return results


class RefactorContracts(unittest.TestCase):
    def compare(self, scenario):
        actual = observe(ROOT, scenario)
        if BASELINE != ROOT:
            self.assertEqual(actual, observe(BASELINE, scenario))
        return actual

    def test_upload_catalog_and_transcript_reuse(self):
        result = self.compare(document_scenario)
        self.assertEqual(len(result["rows"]), 4)
        self.assertTrue(result["reused"])
        self.assertIsNone(result["missing"])
        self.assertEqual(result["staged"], ["document-1"])
        self.assertEqual([x[1] for x in result["imports"]], ["knowledge-import", "doc-import"])
        self.assertEqual(result["rows"][0]["sha256"], hashlib.sha256(b"example").hexdigest())
        self.assertEqual([x[0] for x in result["failures"]], [400, 400])

    def test_transcription_singleton_formats_and_failures(self):
        result = self.compare(transcription_scenario)
        self.assertEqual(len(result["constructors"]), 2)  # One shared success; one forced load failure.
        self.assertEqual(result["results"][:3], ["Prima seconda", True,
                         "[0000.0 - 0001.2]  Prima\n[0001.2 - 0002.5]  seconda\n"])
        self.assertEqual(len(result["errors"]), 4)

    def test_import_collision_reuse_and_boundary_checks(self):
        result = self.compare(import_scenario)
        self.assertEqual(len(result), 13)
        self.assertEqual(result[1][1]["name"], "copy-2.txt")
        self.assertEqual(result[-1]["name"], "copy.txt")
        self.assertIn("sorgente non consentita", result[3])

    def test_delivery_wakeup(self):
        def scenario(root, folder):
            queued, wake = [], threading.Event()
            ns = {"queue_session_delivery": queued.append, "_DELIVERY_WAKE": wake}
            functions(root, "app/main.py", {"deliver_async", "wake_session_delivery"}, ns)
            if "wake_session_delivery" in ns:
                ns["wake_session_delivery"]("s1")
            else:
                ns["deliver_async"]({"id": "s1"}, "m1", "ignored", wait_ready=False)
            return [queued, wake.is_set()]
        self.assertEqual(self.compare(scenario), [["s1"], True])

    def test_http_and_cli_contract_inventory(self):
        def inventory(root, folder):
            routes, commands = [], {}
            tree = ast.parse((root / "app/main.py").read_text())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                                and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app":
                            routes.append([ast.dump(dec), node.name, ast.dump(node.args)])
            for file in sorted((root / "libexec").iterdir()):
                if not file.is_file() or "python" not in file.read_text().splitlines()[0]:
                    continue
                for node in ast.parse(file.read_text()).body:
                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "COMMANDS" for t in node.targets):
                        commands[file.name] = ast.dump(node.value)
            return {"routes": routes, "commands": commands}
        result = self.compare(inventory)
        self.assertGreater(len(result["routes"]), 50)
        self.assertIn("project-ctl", result["commands"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
