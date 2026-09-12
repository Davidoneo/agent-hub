"""Raw downloads over isolated HTTP; temporary logs, no Hub startup or live data."""
import ast
import asyncio
import http.client
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

try:
    import anyio
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
    from starlette.requests import ClientDisconnect
    import uvicorn
except ImportError:
    uvicorn = None


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(uvicorn is None, "requires the application FastAPI/Uvicorn environment")
class RawLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT / ".agent")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "pty.log"
        self.session = {"tmux_name": "session-test"}
        app = FastAPI()
        self.backend = {
            "app": app, "Path": Path, "os": os, "quote": quote, "anyio": anyio,
            "HTTPException": HTTPException, "FileResponse": FileResponse,
            "StreamingResponse": StreamingResponse,
            "get_session": mock.Mock(return_value=self.session),
            "log_path": mock.Mock(return_value=self.path),
        }
        # Compile the actual response and handler, without host config/workers.
        tree = ast.parse((ROOT / "app/main.py").read_text())
        selected = [node for node in tree.body
                    if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef))
                    and node.name in {"RawLogResponse", "session_log_raw"}]
        exec(compile(ast.Module(body=selected, type_ignores=[]), "app/main.py", "exec"),
             self.backend)
        self.after_send = lambda message: None

        async def isolated(scope, receive, send):
            async def sent(message):
                await send(message)
                self.after_send(message)
            await app(scope, receive, sent)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            isolated, lifespan="off", http="h11", access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(5)
            sock.close()
            self.assertFalse(thread.is_alive(), "isolated HTTP server did not stop")
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(server.started)

    def download(self):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.request("GET", "/api/sessions/test/log/raw")
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def assert_download(self, expected):
        status, headers, body = self.download()
        self.assertEqual(status, 200)
        self.assertEqual(body, expected)
        self.assertEqual(int(headers["content-length"]), len(expected))
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["content-disposition"],
                         'attachment; filename="session-test.log"')

    def test_stable_log_including_empty_and_chunk_boundary(self):
        for size in (0, 65536, 2 * 65536 + 137):
            with self.subTest(size=size):
                data = (b"\x00\xff\x1b[0m\r\n" * (size // 8 + 1))[:size]
                self.path.write_bytes(data)
                self.assert_download(data)

    def test_append_during_download_returns_only_initial_bytes(self):
        initial = b"x" * (2 * 65536 + 137)
        self.path.write_bytes(initial)
        appended = threading.Event()

        def append(message):
            if message["type"] == "http.response.body" and message.get("body") and not appended.is_set():
                # Deterministic: the first chunk has been sent; the next read
                # cannot happen until this append finishes. No timing race.
                with self.path.open("ab") as writer:
                    writer.write(b"new log output\n" * 10000)
                appended.set()
        self.after_send = append
        self.assert_download(initial)
        self.assertTrue(appended.is_set())
        self.assertGreater(self.path.stat().st_size, len(initial))

    def test_filename_encoding(self):
        self.path.write_bytes(b"raw")
        self.session["tmux_name"] = "session è"
        status, headers, body = self.download()
        self.assertEqual((status, body), (200, b"raw"))
        self.assertEqual(headers["content-disposition"],
                         "attachment; filename*=utf-8''session%20%C3%A8.log")

    def test_missing_log_and_missing_session_remain_404(self):
        self.assertEqual(self.download()[0], 404)
        self.backend["get_session"].side_effect = HTTPException(404, "sessione inesistente")
        self.backend["log_path"].reset_mock()
        self.assertEqual(self.download()[0], 404)
        self.backend["log_path"].assert_not_called()

    def test_descriptor_closes_when_client_disconnects(self):
        self.path.write_bytes(b"x" * 131209)
        response = asyncio.run(self.backend["session_log_raw"]("test"))
        opened = []
        open_file = anyio.open_file

        async def track_open(*args, **kwargs):
            file = await open_file(*args, **kwargs)
            opened.append(file)
            return file

        async def disconnected(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        async def receive():
            return {"type": "http.disconnect"}

        with mock.patch.object(anyio, "open_file", track_open):
            with self.assertRaises(ClientDisconnect):
                asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}},
                                     receive, disconnected))
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)


if __name__ == "__main__":
    unittest.main()
