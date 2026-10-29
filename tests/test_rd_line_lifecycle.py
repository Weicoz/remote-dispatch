"""工作线生命周期：停滞、残留可见、同用户接手、换机交接包，经 CLI 接缝验证。"""
import contextlib
import io
import json
import os
import time
import unittest
from unittest.mock import patch

from test_rd import rd
from tests import test_rd_line as lines


class LineLifecycleTests(unittest.TestCase):
    # 组合而非继承，避免重复运行 LineTests 的用例。
    L = lines.LineTests
    setUp, git, local_shell, local_rpc, finish, args, capture = (
        L.setUp, L.git, L.local_shell, L.local_rpc, L.finish, L.args, L.capture)
    remote_head, plan, rows, prompt, run_item, first_job, finish_round, review, accepted_first = (
        L.remote_head, L.plan, L.rows, L.prompt, L.run_item, L.first_job, L.finish_round, L.review, L.accepted_first)
    del L

    def main(self, *argv, expect=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            if expect:
                with self.assertRaises(SystemExit) as caught:
                    rd.main(list(argv))
                self.assertEqual(caught.exception.code, expect)
            else:
                rd.main(list(argv))
        return output.getvalue()

    def line_job(self):
        """已验收的第一项加上已接入的第二项：工作线存在，第三项仍待派。"""
        record, _ = self.accepted_first()
        self.run_item("b")
        return rd.load_job(record["job"])

    def board_row(self, job):
        rows = json.loads(self.main("board", "--all", "--json"))["rows"]
        return next(row for row in rows if row["job"] == job)

    def age(self, record, days):
        old = time.time() - days * 86400
        os.utime(rd.job_path(record["job"]), (old, old))
        for ack in (self.root / "state" / "results" / record["job"]).glob("r*.ack.json"):
            os.utime(ack, (old, old))

    def test_line_with_pending_items_turns_stale_after_the_configured_days(self):
        record = self.line_job()
        self.assertNotIn("stale", self.board_row(record["job"]))
        self.age(record, 8)
        row = self.board_row(record["job"])
        self.assertIn("无人推进", row["stale"])
        self.assertIn("停滞", self.main("board", "--all"))
        with patch.dict(os.environ, {"RD_LINE_STALE_DAYS": "30"}):
            self.assertNotIn("stale", self.board_row(record["job"]))

    def test_stale_days_default_and_invalid_value(self):
        self.assertEqual(self.cfg.line_stale_days(), 7)
        with patch.dict(os.environ, {"RD_LINE_STALE_DAYS": "x"}), self.assertRaises(SystemExit) as caught:
            self.cfg.line_stale_days()
        self.assertEqual(caught.exception.code, 4)

    def test_missing_initiator_session_is_stale_at_once(self):
        record = self.line_job()
        changed = rd.load_job(record["job"])
        changed["initiator_session"] = None
        rd.save_job(changed)
        self.assertIn("发起会话缺失", self.board_row(record["job"])["stale"])

    def test_untracked_files_are_listed_in_the_receipt_and_kept(self):
        record, _ = self.accepted_first()
        leftover = self.worker / "wt" / record["job"] / "scratch.tmp"
        leftover.write_text("keep me")
        self.run_item("b")
        self.finish_round(rd.load_job(record["job"]), 2, "b.txt")
        receipt = json.loads(self.main("result", record["job"], "--initiator-session", "sender"))
        self.assertEqual(receipt["residue"], dict(files=["scratch.tmp"], total=1))
        self.assertTrue(leftover.exists())
        self.assertIn("scratch.tmp", self.main("board", "--all"))
        self.assertEqual(self.board_row(record["job"])["residue"]["total"], 1)

    def test_same_user_session_takes_over_the_line_and_it_is_recorded(self):
        record = self.line_job()
        self.main("takeover", record["job"], "--initiator-session", "other")
        changed = rd.load_job(record["job"])
        self.assertEqual(changed["initiator_session"], "other")
        self.assertEqual([(h["from_session"], h["to_session"]) for h in changed["line"]["handovers"]],
                         [("sender", "other")])
        self.main("result", record["job"], "--initiator-session", "sender", expect=4)

    def test_cross_user_takeover_is_rejected_with_exit_4(self):
        record = self.line_job()
        changed = rd.load_job(record["job"])
        changed["user_id"] = "employee_b"
        rd.save_job(changed)
        self.main("takeover", record["job"], "--initiator-session", "other", expect=4)
        self.assertEqual(rd.load_job(record["job"])["initiator_session"], "sender")

    def test_plain_job_cannot_be_taken_over(self):
        self.plan("add", "solo")
        (record,), _ = self.run_item("solo")
        self.main("takeover", record["job"], "--initiator-session", "other", expect=4)
        self.main("result", record["job"], "--initiator-session", "other", expect=4)

    def test_abandoned_line_is_no_longer_held_by_pending_items(self):
        record = self.line_job()
        self.age(record, 8)
        self.assertIn("plan_referenced", self.main("clean", "--expired", "--dry-run", "--json", "--project", "demo"))
        self.age(record, 15)
        out = self.main("clean", "--expired", "--dry-run", "--json", "--project", "demo")
        self.assertNotIn("plan_referenced", out)


if __name__ == "__main__":
    unittest.main()
