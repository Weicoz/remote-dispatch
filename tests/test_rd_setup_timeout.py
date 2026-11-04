"""setup 与主体共用同一超时预算：setup 卡住时 --timeout 生效，超时回执标明阶段。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_native as native
from test_rd import rd
from test_rd_artifact_fingerprint import Fixture


class ShellSetupTimeoutTests(Fixture):
    def run_script(self, setup, body, timeout):
        record = self.t.dispatch(cmd=body)
        job = self.worker / "jobs" / record["job"]
        for name in ("exit_code", "setup.done", "pid"):
            (job / name).unlink(missing_ok=True)
        (job / "setup.sh").write_text(setup + "\n")
        (job / "cmd.r1.sh").write_text(body + "\n")
        shutil.copy(Path(cli.HERE, "rd_messages.py"), job / "rd_messages.py")
        host = rd.scoped_host(self.t.cfg.host("fixture"), "employee_a")
        script = job / "run.r1.sh"
        script.write_text(rd.build_run_script(host, record, 1, timeout))
        started = time.monotonic()
        subprocess.run(["sh", str(script)], timeout=60, capture_output=True)
        return job, time.monotonic() - started

    def timing(self, job):
        return (job / "timing.r1").read_text()

    def test_hung_setup_is_killed_by_the_timeout_and_marked_as_setup_phase(self):
        job, elapsed = self.run_script("sleep 30", "true", 2)
        self.assertLess(elapsed, 12)
        self.assertEqual((job / "exit_code").read_text().strip(), "124")
        self.assertIn("timeout_phase=setup", self.timing(job))
        self.assertFalse((job / "setup.done").exists())
        report = json.loads((job / "reports" / "result.r1.json").read_text())
        self.assertEqual((report["timeout_phase"], report["executor_exit"], report["outcome"]), ("setup", 124, "failed"))

    def test_body_gets_only_the_remaining_budget_and_is_marked_as_body_phase(self):
        job, elapsed = self.run_script("sleep 2", "sleep 30", 4)
        self.assertLess(elapsed, 4 + 6)  # 总预算 4 秒，而不是 setup 2 秒 + 主体 4 秒
        self.assertEqual((job / "exit_code").read_text().strip(), "124")
        self.assertIn("timeout_phase=body", self.timing(job))
        report = json.loads((job / "reports" / "result.r1.json").read_text())
        self.assertEqual(report["timeout_phase"], "body")

    def test_normal_and_failed_setup_are_unchanged(self):
        job, _ = self.run_script("true", "true", 30)
        self.assertEqual((job / "exit_code").read_text().strip(), "0")
        self.assertNotIn("timeout_phase", self.timing(job))
        self.assertTrue((job / "setup.done").exists())
        job, _ = self.run_script("exit 5", "true", 30)
        self.assertEqual((job / "exit_code").read_text().strip(), "97")
        self.assertNotIn("timeout_phase", self.timing(job))

    def test_timeout_phase_parser_accepts_only_known_values(self):
        directory = self.root / "phase"
        directory.mkdir()
        (directory / "timing.r1").write_text("setup_seconds=1\ntimeout_phase=setup\ntimeout_phase=elsewhere\n")
        self.assertEqual(messages.phase_timings(directory, 1)["timeout_phase"], "setup")
        (directory / "timing.r1").write_text("setup_seconds=1\ntimeout_phase=bogus\n")
        self.assertNotIn("timeout_phase", messages.phase_timings(directory, 1))


class FingerprintBudgetTests(Fixture):
    def setUp(self):
        super().setUp()
        self.t.configure_project(fingerprint="echo fp", setup="true")

    def launched_timeout(self, spent):
        seen = []
        with patch.object(rd, "check_fingerprint", return_value=spent), \
                patch.object(rd, "launch", side_effect=lambda host, rec, number, cmd, prompt, timeout:
                             (seen.append(timeout), self.t.finish(host, rec, number))[1]):
            self.t.dispatch(project="app", timeout=100)
        return seen[0]

    def test_launch_gets_the_budget_minus_the_setup_already_run_by_the_fingerprint_check(self):
        self.assertEqual(self.launched_timeout(30.7), 70)
        self.assertEqual(self.launched_timeout(0), 100)

    def test_remaining_budget_is_at_least_one_second(self):
        self.assertEqual(self.launched_timeout(500), 1)
        self.assertEqual(rd.remaining_timeout("60", 59.9), 1)
        self.assertEqual(rd.remaining_timeout(60, None), 60)

    def test_check_fingerprint_reports_the_setup_seconds(self):
        record = self.t.dispatch()
        host = rd.scoped_host(self.t.cfg.host("fixture"), "employee_a")
        done = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(rd, "ssh", side_effect=lambda *a, **k: (time.sleep(0.2), done)[1]), \
                patch.object(rd, "run_remote_fingerprint", return_value=("fp", "")):
            spent = rd.check_fingerprint(host, record, "echo fp", "fp", 30, 100)
        self.assertGreaterEqual(spent, 0.2)
        self.assertLess(spent, 5)


class NativeSetupTimeoutTests(Fixture):
    def run_native(self, setup, command, timeout, test=False):
        record = self.t.dispatch()
        directory, wt = self.root / "n-job", self.root / "n-wt"
        for folder in (directory, wt, self.root / "n-bin"):
            folder.mkdir(exist_ok=True)
        (directory / "prompt.r1.md").write_text("")
        (directory / "setup.ps1").write_text(setup)
        spec = dict(record, round=1, user_id="employee_a", timeout=timeout, max_heavy=1, heavy=False, cmd=command,
                    directory=str(directory), wt=str(wt), bin=str(self.root / "n-bin"),
                    lock_dir=str(self.root / "locks"), sync="direct", profile=None, handoff_version=1, test=test, agent=None)
        spec_file = self.root / "n-spec.json"
        spec_file.write_text(json.dumps(spec))
        self.addCleanup(os.chdir, os.getcwd())
        # 测试里 shell_command 直接交给本机 sh 执行，等价于 Windows 的 PowerShell 入口。
        with patch.object(native, "shell_command", side_effect=lambda text: ["sh", "-c", text]):
            started = time.monotonic()
            native.run_job(str(spec_file))
            elapsed = time.monotonic() - started
        return directory, elapsed, None

    def test_native_setup_timeout_exits_124_with_setup_phase_and_receipt(self):
        directory, elapsed, _ = self.run_native("sleep 30", "true", 2)
        self.assertLess(elapsed, 12)
        self.assertEqual((directory / "exit_code").read_text().strip(), "124")
        self.assertIn("timeout_phase=setup", (directory / "timing.r1").read_text())
        report = json.loads((directory / "reports" / "result.r1.json").read_text())
        self.assertEqual((report["timeout_phase"], report["executor_exit"]), ("setup", 124))

    def test_native_setup_timeout_and_failure_receipts_follow_the_test_checks_rule(self):
        for setup, code, checks in (("sleep 30", 124, "failed"), ("exit 5", 97, "failed")):
            with self.subTest(code=code):
                directory, _, _ = self.run_native(setup, "true", 2, test=True)
                report = json.loads((directory / "reports" / "result.r1.json").read_text())
                self.assertEqual((report["executor_exit"], report["checks"], report["test_command"]), (code, checks, "true"))
                shutil.rmtree(directory)

    def test_native_body_shares_the_budget_and_reports_body_phase(self):
        directory, elapsed, _ = self.run_native("sleep 2", "sleep 30", 4)
        self.assertLess(elapsed, 4 + 6)
        self.assertEqual((directory / "exit_code").read_text().strip(), "124")
        report = json.loads((directory / "reports" / "result.r1.json").read_text())
        self.assertEqual(report["timeout_phase"], "body")


if __name__ == "__main__":
    unittest.main()
