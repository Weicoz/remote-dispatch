"""票 02：真实 serve 子进程经 Unix socket 报告包版本、合同版本与启动加载时间。"""
import datetime
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

from remote_dispatch import __version__
from tests import test_rd_serve as serve_tests


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts" / "v1"


class VersionTests(unittest.TestCase):
    setUp_base = serve_tests.ServeTests.setUp
    assert_structure = serve_tests.ServeTests.assert_structure

    def setUp(self):
        self.setUp_base()
        self.env = dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(self.config),
                        PYTHONPATH=str(ROOT / "src"))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "remote_dispatch", "serve", "--socket", str(self.path)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        self.eventually_ready()

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            out, err = self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            out, err = self.process.communicate()
            self.fail(f"serve 未在 10 秒内退出\n{out}\n{err}")
        if self.process.returncode not in (0, -15):
            self.fail(f"serve 未正常退出：{self.process.returncode}\n{out}\n{err}")

    def eventually_ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                self.fail(f"serve 启动失败：{self.process.returncode}\n{out}\n{err}")
            try:
                self.request("/v1/hosts")
                return
            except (OSError, http.client.HTTPException):
                time.sleep(0.02)
        self.fail("serve 未在 10 秒内就绪")

    def request(self, path):
        connection = http.client.HTTPConnection("localhost", timeout=5)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.sock.settimeout(5)
            connection.sock.connect(str(self.path))
            connection.request("GET", path)
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def command(self, *argv):
        return subprocess.run([sys.executable, "-m", "remote_dispatch", *argv], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_version_endpoint_reports_package_contract_and_unchanged_startup_load(self):
        status, first = self.request("/v1/version")
        self.assertEqual(status, 200, first)
        self.assertEqual(first["package_version"], __version__)
        self.assertEqual(first["contract_version"], 1)
        self.assertTrue(first["config_reload"]["ok"])
        self.assertIsNone(first["config_reload"]["error"])
        loaded_at = datetime.datetime.fromisoformat(first["config_reload"]["at"])
        self.assertIsNotNone(loaded_at.tzinfo)
        self.assertLessEqual(loaded_at, datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(self.request("/v1/version"), (status, first))

    def test_version_contract_example_matches_live_response(self):
        example = json.loads((CONTRACTS / "version.json").read_text())
        self.assertEqual(example["request"]["method"], "GET")
        self.assertEqual(example["events"], [])
        status, body = self.request(example["request"]["path"])
        self.assertEqual(status, example["response"]["status"])
        self.assert_structure(body, example["response"]["body"])
        self.assertEqual(body["contract_version"], example["response"]["body"]["contract_version"])
        self.assertEqual(body["package_version"], __version__)
        self.assertIs(type(body["contract_version"]), int)
        self.assertIsNone(body["config_reload"]["error"])

    def test_cli_version_matches_the_version_reported_by_serve(self):
        _, body = self.request("/v1/version")
        result = self.command("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "rdispatch " + body["package_version"])
