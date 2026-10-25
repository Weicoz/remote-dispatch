"""签收、代签和停止：CLI 与只读查询，目标机 RPC 使用隔离替身。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd


class AcknowledgementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-ack-")
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
        self.rec = dict(job="fixture-job", user_id="fixture", host="fixture", rounds=1,
                        initiator_session="sender", handoff_version=1, agent=None, base="base")
        self.state = dict(state="done", round="1", exit="0", head="base", dirty="0")
        self.report = dict(schema=1, kind="result", job="fixture-job", round=1,
                           updated_at="2026-10-25T00:00:00+00:00", outcome="completed",
                           checks="passed", summary="已完成本轮实现")
        rd.save_job(self.rec)

    def target(self, host, operation, data=None, **kwargs):
        if operation == "state":
            return {job: dict(self.state) for job in data["jobs"]}
        if operation == "report-read":
            return self.report if data["kind"] == "result" else None
        if operation == "stop":
            if self.state["state"] in ("running", "starting"):
                self.state.update(state="done", exit="143")
            return None
        raise AssertionError(operation)

    def command(self, *args, json_output=True):
        with patch.object(sys, "argv", ["rdispatch", *args]), contextlib.redirect_stdout(io.StringIO()) as output:
            rd.main()
        return json.loads(output.getvalue()) if json_output else output.getvalue()

    def acknowledge(self, *args):
        receipt = self.command("result", "fixture-job")
        self.command("ack", "fixture-job", "--round", "1", "--receipt-id", receipt["receipt_id"],
                     *args, json_output=False)
        return self.command("result", "fixture-job")

    def test_default_signer_is_current_session_and_channel_is_cli(self):
        result = self.acknowledge()
        self.assertEqual(result["acknowledgement"]["acked_by"], "sender")
        self.assertEqual(result["acknowledgement"]["via"], "cli")
        self.assertTrue(result["acknowledgement"]["received_at"])
        self.assertEqual(result["delivery_state"], "received")
        self.assertTrue(result["needs_initiator_review"])

    def test_another_session_or_device_can_acknowledge_with_its_own_identity(self):
        for options, signer, via in ((["--initiator-session", "other-session"], "other-session", "cli"),
                                     (["--acked-by", "App MacBook", "--via", "serve"], "App MacBook", "serve")):
            with self.subTest(signer=signer):
                result = self.acknowledge(*options)
                self.assertEqual(result["acknowledgement"]["acked_by"], signer)
                self.assertEqual(result["acknowledgement"]["via"], via)
                self.assertEqual(result["delivery_state"], "received")

    def test_inbox_lists_delegated_receipts_with_signer_time_and_summary_after_disconnect(self):
        with patch.object(rd.messages, "utc_now", return_value="2026-10-25T01:00:00+00:00"):
            self.acknowledge("--acked-by", "App MacBook", "--via", "serve")
        self.state = dict(state="unreachable")
        inbox = self.command("inbox")
        receipt = inbox["results"][0]
        self.assertEqual(inbox["unreachable"], ["fixture-job"])
        self.assertEqual(receipt["delivery_state"], "received")
        self.assertTrue(receipt["delegated"])
        self.assertEqual(receipt["acknowledgement"]["acked_by"], "App MacBook")
        self.assertEqual(receipt["acknowledgement"]["received_at"], "2026-10-25T01:00:00+00:00")
        self.assertEqual(receipt["summary"], "已完成本轮实现")
        self.assertEqual(self.command("inbox", "--initiator-session", "other")["results"], [])

    def test_stop_creates_failed_received_receipt_owned_by_stopper_pending_review(self):
        self.state.update(state="running", exit="")
        self.command("stop", "fixture-job", "--stopped-by", "App MacBook", "--via", "serve", json_output=False)
        result = self.command("result", "fixture-job")
        self.assertEqual(result["report"]["outcome"], "failed")
        self.assertEqual(result["executor_exit"], 143)
        self.assertEqual(result["delivery_state"], "received")
        self.assertEqual(result["acknowledgement"]["acked_by"], "App MacBook")
        self.assertEqual(result["acknowledgement"]["via"], "serve")
        self.assertEqual(result["stopped_by"], "App MacBook")
        self.assertTrue(result["suppress_push"])
        self.assertTrue(result["needs_initiator_review"])
        self.assertEqual(result["review_state"], "pending")

    def test_details_and_summary_expose_acknowledgement_without_writing_query_state(self):
        self.acknowledge("--acked-by", "device")
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (self.root / "state").rglob("*") if p.is_file()}
        details = self.command("status", "fixture-job", "--details")[0]
        summary = self.command("result", "fixture-job", "--summary")
        self.assertEqual(details["acknowledgement"]["acked_by"], "device")
        self.assertEqual(summary["acknowledgement"]["acked_by"], "device")
        after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (self.root / "state").rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_historical_delegated_receipt_stays_visible_while_new_round_runs(self):
        self.acknowledge("--acked-by", "device")
        self.rec["rounds"] = 2
        rd.save_job(self.rec)
        self.state = dict(state="running", round="2", exit="")
        inbox = self.command("inbox")
        self.assertEqual([(r["round"], r["acknowledgement"]["acked_by"]) for r in inbox["results"]], [(1, "device")])

    def test_original_signer_and_legacy_ack_do_not_appear_as_delegated(self):
        result = self.acknowledge("--via", "serve")
        self.assertEqual(self.command("inbox")["results"], [])
        rd.messages.atomic_json(rd.result_path(self.rec, 1).with_suffix(".ack.json"),
                                dict(receipt_id=result["receipt_id"], received_at="2026-10-25T00:00:00+00:00"))
        self.assertEqual(self.command("result", "fixture-job")["delivery_state"], "received")
        self.assertEqual(self.command("inbox")["results"], [])

    def test_no_session_falls_back_to_cli_signer(self):
        with patch.dict(os.environ, {key: "" for key in ("RD_INITIATOR_SESSION", "CODEX_THREAD_ID",
                             "CLAUDE_CODE_SESSION_ID", "DSH_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID")}):
            receipt = self.command("result", "fixture-job", "--initiator-session", "sender")
            self.command("ack", "fixture-job", "--round", "1", "--receipt-id", receipt["receipt_id"], json_output=False)
        self.assertEqual(self.command("result", "fixture-job")["acknowledgement"]["acked_by"], "cli")

    def test_stop_finished_round_does_not_replace_receipt_or_signer(self):
        before = self.acknowledge()
        self.command("stop", "fixture-job", "--stopped-by", "device", json_output=False)
        self.assertEqual(self.command("result", "fixture-job"), before)

    def test_stop_defaults_to_current_session_and_still_requires_review(self):
        self.state.update(state="running", exit="")
        self.command("stop", "fixture-job", "--initiator-session", "stopper", json_output=False)
        result = self.command("result", "fixture-job", "--summary")
        self.assertEqual(result["acknowledgement"]["acked_by"], "stopper")
        self.assertEqual(result["acknowledgement"]["via"], "cli")
        self.assertEqual((result["outcome"], result["review_state"]), ("failed", "pending"))

    def test_stop_retry_finishes_auto_acknowledgement_after_report_read_failure(self):
        self.state.update(state="running", exit="")
        def failing_target(host, operation, data=None, **kwargs):
            if operation == "report-read":
                raise ValueError("fixture report unavailable")
            return self.target(host, operation, data, **kwargs)
        with patch.object(rd.native, "request", side_effect=failing_target), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.command("stop", "fixture-job", "--stopped-by", "original-stopper", json_output=False)
        self.assertEqual(error.exception.code, 4)
        self.command("stop", "fixture-job", "--stopped-by", "retrying-device", json_output=False)
        result = self.command("result", "fixture-job")
        self.assertEqual(result["delivery_state"], "received")
        self.assertEqual(result["acknowledgement"]["acked_by"], "original-stopper")
        self.assertEqual(result["stopped_by"], "original-stopper")
        self.assertTrue(result["suppress_push"])

    def test_stop_retry_after_ack_write_failure_preserves_receipt_and_stopper(self):
        self.state.update(state="running", exit="")
        replace = os.replace
        def fail_ack_write(source, destination):
            if str(destination).endswith(".ack.json"):
                raise OSError("fixture disk full")
            return replace(source, destination)
        with patch("os.replace", side_effect=fail_ack_write), self.assertRaises(OSError):
            self.command("stop", "fixture-job", "--stopped-by", "original-stopper", json_output=False)
        before = self.command("result", "fixture-job")
        self.assertEqual(before["delivery_state"], "pending")
        self.command("stop", "fixture-job", "--stopped-by", "retrying-device", json_output=False)
        after = self.command("result", "fixture-job")
        self.assertEqual(after["receipt_id"], before["receipt_id"])
        self.assertEqual(after["acknowledgement"]["acked_by"], "original-stopper")
        self.assertEqual(after["delivery_state"], "received")

    def test_repeated_stop_keeps_original_receipt_and_acknowledgement_time(self):
        self.state.update(state="running", exit="")
        self.command("stop", "fixture-job", json_output=False)
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (self.root / "state").rglob("*") if p.is_file()}
        self.command("stop", "fixture-job", "--stopped-by", "other-device", json_output=False)
        after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (self.root / "state").rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_stop_retry_after_confirmation_disconnect_preserves_original_actor(self):
        self.state.update(state="running", exit="")
        def failing_target(host, operation, data=None, **kwargs):
            if operation == "state" and self.state.get("exit") == "143":
                raise ValueError("fixture connection lost")
            return self.target(host, operation, data, **kwargs)
        with patch.object(rd.native, "request", side_effect=failing_target), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.command("stop", "fixture-job", "--stopped-by", "original-stopper", json_output=False)
        self.assertEqual(error.exception.code, 4)
        self.command("stop", "fixture-job", "--stopped-by", "other-device", json_output=False)
        result = self.command("result", "fixture-job")
        self.assertEqual(result["delivery_state"], "received")
        self.assertEqual(result["acknowledgement"]["acked_by"], "original-stopper")

    def test_round_finishing_before_stop_is_not_rewritten_as_failed(self):
        self.state.update(state="running", exit="")
        def finishing_target(host, operation, data=None, **kwargs):
            if operation == "stop":
                self.state.update(state="done", exit="0")
                return None
            return self.target(host, operation, data, **kwargs)
        with patch.object(rd.native, "request", side_effect=finishing_target):
            self.command("stop", "fixture-job", json_output=False)
        result = self.command("result", "fixture-job")
        self.assertEqual(result["report"]["outcome"], "completed")
        self.assertEqual(result["delivery_state"], "pending")
        self.assertNotIn("stopped_by", result)

    def test_invalid_identity_channel_or_other_user_cannot_acknowledge(self):
        receipt = self.command("result", "fixture-job")
        for options, code in ((["--acked-by", "bad\nname"], 4), (["--via", "invalid"], 2)):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.command("ack", "fixture-job", "--round", "1", "--receipt-id", receipt["receipt_id"],
                             *options, json_output=False)
            self.assertEqual(error.exception.code, code)
        self.rec["user_id"] = "another-user"
        rd.save_job(self.rec)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.command("ack", "fixture-job", "--round", "1", "--receipt-id", receipt["receipt_id"], json_output=False)
        self.assertEqual(error.exception.code, 4)
