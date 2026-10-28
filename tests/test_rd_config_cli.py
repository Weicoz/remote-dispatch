"""票 06：会话访问总开关与目标机设置的 CLI 行为。

真实 rdispatch serve 子进程只在在线用例启动；离线用例不创建 socket。
存储使用临时 RD_STATE / RD_CONFIG，不接触真实状态目录。
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch.rd_lock import JobLock
from tests.test_rd_serve import UnixHTTPConnection


ROOT = Path(__file__).resolve().parents[1]


class ConfigCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-config-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.config.write_text("; keep header\n[project:demo]\n; keep project\ntest=echo okay\n")
        self.socket = self.state / "serve.sock"
        self.serve = None
        binary = self.root / "bin"
        binary.mkdir()
        herdr = binary / "herdr"
        herdr.write_text("#!" + sys.executable + "\nimport sys\n"
                         "if sys.argv[1:] != ['agent', 'list']: sys.exit(70)\n"
                         "print('{\"id\":\"cli:agent:list\",\"result\":{\"agents\":[]}}')\n")
        herdr.chmod(0o700)
        self.env = dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(self.config),
                        RD_USER_ID="fixture", PYTHONPATH=str(ROOT / "src"), HOME=str(self.root),
                        PATH=str(binary) + os.pathsep + os.environ["PATH"],
                        HERDR_CONFIG_PATH=str(self.root / "herdr" / "config.toml"),
                        CLAUDE_CONFIG_DIR=str(self.root / "claude"), CODEX_HOME=str(self.root / "codex"))
        for key in ("RD_SERVE_SOCKET", "RD_INITIATOR_SESSION", "HERDR_SESSION",
                    "HERDR_WORKSPACE_ID", "HERDR_PANE_ID"):
            self.env.pop(key, None)

    def command(self, *argv, **environment):
        return subprocess.run([sys.executable, "-m", "remote_dispatch", *argv],
                              env=dict(self.env, **environment), cwd=self.root,
                              capture_output=True, text=True, timeout=20)

    def start_serve(self):
        self.serve = subprocess.Popen(
            [sys.executable, "-m", "remote_dispatch", "serve", "--poll-interval", "3600"],
            env=self.env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_serve)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.serve.poll() is not None:
                out, err = self.serve.communicate()
                self.fail(f"serve 提前退出：{self.serve.returncode}\n{out}\n{err}")
            try:
                status, _ = self.request("/v1/hosts")
            except (OSError, ConnectionError):
                time.sleep(0.02)
                continue
            if status == 200:
                return
        self.fail("serve 未在 10 秒内就绪")

    def stop_serve(self):
        if self.serve is None:
            return
        process, self.serve = self.serve, None
        if process.poll() is None:
            process.terminate()
        out, err = process.communicate(timeout=10)
        if process.returncode != 0:
            self.fail(f"serve 退出码 {process.returncode}\n{out}\n{err}")
        if self.socket.exists():
            self.fail("serve 退出后仍保留 socket")

    def eventually(self, read, predicate):
        deadline = time.monotonic() + 15
        value = None
        while time.monotonic() < deadline:
            value = read()
            if predicate(value):
                return value
            time.sleep(0.05)
        self.fail(f"未等到在线 serve 反映变化：{value!r}")

    def request(self, path, method="GET", body=None, headers=None):
        connection = UnixHTTPConnection(self.socket)
        self.addCleanup(connection.close)
        payload = None if body is None else json.dumps(body)
        merged = dict(headers or {})
        if payload is not None:
            merged.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_session_access_offline_show_toggle_backup_and_json(self):
        original = self.config.read_bytes()
        shown = self.command("session-access", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout), {"enabled": True})

        disabled = self.command("session-access", "off", "--json")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(json.loads(disabled.stdout), {"enabled": False})
        self.assertRegex(self.config.read_text(), r"session_access\s*=\s*false")
        backup = next(self.root.glob("config.ini.bak.*"))
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

        human = self.command("session-access")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("关", human.stdout)

        enabled = self.command("session-access", "on", "--json")
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertEqual(json.loads(enabled.stdout), {"enabled": True})
        self.assertRegex(self.config.read_text(), r"session_access\s*=\s*true")
        self.assertEqual(len(list(self.root.glob("config.ini.bak.*"))), 2)

    def test_hosts_offline_list_add_update_delete_json_backup_and_validation(self):
        original = ("; keep header\n[defaults]\nsync = direct\n[host:target]\n; keep host note\n"
                    "ssh = local\nenabled = false\ncodex_auth_file = /private/fixture-secret\n"
                    "tags = /private/fixture-secret\n"
                    "[project:demo]\n; keep project\ntest = echo okay\n")
        self.config.write_text(original)

        listed = self.command("host", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        rows = json.loads(listed.stdout)["hosts"]
        self.assertEqual([row["name"] for row in rows], ["target"])
        self.assertEqual(rows[0]["values"]["sync"], "direct")
        self.assertEqual(rows[0]["values"]["enabled"], "false")
        self.assertIn(dict(field="codex_auth_file", is_set=True), rows[0]["protected_fields"])
        self.assertNotIn("fixture-secret", listed.stdout)
        duplicate = self.command("host", "add", "target", "--ssh", "local")
        self.assertEqual(duplicate.returncode, 1, duplicate.stderr)
        self.assertIn("host_exists", duplicate.stderr)
        missing = self.command("host", "update", "missing", "--ssh", "local")
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn("not_found", missing.stderr)

        added = self.command("host", "add", "second", "--ssh", "worker@example.invalid",
                             "--workdir", "~/worker", "--sync", "origin", "--enabled", "false",
                             "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        host = json.loads(added.stdout)["host"]
        self.assertEqual((host["name"], host["values"]["ssh"], host["values"]["sync"]),
                         ("second", "worker@example.invalid", "origin"))
        self.assertEqual(next(self.root.glob("config.ini.bak.*")).read_text(), original)
        self.assertIn("; keep host note", self.config.read_text())

        updated = self.command("host", "update", "second", "--set", "max_heavy=3",
                               "--unset", "enabled", "--json")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        values = json.loads(updated.stdout)["host"]["values"]
        self.assertEqual((values["max_heavy"], values["enabled"]), ("3", "true"))
        self.assertIn("[project:demo]\n; keep project\ntest = echo okay\n", self.config.read_text())

        bad_unset = self.command("host", "update", "second", "--unset", "name")
        self.assertEqual(bad_unset.returncode, 4, bad_unset.stderr)
        bad = self.command("host", "add", "bad", "--ssh", "local", "--set", "sync=other")
        self.assertEqual(bad.returncode, 4, bad.stderr)
        self.assertIn("invalid_host_field", bad.stderr)
        self.assertNotIn("[host:bad]", self.config.read_text())
        self.assertEqual(len(list(self.root.glob("config.ini.bak.*"))), 2)

        deleted = self.command("host", "delete", "second", "--json")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(deleted.stdout), {"deleted": "second"})
        self.assertNotIn("[host:second]", self.config.read_text())
        self.assertIn("; keep project", self.config.read_text())

    def test_hosts_offline_delete_rejects_active_dispatches_with_count_and_ids(self):
        self.config.write_text("[host:target]\nssh = local\nenabled = false\n[project:demo]\ntest = echo okay\n")
        original = self.config.read_bytes()
        jobs = self.state / "jobs"
        jobs.mkdir(parents=True)
        for job, user in (("running-job", "fixture"), ("other-user-job", "other")):
            (jobs / (job + ".json")).write_text(json.dumps(
                dict(job=job, user_id=user, host="target", rounds=1)))
        rejected = self.command("host", "delete", "target")
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertIn("host_has_active_dispatches", rejected.stderr)
        self.assertIn("running_count=2", rejected.stderr)
        self.assertIn("running-job", rejected.stderr)
        self.assertIn("other-user-job", rejected.stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.root.glob("config.ini.bak.*")), [])

        for path in jobs.glob("*.json"):
            path.unlink()
        deleted = self.command("host", "delete", "target", "--json")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(deleted.stdout), {"deleted": "target"})

    def test_online_session_access_and_hosts_are_written_by_serve(self):
        self.config.write_text("[host:target]\nssh = local\nenabled = false\n[project:demo]\ntest = echo okay\n")
        self.start_serve()
        self.assertEqual(self.request("/v1/config/session-access"), (200, {"enabled": True}))
        shown = self.command("session-access", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout), {"enabled": True})
        listed = self.command("host", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([row["name"] for row in json.loads(listed.stdout)["hosts"]], ["target"])

        disabled = self.command("session-access", "off", "--json")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(json.loads(disabled.stdout), {"enabled": False})
        self.assertEqual(self.request("/v1/config/session-access"), (200, {"enabled": False}))
        self.assertEqual(self.request("/v1/sessions")[0], 403)

        added = self.command("host", "add", "online", "--ssh", "local",
                             "--workdir", str(self.root / "worker"), "--sync", "direct", "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"],
                        lambda rows: any(row["name"] == "online" for row in rows))
        updated = self.command("host", "update", "online", "--set", "max_heavy=4", "--json")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        rows = self.request("/v1/config/hosts")[1]["hosts"]
        self.assertEqual(next(row for row in rows if row["name"] == "online")["values"]["max_heavy"], "4")
        unset = self.command("host", "update", "online", "--unset", "max_heavy", "--json")
        self.assertEqual(unset.returncode, 0, unset.stderr)
        self.assertEqual(json.loads(unset.stdout)["host"]["values"]["max_heavy"], "1")

        deleted = self.command("host", "delete", "online", "--json")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual([row["name"] for row in self.request("/v1/config/hosts")[1]["hosts"]], ["target"])

        enabled = self.command("session-access", "on", "--json")
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertEqual(self.request("/v1/config/session-access"), (200, {"enabled": True}))
        self.assertEqual(self.request("/v1/sessions")[0], 200)

    def test_online_delete_reports_active_dispatch_conflict(self):
        worker = self.root / "worker"
        self.config.write_text(f"[host:target]\nssh = local\nworkdir = {worker}\nenabled = false\n")
        jobs = self.state / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "running-job.json").write_text(json.dumps(
            dict(job="running-job", user_id="fixture", host="target", rounds=1)))
        job_dir = worker / "users" / "fixture" / "jobs" / "running-job"
        job_dir.mkdir(parents=True)
        (job_dir / "pid").write_text(str(os.getpid()))
        (job_dir / "run.r1.sh").write_text("")
        self.start_serve()
        rejected = self.command("host", "delete", "target")
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertIn("host_has_active_dispatches", rejected.stderr)
        self.assertIn("running_count=1", rejected.stderr)
        self.assertIn("running-job", rejected.stderr)
        self.assertEqual([row["name"] for row in self.request("/v1/config/hosts")[1]["hosts"]], ["target"])

        (jobs / "running-job.json").unlink()
        deleted = self.command("host", "delete", "target", "--json")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(deleted.stdout), {"deleted": "target"})

    def test_stale_socket_falls_back_to_direct_write(self):
        self.state.mkdir()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket))
        stale.close()
        result = self.command("session-access", "off", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(self.config.read_text(), r"session_access\s*=\s*false")

    def test_connection_loss_after_request_does_not_fall_back_or_write(self):
        self.state.mkdir()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket))
        listener.listen()
        self.addCleanup(listener.close)
        def accept():
            with listener.accept()[0] as client:
                client.recv(65536)
        worker = threading.Thread(target=accept, daemon=True)
        worker.start()
        before = self.config.read_bytes()
        result = self.command("session-access", "off", "--json")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("cli_forward_failed", result.stderr)
        self.assertEqual(self.config.read_bytes(), before)

    def test_offline_first_host_write_creates_config_and_empty_backup(self):
        self.config.unlink()
        result = self.command("host", "add", "self", "--ssh", "local", "--workdir", "~/worker",
                              "--enabled", "false", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["host"]["name"], "self")
        self.assertIn("[host:self]", self.config.read_text())
        self.assertEqual(next(self.root.glob("config.ini.bak.*")).read_bytes(), b"")

    def test_online_errors_keep_reason_and_exit_codes(self):
        self.config.write_text("[host:target]\nssh = local\nenabled = false\n")
        self.start_serve()
        duplicate = self.command("host", "add", "target", "--ssh", "local")
        self.assertEqual(duplicate.returncode, 1, duplicate.stderr)
        self.assertEqual(duplicate.stderr.strip(), "rdispatch: host_exists: 目标机名称已存在")
        missing = self.command("host", "update", "missing", "--ssh", "local")
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertEqual(missing.stderr.strip(), "rdispatch: not_found: 目标机不存在")
        invalid = self.command("host", "update", "target", "--set", "sync=other")
        self.assertEqual(invalid.returncode, 4, invalid.stderr)
        self.assertEqual(invalid.stderr.strip(), "rdispatch: invalid_host_field: 字段 sync 取值无效")
        lock = JobLock(self.state, "host-config")
        lock.path = self.config.with_name(self.config.name + ".lock")
        lock.acquire()
        self.addCleanup(lock.release)
        busy = self.command("host", "update", "target", "--set", "sync=direct")
        self.assertEqual(busy.returncode, 1, busy.stderr)
        self.assertEqual(busy.stderr.strip(), "rdispatch: host_config_busy: 配置正在写入，请稍后重试")
        self.config.unlink()
        self.config.mkdir()
        unavailable = self.command("host", "list")
        self.assertEqual(unavailable.returncode, 1, unavailable.stderr)
        self.assertEqual(unavailable.stderr.strip(),
                         "rdispatch: host_config_unavailable: 无法读取或保存目标机配置")

    def test_online_rejects_cli_with_a_different_config_path(self):
        self.config.write_text("[host:target]\nssh = local\nenabled = false\n")
        original = self.config.read_bytes()
        other = self.root / "other.ini"
        other.write_text("[host:other]\nssh = local\nenabled = false\n")
        self.start_serve()
        result = self.command("session-access", "off", "--json", RD_CONFIG=str(other))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("cli_config_mismatch", result.stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(other.read_text(), "[host:other]\nssh = local\nenabled = false\n")

    def test_online_request_times_out_without_a_reply(self):
        self.state.mkdir()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket))
        listener.listen()
        self.addCleanup(listener.close)
        def accept():
            try:
                with listener.accept()[0] as client:
                    client.recv(65536)
                    time.sleep(1)
            except OSError:
                pass
        worker = threading.Thread(target=accept, daemon=True)
        worker.start()
        with patch.object(cli, "STATE", str(self.state)), \
                patch.object(cli, "CONFIG", str(self.config)), \
                patch.object(cli, "CONFIG_REQUEST_TIMEOUT", 0.2), \
                patch.dict(os.environ, {"RD_SERVE_SOCKET": str(self.socket)}):
            with self.assertRaises(ValueError) as caught:
                cli.serve_config_request("GET", "/v1/config/session-access")
        self.assertIn("cli_forward_failed", str(caught.exception))
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def test_online_accepts_non_ascii_config_path(self):
        directory = self.root / "张三"
        directory.mkdir()
        self.config = directory / "config.ini"
        self.config.write_text("[serve]\nsession_access = true\n")
        self.env["RD_CONFIG"] = str(self.config)
        self.start_serve()
        result = self.command("session-access", "off", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(self.config.read_text(), r"session_access\s*=\s*false")

    def test_broken_config_reports_an_error_instead_of_a_traceback(self):
        self.config.write_text("garbage\n", encoding="utf-8")
        result = self.command("host", "list")
        self.assertEqual(result.returncode, 4)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("config_invalid", result.stderr)
