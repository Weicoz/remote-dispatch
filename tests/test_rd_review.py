"""验收的 CLI 输出、退出码及持久化查询；仅替换目标机 RPC。"""
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


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-review-")
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
                        initiator_session="sender", handoff_version=1, agent="codex", isolated=True,
                        receiver_session="receiver", base="base", sync="direct")
        self.state = dict(state="done", round="1", exit="0", head="base", dirty="0")
        self.report = dict(schema=1, kind="result", job="fixture-job", round=1,
                           updated_at="2026-10-25T00:00:00+00:00", outcome="completed",
                           checks="passed", summary="本轮完成")
        self.probe = dict(memfree="90", disk="100")
        self.fail_operation = None
        self.launched = []
        rd.save_job(self.rec)

    def target(self, host, operation, data=None, **kwargs):
        if operation == self.fail_operation:
            raise ValueError("fixture target failed")
        if operation == "state":
            return {job: dict(self.state) for job in data["jobs"]}
        if operation == "report-read":
            return self.report if data["kind"] == "result" else None
        if operation == "sessions":
            return '{"session_id":"receiver"}'
        if operation == "probe":
            return self.probe
        if operation == "resume-checkpoint":
            return {}
        if operation in ("upload", "controller-register", "resume-rollback"):
            return None
        if operation == "launch":
            self.launched.append(data)
            self.state.update(state="running", round=str(data["spec"]["round"]))
            return None
        raise AssertionError(operation)

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["rdispatch", *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rd.main()
                code = 0
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def result(self, number=1):
        code, out, err = self.command("result", "fixture-job", "--round", str(number))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def review(self, *options):
        return self.command("review", "fixture-job", "--round", "1", *options)

    def test_accept_is_queryable_and_auto_acknowledges_as_reviewer(self):
        code, out, err = self.review("--accept", "--reviewed-by", "App MacBook", "--via", "serve")
        self.assertEqual(code, 0, err)
        result = self.result()
        self.assertEqual(result["review_state"], "accepted")
        self.assertEqual(result["reviewed_by"], "App MacBook")
        self.assertTrue(result["reviewed_at"])
        self.assertEqual(result["review_note"], "")
        self.assertEqual(result["delivery_state"], "received")
        self.assertEqual(result["acknowledgement"]["acked_by"], "App MacBook")
        self.assertEqual(result["acknowledgement"]["via"], "serve")
        code, out, err = self.command("status", "fixture-job", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["review_state"], "accepted")

    def test_state_root_alias_uses_the_same_review_transaction(self):
        alias = self.root / "state-alias"
        alias.symlink_to(self.root / "state", target_is_directory=True)
        with patch.object(rd, "STATE", str(alias)):
            code, out, err = self.review("--accept")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.result()["review_state"], "accepted")

    def test_outcome_operation_table(self):
        for outcome in ("completed", "partial", "blocked", "failed"):
            for option, expected in (("--accept", "accepted"), ("--reject", "rejected"), ("--void", "voided")):
                with self.subTest(outcome=outcome, option=option):
                    self.report["outcome"] = outcome
                    self.state.update(state="done", round="1")
                    rd.save_job(self.rec)
                    directory = self.root / "state/results/fixture-job"
                    if directory.exists():
                        for path in directory.iterdir():
                            path.unlink()
                    options = [option, "请完成剩余项"] if option == "--reject" else [option]
                    code, out, err = self.review(*options)
                    if outcome in ("blocked", "failed") and option == "--accept":
                        self.assertNotEqual(code, 0)
                        self.assertIn("review_not_allowed", err)
                        self.assertEqual(self.result()["review_state"], "pending")
                    else:
                        self.assertEqual(code, 0, err)
                        result = self.result()
                        self.assertEqual(result["review_state"], expected)
                        if option == "--reject":
                            self.assertEqual(result["review_note"], "请完成剩余项")
                            code, out, err = self.command("status", "fixture-job", "--json")
                            self.assertEqual(json.loads(out)[0]["review_state"], "pending")

    def test_failed_rejection_leaves_receipt_unacknowledged_pending_and_no_new_round(self):
        for operation, probe, reason in ((None, dict(memfree="90", disk="100", load1="1000", ncpu="1"), "resume_resource_check_failed"),
                                         (None, dict(memfree="1", disk="100"), "resume_resource_check_failed"),
                                         ("probe", self.probe, "resume_host_unavailable"),
                                         ("launch", self.probe, "resume_launch_failed")):
            with self.subTest(operation=operation, probe=probe):
                self.fail_operation, self.probe = operation, probe
                code, out, err = self.review("--reject", "继续")
                self.assertNotEqual(code, 0)
                self.assertIn(reason, err)
                result = self.result()
                self.assertEqual(result["review_state"], "pending")
                self.assertEqual(result["delivery_state"], "pending")
                code, out, err = self.command("status", "fixture-job", "--json")
                self.assertEqual(json.loads(out)[0]["rounds"], 1)
                self.assertFalse(list((self.root / "state/results").glob("**/*.review.json")))

    def test_duplicate_review_reports_original_actor_time_and_state_without_changes(self):
        self.assertEqual(self.review("--accept", "--reviewed-by", "device-a")[0], 0)
        before = self.result()
        code, out, err = self.review("--void", "--reviewed-by", "device-b")
        self.assertNotEqual(code, 0)
        for value in ("already_reviewed", "device-a", before["reviewed_at"], "accepted"):
            self.assertIn(value, err)
        self.assertEqual(self.result(), before)

    def test_review_write_failure_rolls_back_auto_acknowledgement(self):
        replace = os.replace
        def fail_review(source, destination):
            if str(destination).endswith(".review.json"):
                raise OSError("fixture disk full")
            return replace(source, destination)
        with patch.object(os, "replace", side_effect=fail_review):
            code, out, err = self.review("--accept")
        self.assertNotEqual(code, 0)
        self.assertIn("review_write_failed", err)
        result = self.result()
        self.assertEqual((result["review_state"], result["delivery_state"]), ("pending", "pending"))

    def test_failed_reject_preserves_existing_acknowledgement(self):
        receipt = self.result()
        code, out, err = self.command("ack", "fixture-job", "--round", "1", "--receipt-id",
                                      receipt["receipt_id"], "--acked-by", "original-device")
        self.assertEqual(code, 0, err)
        before = self.result()
        self.fail_operation = "launch"
        self.assertNotEqual(self.review("--reject", "继续")[0], 0)
        self.assertEqual(self.result(), before)

    def test_nonzero_exit_cannot_pass_even_when_report_claims_completed(self):
        self.state["exit"] = "1"
        code, out, err = self.review("--accept")
        self.assertNotEqual(code, 0)
        self.assertIn("review_not_allowed", err)

    def test_blank_rejection_is_refused_without_signing_or_launching(self):
        code, out, err = self.review("--reject", "  \n")
        self.assertNotEqual(code, 0)
        self.assertIn("review_not_allowed", err)
        self.assertEqual(self.result()["delivery_state"], "pending")

    def test_dead_and_missing_cannot_be_accepted_but_can_be_rejected_or_voided(self):
        for state in ("dead", "missing"):
            for option in ("--accept", "--reject", "--void"):
                with self.subTest(state=state, option=option):
                    self.state.update(state=state, round="1")
                    rd.save_job(self.rec)
                    for path in (self.root / "state/results").glob("**/*.json"):
                        path.unlink()
                    args = (option, "修复") if option == "--reject" else (option,)
                    code, out, err = self.review(*args)
                    self.assertEqual(code == 0, option != "--accept", err)
                    if option == "--accept":
                        self.assertIn("review_not_allowed", err)

    def test_missing_target_with_zero_discovered_rounds_can_be_voided(self):
        self.state.update(state="missing", round="0")
        code, out, err = self.review("--void")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.result()["review_state"], "voided")

    def test_review_of_running_round_has_stable_error(self):
        self.state.update(state="running")
        code, out, err = self.review("--accept")
        self.assertNotEqual(code, 0)
        self.assertIn("round_in_progress", err)

    def test_resume_cannot_change_harness(self):
        prompt = self.root / "prompt.md"
        prompt.write_text("继续")
        for command in (("resume", "fixture-job", "--prompt-file", str(prompt)),
                        ("review", "fixture-job", "--round", "1", "--reject", "继续")):
            with self.subTest(command=command):
                code, out, err = self.command(*command, "--agent", "dsh")
                self.assertNotEqual(code, 0)
                self.assertIn("harness_change_not_allowed", err)

    def test_rejection_to_full_or_offline_alternate_host_does_not_create_a_round(self):
        config = Path(rd.CONFIG)
        config.write_text(config.read_text() + "\n[host:other]\nssh=other.invalid\nplatform=windows\nworkdir=C:/other\n")
        self.probe = dict(memfree="90", disk="100", load1="1000", ncpu="1")
        for operation, reason in ((None, "resume_resource_check_failed"), ("probe", "resume_host_unavailable")):
            with self.subTest(operation=operation):
                self.fail_operation = operation
                code, out, err = self.review("--reject", "继续", "--host", "other")
                self.assertNotEqual(code, 0)
                self.assertIn(reason, err)
                self.assertEqual(self.result()["review_state"], "pending")
                code, out, err = self.command("status", "fixture-job", "--json")
                row = json.loads(out)[0]
                self.assertEqual((row["rounds"], row["host"]), (1, "fixture"))

    def test_accepted_and_voided_rounds_can_resume_with_a_new_model(self):
        prompt = self.root / "prompt.md"
        prompt.write_text("继续")
        for option in ("--accept", "--void"):
            with self.subTest(option=option):
                self.state.update(state="done", round="1")
                rd.save_job(self.rec)
                for path in (self.root / "state/results").glob("**/*.json"):
                    path.unlink()
                self.assertEqual(self.review(option)[0], 0)
                code, out, err = self.command("resume", "fixture-job", "--prompt-file", str(prompt), "--model", "new-model")
                self.assertEqual(code, 0, err)
                code, out, err = self.command("status", "fixture-job", "--json")
                row = json.loads(out)[0]
                self.assertEqual((row["rounds"], row["review_state"], row["model"]), (2, "pending", "new-model"))

    def test_result_summary_and_inbox_preserve_review_and_do_not_treat_review_file_as_receipt(self):
        self.assertEqual(self.review("--accept", "--reviewed-by", "device")[0], 0)
        code, out, err = self.command("result", "fixture-job", "--summary")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["reviewed_by"], "device")
        code, out, err = self.command("inbox")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["results"]), 1)
