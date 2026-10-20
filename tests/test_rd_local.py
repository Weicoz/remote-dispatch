"""本机派发（ssh = local）回归；不连接远端，不调用模型。"""
import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
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
            rd.cmd_doctor(argparse.Namespace(host="self", agent="codex"), cfg)
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


if __name__ == "__main__":
    unittest.main()
