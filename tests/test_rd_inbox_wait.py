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
        stack.enter_context(patch.object(rd, "serve_online", side_effect=lambda: self.online))
        self.state = dict(state="running", round="1", exit="", head="base", dirty="0")
        rd.save_job(dict(job="fixture-job", user_id="fixture", host="fixture", rounds=1,
                         initiator_session="sender", handoff_version=1, agent=None, base="base"))
        self.rows = []

    def target(self, host, operation, data=None, **kwargs):
        if operation == "state":
            return {job: dict(self.state) for job in data["jobs"]}
        raise AssertionError(operation)

    def add_report(self, job="fixture-job", number=1, state="completed", owner="sender"):
        text = {"completed": "已完成", "failed": "失败"}[state]
        self.rows.append(dict(id="r%d" % len(self.rows), initiator_session=owner,
                              created_at="2026-10-30T00:00:00+00:00",
                              entries=[dict(job=job, title="标题", host="fixture", round=number, state=state,
                                            state_text=text, summary=SECRET)],
                              delivery=dict(status="inbox_only", reason=None, pane_id=None, delivered_at=None)))
        path = reports.store_path(rd.STATE, "fixture")
        messages.atomic_json(path, dict(cursor=0, reports=self.rows), limit=None)

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
        code, text = self.wait("--timeout", "5", during=lambda n: self.add_report(number=2) if n == 1 else None)
        self.assertEqual(code, 0)
        self.assertIn("fixture-job 第2轮 已完成", text)
        self.assertIn("rdispatch result fixture-job", text)
        self.assertNotIn("第1轮", text)

    def test_baseline_report_does_not_trigger_and_timeout_exits_two(self):
        self.add_report(number=1)
        code, text = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)
        self.assertNotIn("第1轮", text)

    def test_multiple_new_reports_all_printed_once_and_other_session_ignored(self):
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

    def test_offline_job_already_finished_at_start_is_baseline(self):
        self.online = False
        self.state.update(state="done", exit="0")
        code, _ = self.wait("--timeout", "0.05")
        self.assertEqual(code, 2)

    def test_offline_failed_exit_reports_failed(self):
        self.online = False
        code, text = self.wait("--timeout", "5", during=lambda n: self.state.update(state="done", exit="1"))
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
