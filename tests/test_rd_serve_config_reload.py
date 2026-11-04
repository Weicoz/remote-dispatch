"""票 03：真实 serve 子进程自动重载配置，失败保留旧值并可恢复。"""
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

from remote_dispatch import rd_events as events
from tests import test_rd_serve as serve_tests


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts" / "v1"


class ConfigReloadTests(unittest.TestCase):
    setUp_base = serve_tests.ServeTests.setUp
    assert_structure = serve_tests.ServeTests.assert_structure

    def setUp(self):
        self.setUp_base()
        self.env = dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(self.config),
                        PYTHONPATH=str(ROOT / "src"))
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "remote_dispatch", "serve", "--socket", str(self.path),
             "--poll-interval", "0.1"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        self.eventually_ready()

    def stop(self):
        if self.process is None:
            return
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
                self.request("/v1/version")
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

    def open_events(self):
        connection = http.client.HTTPConnection("localhost", timeout=5)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.sock.settimeout(5)
        connection.sock.connect(str(self.path))
        connection.request("GET", "/v1/events")
        reply = connection.getresponse()
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.readline(), b": connected\n")
        self.assertEqual(reply.readline(), b"\n")
        return reply

    def command(self, *argv):
        return subprocess.run([sys.executable, "-m", "remote_dispatch", *argv], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def host_config(self, name, *, enabled=True, extra=""):
        return (f"[host:{name}]\nssh=local\nworkdir={self.root / 'worker'}\n"
                f"sync=direct\nenabled={'true' if enabled else 'false'}\n{extra}")

    def write_config(self, text):
        self.config.write_text(text, encoding="utf-8")

    def eventually(self, read, predicate, message):
        deadline = time.monotonic() + 8
        value = None
        while time.monotonic() < deadline:
            value = read()
            if predicate(value):
                return value
            time.sleep(0.02)
        self.fail(f"{message}：{value!r}")

    def test_added_host_appears_and_version_reports_the_latest_reload(self):
        self.start()
        _, started = self.request("/v1/version")
        self.assertTrue(started["config_reload"]["ok"])
        self.assertIsNone(started["config_reload"]["error"])
        self.assertEqual(self.request("/v1/hosts")[1]["hosts"], [])

        self.write_config(self.host_config("reload-target"))
        hosts = self.eventually(lambda: self.request("/v1/hosts")[1],
                                lambda body: [row["name"] for row in body["hosts"]] == ["reload-target"],
                                "新增目标机没有自动出现在 /v1/hosts")
        self.assertTrue(hosts["config_reload"]["ok"])
        self.assertIsNone(hosts["config_reload"]["error"])
        self.assertNotEqual(hosts["config_reload"]["at"], started["config_reload"]["at"])
        loaded_at = datetime.datetime.fromisoformat(hosts["config_reload"]["at"])
        self.assertIsNotNone(loaded_at.tzinfo)

        version = self.eventually(lambda: self.request("/v1/version")[1],
                                  lambda body: body["config_reload"] == hosts["config_reload"],
                                  "/v1/version 没有反映最近一次配置重载")
        self.assertEqual(version["package_version"], started["package_version"])

        # 修改已启用目标机的字段也必须重载，而不是只在禁用/启用时触发。
        changed_config = self.host_config("reload-target").replace(
            str(self.root / "worker"), str(self.root / "worker-moved"))
        self.write_config(changed_config)
        modified = self.eventually(lambda: self.request("/v1/hosts")[1],
                                   lambda body: [row["name"] for row in body["hosts"]] == ["reload-target"]
                                   and body["config_reload"]["at"] != hosts["config_reload"]["at"],
                                   "修改已启用目标机后配置没有自动重载")
        self.assertTrue(modified["config_reload"]["ok"])

        # 同一目标机改成禁用属于“修改”，也必须触发重载并从指标列表移除。
        self.write_config(self.host_config("reload-target", enabled=False))
        cleared = self.eventually(lambda: self.request("/v1/hosts")[1],
                                  lambda body: body["hosts"] == []
                                  and body["config_reload"]["at"] != hosts["config_reload"]["at"],
                                  "禁用目标机后配置没有自动重载")
        self.assertTrue(cleared["config_reload"]["ok"])

        self.write_config(self.host_config("reload-target"))
        restored = self.eventually(lambda: self.request("/v1/hosts")[1],
                                   lambda body: [row["name"] for row in body["hosts"]] == ["reload-target"],
                                   "重新启用目标机后配置没有自动重载")
        self.assertNotEqual(restored["config_reload"]["at"], cleared["config_reload"]["at"])

    def test_broken_config_keeps_old_hosts_and_reports_error_until_fixed(self):
        self.write_config(self.host_config("stable"))
        self.start()
        self.eventually(lambda: self.request("/v1/hosts")[1],
                        lambda body: [row["name"] for row in body["hosts"]] == ["stable"],
                        "启动配置没有出现在 /v1/hosts")
        before = self.request("/v1/hosts")[1]

        self.write_config("[host:broken]\nplatform=posix\nworkdir=/tmp/worker\n")
        broken = self.eventually(lambda: self.request("/v1/version")[1]["config_reload"],
                                 lambda reload: reload["ok"] is False and reload["error"],
                                 "无效配置没有记录重载错误")
        failed_at = datetime.datetime.fromisoformat(broken["at"])
        self.assertIsNotNone(failed_at.tzinfo)
        self.assertIn("broken", broken["error"])
        self.assertIn("ssh", broken["error"])
        hosts = self.request("/v1/hosts")[1]
        self.assertEqual([row["name"] for row in hosts["hosts"]], ["stable"])
        self.assertEqual(hosts["config_reload"], broken)
        example = json.loads((CONTRACTS / "hosts-config-reload-error.json").read_text())
        self.assertEqual(hosts["config_reload"]["ok"],
                         example["response"]["body"]["config_reload"]["ok"])
        self.assert_structure(hosts, example["response"]["body"])

        # 解析错误也保留旧配置；出错行里的凭据不得出现在 API 响应里。
        self.write_config("codex_auth_file=/private/secret-value\n")
        parsed = self.eventually(lambda: self.request("/v1/version")[1]["config_reload"],
                                 lambda reload: reload["ok"] is False and reload["at"] != broken["at"],
                                 "INI 解析错误没有记录为重载失败")
        self.assertTrue(parsed["error"])
        self.assertNotIn("secret-value", json.dumps(parsed))
        self.assertEqual([row["name"] for row in self.request("/v1/hosts")[1]["hosts"]], ["stable"])

        # 与启动校验一致：无效的汇报合并窗口也保留旧配置。
        self.write_config(self.host_config("stable") + "[monitor]\nreport_merge_window_seconds=-1\n")
        window = self.eventually(lambda: self.request("/v1/version")[1]["config_reload"],
                                 lambda reload: reload["ok"] is False and reload["at"] != parsed["at"],
                                 "无效的汇报合并窗口没有记录为重载失败")
        self.assertIn("汇报合并窗口", window["error"])

        self.write_config(self.host_config("stable") + self.host_config("recovered"))
        fixed = self.eventually(lambda: self.request("/v1/version")[1]["config_reload"],
                                lambda reload: reload["ok"] is True and reload["error"] is None
                                and reload["at"] != broken["at"],
                                "修好配置后重载错误没有清除")
        hosts = self.eventually(lambda: self.request("/v1/hosts")[1],
                                lambda body: [row["name"] for row in body["hosts"]] == ["stable", "recovered"],
                                "修好配置后目标机列表没有恢复")
        self.assertEqual(hosts["config_reload"], fixed)
        self.assertEqual([row["name"] for row in hosts["hosts"]],
                         [row["name"] for row in before["hosts"]] + ["recovered"])

    def test_startup_with_one_invalid_host_still_serves_the_others_and_lists_the_error(self):
        self.write_config(self.host_config("good") + self.host_config("bad", extra="max_load_per_core=nan\n")
                          + self.host_config("old", enabled=False, extra="timeout=0\nmax_swap_io_mb_s=\n"))
        self.start()
        body = self.eventually(lambda: self.request("/v1/hosts")[1],
                               lambda value: [row["name"] for row in value["hosts"]] == ["good"],
                               "无效目标机不应让 serve 起不来，其余目标机应正常列出")
        self.assertEqual([item["name"] for item in body["config_errors"]], ["bad"])
        self.assertIn("max_load_per_core", body["config_errors"][0]["error"])

    def test_reload_keeps_the_old_definition_when_a_running_host_becomes_invalid(self):
        self.write_config(self.host_config("stable"))
        self.start()
        self.eventually(lambda: self.request("/v1/hosts")[1],
                        lambda value: [row["name"] for row in value["hosts"]] == ["stable"], "启动目标机未出现")
        self.write_config(self.host_config("stable", extra="timeout=0\n"))
        failed = self.eventually(lambda: self.request("/v1/version")[1]["config_reload"],
                                 lambda reload: reload["ok"] is False, "已在用的目标机写坏后应保留旧定义并报错")
        self.assertIn("stable", failed["error"])
        self.assertIn("timeout", failed["error"])
        self.assertEqual([row["name"] for row in self.request("/v1/hosts")[1]["hosts"]], ["stable"])

    def test_known_invalid_host_does_not_block_unrelated_reloads(self):
        self.write_config(self.host_config("good") + self.host_config("bad", extra="timeout=0\n"))
        self.start()
        self.eventually(lambda: self.request("/v1/hosts")[1],
                        lambda value: [item["name"] for item in value.get("config_errors", [])] == ["bad"], "启动错误未列出")
        self.write_config(self.host_config("good") + self.host_config("bad", extra="timeout=0\n")
                          + self.host_config("added"))
        body = self.eventually(lambda: self.request("/v1/hosts")[1],
                               lambda value: sorted(row["name"] for row in value["hosts"]) == ["added", "good"],
                               "已知的无效目标机不应阻塞其他修改的重载")
        self.assertTrue(body["config_reload"]["ok"])
        self.assertEqual([item["name"] for item in body["config_errors"]], ["bad"])

    def test_reload_keeps_an_open_stream_and_forwarded_write_command_running(self):
        self.write_config(self.host_config("reload-target"))
        self.start()
        self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"], bool, "目标机未就绪")
        stream = self.open_events()
        write = subprocess.Popen(
            [sys.executable, "-m", "remote_dispatch", "session-default", "reload-session", "--clear"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: write.poll() is None and write.kill())
        for index in range(8):
            self.write_config(self.host_config("reload-target", enabled=bool(index % 2)))
            time.sleep(0.03)
        out, err = write.communicate(timeout=15)
        self.assertEqual(write.returncode, 0, f"写命令被配置重载打断\n{out}\n{err}")
        self.assertEqual(json.loads(out), {"host": None, "harness": None})
        read = self.command("session-default", "reload-session")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(read.stdout, '{"host": null, "harness": null}\n')

        events.EventLog(self.state).append("job.created", dict(job="reload-event", user_id="fixture", round=1))
        for _ in range(20):
            fields = {}
            while True:
                line = stream.readline().decode("utf-8").rstrip("\r\n")
                if not line:
                    if "data" in fields:
                        break
                    continue
                key, value = line.split(":", 1)
                fields[key] = value.lstrip()
            if fields.get("event") == "job.created":
                break
        else:
            self.fail("配置重载期间打开的 SSE 没有继续收到事件")
        self.assertEqual(json.loads(fields["data"])["data"]["job"], "reload-event")
        status, body = self.request("/v1/version")
        self.assertEqual(status, 200)
        self.assertTrue(body["config_reload"]["ok"])


if __name__ == "__main__":
    unittest.main()
