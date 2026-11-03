"""非 Git 产物取回与派单前环境指纹核对：真实临时 Git 仓库与 rsync，ssh 用本机 shell 模拟。"""
import argparse
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

import test_rd_dispatch as base
from remote_dispatch import rd_artifacts as artifacts
from test_rd import rd


class Fixture(unittest.TestCase):
    """复用 DispatchTests 的仓库、配置与本机 ssh 模拟，但不重复运行它的用例。"""
    def setUp(self):
        self.t = base.DispatchTests("test_include_uses_selected_repository_root_with_spaces_and_option_names")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.repo, self.root, self.worker = self.t.repo, self.t.root, self.t.worker

    def worktree(self, record):
        return self.worker / "wt" / record["job"]

    def second_host(self):
        """追加第二台目标机 fixture2，与 fixture 共用 ssh 模拟，工作目录不同。"""
        with open(rd.CONFIG, "a") as stream:
            stream.write(f"\n[host:fixture2]\nssh=fixture2.invalid\nworkdir={self.root / 'worker2'}\n"
                         f"sync=direct\npath={os.environ['PATH']}\n")
        self.t.cfg = rd.Config()
        return self.t.cfg

    def fetch_args(self, record, **overrides):
        values = dict(job=record["job"], artifacts=True)
        values.update(overrides)
        return argparse.Namespace(**values)


class ArtifactPathTests(unittest.TestCase):
    def test_accepts_relative_paths_and_normalises(self):
        self.assertEqual(artifacts.parse_artifacts(["./out/a.txt", "out/a.txt", "dist/", "中文 目录/x.bin"]),
                         ["out/a.txt", "dist", "中文 目录/x.bin"])

    def test_rejects_absolute_escaping_root_git_and_glob_paths(self):
        for bad in ("/etc/passwd", "~/x", "C:/x", "../x", "a/../../x", "..", ".", "./", ".git/config",
                    "sub/.git", "out/*.txt", "a;b", "a\nb", ""):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                artifacts.parse_artifacts([bad])


class ArtifactTests(Fixture):
    def test_invalid_artifact_stops_before_any_remote_work(self):
        errors = io.StringIO()
        with patch.object(rd, "ssh") as ssh, patch.object(rd, "pick_host") as pick, \
             contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as stop:
            rd.cmd_run(self.t.args(artifact=["../outside"], force_host=False), self.t.cfg)
        self.assertEqual(stop.exception.code, 4)
        self.assertIn("..", errors.getvalue())
        ssh.assert_not_called()
        pick.assert_not_called()
        self.assertEqual(rd.all_jobs(), [])

    def test_paths_are_recorded_in_job(self):
        record = self.t.dispatch(artifact=["./out/report.txt", "dist"])
        self.assertEqual(record["artifact_paths"], ["out/report.txt", "dist"])

    def test_fetch_artifacts_pulls_file_and_directory_and_reports_sizes(self):
        record = self.t.dispatch(artifact=["out/report.txt", "dist"])
        wt = self.worktree(record)
        (wt / "out").mkdir()
        (wt / "out" / "report.txt").write_text("hello")
        (wt / "dist" / "deep").mkdir(parents=True)
        (wt / "dist" / "a.bin").write_bytes(b"x" * 10)
        (wt / "dist" / "deep" / "b.bin").write_bytes(b"y" * 5)
        output = self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertEqual((self.repo / "out" / "report.txt").read_text(), "hello")
        self.assertEqual((self.repo / "dist" / "deep" / "b.bin").read_bytes(), b"y" * 5)
        self.assertIn("已拉回  out/report.txt  1 个文件，5B", output)
        self.assertIn("已拉回  dist  2 个文件，15B", output)
        saved = rd.load_job(record["job"])
        self.assertFalse(artifacts.artifacts_pending(saved))

    def test_missing_path_is_reported_without_failing_the_whole_fetch(self):
        record = self.t.dispatch(artifact=["out/here.txt", "out/gone.txt"])
        (self.worktree(record) / "out").mkdir()
        (self.worktree(record) / "out" / "here.txt").write_text("1")
        output = self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertIn("缺失  out/gone.txt", output)
        self.assertIn("已拉回  out/here.txt", output)
        self.assertTrue((self.repo / "out" / "here.txt").exists())
        self.assertFalse((self.repo / "out" / "gone.txt").exists())
        self.assertFalse(artifacts.artifacts_pending(rd.load_job(record["job"])))

    def test_fetch_without_declared_artifacts_is_rejected(self):
        record = self.t.dispatch()
        with self.assertRaises(SystemExit) as stop:
            rd.cmd_fetch(self.fetch_args(record), self.t.cfg)
        self.assertEqual(stop.exception.code, 4)

    def test_symlinked_local_destination_outside_repository_is_refused(self):
        record = self.t.dispatch(artifact=["linked/x.txt"])
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.repo / "linked")
        (self.worktree(record) / "linked").mkdir()
        (self.worktree(record) / "linked" / "x.txt").write_text("leak")
        with self.assertRaises(SystemExit):
            self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertFalse((outside / "x.txt").exists())

    def test_rsync_failure_keeps_artifacts_pending(self):
        record = self.t.dispatch(artifact=["out.txt"])
        (self.worktree(record) / "out.txt").write_text("1")
        real = rd.subprocess.run

        def run(cmd, **kwargs):
            if cmd[0] == "rsync":
                raise OSError("no rsync")
            return real(cmd, **kwargs)
        with patch.object(rd.subprocess, "run", side_effect=run), self.assertRaises(SystemExit):
            self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertTrue(artifacts.artifacts_pending(rd.load_job(record["job"])))

    def test_wait_pulls_once_after_finish_and_lists_files(self):
        record = self.t.dispatch(artifact=["out.txt"])
        (self.worktree(record) / "out.txt").write_text("done")
        with self.assertRaises(SystemExit) as stop:
            output = self.t.capture(rd.cmd_wait, argparse.Namespace(job=record["job"], timeout=5, interval=0.01))
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual((self.repo / "out.txt").read_text(), "done")
        (self.repo / "out.txt").write_text("local edit")
        with self.assertRaises(SystemExit):
            self.t.capture(rd.cmd_wait, argparse.Namespace(job=record["job"], timeout=5, interval=0.01))
        self.assertEqual((self.repo / "out.txt").read_text(), "local edit")  # 已拉回的不再重复拉

    def test_wait_output_names_fetched_files(self):
        record = self.t.dispatch(artifact=["out.txt"])
        (self.worktree(record) / "out.txt").write_text("done")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit):
            rd.cmd_wait(argparse.Namespace(job=record["job"], timeout=5, interval=0.01), self.t.cfg)
        self.assertIn("已拉回  out.txt  1 个文件，4B", buffer.getvalue())

    def test_receipt_lists_artifacts_once_collected(self):
        record = self.t.dispatch(artifact=["out.txt", "gone.txt"])
        (self.worktree(record) / "out.txt").write_text("done")
        snapshot = rd.job_states(self.t.cfg, [record])[record["job"]]
        receipt = rd.collect_result(self.t.cfg, rd.load_job(record["job"]), snapshot)
        results = {item["path"]: item["status"] for item in receipt["artifacts"]["results"]}
        self.assertEqual(results, {"out.txt": "fetched", "gone.txt": "missing"})
        saved = json.loads(rd.result_path(record, receipt["round"]).read_text())
        self.assertEqual(saved["artifacts"]["paths"], ["out.txt", "gone.txt"])

    def test_clean_expired_does_not_reclaim_while_artifacts_are_not_pulled(self):
        record = self.t.dispatch(artifact=["out.txt"])
        host = rd.job_host(self.t.cfg, record)
        code, detail, _, _ = rd.reclaim_check(self.t.cfg, record, {"state": "done"}, host,
                                              a=self.t.bulk_args(merged=False, expired=True), mode="expired")
        self.assertEqual(code, rd.BLOCK_ARTIFACTS_PENDING)
        self.assertIn("fetch", detail)
        (self.worktree(record) / "out.txt").write_text("1")
        self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        code, _, _, _ = rd.reclaim_check(self.t.cfg, rd.load_job(record["job"]), {"state": "done"}, host,
                                         a=self.t.bulk_args(merged=False, expired=True), mode="expired")
        self.assertNotEqual(code, rd.BLOCK_ARTIFACTS_PENDING)


class FingerprintTests(Fixture):
    def configure(self, command, **extra):
        self.t.configure_project(fingerprint=command, **extra)

    def test_matching_fingerprint_lets_the_job_start(self):
        self.configure("cat tracked.txt")
        record = self.t.dispatch(project="app")
        self.assertEqual(record["dispatch_state"], "started")
        self.assertTrue(record["fingerprint"]["match"])
        self.assertEqual(record["fingerprint"]["local"], "base\n")

    def test_setup_runs_before_remote_fingerprint(self):
        # 本机已有 from-setup.txt；目标机上只有 setup 生成后才一致。
        (self.repo / "from-setup.txt").write_text("x\n")
        self.configure("cat from-setup.txt 2>/dev/null || echo missing", setup="echo x > from-setup.txt")
        record = self.t.dispatch(project="app")
        self.assertEqual(record["dispatch_state"], "started")
        self.assertTrue((self.worker / "jobs" / record["job"] / "setup.done").exists())

    def test_mismatch_rejects_with_diff_and_records_both_fingerprints(self):
        self.configure('echo "wt=${RD_WT:+remote}"')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as stop:
            self.t.dispatch(project="app")
        self.assertEqual(stop.exception.code, 5)
        text = errors.getvalue()
        self.assertIn("--- local", text)
        self.assertIn("+++ remote:fixture", text)
        self.assertIn("-wt=", text)
        self.assertIn("+wt=remote", text)
        record = rd.all_jobs()[0]
        self.assertEqual(record["dispatch_state"], "fingerprint_rejected")
        self.assertEqual((record["fingerprint"]["local"], record["fingerprint"]["remote"]), ("wt=\n", "wt=remote\n"))
        self.assertFalse(record["fingerprint"]["match"])
        status = self.t.queried_job(record["job"])
        self.assertEqual(status["state"], "done")
        self.assertEqual(str(status["exit"]), "95")
        stdout = (self.worker / "jobs" / record["job"] / "stdout.r1.log").read_text()
        self.assertIn("+wt=remote", stdout)

    def test_mismatch_never_launches_the_job_body(self):
        self.configure('echo "wt=${RD_WT:+remote}"')
        with patch.object(rd, "launch") as launch, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.t.dispatch(project="app")
        launch.assert_not_called()

    def test_rejected_receipt_carries_fingerprints(self):
        self.configure('echo "wt=${RD_WT:+remote}"')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.t.dispatch(project="app")
        record = rd.all_jobs()[0]
        snapshot = rd.job_states(self.t.cfg, [record])[record["job"]]
        receipt = rd.collect_result(self.t.cfg, record, snapshot)
        self.assertEqual((receipt["fingerprint"]["local"], receipt["fingerprint"]["remote"]), ("wt=\n", "wt=remote\n"))

    def test_remote_command_failure_is_a_rejection(self):
        self.configure('[ -n "$RD_WT" ] && exit 3; echo ok')
        with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as stop:
            self.t.dispatch(project="app")
        self.assertEqual(stop.exception.code, 5)
        self.assertIn("退出码 3", errors.getvalue())

    def test_local_command_failure_stops_before_dispatch(self):
        self.configure("exit 7")
        with patch.object(rd, "ssh") as ssh, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            self.t.dispatch(project="app")
        self.assertEqual(stop.exception.code, 4)
        ssh.assert_not_called()

    def test_timeout_is_configurable_and_reported(self):
        self.configure('[ -n "$RD_WT" ] && sleep 5; echo ok', fingerprint_timeout="1")
        shell = self.t.local_shell

        def ssh(host, script, check=True, **kwargs):  # 与真实 ssh() 一致：超时且 check=False 返回 None
            try:
                return shell(host, script, check=check, **kwargs)
            except subprocess.TimeoutExpired:
                return None
        with patch.object(rd, "ssh", side_effect=ssh), contextlib.redirect_stderr(io.StringIO()) as errors, \
             self.assertRaises(SystemExit) as stop:
            self.t.dispatch(project="app")
        self.assertEqual(stop.exception.code, 5)
        self.assertIn("超过 1 秒", errors.getvalue())

    def test_invalid_timeout_is_rejected(self):
        self.configure("true", fingerprint_timeout="abc")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            self.t.dispatch(project="app")
        self.assertEqual(stop.exception.code, 4)

    def test_skip_fingerprint_warns_and_dispatches(self):
        self.configure('echo "wt=${RD_WT:+remote}"')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            record = self.t.dispatch(project="app", skip_fingerprint=True)
        self.assertIn("--skip-fingerprint", errors.getvalue())
        self.assertEqual(record["dispatch_state"], "started")
        self.assertTrue(record["fingerprint"]["skipped"])

    def test_auto_moves_to_next_candidate_when_first_mismatches(self):
        cfg = self.second_host()
        # fixture 的工作根目录放一个标记，使其指纹与本机不同；fixture2 没有标记，与本机一致。
        self.worker.mkdir(parents=True, exist_ok=True)
        (self.worker / "marker.txt").write_text("other\n")
        self.configure('cat "$RD_WT/../../marker.txt" 2>/dev/null || echo base')
        hosts = [cfg.host("fixture"), cfg.host("fixture2")]
        with patch.object(rd, "pick_hosts", return_value=hosts), contextlib.redirect_stderr(io.StringIO()) as errors:
            record = self.t.dispatch(project="app", host="auto", force_host=False)
        self.assertIn("换下一台候选", errors.getvalue())
        self.assertEqual(record["host"], "fixture2")
        self.assertEqual(record["dispatch_state"], "started")
        self.assertTrue(record["fingerprint"]["match"])
        self.assertEqual(len(rd.all_jobs()), 1)
        self.assertFalse((self.worker / "wt" / record["job"]).exists())  # 第一台的 worktree 已清掉
        self.assertTrue((self.root / "worker2" / "users" / "employee_a" / "wt" / record["job"]).is_dir())

    def test_auto_fails_when_every_candidate_mismatches(self):
        cfg = self.second_host()
        self.configure('echo "wt=${RD_WT:+remote}"')
        hosts = [cfg.host("fixture"), cfg.host("fixture2")]
        with patch.object(rd, "pick_hosts", return_value=hosts), contextlib.redirect_stderr(io.StringIO()) as errors, \
             self.assertRaises(SystemExit) as stop:
            self.t.dispatch(project="app", host="auto", force_host=False)
        self.assertEqual(stop.exception.code, 5)
        self.assertIn("[fixture]", errors.getvalue())
        self.assertIn("[fixture2]", errors.getvalue())
        record = rd.all_jobs()[0]
        self.assertEqual(record["host"], "fixture2")
        self.assertEqual([item["host"] for item in record["fingerprint"]["tried"]], ["fixture", "fixture2"])

    def test_doctor_reports_match_and_mismatch(self):
        for command, expected, code in (("echo same", "环境指纹一致", 0),
                                        ('echo "wt=${RD_WT:+remote}"', "环境指纹与本机不一致", 1)):
            with self.subTest(command=command):
                if not self.t.cfg.cp.has_section("project:repo---"):  # doctor 按仓库目录名（无 origin）找项目
                    self.t.cfg.cp.add_section("project:repo---")
                self.t.cfg.cp.set("project:repo---", "fingerprint", command)
                output = io.StringIO()
                args = argparse.Namespace(host="fixture", agent=None, dir=str(self.repo))
                with patch.object(rd, "probe", return_value=dict(reachable=False, error="x")), \
                     patch.object(rd, "assess", return_value=[]), patch.object(rd, "fmt_probe", return_value=""), \
                     contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stop:
                    rd.cmd_doctor(args, self.t.cfg)
                self.assertIn(expected, output.getvalue())
                self.assertEqual(stop.exception.code, code)


class WindowsBranchTests(Fixture):
    """Windows 目标不经 sh：指纹走 PowerShell（win_ps），产物经原生 worker 取回。"""
    def win_host(self):
        return dict(self.t.cfg.host("fixture"), platform="windows", workdir="C:/rd", python="python", path="")

    def done(self, out="", code=0):
        return subprocess.CompletedProcess([], code, out, "")

    def test_fingerprint_windows_preferred_for_windows_targets_only(self):
        proj = {"fingerprint": "node -v", "fingerprint_windows": "node -v | Out-String"}
        self.assertEqual(rd.host_fingerprint_command(proj, self.win_host(), "node -v"), "node -v | Out-String")
        self.assertEqual(rd.host_fingerprint_command(proj, self.t.cfg.host("fixture"), "node -v"), "node -v")
        self.assertEqual(rd.host_fingerprint_command({"fingerprint": "x"}, self.win_host(), "x"), "x")

    def test_windows_match_and_mismatch_use_powershell_not_sh(self):
        rec = {"job": "j1", "bare": "repos/x.git"}
        with patch.object(rd, "win_ps", return_value=self.done("v20\r\n")) as ps, patch.object(rd, "ssh") as sh:
            rd.check_fingerprint(self.win_host(), rec, "node -v", "v20\n", 30, 30)
        sh.assert_not_called()
        self.assertEqual(ps.call_count, 2)  # setup + 指纹
        self.assertTrue(rec["fingerprint"]["match"])
        with patch.object(rd, "win_ps", return_value=self.done("v18\n")), \
             self.assertRaises(rd.FingerprintMismatch) as mismatch:
            rd.check_fingerprint(self.win_host(), rec, "node -v", "v20\n", 30, 30)
        self.assertIn("+v18", mismatch.exception.attempt["diff"])


class WindowsRunTests(Fixture):
    """cmd_run 在 Windows 目标上的端到端：worker 用本机临时实现，ssh/PowerShell 用 win_ps 桩模拟。"""
    def setUp(self):
        super().setUp()
        text = Path(rd.CONFIG).read_text()
        Path(rd.CONFIG).write_text(text.replace("sync=direct\n", "sync=direct\nplatform=windows\n", 1))
        self.t.cfg = rd.Config()
        self.scripts = []
        self.remote_output = "base\n"

    def fake_win_ps(self, host, script, timeout):
        self.scripts.append(script)
        out = self.remote_output if "-EncodedCommand" in script else ""
        return subprocess.CompletedProcess([], 0, out, "")

    def run_windows(self, **kwargs):
        with patch.object(rd, "win_ps", side_effect=self.fake_win_ps):
            return self.t.dispatch(**kwargs)

    def test_windows_fingerprint_match_runs_powershell_in_task_worktree(self):
        self.t.configure_project(fingerprint="cat tracked.txt", fingerprint_windows="Get-Content tracked.txt")
        record = self.run_windows(project="app")
        self.assertEqual(record["dispatch_state"], "started")
        self.assertTrue(record["fingerprint"]["match"])
        self.assertEqual(record["fingerprint"]["command"], "Get-Content tracked.txt")
        probe = self.scripts[-1]
        self.assertIn("powershell.exe", probe)
        self.assertIn("wt/" + record["job"], probe)

    def test_windows_fingerprint_mismatch_rejects_before_launch(self):
        self.t.configure_project(fingerprint="cat tracked.txt")
        self.remote_output = "other\n"
        errors = io.StringIO()
        with patch.object(rd, "win_ps", side_effect=self.fake_win_ps), contextlib.redirect_stderr(errors), \
             self.assertRaises(SystemExit) as stop:
            self.t.dispatch(project="app")
        self.assertEqual(stop.exception.code, 5)
        self.assertIn("+other", errors.getvalue())
        record = rd.all_jobs()[0]
        self.assertEqual(record["dispatch_state"], "fingerprint_rejected")
        self.assertFalse(record["fingerprint"]["match"])

    def test_windows_candidates_are_not_filtered_for_auto(self):
        self.t.configure_project(fingerprint="cat tracked.txt")
        with patch.object(rd, "pick_hosts", return_value=[self.t.cfg.host("fixture")]) as pick:
            self.run_windows(project="app", host="auto", force_host=False)
        pick.assert_called_once()

    def test_windows_artifacts_are_pulled_through_worker_and_rerunnable(self):
        record = self.run_windows(artifact=["out dir/a b.txt", "dist"])
        wt = self.worker / "wt" / record["job"]
        (wt / "out dir").mkdir()
        (wt / "out dir" / "a b.txt").write_text("hello")
        (wt / "dist" / "deep").mkdir(parents=True)
        (wt / "dist" / "deep" / "b.bin").write_bytes(b"y" * 5)
        self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertEqual((self.repo / "out dir" / "a b.txt").read_text(), "hello")
        self.assertEqual((self.repo / "dist" / "deep" / "b.bin").read_bytes(), b"y" * 5)
        self.assertFalse(artifacts.artifacts_pending(rd.load_job(record["job"])))

    def test_windows_failure_stays_pending_missing_is_terminal(self):
        record = self.run_windows(artifact=["out.txt", "gone.txt"])
        (self.worker / "wt" / record["job"] / "out.txt").write_text("1")
        real = rd.native.request

        def request(host, operation, *args, **kwargs):
            if operation == "fetch-artifact":
                raise ValueError("ssh 断开")
            return real(host, operation, *args, **kwargs)
        with patch.object(rd.native, "request", side_effect=request), self.assertRaises(SystemExit) as stop:
            self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertEqual(stop.exception.code, 1)
        saved = {i["path"]: i["status"] for i in rd.load_job(record["job"])["artifacts_state"]["results"]}
        self.assertEqual(saved, {"out.txt": "error", "gone.txt": "missing"})
        self.assertTrue(artifacts.artifacts_pending(rd.load_job(record["job"])))
        self.t.capture(rd.cmd_fetch, self.fetch_args(record))  # 重试成功
        self.assertFalse(artifacts.artifacts_pending(rd.load_job(record["job"])))

    def test_windows_worker_refuses_symlink_artifacts(self):
        record = self.run_windows(artifact=["link"])
        os.symlink(self.root, self.worker / "wt" / record["job"] / "link")
        with self.assertRaises(SystemExit):
            self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertIn("符号链接", rd.load_job(record["job"])["artifacts_state"]["results"][0]["error"])


class TrackedAndQuotingTests(Fixture):
    def test_run_rejects_artifact_pointing_at_tracked_file_or_directory_with_tracked_files(self):
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("x")
        self.t.git("add", "src/app.py")
        self.t.git("commit", "-qm", "src")
        for path in ("tracked.txt", "src", "src/app.py", "tracked.txt/child"):
            with self.subTest(path=path):
                errors = io.StringIO()
                with patch.object(rd, "ssh") as ssh, contextlib.redirect_stderr(errors), \
                     self.assertRaises(SystemExit) as stop:
                    self.t.dispatch(artifact=[path])
                self.assertEqual(stop.exception.code, 4)
                self.assertIn("Git 跟踪", errors.getvalue())
                ssh.assert_not_called()
        self.assertEqual(rd.all_jobs(), [])

    def test_fetch_skips_target_that_became_tracked_after_dispatch(self):
        record = self.t.dispatch(artifact=["late.txt", "ok.txt"])
        (self.worktree(record) / "late.txt").write_text("remote")
        (self.worktree(record) / "ok.txt").write_text("fine")
        (self.repo / "late.txt").write_text("source")
        self.t.git("add", "late.txt")
        output = self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertEqual((self.repo / "late.txt").read_text(), "source")
        self.assertEqual((self.repo / "ok.txt").read_text(), "fine")
        self.assertIn("已跳过  late.txt", output)
        self.assertFalse(artifacts.artifacts_pending(rd.load_job(record["job"])))

    def run_pull(self, supports, path):
        record = self.t.dispatch(artifact=[path])
        (self.worktree(record) / Path(path).parent).mkdir(parents=True, exist_ok=True)
        (self.worktree(record) / path).write_text("v")
        seen = []
        real = rd.subprocess.run

        def run(cmd, **kwargs):
            if cmd[0] == "rsync" and "--help" not in cmd:
                seen.append(cmd)
            return real(cmd, **kwargs) if cmd[0] != "rsync" or "--help" in cmd else subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(rd, "rsync_supports_protect_args", return_value=supports), \
             patch.object(rd, "scp_path", side_effect=lambda h, rel: "fixture.invalid:/r/" + rel), \
             patch.object(rd.subprocess, "run", side_effect=run):
            rd.pull_artifacts(self.t.cfg, rd.load_job(record["job"]))
        return seen[0]

    def test_rsync_protects_args_or_quotes_remote_path_with_spaces(self):
        command = self.run_pull(True, "my dir/a b.txt")
        self.assertIn("-s", command)
        self.assertIn("fixture.invalid:/r/wt/", " ".join(command))
        self.assertNotIn("'", " ".join(command))
        command = self.run_pull(False, "my dir/a b.txt")
        self.assertNotIn("-s", command)
        self.assertTrue(any(arg.endswith("'") and arg.startswith("fixture.invalid:'") for arg in command))

    def test_space_path_really_transfers(self):
        record = self.t.dispatch(artifact=["my dir/a b.txt"])
        (self.worktree(record) / "my dir").mkdir()
        (self.worktree(record) / "my dir" / "a b.txt").write_text("sp")
        self.t.capture(rd.cmd_fetch, self.fetch_args(record))
        self.assertEqual((self.repo / "my dir" / "a b.txt").read_text(), "sp")


class DoctorLocalFailureTests(Fixture):
    def test_local_fingerprint_failure_prints_missing_and_returns_one(self):
        self.t.cfg.cp.add_section("project:repo---")
        self.t.cfg.cp.set("project:repo---", "fingerprint", "exit 3")
        output = io.StringIO()
        args = argparse.Namespace(host="fixture", agent=None, dir=str(self.repo))
        with patch.object(rd, "probe", return_value=dict(reachable=False, error="x")), \
             patch.object(rd, "assess", return_value=[]), patch.object(rd, "fmt_probe", return_value="PROBE"), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit) as stop:
            rd.cmd_doctor(args, self.t.cfg)
        self.assertIn("MISSING", output.getvalue())
        self.assertIn("PROBE", output.getvalue())  # doctor 没被打断，后续探针照常输出
        self.assertEqual(stop.exception.code, 1)




class FingerprintTimeoutTypeTests(unittest.TestCase):
    def test_string_setup_timeout_from_config_is_coerced_to_int(self):
        from unittest import mock
        import remote_dispatch.cli as c
        seen = {}

        def fake_ssh(host, script, check=True, timeout=None, **kw):
            seen["timeout"] = timeout
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(c, "ssh", fake_ssh), \
                mock.patch.object(c, "run_remote_fingerprint", return_value=("fp", None)):
            c.check_fingerprint({"platform": "posix", "name": "h"}, {"job": "j"}, "cmd", "fp", 120, "3600")
        self.assertEqual(seen["timeout"], 3600)
        self.assertIsInstance(seen["timeout"], int)
