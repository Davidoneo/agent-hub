import importlib.machinery
import importlib.util
import os
import pwd
import socket
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "libexec" / "health-ctl"
LOADER = importlib.machinery.SourceFileLoader("health_ctl", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
health_ctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(health_ctl)


class HealthTransportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tempdir.name) / "agent-hub.sock"
        os.chmod(self.tempdir.name, 0o700)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        self.server.listen(1)
        os.chmod(self.socket_path, 0o600)
        self.old_socket = health_ctl.SOCKET
        self.old_owner = health_ctl.OWNER
        self.old_origin = health_ctl.ORIGIN
        self.old_run = health_ctl.run
        health_ctl.SOCKET = str(self.socket_path)
        health_ctl.OWNER = pwd.getpwuid(os.getuid()).pw_name

    def tearDown(self):
        health_ctl.SOCKET = self.old_socket
        health_ctl.OWNER = self.old_owner
        health_ctl.ORIGIN = self.old_origin
        health_ctl.run = self.old_run
        self.server.close()
        self.tempdir.cleanup()

    def test_private_unix_socket_is_a_valid_proxy_boundary(self):
        result = health_ctl.check_socket_boundary()
        self.assertEqual(result["status"], health_ctl.OK, result)

        os.chmod(self.socket_path, 0o666)
        result = health_ctl.check_socket_boundary()
        self.assertEqual(result["status"], health_ctl.CRIT, result)

    def test_backend_probe_requires_a_real_http_response(self):
        def serve_once():
            conn, _ = self.server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        thread = threading.Thread(target=serve_once)
        thread.start()
        result = health_ctl.check_backend_http()
        thread.join(timeout=2)
        self.assertEqual(result["status"], health_ctl.OK, result)

    def test_end_to_end_probe_pins_magicdns_name_to_tailnet_ip(self):
        health_ctl.ORIGIN = "https://hub.example.invalid"

        def fake_run(command, timeout=30, env=None):
            if command[:3] == ["tailscale", "ip", "-4"]:
                return 0, "100.64.0.10"
            self.assertEqual(command[0], "curl")
            self.assertIn("hub.example.invalid:443:100.64.0.10", command)
            return 0, "200"

        health_ctl.run = fake_run
        result = health_ctl.check_end_to_end()
        self.assertEqual(result["status"], health_ctl.OK, result)


class ServiceTemplateTests(unittest.TestCase):
    def test_service_uses_private_socket_and_startup_probe(self):
        unit = (ROOT / "roles/agent_hub/templates/agent-hub.service.j2").read_text()
        socket_unit = (ROOT / "roles/agent_hub/templates/agent-hub.socket.j2").read_text()
        self.assertIn("--fd 3", unit)
        self.assertIn("health-ctl --backend-only", unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn("LimitNOFILE=8192:524288", unit)
        self.assertNotIn("--port", unit)
        self.assertNotIn("--uds", unit)
        self.assertIn("SocketMode=0600", socket_unit)
        self.assertIn("ListenStream={{ agent_hub_socket }}", socket_unit)

    def test_prompt_files_use_a_separate_cross_account_runtime_directory(self):
        main = (ROOT / "app/main.py").read_text()
        session_ctl = (ROOT / "libexec/session-ctl").read_text()
        tmpfiles = (ROOT / "roles/agent_hub/templates/agent-hub-tmpfiles.conf.j2").read_text()
        self.assertIn('INPUT_RUNTIME_DIR = Path("/run/agent-hub-inputs")', main)
        self.assertIn('INPUT_DIRS = ("/run/agent-hub-inputs/",', session_ctl)
        self.assertIn("d /run/agent-hub-inputs 0710", tmpfiles)
        self.assertIn("{{ agent_hub_agent_group }}", tmpfiles)

    def test_http_errors_preserve_launch_diagnostics_headers(self):
        main = (ROOT / "app/main.py").read_text()
        self.assertIn('"X-Agent-Hub-Error-Code": "launch_failed"', main)
        self.assertIn('"X-Agent-Hub-Session-Id": sid', main)
        self.assertIn("headers=exc.headers", main)


if __name__ == "__main__":
    unittest.main()
