"""设备与配对 CLI 经真实 serve 子进程和 Unix socket 驱动。"""
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from remote_dispatch.rd_serve import UnixHTTPConnection

ROOT = Path(__file__).resolve().parents[1]


class DeviceCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-device-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.config.write_text("", encoding="utf-8")
        self.path = self.root / "serve.sock"
        self.process = None
        self.addCleanup(self.stop)
        # Serve must never contact the user's real Herdr or harness state.
        binary = self.root / "bin"
        binary.mkdir()
        herdr = binary / "herdr"
        herdr.write_text("#!" + sys.executable + "\nimport sys\n"
                         "if sys.argv[1:] != ['agent', 'list']: sys.exit(70)\n"
                         "print('{\"id\":\"cli:agent:list\",\"result\":{\"agents\":[]}}')\n")
        herdr.chmod(0o700)
        self.extra_env = {
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.root / "home"),
            "HERDR_CONFIG_PATH": str(self.root / "herdr" / "config.toml"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
        }

    def env(self, **extra):
        return dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(self.config),
                    RD_USER_ID="fixture", RD_SERVE_SOCKET=str(self.path),
                    PYTHONPATH=str(ROOT / "src"), **self.extra_env, **extra)

    def command(self, *argv, **extra):
        return subprocess.run([sys.executable, "-m", "remote_dispatch", *argv],
                              env=self.env(**extra), capture_output=True, text=True, timeout=20)

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "remote_dispatch", "serve", "--socket", str(self.path)],
            env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                self.fail(f"serve 提前退出：{out}\n{err}")
            try:
                self.request("GET", "/v1/hosts")
                return self.process
            except (OSError, http.client.HTTPException):
                time.sleep(0.02)
        self.fail("serve 未在期限内就绪")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate()
        self.process = None

    def request(self, method, path, body=None, headers=None):
        connection = UnixHTTPConnection(self.path, timeout=5)
        try:
            payload = None if body is None else json.dumps(body).encode("utf-8")
            request_headers = dict(headers or {})
            if payload is not None:
                request_headers.setdefault("Content-Type", "application/json")
            connection.request(method, path, payload, request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def snapshot(self):
        if not self.state.exists():
            return None
        return {str(path.relative_to(self.state)): (path.stat().st_mtime_ns, path.read_bytes())
                for path in self.state.rglob("*") if path.is_file()}

    def test_device_and_pairing_commands_fail_with_install_hint_and_write_nothing(self):
        for argv in (("device", "list"), ("device", "revoke", "0123456789abcdef0123456789abcdef"),
                     ("pair", "open"), ("pair", "close")):
            with self.subTest(argv=argv):
                result = self.command(*argv)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn("serve", result.stderr)
                self.assertIn("rdispatch service install", result.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_pair_open_and_close_drive_the_real_serve_window(self):
        self.start()
        opened = self.command("pair", "open", "--json")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        window = json.loads(opened.stdout)
        self.assertIsInstance(window["code"], str)
        self.assertTrue(window["code"])
        self.assertEqual(window["expires_in"], 300)
        self.assertIsInstance(window["spki_sha256"], str)
        self.assertIsInstance(window["candidates"], list)
        status, device = self.request("POST", "/v1/pair", {"code": window["code"], "name": "Phone"},
                                      {"Idempotency-Key": "pair-cli-open"})
        self.assertEqual(status, 200, device)
        self.assertTrue(device["device_token"])
        second = self.command("pair", "open", "--json")
        self.assertEqual(second.returncode, 0, second.stderr)
        second = json.loads(second.stdout)
        self.assertNotEqual(second["code"], window["code"])
        self.assertEqual(self.request("POST", "/v1/pair", {"code": window["code"], "name": "Old"},
                                      {"Idempotency-Key": "pair-old"}),
                         (403, {"error": {"reason": "pairing_closed"}}))
        closed = self.command("pair", "close", "--json")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(json.loads(closed.stdout), {"open": False})
        self.assertEqual(self.request("POST", "/v1/pair", {"code": second["code"], "name": "Phone"},
                                      {"Idempotency-Key": "pair-closed"}),
                         (403, {"error": {"reason": "pairing_closed"}}))

    def pair_device(self, name="Phone"):
        opened = self.command("pair", "open", "--json")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        window = json.loads(opened.stdout)
        status, device = self.request("POST", "/v1/pair", {"code": window["code"], "name": name},
                                      {"Idempotency-Key": "pair-" + secrets.token_hex(8)})
        self.assertEqual(status, 200, device)
        return device

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_device_list_and_revoke_use_serve_state(self):
        self.start()
        device = self.pair_device()
        listed = self.command("device", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        payload = json.loads(listed.stdout)
        self.assertEqual(payload, self.request("GET", "/v1/devices")[1])
        row = payload["devices"][0]
        self.assertEqual(row["device_id"], device["device_id"])
        self.assertEqual(row["name"], "Phone")
        self.assertFalse(row["revoked"])
        self.assertIn("paired_at", row)
        self.assertIn("last_seen_at", row)
        human = self.command("device", "list")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.splitlines()[0].split(),
                         ["ID", "NAME", "PAIRED_AT", "LAST_SEEN_AT", "STATUS"])
        self.assertIn(device["device_id"], human.stdout)
        self.assertIn("Phone", human.stdout)
        self.assertIn(row["paired_at"], human.stdout)
        revoked = self.command("device", "revoke", device["device_id"], "--json")
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertEqual(json.loads(revoked.stdout), {"device_id": device["device_id"], "revoked": True})
        self.assertTrue(self.request("GET", "/v1/devices")[1]["devices"][0]["revoked"])
        human = self.command("device", "revoke", device["device_id"])
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.strip(), "revoked " + device["device_id"])

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_pair_human_output_shows_the_live_code_and_expiry(self):
        self.start()
        opened = self.command("pair", "open")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("pairing code", opened.stdout)
        self.assertRegex(opened.stdout, r"expires in\s+300s \(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\)")
        code = opened.stdout.split("pairing code", 1)[1].split()[0]
        self.assertEqual(self.request("POST", "/v1/pair", {"code": code, "name": "Human"},
                                      {"Idempotency-Key": "pair-human"})[0], 200)
        closed = self.command("pair", "close")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(closed.stdout.strip(), "pairing window closed")

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_unknown_device_reports_serve_error(self):
        self.start()
        self.pair_device()
        result = self.command("device", "revoke", "ffffffffffffffffffffffffffffffff")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("device_not_found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.request("GET", "/v1/devices")[1]["devices"][0]["revoked"], False)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_stale_socket_after_serve_death_reports_install_hint_without_writing(self):
        self.start()
        self.pair_device()
        before = self.snapshot()
        self.process.kill()
        self.process.communicate()
        self.process = None
        self.assertTrue(self.path.exists())
        for argv in (("device", "list"), ("pair", "open"), ("pair", "close")):
            with self.subTest(argv=argv):
                result = self.command(*argv)
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertIn("rdispatch service install", result.stderr)
                self.assertIn("serve_unavailable", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_empty_device_list_and_idempotent_pair_close(self):
        self.start()
        human = self.command("device", "list")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.strip(), "no paired devices.")
        listed = self.command("device", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), {"devices": []})
        for _ in range(2):
            closed = self.command("pair", "close", "--json")
            self.assertEqual(closed.returncode, 0, closed.stderr)
            self.assertEqual(json.loads(closed.stdout), {"open": False})

