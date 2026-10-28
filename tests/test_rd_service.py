"""票 04：`rdispatch service` 在临时 HOME 与假 launchctl 下管理 LaunchAgent。

测试只经 CLI 子进程观察行为：plist 内容、launchctl 调用序列、退出码与输出。
不调用真实 launchctl，不写真实 ~/Library/LaunchAgents，不碰任何正在运行的 serve。
"""
import http.client
import json
import os
from pathlib import Path
import plistlib
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from remote_dispatch import __version__

ROOT = Path(__file__).resolve().parents[1]
LABEL = "io.github.weicoz.remote-dispatch.serve"
UID = os.getuid() if hasattr(os, "getuid") else 0


@unittest.skipUnless(sys.platform == "darwin", "rdispatch service 只支持 macOS")
class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-service-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.config.write_text("[host:mini]\nssh = local\n", encoding="utf-8")
        self.binary = self.root / "bin"
        self.binary.mkdir()
        self.calls_log = self.root / "launchctl-calls.log"
        self.jobs_list = self.root / "launchctl-list.txt"
        self.jobs_list.write_text("", encoding="utf-8")
        self.launchctl = self.binary / "launchctl"
        self.launchctl.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "with open(os.environ['RD_FAKE_LAUNCHCTL_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps(argv) + '\\n')\n"
            "action = argv[0] if argv else ''\n"
            "if action == os.environ.get('RD_FAKE_LAUNCHCTL_FAIL'):\n"
            "    print('fake launchctl: ' + action + ' failed', file=sys.stderr)\n"
            "    sys.exit(int(os.environ.get('RD_FAKE_LAUNCHCTL_CODE', '5')))\n"
            "if action == 'list':\n"
            "    sys.stdout.write(open(os.environ['RD_FAKE_LAUNCHCTL_LIST'], encoding='utf-8').read())\n")
        self.launchctl.chmod(0o755)
        self.entry = self.binary / "rdispatch"
        self.entry.write_text("#!" + sys.executable + "\nimport sys\n"
                              "from remote_dispatch.cli import main\nsys.exit(main())\n")
        self.entry.chmod(0o755)
        # serve 不得读宿主 Herdr 或真实 harness 状态。
        herdr = self.binary / "herdr"
        herdr.write_text("#!" + sys.executable + "\nimport sys\n"
                         "if sys.argv[1:] != ['agent', 'list']: sys.exit(70)\n"
                         "print('{\"id\":\"cli:agent:list\",\"result\":{\"agents\":[]}}')\n")
        herdr.chmod(0o700)
        self.serves = []
        self.addCleanup(self.stop_serves)
        self.env = {
            "HOME": str(self.home),
            # 只留本机 launchd 默认会有的路径，避免依赖宿主安装位置。
            "PATH": str(self.binary) + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
            "RD_STATE": str(self.state),
            "RD_CONFIG": str(self.config),
            "RD_USER_ID": "fixture",
            "PYTHONPATH": str(ROOT / "src"),
            "RD_FAKE_LAUNCHCTL_LOG": str(self.calls_log),
            "RD_FAKE_LAUNCHCTL_LIST": str(self.jobs_list),
            "TMPDIR": str(self.root),
            "HERDR_CONFIG_PATH": str(self.root / "herdr" / "config.toml"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
        }
        for key in ("RD_SERVE_SOCKET", "RD_INITIATOR_SESSION", "HERDR_SESSION"):
            self.env.pop(key, None)

    @property
    def plist(self):
        return self.home / "Library" / "LaunchAgents" / (LABEL + ".plist")

    def command(self, *argv, environment=None, **extra):
        return subprocess.run([sys.executable, "-m", "remote_dispatch", *argv],
                              env=dict(self.env if environment is None else environment, **extra),
                              cwd=self.root, capture_output=True, text=True, timeout=30)

    def calls(self):
        if not self.calls_log.exists():
            return []
        return [json.loads(line) for line in self.calls_log.read_text().splitlines() if line.strip()]

    def test_install_writes_launch_agent_plist_and_loads_it(self):
        result = self.command("service", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.plist.is_file(), result.stderr)
        self.assertEqual(stat.S_IMODE(self.plist.stat().st_mode), 0o600, "plist 只有本用户可读")
        document = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(set(document), {"Label", "ProgramArguments", "EnvironmentVariables", "RunAtLoad",
                                         "KeepAlive", "StandardOutPath", "StandardErrorPath"})
        self.assertEqual(document["Label"], LABEL)
        self.assertEqual(document["ProgramArguments"],
                         [sys.executable, "-m", "remote_dispatch", "serve"])
        environment = document["EnvironmentVariables"]
        self.assertEqual(set(environment), {"RD_STATE", "RD_USER_ID", "RD_CONFIG", "PATH"})
        self.assertEqual(environment["RD_STATE"], str(self.state))
        self.assertEqual(environment["RD_USER_ID"], "fixture")
        self.assertEqual(environment["RD_CONFIG"], str(self.config))
        self.assertEqual(document["StandardOutPath"], str(self.state / "serve.log"))
        self.assertEqual(document["StandardErrorPath"], str(self.state / "serve.err.log"))
        self.assertIs(document["RunAtLoad"], True)
        self.assertIs(document["KeepAlive"], True)
        self.assertEqual(self.calls(), [["bootout", f"gui/{UID}/{LABEL}"],
                                        ["bootstrap", f"gui/{UID}", str(self.plist)]])
        self.assertTrue(self.state.is_dir(), "install 必须建好日志所在的 RD_STATE")
        self.assertIn("installed serve " + LABEL, result.stdout)

    def test_install_records_the_saved_user_and_omits_an_unset_config(self):
        profile = self.home / ".config" / "remote-dispatch"
        profile.mkdir(parents=True)
        (profile / "default-user.json").write_text(json.dumps({"user_id": "saved-user"}), encoding="utf-8")
        environment = {key: value for key, value in self.env.items()
                       if key not in ("RD_CONFIG", "RD_USER_ID")}
        result = self.command("service", "install", environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        variables = plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]
        self.assertEqual(variables["RD_USER_ID"], "saved-user")
        self.assertNotIn("RD_CONFIG", variables, "未设置 RD_CONFIG 时不写该键，serve 用默认配置路径")

    def test_install_records_the_running_rdispatch_entry_as_an_absolute_path(self):
        result = subprocess.run([str(self.entry), "service", "install"], env=self.env, cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(document["ProgramArguments"], [str(self.entry), "serve"])

    def test_install_reports_a_launchctl_failure_and_keeps_the_plist(self):
        result = self.command("service", "install", RD_FAKE_LAUNCHCTL_FAIL="bootstrap")
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("service_launch_failed", result.stderr)
        self.assertIn("fake launchctl: bootstrap failed", result.stderr)
        self.assertTrue(self.plist.is_file(), "加载失败时保留 plist，便于排查与重新加载")

    def test_install_twice_overwrites_the_plist_and_reloads(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        first = self.plist.read_bytes()
        again = self.command("service", "install")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.plist.read_bytes(), first)
        self.assertEqual(self.calls(), [["bootout", f"gui/{UID}/{LABEL}"],
                                        ["bootstrap", f"gui/{UID}", str(self.plist)],
                                        ["bootout", f"gui/{UID}/{LABEL}"],
                                        ["bootstrap", f"gui/{UID}", str(self.plist)]])
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["ProgramArguments"],
                         [sys.executable, "-m", "remote_dispatch", "serve"])

    def start_serve(self, state=None):
        """常驻 serve 用默认 socket：不传 RD_SERVE_SOCKET，也不传 --socket。"""
        state = Path(state or self.state)
        process = subprocess.Popen(
            [sys.executable, "-m", "remote_dispatch", "serve"],
            env=dict(self.env, RD_STATE=str(state)), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.serves.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                out, err = process.communicate()
                self.fail(f"serve 提前退出：{process.returncode}\n{out}\n{err}")
            if self.version(state) is not None:
                return process
            time.sleep(0.02)
        self.fail("serve 未在 10 秒内响应默认 socket")

    def stop_serves(self):
        processes, self.serves = self.serves, []
        for process in processes:
            if process.poll() is None:
                process.terminate()
            out, err = process.communicate(timeout=10)
            if process.returncode != 0:
                self.fail(f"serve 退出码 {process.returncode}\n{out}\n{err}")

    @staticmethod
    def version(state):
        """直接读该状态目录默认 socket 上的 /v1/version；不经过被测代码。"""
        connection = http.client.HTTPConnection("localhost", timeout=5)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.sock.settimeout(5)
            connection.sock.connect(str(Path(state) / "serve.sock"))
            connection.request("GET", "/v1/version")
            reply = connection.getresponse()
            return json.loads(reply.read()) if reply.status == 200 else None
        except (OSError, http.client.HTTPException, ValueError):
            return None
        finally:
            connection.close()

    def test_status_reports_the_loaded_serve_version_and_socket(self):
        serve = self.start_serve()
        self.jobs_list.write_text(f"{serve.pid}\t0\t{LABEL}\n", encoding="utf-8")
        self.assertEqual(self.command("service", "install").returncode, 0)
        result = self.command("service", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{'loaded':<15}yes", result.stdout)
        self.assertIn(f"{'process':<15}{serve.pid} (running)", result.stdout)
        self.assertIn(f"{'socket':<15}{self.state / 'serve.sock'}", result.stdout)
        self.assertIn(f"{'serve':<15}online", result.stdout)
        self.assertIn(f"{'package':<15}{__version__}", result.stdout)
        self.assertIn(f"{'contract':<15}1", result.stdout)
        self.assertIn(f"{'config reload':<15}ok ", result.stdout)

        reply = json.loads(self.command("service", "status", "--json").stdout)
        self.assertEqual(reply["installed"], True)
        self.assertEqual(reply["label"], LABEL)
        self.assertEqual(reply["plist"], str(self.plist))
        self.assertEqual(reply["socket"], str(self.state / "serve.sock"))
        self.assertEqual(reply["state_dir"], str(self.state))
        self.assertEqual(reply["config"], str(self.config))
        self.assertEqual(reply["executable"], sys.executable)
        self.assertEqual(reply["log"], str(self.state / "serve.log"))
        self.assertEqual(reply["error_log"], str(self.state / "serve.err.log"))
        self.assertEqual(reply["loaded"], True)
        self.assertEqual(reply["pid"], serve.pid)
        self.assertEqual(reply["running"], True)
        self.assertEqual(reply["serve_online"], True)
        self.assertEqual(reply["package_version"], __version__)
        self.assertEqual(reply["contract_version"], 1)
        self.assertEqual(reply["config_reload"]["ok"], True)
        self.assertIsNone(reply["config_reload"]["error"])
        self.assertEqual(reply["cli_version"], __version__)

    def test_status_does_not_call_a_responding_serve_stopped(self):
        self.start_serve()
        self.assertEqual(self.command("service", "install").returncode, 0)
        result = self.command("service", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{'serve':<15}online", result.stdout)
        self.assertNotIn("serve 未运行", result.stdout, "serve 在响应时不得提示未运行")
        self.assertIn("launchd 未加载", result.stdout)
        reply = json.loads(self.command("service", "status", "--json").stdout)
        self.assertEqual(reply["loaded"], False)
        self.assertEqual(reply["running"], False)
        self.assertEqual(reply["serve_online"], True)

    def test_status_survives_a_socket_that_is_not_http(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        self.state.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.state / "serve.sock"))
        server.listen(4)
        self.addCleanup(server.close)

        def answer_garbage():
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                with connection:
                    connection.recv(1024)
                    connection.sendall(b"garbage\n")

        threading.Thread(target=answer_garbage, daemon=True).start()
        result = self.command("service", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{'serve':<15}offline", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_status_does_not_probe_a_socket_it_cannot_attribute_to_the_plist(self):
        self.start_serve()
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("not a plist", encoding="utf-8")
        reply = json.loads(self.command("service", "status", "--json").stdout)
        self.assertIsNone(reply["socket"], "plist 不可读时不得回退到 CLI 的 socket")
        self.assertEqual(reply["serve_online"], False)

    def test_status_reports_an_unreadable_plist(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("not a plist", encoding="utf-8")
        result = self.command("service", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plist_invalid", result.stdout)
        self.assertNotIn("查看 None", result.stdout)
        reply = json.loads(self.command("service", "status", "--json").stdout)
        self.assertIn("plist_invalid", reply["plist_error"])

    def test_status_reports_plist_and_launchd_state_when_serve_is_offline(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        self.jobs_list.write_text(f"-\t0\t{LABEL}\n", encoding="utf-8")
        result = self.command("service", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{'loaded':<15}yes", result.stdout)
        self.assertIn(f"{'process':<15}-", result.stdout)
        self.assertIn(f"{'socket':<15}{self.state / 'serve.sock'}", result.stdout)
        self.assertIn(f"{'serve':<15}offline", result.stdout)
        self.assertIn(f"{'package':<15}-", result.stdout)
        self.assertIn(f"{'contract':<15}-", result.stdout)
        self.assertIn(f"{'config reload':<15}-", result.stdout)
        self.assertIn("serve 未运行", result.stdout)

        reply = json.loads(self.command("service", "status", "--json").stdout)
        self.assertEqual(reply["loaded"], True)
        self.assertIsNone(reply["pid"])
        self.assertEqual(reply["running"], False)
        self.assertEqual(reply["serve_online"], False)
        self.assertIsNone(reply["package_version"])
        self.assertIsNone(reply["contract_version"])
        self.assertIsNone(reply["config_reload"])

    def test_status_reports_unknown_load_state_when_launchctl_fails(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        result = self.command("service", "status", RD_FAKE_LAUNCHCTL_FAIL="list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{'loaded':<15}unknown", result.stdout)
        self.assertIn(str(self.plist), result.stdout)
        reply = json.loads(self.command("service", "status", "--json",
                                        RD_FAKE_LAUNCHCTL_FAIL="list").stdout)
        self.assertIsNone(reply["loaded"])
        self.assertIn("service_launch_failed", reply["launchctl_error"])

    def test_status_json_without_install_reports_installed_false(self):
        result = self.command("service", "status", "--json")
        self.assertEqual(result.returncode, 4, result.stdout)
        reply = json.loads(result.stdout)
        self.assertEqual(reply["installed"], False)
        self.assertIsNone(reply["loaded"])
        self.assertEqual(reply["serve_online"], False)
        self.assertIn("rdispatch service install", result.stderr)

    def test_two_state_directories_run_their_own_serve_on_default_sockets(self):
        resident = self.start_serve()
        dev_state = self.root / "dev-state"
        dev = self.start_serve(dev_state)
        self.assertNotEqual(resident.pid, dev.pid)
        self.assertEqual(self.version(self.state)["package_version"], __version__)
        self.assertEqual(self.version(dev_state)["package_version"], __version__)
        self.assertEqual(self.command("service", "install").returncode, 0)
        self.jobs_list.write_text(f"{resident.pid}\t0\t{LABEL}\n", encoding="utf-8")
        reply = json.loads(self.command("service", "status", "--json").stdout)
        self.assertEqual(reply["socket"], str(self.state / "serve.sock"))
        self.assertEqual(reply["package_version"], __version__)
        self.assertEqual(reply["serve_online"], True)

    def test_status_without_install_exits_nonzero_and_names_the_install_command(self):
        result = self.command("service", "status")
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("rdispatch service install", result.stderr)
        self.assertIn("installed      no", result.stdout)
        self.assertIn(str(self.plist), result.stdout)
        self.assertEqual(self.calls(), [], "未安装时 status 不需要问 launchd")

    def test_uninstall_unloads_removes_the_plist_and_keeps_state_and_logs(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        self.state.mkdir(parents=True, exist_ok=True)
        log = self.state / "serve.log"
        log.write_text("serve output\n", encoding="utf-8")
        result = self.command("service", "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.plist.exists(), "卸载必须删除 plist")
        self.assertTrue(self.state.is_dir(), "卸载不得删除状态目录")
        self.assertEqual(log.read_text(encoding="utf-8"), "serve output\n", "卸载不得删除日志")
        self.assertEqual(self.calls()[2:], [["bootout", f"gui/{UID}/{LABEL}"], ["list"]])
        self.assertIn("uninstalled serve " + LABEL, result.stdout)

    def test_restart_reloads_the_installed_plist(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        result = self.command("service", "restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[2:], [["bootout", f"gui/{UID}/{LABEL}"],
                                            ["bootstrap", f"gui/{UID}", str(self.plist)]])
        self.assertIn("reloaded serve " + LABEL, result.stdout)

    def test_restart_without_install_fails_with_the_install_hint(self):
        result = self.command("service", "restart")
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("rdispatch service install", result.stderr)
        self.assertFalse(self.plist.exists(), "restart 不得凭空生成 plist")
        self.assertEqual(self.calls(), [])

    def test_uninstall_keeps_the_plist_while_launchd_still_lists_the_job(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        self.jobs_list.write_text(f"{os.getpid()}\t0\t{LABEL}\n", encoding="utf-8")
        result = self.command("service", "uninstall")
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("service_unload_failed", result.stderr)
        self.assertTrue(self.plist.is_file(), "仍在加载时不得删除 plist")

    def test_uninstall_keeps_the_plist_when_launchd_cannot_be_queried(self):
        self.assertEqual(self.command("service", "install").returncode, 0)
        result = self.command("service", "uninstall", RD_FAKE_LAUNCHCTL_FAIL="list")
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("service_unload_failed", result.stderr)
        self.assertTrue(self.plist.is_file(), "无法确认已卸载时不得删除 plist")

    def test_uninstall_without_install_succeeds_without_touching_state(self):
        result = self.command("service", "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.state.exists())
        self.assertIn("not installed", result.stdout)


if __name__ == "__main__":
    unittest.main()
