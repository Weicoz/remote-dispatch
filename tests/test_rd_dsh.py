"""DSH 派单：模板、隔离目录与白名单环境。不连接设备，不调用模型。"""
import argparse
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import unittest

from remote_dispatch import cli as rd
from remote_dispatch import rd_profiles as profiles


class DshTemplateTests(unittest.TestCase):
    """断言针对内置模板；本机 config.ini 的 [agent:*] 覆盖不参与。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-dsh-cfg-")
        self.addCleanup(self.temp.cleanup)
        original = rd.CONFIG
        rd.CONFIG = str(pathlib.Path(self.temp.name) / "config.ini")
        self.addCleanup(setattr, rd, "CONFIG", original)
        self.cfg = rd.Config()

    def test_agent_template_override_is_honored(self):
        pathlib.Path(rd.CONFIG).write_text(
            '[agent:dsh]\nstart = my-dsh {model_args} - < "$RD_PROMPT"\n', encoding="utf-8")
        self.assertEqual(rd.Config().agent("dsh")["start"], 'my-dsh {model_args} - < "$RD_PROMPT"')

    def test_start_and_resume_use_headless_json(self):
        agent = self.cfg.agent("dsh")
        start = rd.agent_command(agent, None, None)
        resume = rd.agent_command(agent, None, None, session="session-123")
        self.assertIn("dsh headless --json", start)
        self.assertNotIn("--session-id", start)
        self.assertIn("--session-id session-123", resume)
        self.assertTrue(start.endswith('< "$RD_PROMPT"'))

    def test_model_flag_is_rejected(self):
        agent = self.cfg.agent("dsh")
        with self.assertRaises(SystemExit):
            rd.agent_command(agent, "m1", None)

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(SystemExit):
            rd.agent_command({"start": "", "resume": ""}, "m", None)


class DshRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-dsh-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.job = self.root / "jobs" / "employee_a-demo"
        self.job.mkdir(parents=True)
        self.auth = self.root / "credentials.yaml"
        self.auth.write_text("deepseek:\n  api_key: fixture\n", encoding="utf-8")

    def test_prepare_copies_credentials_and_marks_isolation(self):
        home = profiles.prepare_dsh_runtime(str(self.job), str(self.auth))
        self.assertEqual(home, (self.job / "dsh-home").resolve())
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        copied = home / ".credentials.yaml"
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)
        self.assertEqual(copied.read_text(encoding="utf-8"), self.auth.read_text(encoding="utf-8"))
        self.assertIn("隔离工作区", (home / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((home / "rd-isolation.json").exists())

    def test_missing_auth_file_is_refused(self):
        with self.assertRaises(ValueError):
            profiles.prepare_dsh_runtime(str(self.job), str(self.root / "absent.yaml"))

    def test_unmarked_existing_home_is_refused(self):
        (self.job / "dsh-home").mkdir()
        with self.assertRaises(ValueError):
            profiles.prepare_dsh_runtime(str(self.job), str(self.auth))

    def test_second_prepare_reuses_home(self):
        home = profiles.prepare_dsh_runtime(str(self.job), str(self.auth))
        agents = home / "AGENTS.md"
        agents.write_text("kept", encoding="utf-8")
        again = profiles.prepare_dsh_runtime(str(self.job), str(self.auth))
        self.assertEqual(again, home)
        self.assertEqual(agents.read_text(encoding="utf-8"), "kept")

    def test_refresh_overwrites_only_the_credentials_file(self):
        home = profiles.prepare_dsh_runtime(str(self.job), str(self.auth))
        (home / "AGENTS.md").write_text("kept", encoding="utf-8")
        self.auth.write_text("deepseek:\n  api_key: renewed\n", encoding="utf-8")
        profiles.prepare_dsh_runtime(str(self.job), str(self.auth))
        self.assertIn("fixture", (home / ".credentials.yaml").read_text(encoding="utf-8"))
        profiles.prepare_dsh_runtime(str(self.job), str(self.auth), refresh=True)
        self.assertIn("renewed", (home / ".credentials.yaml").read_text(encoding="utf-8"))
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8"), "kept")

    def test_environment_is_whitelisted(self):
        home = profiles.prepare_dsh_runtime(str(self.job), str(self.auth))
        inherited = {"PATH": "/usr/bin", "HOME": "/home/x", "DEEPSEEK_API_KEY": "k",
                     "DSH_MODEL": "m", "RD_JOB": "j", "AWS_SECRET_ACCESS_KEY": "nope",
                     "CODEX_HOME": "/home/x/.codex"}
        env = profiles.dsh_environment(home, inherited)
        self.assertEqual(env["DSH_HOME"], str(home))
        self.assertEqual(env["DEEPSEEK_API_KEY"], "k")
        self.assertEqual(env["RD_JOB"], "j")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("CODEX_HOME", env)

    def test_git_identity_is_injected(self):
        (self.job / "dsh-git.json").write_text('{"name": "RD", "email": "rd@example.invalid"}', encoding="utf-8")
        home = profiles.prepare_dsh_runtime(str(self.job), str(self.auth))
        env = profiles.dsh_environment(home, {"PATH": "/usr/bin"})
        self.assertEqual(env["GIT_AUTHOR_NAME"], "RD")
        self.assertEqual(env["GIT_COMMITTER_EMAIL"], "rd@example.invalid")


class DshRunScriptTests(unittest.TestCase):
    def test_run_script_wraps_isolated_dsh_job(self):
        host = dict(rd.DEFAULTS, name="fixture", ssh="u@h", platform="posix", codex_auth_file="", dsh_auth_file="~/creds.yaml")
        rec = {"job": "employee_a-demo-r1", "sync": "direct", "user_id": "employee_a",
               "profile": None, "isolated": True, "agent_kind": "dsh", "auth_file": "~/creds.yaml",
               "handoff_version": 1, "agent": "dsh"}
        script = rd.build_run_script(host, rec, 1, 60)
        self.assertIn("rd_profiles.py", script)
        self.assertIn("~/creds.yaml", script)
        self.assertIn("dsh", script)


if __name__ == "__main__":
    unittest.main()
