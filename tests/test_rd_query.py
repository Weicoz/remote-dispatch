"""Read commands leave the controller state untouched."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd


class PureQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(rd, "STATE", str(self.root)))
        stack.enter_context(patch.dict(os.environ, {"RD_USER_ID": "fixture"}))
        self.rec = dict(job="fixture-job", user_id="fixture", host="fixture", rounds=1,
                        agent="codex", agent_config=rd.AGENTS["codex"],
                        initiator_session="sender", handoff_version=1, sync="direct", base="base")
        rd.save_job(self.rec)
        stack.enter_context(patch.object(rd, "job_host", return_value={"platform": "posix"}))
        self.states = {"fixture-job": dict(state="done", round="1", exit="0", head="base", dirty="0")}
        stack.enter_context(patch.object(rd, "job_states", side_effect=lambda *args: self.states))
        stack.enter_context(patch.object(rd, "remote_report", return_value=None))
        stack.enter_context(patch.object(rd, "find_session", return_value="receiver"))

    def output(self, command, **kwargs):
        args = argparse.Namespace(job="fixture-job", initiator_session="sender", round=None,
                                  details=False, json=False, summary=False)
        vars(args).update(kwargs)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            command(args, None)
        return json.loads(output.getvalue())

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                               p.read_bytes() if p.is_file() else None)
                for p in self.root.rglob("*")}

    def test_read_commands_return_finished_result_without_changing_state(self):
        before = self.snapshot()
        status = self.output(rd.cmd_status, json=True)
        details = self.output(rd.cmd_status, details=True)
        result = self.output(rd.cmd_result)
        summary = self.output(rd.cmd_result, summary=True)
        self.assertEqual(status[0]["state"], "done")
        self.assertEqual(details[0]["result"]["receiver_session"], "receiver")
        self.assertEqual((result["executor_exit"], result["delivery_state"]), (0, "pending"))
        self.assertEqual(summary["receipt_id"], result["receipt_id"])
        self.assertEqual(self.snapshot(), before)

    def test_running_dead_missing_and_unreachable_queries_do_not_write(self):
        for state in ("running", "starting", "dead", "missing", "unreachable"):
            with self.subTest(state=state):
                self.states["fixture-job"] = dict(state=state, round="1")
                before = self.snapshot()
                status = self.output(rd.cmd_status, json=True)
                details = self.output(rd.cmd_status, details=True)
                self.assertEqual(status[0]["state"], state)
                if state in ("dead", "missing"):
                    self.assertEqual(self.output(rd.cmd_result)["execution_state"], state)
                else:
                    self.assertIsNone(details[0]["result"])
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        self.output(rd.cmd_result)
                    self.assertEqual(error.exception.code, 2)
                self.assertEqual(self.snapshot(), before)

    def test_existing_historical_receipt_keeps_id_without_connecting(self):
        receipt = dict(job="fixture-job", round=1, receipt_id="legacy-id", executor_exit=0)
        rd.messages.atomic_json(rd.result_path(self.rec, 1), receipt)
        self.rec["rounds"] = 2
        rd.save_job(self.rec)
        before = self.snapshot()
        with patch.object(rd, "job_states", side_effect=AssertionError("historical read must stay offline")):
            result = self.output(rd.cmd_result, round=1)
        self.assertEqual(result["receipt_id"], "legacy-id")
        self.assertEqual(self.snapshot(), before)

    def test_wrong_receipt_id_does_not_materialize_an_uncached_receipt(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.output(rd.cmd_ack, round=1, receipt_id="wrong")
        self.assertEqual(error.exception.code, 4)
        # A write command may create its persistent lock file, but no receipt.
        self.assertFalse(rd.result_path(self.rec, 1).exists())
        self.assertEqual(rd.load_job("fixture-job"), self.rec)

    def test_result_can_be_acknowledged_later_and_read_after_disconnect(self):
        with patch.object(rd.messages, "utc_now", return_value="2026-10-25T00:00:00+00:00"):
            receipt = self.output(rd.cmd_result)
        args = argparse.Namespace(job="fixture-job", initiator_session="sender", round=1,
                                  receipt_id=receipt["receipt_id"])
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(rd.messages, "utc_now", return_value="2026-10-25T01:00:00+00:00"):
            rd.cmd_ack(args, None)
        self.states["fixture-job"] = dict(state="unreachable")
        before = self.snapshot()
        result = self.output(rd.cmd_result)
        self.assertEqual(result["receipt_id"], receipt["receipt_id"])
        self.assertEqual(result["delivery_state"], "received")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
