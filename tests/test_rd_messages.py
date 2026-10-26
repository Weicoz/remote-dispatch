"""本地回执/会话路由回归，不连接远端或调用模型。"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_native as native
from test_rd import rd, WorkerTests


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-messages-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_progress_appends_each_update_and_keeps_latest_with_existing_summary_limit(self):
        first = messages.report(self.root, "job-a", 1, "progress", "reading", "读取\n票据")
        second = messages.report(self.root, "job-a", 1, "progress", "checking", "中" * 8000)
        history = self.root / "reports/progress.r1.jsonl"
        rows = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows, [dict(updated_at=first["updated_at"], phase="reading", summary="读取\n票据"),
                                dict(updated_at=second["updated_at"], phase="checking", summary="中" * 8000)])
        self.assertEqual(messages.read_json(self.root / "reports/progress.r1.json"), second)
        self.assertEqual(history.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            messages.report(self.root, "job-a", 1, "progress", summary="中" * 8001)
        self.assertEqual(len(history.read_text(encoding="utf-8").splitlines()), 2)

    def test_windows_report_transport_reads_large_append_history_and_legacy_latest(self):
        directory = self.root / "jobs" / "job-a"
        for index in range(5):
            messages.report(directory, "job-a", 1, "progress", str(index), "中" * 8000)
        query = dict(operation="report-read", workdir=str(self.root), shared_workdir=str(self.root),
                     job="job-a", round=1, kind="progress")
        with patch.object(native, "request", side_effect=lambda host, op, data: native.handle(
                dict(query, **data), io.BytesIO())):
            report = rd.remote_report({"platform": "windows"}, dict(job="job-a"), 1, "progress")
        self.assertEqual([row["phase"] for row in report["history"]], ["0", "1", "2", "3", "4"])
        self.assertEqual(len(report["history"][-1]["summary"]), 8000)
        (directory / "reports/progress.r1.jsonl").unlink()
        legacy = native.handle(query, io.BytesIO())
        self.assertEqual([row["phase"] for row in legacy["history"]], ["4"])
        self.assertIsNone(native.handle(dict(query, round=2), io.BytesIO()))

    def test_progress_append_refuses_symlinks_and_reader_ignores_incomplete_tail(self):
        messages.report(self.root, "job-a", 1, "progress", "first", "读取中")
        path = self.root / "reports/progress.r1.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"updated_at":')
        self.assertEqual([row["phase"] for row in messages.progress_rows(self.root, "job-a", 1)], ["first"])
        messages.report(self.root, "job-a", 1, "progress", "recovered", "恢复追加")
        self.assertEqual([row["phase"] for row in messages.progress_rows(self.root, "job-a", 1)],
                         ["first", "recovered"])
        path.unlink()
        private = self.root / "private"
        private.write_text("marker")
        path.symlink_to(private)
        with self.assertRaises(ValueError):
            messages.report(self.root, "job-a", 1, "progress", "second", "检查中")
        with self.assertRaises(ValueError):
            messages.progress_rows(self.root, "job-a", 1)
        self.assertEqual(private.read_text(), "marker")

    def test_progress_and_partial_result_preserve_check_evidence_and_do_not_schedule(self):
        messages.report(self.root, "job-a", 1, "progress", "checking", "等待检查")
        messages.report(self.root, "job-a", 1, "result", summary="一个检查失败", outcome="partial", checks="failed", artifacts=["private/check.json"])
        result = messages.finish(self.root, "job-a", 1, 0)
        self.assertEqual((result["executor_exit"], result["outcome"], result["checks"]), (0, "partial", "failed"))
        self.assertEqual(result["artifacts"], ["private/check.json"])
        self.assertEqual(messages.read_json(self.root / "reports/progress.r1.json")["phase"], "checking")
        self.assertEqual((self.root / "reports/result.r1.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "reports/result.r2.json").exists())

    def test_fallback_delivers_last_reply_without_inventing_passed_checks(self):
        (self.root / "last.r2.txt").write_text("实现完成，仍待验收", encoding="utf-8")
        result = messages.finish(self.root, "job-a", 2, 0)
        self.assertEqual(result["summary"], "实现完成，仍待验收")
        self.assertEqual(result["checks"], "not_reported")
        self.assertEqual(result["reply_file"], "last.r2.txt")

    def test_nonzero_execution_cannot_be_hidden_by_worker_completed_claim(self):
        messages.report(self.root, "job-a", 1, "result", outcome="completed", checks="passed")
        self.assertEqual(messages.finish(self.root, "job-a", 1, 124)["outcome"], "failed")

    def test_bad_round_cross_job_oversized_or_symlink_receipts_are_rejected(self):
        report = messages.report(self.root, "job-a", 1, "result")
        with self.assertRaises(ValueError):
            messages.validate_report(report, "job-b", 1, "result")
        with self.assertRaises(ValueError):
            messages.validate_report(report, "job-a", 2, "result")
        with self.assertRaises(ValueError):
            messages.validate_report(dict(report, round=True), "job-a", 1, "result")
        with self.assertRaises(ValueError):
            messages.report(self.root, "../escape", 1, "result")
        with self.assertRaises(ValueError):
            messages.report(self.root, "job-a", 0, "progress")
        with self.assertRaises(ValueError):
            messages.report(self.root, "job-a", 1, "result", summary="x" * 9000)
        link = self.root / "alias.json"
        link.symlink_to(self.root / "reports/result.r1.json")
        with self.assertRaises(ValueError):
            messages.read_json(link)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.records = {"job-a": dict(job="job-a", user_id="employee_a", host="fixture", rounds=1,
                                     initiator_session="sender-a", handoff_version=1, agent=None),
                        "job-b": dict(job="job-b", user_id="employee_a", host="fixture", rounds=1,
                                     initiator_session="sender-b", handoff_version=1, agent=None)}
        self.done = {name: dict(state="done", round="1", exit="0", head="fixture-head", dirty="0") for name in self.records}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(rd, "STATE", str(self.root)))
        self.stack.enter_context(patch.object(rd, "require_user", return_value="employee_a"))
        self.stack.enter_context(patch.object(rd, "job_host", return_value={"platform": "posix"}))
        self.stack.enter_context(patch.object(rd, "load_job", side_effect=self.records.__getitem__))
        self.stack.enter_context(patch.object(rd, "all_jobs", return_value=list(self.records.values())))
        self.stack.enter_context(patch.object(rd, "job_states", return_value=self.done))
        self.stack.enter_context(patch.object(rd, "remote_report", return_value=None))
        self.stack.enter_context(patch.dict(os.environ, {"RD_INITIATOR_SESSION": "sender-a", "CODEX_THREAD_ID": ""}))

    def output(self, fn, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            fn(args, None)
        return json.loads(output.getvalue())

    def test_inbox_only_returns_original_sender_and_keeps_pending_until_exact_ack(self):
        result = self.output(rd.cmd_inbox, argparse.Namespace(initiator_session=None))
        self.assertEqual([r["job"] for r in result["results"]], ["job-a"])
        receipt = result["results"][0]
        self.assertTrue(receipt["needs_initiator_review"])
        self.assertEqual((receipt["executor_exit"], receipt["delivery_state"]), (0, "pending"))
        args = argparse.Namespace(job="job-a", round=1, receipt_id=receipt["receipt_id"], initiator_session=None)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(rd, "launch") as launch:
            rd.cmd_ack(args, None)
            launch.assert_not_called()
        self.assertEqual(self.output(rd.cmd_inbox, argparse.Namespace(initiator_session=None))["results"], [])

    def test_wrong_sender_and_wrong_receipt_cannot_ack_or_resume(self):
        receipt = rd.collect_result(None, self.records["job-a"], self.done["job-a"])
        with patch.dict(os.environ, {"RD_INITIATOR_SESSION": "sender-b"}), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            rd.cmd_resume(argparse.Namespace(job="job-a", initiator_session=None), None)
        self.assertEqual(error.exception.code, 4)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rd.cmd_ack(argparse.Namespace(job="job-a", round=1, receipt_id="wrong", initiator_session="sender-a"), None)
        self.assertEqual(rd.delivery(self.records["job-a"], receipt), "pending")

    def test_cached_receipt_survives_disconnect_and_can_be_read_without_remote(self):
        rd.collect_result(None, self.records["job-a"], self.done["job-a"])
        with patch.object(rd, "job_states", return_value={"job-a": {"state": "unreachable"}}):
            inbox = self.output(rd.cmd_inbox, argparse.Namespace(initiator_session=None))
        self.assertEqual(inbox["unreachable"], ["job-a"])
        self.assertEqual(len(inbox["results"]), 1)
        with patch.object(rd, "job_states", side_effect=AssertionError("cached historical result must not connect")):
            result = self.output(rd.cmd_result, argparse.Namespace(job="job-a", round=None, initiator_session=None))
        self.assertEqual(result["executor_exit"], 0)

    def test_unreceived_result_blocks_cleanup_before_any_remote_deletion(self):
        rec = self.records["job-a"]
        rec.update(repo="fixture-repo", branch="fixture-branch", base="fixture-base")
        with patch.object(rd, "remote_state", return_value=self.done), patch.object(rd, "ssh") as ssh, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            rd.cmd_clean(argparse.Namespace(job="job-a", force=False, delete_branch=False, initiator_session=None), None)
        self.assertEqual(error.exception.code, 4)
        ssh.assert_not_called()

    def test_resume_rejects_another_receiver_thread_before_launch(self):
        rec = self.records["job-a"]
        rec.update(agent="codex", profile={"fingerprint": "fixture"}, receiver_session="original-worker", agent_config={"kind": "codex"})
        with patch.object(rd, "remote_state", return_value=self.done), \
             patch.object(rd, "probe", return_value={}), patch.object(rd, "assess", return_value=[]), \
             patch.object(rd, "fmt_probe", return_value="fixture ready"), patch.object(rd, "launch") as launch, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            rd.cmd_resume(argparse.Namespace(job="job-a", session="other-worker", force_host=False, initiator_session=None), None)
        self.assertEqual(error.exception.code, 4)
        launch.assert_not_called()

    def test_watch_can_recover_from_disconnect_without_resetting_deadline(self):
        args = argparse.Namespace(jobs=["job-a"], timeout=60, keep_on_disconnect=True)
        states = [{"job-a": dict(state="unreachable")}, {"job-a": self.done["job-a"]}]
        with patch.object(rd, "job_states", side_effect=states), patch.object(rd.time, "monotonic", side_effect=[0, 10, 30]), \
             patch.object(rd.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as result:
            rd.cmd_watch(args, None)
        event = json.loads(output.getvalue())
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(event["jobs"][0]["initiator_session"], "sender-a")
        self.assertTrue(event["jobs"][0]["result_ready"])
        sleep.assert_called_once_with(30)

    def test_status_details_delivers_running_progress_without_claiming_result(self):
        with patch.object(rd, "job_states", return_value={"job-a": dict(state="running", round="1")}), \
             patch.object(rd, "remote_report", return_value=dict(phase="building", summary="编译中")):
            status = self.output(rd.cmd_status, argparse.Namespace(job="job-a", details=True, initiator_session=None, json=False))
        self.assertEqual(status[0]["progress"]["phase"], "building")
        self.assertIsNone(status[0]["result"])
        self.assertEqual(status[0]["delivery_state"], "not_available")


class HandoffWorkerTests(WorkerTests):
    def test_real_command_progress_result_delivery_and_no_automatic_next_round(self):
        repo = self.root / "message-repo"
        repo.mkdir()
        rd.git(["init", "-q"], str(repo))
        rd.git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "base"], str(repo))
        command = 'python3 "$RD_REPORTER" progress --phase testing --summary checking; python3 "$RD_REPORTER" result --outcome partial --checks failed --summary "needs fix" --artifact private/test.json'
        args = argparse.Namespace(agent=None, prompt_file=None, cmd=command, test=False, heavy=False,
                                  host="fixture", force_host=True, tag=[], ref="HEAD", wip=False, branch=None,
                                  name="message", project=None, include=[], model=None, effort=None, timeout=15,
                                  initiator_session="sender-fixture")
        with patch.object(rd, "local_repo", return_value=str(repo)), patch.object(rd, "scp_path", side_effect=lambda h, rel: str(Path(h["workdir"]) / rel)), contextlib.redirect_stdout(io.StringIO()):
            rd.cmd_run(args, self.cfg)
            rec = rd.all_jobs()[0]
            self.wait_success(rec["job"])
        with patch.dict(os.environ, {"RD_INITIATOR_SESSION": "sender-fixture"}), contextlib.redirect_stdout(io.StringIO()) as output:
            rd.cmd_result(argparse.Namespace(job=rec["job"], round=None, initiator_session=None), self.cfg)
        receipt = json.loads(output.getvalue())
        self.assertEqual((receipt["executor_exit"], receipt["report"]["outcome"], receipt["report"]["checks"]), (0, "partial", "failed"))
        self.assertEqual(receipt["report"]["summary"], "needs fix")
        self.assertEqual(receipt["delivery_state"], "pending")
        self.assertTrue(receipt["needs_initiator_review"])
        self.assertEqual(rd.load_job(rec["job"])["rounds"], 1)
        wt = Path(rec["worker_root"]) / "wt" / rec["job"]
        (wt / "live-change.txt").write_text("正在修改的文件", encoding="utf-8")
        holder = subprocess.Popen(["sleep", "5"])
        self.addCleanup(lambda: holder.poll() is None and holder.terminate())
        try:
            directory = Path(rec["worker_root"]) / "jobs" / rec["job"]
            (directory / "exit_code").unlink()
            (directory / "pid").write_text(str(holder.pid))
            snapshot = rd.remote_state(rd.job_host(self.cfg, rec), [rec["job"]])[rec["job"]]
            self.assertEqual((snapshot["state"], snapshot["head_source"], snapshot["dirty"]), ("running", "live", "1"))
            self.assertTrue(snapshot["observed_at"])
        finally:
            holder.terminate()
            holder.wait(timeout=3)


def load_tests(loader, standard, pattern):
    suite = unittest.TestSuite()
    for cls in (MessageTests, DeliveryTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    suite.addTest(HandoffWorkerTests("test_real_command_progress_result_delivery_and_no_automatic_next_round"))
    return suite


if __name__ == "__main__":
    unittest.main()
