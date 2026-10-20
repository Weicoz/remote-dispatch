"""本机派发（ssh = local）回归；不连接远端，不调用模型。"""
import os
from pathlib import Path
import tempfile
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


if __name__ == "__main__":
    unittest.main()
