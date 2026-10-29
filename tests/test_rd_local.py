"""本机派发（ssh = local）回归；不连接远端，不调用模型。"""
import argparse
import contextlib
import io
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd


def host(**overrides):
    value = dict(rd.DEFAULTS, workdir="~/rd-test", shared_workdir="~/rd-test", name="self")
    value.update(overrides)
    return value


class LocalTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-local-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def config(self, text):
        path = self.root / "config.ini"
        path.write_text(text, encoding="utf-8")
        patcher = patch.object(rd, "CONFIG", str(path))
        patcher.start()
        self.addCleanup(patcher.stop)
        return rd.Config()

    def test_local_host_is_recognised(self):
        cfg = self.config("[host:self]\nssh = local\nworkdir = ~/rd-test\n")
        h = cfg.host("self")
        self.assertTrue(rd.is_local(h))
        self.assertFalse(rd.is_local(host(ssh="user@example.invalid")))

    def test_local_host_rejects_windows_target(self):
        cfg = self.config("[host:self]\nssh = local\nplatform = windows\n")
        with self.assertRaises(SystemExit):
            cfg.host("self")

    def test_ssh_runs_on_this_machine(self):
        h = host(ssh="local", workdir=str(self.root / "worker"), path="/usr/bin:/bin")
        result = rd.ssh(h, 'echo "root=$ROOT"; echo hi')
        self.assertEqual(result.returncode, 0)
        self.assertIn("root=" + str(self.root / "worker"), result.stdout)
        self.assertIn("hi", result.stdout)

    def test_ssh_reports_failure_without_ssh(self):
        h = host(ssh="local", workdir=str(self.root / "worker"), path="/usr/bin:/bin")
        with self.assertRaises(SystemExit):
            rd.ssh(h, "exit 7")

    def test_local_scp_path_expands_home(self):
        h = host(ssh="local", workdir="~/rd-test")
        self.assertEqual(rd.scp_path(h, "repos/x.git"), os.path.expanduser("~/rd-test") + "/repos/x.git")
        h["workdir"] = "$HOME/rd-test"
        self.assertEqual(rd.scp_path(h, "repos/x.git"), os.path.expanduser("~/rd-test") + "/repos/x.git")
        h["workdir"] = str(self.root / "abs")
        self.assertEqual(rd.scp_path(h, "a"), str(self.root / "abs") + "/a")

    def test_remote_scp_path_keeps_host_prefix(self):
        h = host(ssh="user@example.invalid", workdir="~/rd-test")
        self.assertEqual(rd.scp_path(h, "repos/x.git"), "user@example.invalid:rd-test/repos/x.git")

    def test_git_ssh_env_skips_ssh_for_local(self):
        self.assertNotIn("GIT_SSH_COMMAND", rd.git_ssh_env(host(ssh="local")))
        self.assertIn("GIT_SSH_COMMAND", rd.git_ssh_env(host(ssh="user@example.invalid")))


class LocalProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-local-probe-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_probe_skips_network_checks_for_local_host(self):
        info = rd.probe(host(ssh="local", workdir=str(self.root / "worker"), path="/usr/bin:/bin"))
        self.assertTrue(info["reachable"])
        self.assertNotIn("network", info)
        self.assertIn("(本机)", rd.fmt_probe(host(ssh="local"), info, rd.assess(host(ssh="local"), info)))
        self.assertIn("network [skipped for local]", rd.fmt_probe(host(ssh="local"), info, []))

    def test_root_expr_expands_home_forms_consistently(self):
        for workdir in ("$HOME/rd-test", "$HOME", "~/rd-test"):
            self.assertTrue(rd.root_expr(host(workdir=workdir)).startswith('"$HOME'))
        result = rd.ssh(host(ssh="local", workdir="$HOME/rd-local-tests-path", path="/usr/bin:/bin"),
                        'printf "%s" "$ROOT"')
        self.assertEqual(result.stdout, os.path.expanduser("~/rd-local-tests-path"))


class LocalSelectionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rd-local-select-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config_path = self.root / "config.ini"

    def config(self, text):
        self.config_path.write_text(text, encoding="utf-8")
        patcher = patch.object(rd, "CONFIG", str(self.config_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        return rd.Config()

    def test_auto_excludes_local_host(self):
        cfg = self.config("[host:self]\nssh = local\nworkdir = ~/rd-test\n"
                          "[host:remote]\nssh = fixture.invalid\nworkdir = ~/rd-test\n")
        seen = []
        def fake_probe_all(hosts):
            seen.extend(h["name"] for h in hosts)
            return [(h, dict(reachable=True, load1="0", ncpu="1", memfree="50", disk="100", running="0", heavy="0"))
                    for h in hosts]
        with patch.object(rd, "probe_all", side_effect=fake_probe_all):
            chosen = rd.pick_host(cfg, "auto", [])
        self.assertEqual(chosen["name"], "remote")
        self.assertEqual(seen, ["remote"])

    def test_auto_explains_when_only_local_host_matches(self):
        cfg = self.config("[host:self]\nssh = local\nworkdir = ~/rd-test\n")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            rd.pick_host(cfg, "auto", [])
        self.assertEqual(result.exception.code, 4)
        self.assertIn("不会选择本机", errors.getvalue())
        self.assertIn("--host self", errors.getvalue())

    def test_windows_initiator_rejects_local_host_before_shell(self):
        errors = io.StringIO()
        with patch.object(rd.os, "name", "nt"), contextlib.redirect_stderr(errors), \
             self.assertRaises(SystemExit):
            rd.ssh(host(ssh="local"), "echo should-not-run")
        self.assertIn("Windows 发起方不能用 ssh = local", errors.getvalue())

    def test_doctor_explicit_agent_cli_is_required(self):
        cfg = self.config("[host:self]\nssh = local\nworkdir = ~/rd-test\n")
        captured = {}
        def fake_ssh(host, script, **kwargs):
            captured["script"] = script
            return subprocess.CompletedProcess(["sh"], 1, "", "")
        info = dict(reachable=True, memfree="50", disk="100", load1="0", ncpu="1", running="0", heavy="0")
        with patch.object(rd, "ssh", side_effect=fake_ssh), patch.object(rd, "probe", return_value=info), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit) as result:
            rd.cmd_doctor(argparse.Namespace(host="self", agent=["codex"]), cfg)
        self.assertEqual(result.exception.code, 1)
        self.assertIn('bad "agent codex CLI"', captured["script"])


class LocalEndToEndTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-local-e2e-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture base")
        self.config_path = self.root / "config.ini"
        self.config_path.write_text(
            f"[host:self]\nssh = local\nworkdir = {self.root / 'worker'}\nsync = direct\n"
            f"path = {os.environ['PATH']}\ntimeout = 30\n", encoding="utf-8")
        for mock in (
            patch.object(rd, "CONFIG", str(self.config_path)),
            patch.object(rd, "STATE", str(self.root / "state")),
            patch.dict(os.environ, {"RD_USER_ID": "employee_a"}),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            mock.__enter__()
            self.addCleanup(mock.__exit__, None, None, None)
        self.cfg = rd.Config()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def args(self):
        return argparse.Namespace(dir=str(self.repo), agent=None, prompt_file=None, cmd="echo e2e-ok",
                                  test=False, heavy=False, host="self", force_host=True, tag=[], ref="HEAD",
                                  wip=False, branch=None, name="e2e", project=None, include=[], model=None,
                                  effort=None, timeout=15, initiator_session=None, workdir=None)

    def wait_for_completion(self, record):
        deadline = time.time() + 20
        while time.time() < deadline:
            state = rd.job_states(self.cfg, [record])[record["job"]]
            if state.get("state") == "done":
                self.assertEqual(state.get("exit"), "0")
                return
            time.sleep(0.1)
        self.fail("本机派单未在 20 秒内完成")

    def test_run_uses_normalized_username_and_keeps_it_after_rename(self):
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(getpass, "getuser", return_value=" Alice Smith "):
            rd.cmd_run(self.args(), self.cfg)
            first = rd.all_jobs()[0]
            self.assertEqual(first["user_id"], "alice-smith")
            self.assertTrue(first["job"].startswith("alice-smith-"))
            self.wait_for_completion(first)
        # 新一轮从配置读回，电脑用户名变化不改变派单归属。
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(getpass, "getuser", return_value="Other User"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rd.cmd_profile(argparse.Namespace(action="check", host="self"), self.cfg)
            self.assertIn("ID=alice-smith ", output.getvalue())

    def test_default_username_normalization_boundaries_are_visible_in_profile_check(self):
        cases = [("ALICE", "alice"), (" Alice Smith ", "alice-smith"),
                 ("张Alice三", "alice"), ("A" * 80, "a" * 64)]
        for number, (username, expected) in enumerate(cases):
            with self.subTest(username=username):
                output = io.StringIO()
                config = self.root / str(number) / "config.ini"
                with patch.object(rd, "CONFIG", str(config)), \
                     patch.dict(os.environ, {"RD_USER_ID": ""}), \
                     patch.object(getpass, "getuser", return_value=username), \
                     contextlib.redirect_stdout(output):
                    rd.cmd_profile(argparse.Namespace(action="check", host="self"), self.cfg)
                self.assertIn("ID=" + expected + " ", output.getvalue())
                output = io.StringIO()
                with patch.object(rd, "CONFIG", str(config)), \
                     patch.dict(os.environ, {"RD_USER_ID": ""}), \
                     patch.object(getpass, "getuser", return_value="Changed Name"), \
                     contextlib.redirect_stdout(output):
                    rd.cmd_profile(argparse.Namespace(action="check", host="self"), self.cfg)
                self.assertIn("ID=" + expected + " ", output.getvalue())

    def test_explicit_user_id_overrides_saved_default_without_replacing_it(self):
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(getpass, "getuser", return_value="Alice"):
            rd.cmd_profile(argparse.Namespace(action="check", host="self"), self.cfg)
        with patch.dict(os.environ, {"RD_USER_ID": "custom-id"}), \
             patch.object(getpass, "getuser", side_effect=AssertionError("不应取用户名")):
            rd.cmd_run(self.args(), self.cfg)
            record = rd.all_jobs()[0]
            self.assertEqual(record["user_id"], "custom-id")
            self.wait_for_completion(record)
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(getpass, "getuser", return_value="Bob"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rd.cmd_profile(argparse.Namespace(action="check", host="self"), self.cfg)
            self.assertIn("ID=alice ", output.getvalue())
        with patch.dict(os.environ, {"RD_USER_ID": "INVALID"}), self.assertRaises(SystemExit) as failure:
            rd.cmd_run(self.args(), self.cfg)
        self.assertEqual(failure.exception.code, 4)
        self.assertEqual(len(rd.all_jobs()), 1)

    def test_invalid_default_username_exits_with_manual_id_hint(self):
        for username in ("中文", " !!! ", "_alice", "CON", "LPT1"):
            with self.subTest(username=username):
                errors = io.StringIO()
                with patch.dict(os.environ, {"RD_USER_ID": ""}), \
                     patch.object(getpass, "getuser", return_value=username), \
                     contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as failure:
                    rd.cmd_run(self.args(), self.cfg)
                self.assertEqual(failure.exception.code, 4)
                self.assertIn("手动设置 RD_USER_ID", errors.getvalue())
                self.assertEqual(rd.all_jobs(), [])

    def test_another_controller_with_same_id_warns_and_still_dispatches(self):
        with patch.dict(os.environ, {"RD_USER_ID": "shared-user"}):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                rd.cmd_run(self.args(), self.cfg)
            first = rd.all_jobs()[0]
            self.wait_for_completion(first)
            self.assertNotIn("警告", errors.getvalue())
            # 同一控制端再次使用保持安静。
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                rd.cmd_run(self.args(), self.cfg)
            for record in rd.all_jobs():
                self.wait_for_completion(record)
            self.assertNotIn("警告", errors.getvalue())
            other_config = self.root / "other-controller/config.ini"
            other_config.parent.mkdir()
            other_config.write_text(self.config_path.read_text())
            errors, output = io.StringIO(), io.StringIO()
            with patch.object(rd, "CONFIG", str(other_config)), \
                 contextlib.redirect_stderr(errors), contextlib.redirect_stdout(output):
                rd.cmd_run(self.args(), self.cfg)
            for record in rd.all_jobs():
                self.wait_for_completion(record)
            self.assertIn("警告", errors.getvalue())
            self.assertIn("其他控制端", errors.getvalue())
            self.assertIn("started. job id:", output.getvalue())
            self.assertEqual(len(rd.all_jobs()), 3)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                rd.cmd_run(self.args(), self.cfg)
            for record in rd.all_jobs():
                self.wait_for_completion(record)
            self.assertNotIn("警告", errors.getvalue())

    def test_run_status_fetch_clean_on_this_machine(self):
        rd.cmd_run(self.args(), self.cfg)
        rec = rd.all_jobs()[0]
        deadline = time.time() + 20
        while True:
            state = rd.job_states(self.cfg, [rec])[rec["job"]]
            if state.get("state") in ("done", "dead", "missing"):
                break
            if time.time() > deadline:
                self.fail("本机派单未在 20 秒内结束")
            time.sleep(0.1)
        self.assertEqual(state.get("state"), "done")
        self.assertEqual(state.get("exit"), "0")
        stdout = Path(rec["worker_root"], "jobs", rec["job"], "stdout.r1.log").read_text()
        self.assertIn("e2e-ok", stdout)
        rd.cmd_fetch(argparse.Namespace(job=rec["job"]), self.cfg)
        fetched = self.git("rev-parse", "--verify", "refs/remotes/rd/" + rec["branch"]).strip()
        self.assertEqual(fetched, rd.load_job(rec["job"])["base"])
        rd.cmd_clean(argparse.Namespace(job=rec["job"], force=True, delete_branch=True,
                                        discard_reports=True, merged=False, into=None, dry_run=False,
                                        project=None, initiator_session=None), self.cfg)
        self.assertEqual(rd.all_jobs(), [])
        self.assertFalse(Path(rec["worker_root"], "wt", rec["job"]).exists())
        branch = subprocess.run(["git", "-C", str(Path(rec["worker_root"], rec["bare"])), "show-ref",
                                 "--verify", "refs/heads/" + rec["branch"]], capture_output=True)
        self.assertNotEqual(branch.returncode, 0)

    def finished_job(self):
        rd.cmd_run(self.args(), self.cfg)
        rec = rd.all_jobs()[0]
        self.wait_for_completion(rec)
        rd.cmd_fetch(argparse.Namespace(job=rec["job"]), self.cfg)
        return rec

    def clean_args(self, rec, **options):
        values = dict(job=rec["job"], force=False, delete_branch=False, discard_reports=True, merged=False,
                      into=None, dry_run=False, project=None, initiator_session=None)
        values.update(options)
        return argparse.Namespace(**values)

    def test_clean_removes_directory_left_after_worktree_registration_is_lost(self):
        rec = self.finished_job()
        wt = Path(rec["worker_root"], "wt", rec["job"])
        (wt / ".git").unlink()
        (wt / "leftover.txt").write_text("x")
        rd.save_job(dict(rd.load_job(rec["job"]), handoff_version=None))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            rd.cmd_clean(self.clean_args(rec), self.cfg)
        self.assertIn("--force", errors.getvalue())
        self.assertTrue(wt.exists())
        self.assertEqual(len(rd.all_jobs()), 1)
        rd.cmd_clean(self.clean_args(rec, force=True), self.cfg)
        self.assertFalse(wt.exists())
        self.assertEqual(rd.all_jobs(), [])
        pruned = self.git("-C", str(Path(rec["worker_root"], rec["bare"])), "worktree", "list", "--porcelain")
        self.assertNotIn(str(wt.resolve()), pruned)

    def test_clean_removes_write_only_worktree_directory(self):
        rec = self.finished_job()
        wt = Path(rec["worker_root"], "wt", rec["job"])
        (wt / "leftover.txt").write_text("x")
        sub = wt / "sub"
        sub.mkdir()
        (sub / "f").write_text("x")
        sub.chmod(0o200)
        self.addCleanup(lambda: sub.exists() and sub.chmod(0o700))
        rd.cmd_clean(self.clean_args(rec, force=True), self.cfg)
        self.assertFalse(wt.exists())
        self.assertEqual(rd.all_jobs(), [])

    def test_clean_without_force_still_refuses_uncommitted_files_and_removes_clean_worktree(self):
        rec = self.finished_job()
        rd.save_job(dict(rd.load_job(rec["job"]), handoff_version=None))
        wt = Path(rec["worker_root"], "wt", rec["job"])
        (wt / "dirty.txt").write_text("x")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            rd.cmd_clean(self.clean_args(rec), self.cfg)
        self.assertIn("uncommitted", errors.getvalue())
        self.assertTrue((wt / "dirty.txt").exists())
        (wt / "dirty.txt").unlink()
        rd.cmd_clean(self.clean_args(rec), self.cfg)
        self.assertFalse(wt.exists())
        self.assertEqual(rd.all_jobs(), [])


FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, pathlib, sys
if "--help" in sys.argv:
    print("--disable --permission-profile")
    sys.exit(0)
if sys.argv[1:] == ["features", "list"]:
    print("hooks stable true\\nplugins stable true\\nmemories stable true\\nexternal_agent_memory_import development false")
    sys.exit(0)
home = pathlib.Path(os.environ["CODEX_HOME"])
record = {"codex_home": str(home), "account_home": os.environ.get("HOME"),
          "config": (home / "config.toml").read_text(), "agents": (home / "AGENTS.md").read_text()}
pathlib.Path(os.environ["RD_JOB_DIR"], "codex-record.json").write_text(json.dumps(record))
print(json.dumps({"session_id": "fixture-session"}))
'''

# 假 ssh 只替换传输层：参数按本机命令执行，目标机逻辑与真实 SSH 目标一致。
FAKE_SSH = '''#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
exec sh -c "$*"
'''


class LocalTargetProfileSyncTest(unittest.TestCase):
    """本机目标派 Codex 前自动同步员工配置副本；SSH 目标仍要求确认。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-local-profile-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "profile-source"
        self.source.mkdir()
        (self.source / "config.toml").write_text(
            'approval_policy="never"\nsandbox_mode="workspace-write"\nmodel="fixture-a"\n', encoding="utf-8")
        (self.source / "AGENTS.md").write_text("公司规范\nEMPLOYEE_RULE_MARKER\n", encoding="utf-8")
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.worker = self.root / "worker"
        self.users = self.worker / "users" / "employee_a"
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        (self.bin / "codex").write_text(FAKE_CODEX, encoding="utf-8")
        (self.bin / "codex").chmod(0o755)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture base")
        self.prompt = self.root / "task.md"
        self.prompt.write_text("Fixture only; no model API request.\n", encoding="utf-8")
        self.write_config(f"[host:self]\nssh = local\nworkdir = {self.worker}\nsync = direct\n"
                          f"path = {self.bin}:{os.environ['PATH']}\ntimeout = 30\n")
        self.env = dict({k: v for k, v in os.environ.items() if not k.startswith("RD_")},
                        RD_STATE=str(self.state), RD_CONFIG=str(self.config), RD_USER_ID="employee_a",
                        RD_PROFILE_DIR=str(self.source), HOME=str(self.home),
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def write_config(self, text):
        self.config.write_text(text, encoding="utf-8")

    def cli(self, *argv, env=None):
        return subprocess.run([sys.executable, "-m", "remote_dispatch", *argv], env=env or self.env,
                              capture_output=True, text=True, timeout=60)

    def dispatch(self, host="self", env=None):
        return self.cli("run", "--dir", str(self.repo), "--agent", "codex", "--prompt-file", str(self.prompt),
                        "--host", host, "--force-host", "--timeout", "30", env=env)

    def job_id(self, result):
        match = re.search(r"started\. job id: (\S+)", result.stdout)
        self.assertIsNotNone(match, "派单未启动：\n" + result.stdout + result.stderr)
        return match.group(1)

    def wait_round(self, job, round_no=1, worker=None):
        directory = Path(worker or self.worker) / "users" / "employee_a" / "jobs" / job
        deadline = time.time() + 30
        while time.time() < deadline:
            if (directory / f"exit.r{round_no}").exists():
                return directory
            time.sleep(0.05)
        self.fail(f"任务第 {round_no} 轮未在 30 秒内结束：{directory}")

    def codex_record(self, directory, round_no=1):
        path = directory / "codex-record.json"
        self.assertTrue(path.exists(), "任务没有在隔离的 Codex 配置下运行：\n"
                        + (directory / f"stderr.r{round_no}.log").read_text(encoding="utf-8"))
        return json.loads(path.read_text(encoding="utf-8"))

    def current_profile(self, worker=None):
        directory = Path(worker or self.worker) / "users" / "employee_a" / "profiles"
        return json.loads((directory / "current.json").read_text(encoding="utf-8"))

    def test_local_target_syncs_missing_profile_and_runs_with_an_isolated_copy(self):
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("本机目标 self：已自动同步员工配置副本 version=", result.stdout)
        self.assertNotIn("--confirm-version", result.stdout + result.stderr)
        current = self.current_profile()
        self.assertEqual(current["user_id"], "employee_a")
        self.assertTrue((self.users / "profiles" / current["fingerprint"] / "config.toml").exists())
        job = self.job_id(result)
        directory = self.wait_round(job)
        self.assertEqual((directory / "exit.r1").read_text(encoding="utf-8").strip(), "0")
        record = self.codex_record(directory)
        self.assertEqual(record["codex_home"], str(directory / "codex"))
        self.assertEqual(record["account_home"], str(self.home))
        self.assertIn("EMPLOYEE_RULE_MARKER", record["agents"])
        self.assertFalse((self.home / ".codex").exists())

    def test_local_target_does_not_sync_when_the_copy_is_current(self):
        first = self.dispatch()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.wait_round(self.job_id(first))
        second = self.dispatch()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertNotIn("已自动同步", second.stdout)
        versions = [p.name for p in (self.users / "profiles").iterdir()
                    if p.is_dir() and not p.name.startswith(".")]
        self.assertEqual(versions, [self.current_profile()["fingerprint"]])

    def test_local_target_updates_a_stale_copy_and_keeps_old_versions(self):
        check = self.cli("profile", "check", "self")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("Remote Dispatch: ID=employee_a host=self", check.stdout)
        fingerprint = json.loads((self.state / "profiles" / "employee_a" / "source.json").read_text())["fingerprint"]
        synced = self.cli("profile", "sync", "self", "--confirm-user-id", "employee_a",
                          "--confirm-version", fingerprint)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        self.assertIn("已同步：ID=employee_a version=", synced.stdout)
        first = self.current_profile()["fingerprint"]
        (self.source / "AGENTS.md").write_text("公司规范\nEMPLOYEE_RULE_MARKER_V2\n", encoding="utf-8")
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("本机目标 self：已自动同步员工配置副本 version=", result.stdout)
        second = self.current_profile()["fingerprint"]
        self.assertNotEqual(first, second)
        self.assertTrue((self.users / "profiles" / first).is_dir())
        directory = self.wait_round(self.job_id(result))
        record = self.codex_record(directory)
        self.assertIn("EMPLOYEE_RULE_MARKER_V2", record["agents"])

    def test_local_target_keeps_a_newer_copy_instead_of_overwriting_it(self):
        profiles = self.users / "profiles"
        profiles.mkdir(parents=True)
        manifest = {"user_id": "employee_a", "fingerprint": "a" * 64, "source_updated_at": time.time() + 60,
                    "files": {"config.toml": {"sha256": "b" * 64, "mode": 0o644}}}
        (profiles / "current.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = self.dispatch()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("本机目标的员工配置副本比本地配置包新；不覆盖较新副本", result.stderr)
        self.assertEqual(json.loads((profiles / "current.json").read_text(encoding="utf-8")), manifest)
        self.assertFalse((self.users / "jobs").exists())

    def test_local_target_without_any_config_package_stops_before_dispatch(self):
        env = dict(self.env, RD_PROFILE_DIR=str(self.root / "absent-profile"))
        result = self.dispatch(env=env)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("本机也没有配置包，无法自动同步", result.stderr)
        self.assertFalse((self.users / "jobs").exists())

    def test_resume_moving_to_the_local_target_keeps_the_original_profile(self):
        remote_worker = self.root / "remote-worker"
        local_worker = self.root / "local-worker"
        (self.bin / "ssh").write_text(FAKE_SSH, encoding="utf-8")
        (self.bin / "ssh").chmod(0o755)
        self.write_config(f"[host:self]\nssh = local\nworkdir = {local_worker}\nsync = direct\n"
                          f"path = {self.bin}:{os.environ['PATH']}\ntimeout = 30\n"
                          f"[host:remote]\nssh = worker@example.invalid\nworkdir = {remote_worker}\n"
                          f"sync = direct\npath = {self.bin}:{os.environ['PATH']}\ntimeout = 30\n")
        env = dict(self.env, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        check = self.cli("profile", "check", "remote", env=env)
        self.assertEqual(check.returncode, 0, check.stderr)
        fingerprint = json.loads(
            (self.state / "profiles" / "employee_a" / "source.json").read_text())["fingerprint"]
        for host in ("remote", "self"):
            synced = self.cli("profile", "sync", host, "--confirm-user-id", "employee_a",
                              "--confirm-version", fingerprint, env=env)
            self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        first = self.dispatch(host="remote", env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        job = self.job_id(first)
        self.wait_round(job, worker=remote_worker)
        (self.source / "AGENTS.md").write_text("公司规范\nEMPLOYEE_RULE_MARKER_V2\n", encoding="utf-8")
        resumed = self.cli("resume", job, "--host", "self", "--force-host",
                          "--prompt-file", str(self.prompt), env=env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertNotIn("已自动同步", resumed.stdout)
        record = json.loads((self.state / "jobs" / (job + ".json")).read_text(encoding="utf-8"))
        self.assertEqual((record["host"], record["rounds"]), ("self", 2))
        self.assertEqual(self.current_profile(worker=local_worker)["fingerprint"], fingerprint)
        directory = self.wait_round(job, round_no=2, worker=local_worker)
        self.assertEqual((directory / "exit.r2").read_text(encoding="utf-8").strip(), "0")
        codex = self.codex_record(directory, round_no=2)
        self.assertIn("EMPLOYEE_RULE_MARKER", codex["agents"])
        self.assertNotIn("EMPLOYEE_RULE_MARKER_V2", codex["agents"])

    def test_ssh_target_still_requires_profile_confirmation(self):
        (self.bin / "ssh").write_text(FAKE_SSH, encoding="utf-8")
        (self.bin / "ssh").chmod(0o755)
        self.write_config(f"[host:remote]\nssh = worker@example.invalid\nworkdir = {self.worker}\n"
                          f"sync = direct\npath = {self.bin}:{os.environ['PATH']}\ntimeout = 30\n")
        env = dict(self.env, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        result = self.dispatch(host="remote", env=env)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("此 ID 尚无远端配置；先询问是否同步、确认 ID，再执行 rdispatch profile sync", result.stderr)
        self.assertNotIn("已自动同步", result.stdout)
        self.assertFalse((self.users / "profiles").exists())
        self.assertFalse((self.users / "jobs").exists())


if __name__ == "__main__":
    unittest.main()
