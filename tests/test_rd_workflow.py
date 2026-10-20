"""Failure recovery and evidence regressions; no remote host or model API."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remote_dispatch import rd_messages as messages
from test_rd import rd


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def failure(self, text, work=False):
        (self.root / "stderr.r1.log").write_text("")
        rows = [{"type": "turn.started"}]
        if work:
            rows.append({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}})
        rows.append({"type": "turn.failed", "error": {"message": text}})
        (self.root / "stdout.r1.log").write_text("\n".join(map(json.dumps, rows)))
        with patch.dict(os.environ, {"RD_AGENT": "codex"}):
            return messages.finish(self.root, "job-a", 1, 1)

    def test_route_failure_is_distinct_from_a_compaction_failure(self):
        row = self.failure("Error running remote compact task: workspace routing discovery failed")
        self.assertEqual(row["runtime_failure"]["kind"], "model_route")
        self.assertIs(row["runtime_failure"]["work_observed"], False)
        self.assertEqual(row["checks"], "not_reported")

    def test_compaction_wrapper_does_not_hide_network_rate_or_provider_causes(self):
        for message, expected in [
            ("Error running remote compact task: error sending request: TLS failure", "transport"),
            ("Error running remote compact task: 429 rate limit", "rate_limit"),
            ("Error running remote compact task: 503 service unavailable", "provider"),
            ("Error running remote compact task", "compaction"),
        ]:
            with self.subTest(message=message):
                row = self.failure(message)
                self.assertEqual(row["runtime_failure"]["kind"], expected)

    def test_work_before_model_failure_is_not_reported_as_zero_work(self):
        row = self.failure("context window exceeded", work=True)
        self.assertIs(row["runtime_failure"]["work_observed"], True)

    def test_missing_stdout_does_not_prove_zero_work(self):
        (self.root / "stderr.r1.log").write_text("workspace routing discovery failed\n")
        with patch.dict(os.environ, {"RD_AGENT": "codex"}):
            result = messages.finish(self.root, "job-a", 1, 1)
        self.assertIs(result["runtime_failure"]["scan_complete"], False)
        self.assertIsNone(result["runtime_failure"]["work_observed"])

    def test_large_log_does_not_invent_absence_of_work_outside_scan(self):
        log = self.root / "stdout.r1.log"
        log.write_text('x' * 100000 + '\n' + json.dumps({"type": "turn.failed", "error": {"message": "context window exceeded"}}))
        with patch.dict(os.environ, {"RD_AGENT": "codex"}):
            row = messages.finish(self.root, "job-a", 1, 1)
        self.assertIsNone(row["runtime_failure"]["work_observed"])
        self.assertIs(row["runtime_failure"]["scan_complete"], False)

    def test_artifact_index_records_real_digest_and_missing_files(self):
        (self.root / "proof.json").write_text('{"passed":true}\n')
        index = messages.artifact_index(self.root, ["proof.json", "missing.json"])
        self.assertEqual(index[0]["state"], "present")
        self.assertEqual(len(index[0]["sha256"]), 64)
        self.assertEqual(index[1]["state"], "missing")
        (self.root / "link").symlink_to(self.root / "proof.json")
        self.assertEqual(messages.artifact_index(self.root, ["link"])[0]["state"], "unverified")

    def test_summary_is_bounded_without_claiming_evidence_was_accepted(self):
        report = {"summary": "z" * 8000, "outcome": "partial", "checks": "failed",
                  "evidence": {"source_fingerprint": "a" * 64,
                               "artifacts": [{"path": "proof", "state": "missing"}]}}
        row = messages.result_summary({"job": "job-a", "round": 1, "receipt_id": "fixture",
                                       "executor_exit": 0, "needs_initiator_review": True, "report": report})
        self.assertLess(len(json.dumps(row)), 3000)
        self.assertEqual(row["checks"], "failed")
        self.assertIs(row["needs_initiator_review"], True)
        self.assertEqual(row["evidence"]["missing_artifacts"], 1)

    def test_malformed_evidence_is_rejected_and_unknown_activity_is_not_zero_work(self):
        for evidence in [{"checks": None}, {"artifacts": "not-a-list"}, {"source_fingerprint": "not-a-digest"}]:
            with self.assertRaises(ValueError):
                messages.validate_evidence(evidence)
        (self.root / "stdout.r1.log").write_text("\n".join(map(json.dumps, [
            {"type": "item.completed", "item": {"type": "future_tool"}},
            {"type": "turn.failed", "error": {"message": "workspace routing discovery failed"}}])))
        with patch.dict(os.environ, {"RD_AGENT": "codex"}):
            result = messages.finish(self.root, "job-a", 1, 1)
        self.assertIsNone(result["runtime_failure"]["work_observed"])

    def resume(self, kind, number=2, new=False):
        record = dict(job="job-a", rounds=number, user_id="fixture", profile={"fingerprint": "fixture"},
                      handoff_version=1,
                      agent="codex", receiver_session="old-receiver", initiator_session="sender",
                      agent_config=dict(rd.AGENTS["codex"], kind="codex"), model=None, effort=None)
        failure = {"kind": kind, "work_observed": False, "scan_complete": True}
        receipt = {"job": "job-a", "round": number, "receipt_id": "fixture-receipt",
                   "report": {"runtime_failure": failure}}
        prompt = self.root / "prompt.md"
        prompt.write_text("Continue only the remaining documented work.")
        args = argparse.Namespace(job="job-a", initiator_session="sender", session=None,
                                  prompt_file=str(prompt), model=None, effort=None, timeout=30,
                                  force_host=False, new_context=new, retry_after_repair=None)
        with patch.object(rd, "STATE", str(self.root)), \
             patch.object(rd, "require_user", return_value="fixture"), \
             patch.object(rd, "load_job", return_value=record), \
             patch.object(rd, "job_host", return_value={"name": "fixture", "platform": "posix", "timeout": 30}), \
             patch.object(rd, "remote_state", return_value={"job-a": {"state": "done", "exit": "1", "round": str(number)}}), \
             patch.object(rd, "collect_result", return_value=receipt), \
             patch.object(rd, "find_session", return_value=None), \
             patch.object(rd, "probe", return_value={}), patch.object(rd, "assess", return_value=[]), \
             patch.object(rd, "fmt_probe", return_value="fixture"), \
             patch.object(rd, "save_job"), patch.object(rd, "launch") as launch, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if number > 1:
                messages.atomic_json(rd.result_path(record, number - 1), dict(receipt, round=number - 1))
            rd.cmd_resume(args, None)
        return record, launch

    def test_repeated_route_failure_stops_before_another_launch(self):
        with self.assertRaises(SystemExit) as stopped:
            self.resume("model_route")
        self.assertEqual(stopped.exception.code, 4)

    def test_first_failure_can_resume_once_in_original_context(self):
        record, launch = self.resume("model_route", number=1)
        self.assertIn("exec resume", launch.call_args.args[3])
        self.assertEqual(record["receiver_session"], "old-receiver")

    def test_context_replacement_is_explicit_and_records_predecessor(self):
        record, launch = self.resume("context", new=True)
        self.assertNotIn("exec resume", launch.call_args.args[3])
        self.assertIsNone(record["receiver_session"])
        self.assertEqual(record["context_history"][-1]["receiver_session"], "old-receiver")
        with self.assertRaises(SystemExit):
            self.resume("model_route", new=True)


if __name__ == "__main__":
    unittest.main()
