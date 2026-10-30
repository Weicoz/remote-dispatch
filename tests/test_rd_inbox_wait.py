"""inbox --wait：CLI 接缝；目标机 RPC 使用隔离替身，汇报存储直接写入状态目录。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd
from remote_dispatch import rd_events as events
from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_reports as reports

SECRET = "执行方撰写的机密摘要"


class InboxWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-inbox-wait-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        config = self.root / "config.ini"
        config.write_text("[host:fixture]\nssh=fixture.invalid\nplatform=windows\nworkdir=C:/fixture\n")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(rd, "STATE", str(self.root / "state")))
        stack.enter_context(patch.object(rd, "CONFIG", str(config)))
        stack.enter_context(patch.dict(os.environ, {"RD_USER_ID": "fixture", "RD_INITIATOR_SESSION": "sender"}))
        stack.enter_context(patch.object(rd.native, "request", side_effect=self.target))
        self.online = True
        self.probes = {}  # (派单, 轮次) -> 只读探测返回的回执；缺省探测不到
        self.probe_calls = []

        def probe(query_self, rec, number=None, snapshot=None):
            self.probe_calls.append((rec["job"], number))
            if (rec["job"], number) not in self.probes:
                raise ValueError("探测不到回执")
            return dict(self.probes[(rec["job"], number)])
        stack.enter_context(patch.object(rd.query.Query, "result", probe))
        stack.enter_context(patch.object(rd, "serve_online", side_effect=lambda: self.online))
        self.state = dict(state="running", round="1", exit="", head="base", dirty="0")
        rd.save_job(dict(job="fixture-job", user_id="fixture", host="fixture", rounds=1,
                         initiator_session="sender", handoff_version=1, agent=None, base="base"))
        self.rows = []

    def target(self, host, operation, data=None, **kwargs):
        if operation == "state":
            return {job: dict(self.state) for job in data["jobs"]}
        raise AssertionError(operation)

    def add_report(self, job="fixture-job", number=1, state="completed", owner="sender", kind=None):
        text = {"completed": "已完成", "failed": "失败", "running": "运行中"}[state]
        self.rows.append(dict(id="r%d" % len(self.rows), initiator_session=owner,
                              **(dict(kind=kind) if kind else {}),
                              created_at="2026-10-30T00:00:00+00:00",
                              entries=[dict(job=job, title="标题", host="fixture", round=number, state=state,
                                            state_text=text, summary=SECRET)],
                              delivery=dict(status="inbox_only", reason=None, pane_id=None, delivered_at=None)))
        path = reports.store_path(rd.STATE, "fixture")
        messages.atomic_json(path, dict(cursor=0, reports=self.rows), limit=None)

    def add_job(self, job, rounds=1):
        rd.save_job(dict(job=job, user_id="fixture", host="fixture", rounds=rounds,
                         initiator_session="sender", handoff_version=1, agent=None, base="base"))
        return rd.load_job(job)

    def receipt(self, job="fixture-job", number=1):
        """本地已收取的回执（未签收）。"""
        rec = rd.load_job(job) if os.path.exists(rd.job_path(job)) else self.add_job(job, number)
        path = rd.result_path(rec, number)
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt_id = "rc-%s-%d" % (job, number)
        messages.atomic_json(path, dict(round=number, receipt_id=receipt_id))
        return rec, path, receipt_id

    def probeable(self, job="fixture-job", number=1, **extra):
        """让只读探测返回回执（本地不落盘）。"""
        self.probes[(job, number)] = dict(round=number, receipt_id="rc-%s-%d" % (job, number), **extra)

    def ack(self, job="fixture-job", number=1):
        """模拟 rdispatch ack：本地回执与签收文件，签收后同轮汇报不再算新。"""
        rec, path, receipt_id = self.receipt(job, number)
        messages.atomic_json(path.with_suffix(".ack.json"), dict(receipt_id=receipt_id, acked_by="sender", via="cli"))

    def wait(self, *args, during=None):
        calls = []
        real_sleep = time.sleep

        def sleep(seconds):
            calls.append(seconds)
            real_sleep(0.01)
            if during:
                during(len(calls))
            if len(calls) > 200:
                raise AssertionError("等待没有结束")
        argv = ["rdispatch", "inbox", "--wait", "--interval", "0.01", *args]
        out = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(rd.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            rd.main()
        return raised.exception.code, out.getvalue()

    def snapshot(self):
        base = Path(rd.STATE)
        return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()
                and "reports" not in p.parts}

    def test_new_report_exits_zero_and_prints_line_with_view_command(self):
        self.add_report(number=1)
        self.ack(number=1)

        def second(n):
            if n == 1:
                self.add_job("fixture-job", 2)
                self.add_report(number=2)
        code, text = self.wait("--timeout", "5", during=second)
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第2轮 已完成", text)
        self.assertIn("rdispatch result fixture-job", text)
        self.assertNotIn("第1轮", text)

    def test_acked_report_does_not_trigger_and_timeout_exits_two(self):
        self.add_report(number=1)
        self.ack(number=1)
        code, text = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)
        self.assertNotIn("第1轮", text)

    def test_existing_unacked_report_wakes_immediately(self):
        self.add_report(number=1)
        self.receipt(number=1)  # 本地已有回执、未签收
        code, text = self.wait("--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第1轮 已完成", text)

    def test_report_after_ack_before_rehang_without_local_receipt_wakes_via_probe(self):
        self.add_report(number=1)
        self.ack(number=1)
        self.add_job("fixture-job", 2)
        self.add_report(number=2)
        self.probeable(number=2)  # 没有本地回执，只读探测能取到
        code, text = self.wait("--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn("第2轮", text)
        self.assertNotIn("第1轮", text)
        self.assertFalse(rd.result_path(rd.load_job("fixture-job"), 2).exists())

    def test_probe_result_is_cached_per_job_round(self):
        self.add_report(number=1)
        self.probeable(number=1)
        self.wait("--timeout", "5")
        self.assertEqual(self.probe_calls.count(("fixture-job", 1)), 1)

    def test_acked_report_is_not_probed_or_woken(self):
        self.add_report(number=1)
        self.ack(number=1)
        self.probeable(number=1)
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)
        self.assertEqual(self.probe_calls, [])

    def test_older_round_unacked_with_receipt_wakes(self):
        self.add_job("fixture-job", 2)
        self.receipt(number=1)
        self.add_report(number=1)
        code, text = self.wait("--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn("第1轮", text)

    def test_older_round_unacked_with_probe_wakes_and_without_probe_does_not(self):
        self.add_job("fixture-job", 2)
        self.add_report(number=1)
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)
        self.probeable(number=1)
        code, text = self.wait("--timeout", "5")
        self.assertEqual(code, 0)
        self.assertIn("第1轮", text)

    def test_receipt_without_receipt_id_is_treated_as_missing_without_crash(self):
        rec, path, _ = self.receipt(number=1)
        messages.atomic_json(path, dict(round=1))
        self.add_report(number=1)
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)
        self.probeable(number=1)  # 探测得到有效回执时仍按未签收唤醒
        code, _ = self.wait("--timeout", "5")
        self.assertEqual(code, 0)

    def test_multiple_new_reports_all_printed_once_and_other_session_ignored(self):
        self.add_job("a", 1)
        self.add_job("b", 3)

        def add(n):
            if n == 1:
                self.add_report(job="a", number=1)
                self.add_report(job="b", number=3, state="failed")
                self.add_report(job="c", number=1, owner="other")
        code, text = self.wait("--timeout", "5", during=add)
        self.assertEqual(code, 0)
        self.assertIn("a 第1轮 已完成", text)
        self.assertIn("b 第3轮 失败", text)
        self.assertNotIn("c 第", text)

    def test_text_and_json_output_never_contain_executor_text(self):
        for extra in ([], ["--json"]):
            self.rows = []
            self.add_report(number=1, job="old")
            self.ack("old", 1)
            code, text = self.wait("--timeout", "5", *extra,
                                   during=lambda n: self.add_report(number=2) if n == 1 else None)
            self.assertEqual(code, 0)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("标题", text)
        data = json.loads(text)
        self.assertEqual([(r["job"], r["round"], r["state"]) for r in data["reports"]],
                         [("fixture-job", 2, "completed")])

    def test_offline_wakes_on_terminal_state_with_same_format(self):
        self.online = False

        def finish(n):
            self.state.update(state="done", exit="0")
        code, text = self.wait("--timeout", "5", during=finish)
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第1轮 已完成", text)
        self.assertIn("rdispatch result fixture-job", text)

    def test_offline_job_finished_and_unacked_wakes_immediately(self):
        self.online = False
        self.state.update(state="done", exit="0")
        self.receipt(number=1)
        code, text = self.wait("--timeout", "0.05")
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第1轮 已完成", text)

    def test_offline_job_finished_and_acked_does_not_wake(self):
        self.online = False
        self.state.update(state="done", exit="0")
        self.ack(number=1)
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)

    def test_offline_failed_exit_reports_failed(self):
        self.online = False
        code, text = self.wait("--timeout", "5", during=lambda n: self.state.update(state="done", exit="1"))
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第1轮 失败", text)

    def test_digest_does_not_wake_and_later_terminal_report_of_same_round_does(self):
        def add(n):
            if n == 1:
                self.add_report(number=1, state="running", kind="digest")
        code, _ = self.wait("--timeout", "0.1", during=add)
        self.assertEqual(code, 2)
        # 重挂：摘要进了基线，同一轮之后的完成汇报仍必须唤醒。
        code, text = self.wait("--timeout", "5", during=lambda n: self.add_report(number=1) if n == 1 else None)
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第1轮 已完成", text)

    def test_job_finished_while_serve_online_wakes_after_serve_goes_offline(self):
        def finish_then_offline(n):
            if n == 1:
                self.state.update(state="done", exit="0")
            elif n == 3:
                self.online = False
        code, text = self.wait("--timeout", "5", during=finish_then_offline)
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第1轮 已完成", text)

    def test_report_woken_online_does_not_wake_again_after_serve_goes_offline(self):
        def step(n):
            if n == 1:
                self.state.update(state="done", exit="0")
                self.add_report()
        code, text = self.wait("--timeout", "5", during=step)
        self.assertEqual(code, 0)
        self.assertEqual(text.count("第1轮"), 1)
        # 在线唤醒后签收再重挂：同轮已签收；换到离线也不得再唤醒。
        self.ack(number=1)
        self.online = False
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)

    def test_report_of_deleted_job_does_not_wake(self):
        self.add_report(job="gone", number=1)
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)

    def test_existing_report_without_local_receipt_does_not_spin_across_waits(self):
        self.add_report(number=1)
        for _ in range(2):
            code, _ = self.wait("--timeout", "0.05")
            self.assertEqual(code, 2)

    def test_legacy_ack_record_with_old_style_receipt_id_counts_as_acked(self):
        rec, path, _ = self.receipt(number=1)
        messages.atomic_json(path, dict(round=1, receipt_id="legacy-id"))
        messages.atomic_json(path.with_suffix(".ack.json"), dict(receipt_id="legacy-id", acked_by="sender"))
        self.add_report(number=1)
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)

    def test_offline_dead_job_without_receipt_is_baseline_then_new_death_wakes(self):
        self.online = False
        self.state.update(state="dead", exit="")
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)
        self.state.update(state="running")
        code, text = self.wait("--timeout", "5", during=lambda n: self.state.update(state="dead"))
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第1轮 失败", text)

    def test_missing_initiator_session_exits_four(self):
        with patch.dict(os.environ, {"RD_INITIATOR_SESSION": ""}), \
                patch.object(rd.initiators, "session", return_value=None):
            code, _ = self.wait("--timeout", "1")
        self.assertEqual(code, 4)

    def test_wait_does_not_change_acknowledgements_or_receipts(self):
        self.online = False
        self.add_report(number=1)
        before = self.snapshot()
        count = len(events.read_after(rd.STATE, 0))
        self.wait("--timeout", "5", during=lambda n: self.state.update(state="done", exit="0"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(events.read_after(rd.STATE, 0)), count)


if __name__ == "__main__":
    unittest.main()
