"""看板 board：经 CLI 接缝验证范围、头部计数、老记录兜底、阻塞原因与只读。"""
import contextlib
import io
import json
import os
import unittest
import unittest.mock

from test_rd import rd
from tests import test_rd_dispatch as dispatch


class BoardTests(unittest.TestCase):
    setUp = dispatch.DispatchTests.setUp
    git = dispatch.DispatchTests.git
    local_shell = dispatch.DispatchTests.local_shell
    local_rpc = dispatch.DispatchTests.local_rpc
    finish = dispatch.DispatchTests.finish
    args = dispatch.DispatchTests.args
    capture = dispatch.DispatchTests.capture

    def board(self, *argv, expect=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            if expect:
                with self.assertRaises(SystemExit) as caught:
                    rd.main(["board", *argv])
                self.assertEqual(caught.exception.code, expect)
            else:
                rd.main(["board", *argv])
        return output.getvalue()

    def data(self, *argv):
        return json.loads(self.board("--json", *argv))

    def plan(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            rd.main(["plan", "-C", str(self.repo), "--project", "demo", *argv])

    def dispatch(self, **kwargs):
        before = {r["job"] for r in rd.all_jobs()}
        rd.cmd_run(self.args(**kwargs), self.cfg)
        return next(r for r in rd.all_jobs() if r["job"] not in before)

    def test_default_scope_is_current_session_and_all_includes_unowned_jobs(self):
        mine = self.dispatch(initiator_session="s1")
        other = self.dispatch(initiator_session="s2")
        orphan = self.dispatch()
        session = self.data("--initiator-session", "s1")
        self.assertEqual((session["scope"], session["initiator_session"]), ("session", "s1"))
        self.assertEqual([row["job"] for row in session["rows"]], [mine["job"]])
        everything = self.data("--all")
        self.assertEqual(everything["scope"], "all")
        self.assertEqual({row["job"] for row in everything["rows"]}, {mine["job"], other["job"], orphan["job"]})
        self.assertEqual(everything["summary"]["dispatched"], 3)

    def test_default_scope_requires_a_session(self):
        with unittest.mock.patch.dict(os.environ, {"RD_INITIATOR_SESSION": ""}), \
             unittest.mock.patch.object(rd.initiators, "session", return_value=None):
            self.board("--json", expect=4)

    def test_header_counts_match_plan_registry(self):
        self.plan("add", "a")
        self.plan("add", "b", "--depends", "a")
        self.plan("add", "c")
        job = self.dispatch(project="demo", plan_item="c", initiator_session="s1")
        with contextlib.redirect_stdout(io.StringIO()) as listed:
            rd.main(["plan", "-C", str(self.repo), "--project", "demo", "list", "--json"])
        registry = json.loads(listed.getvalue())["summary"]
        board = self.data("--all")
        self.assertEqual((board["summary"]["pending"], board["summary"]["blocked"]),
                         (registry["pending"], registry["blocked"]))
        self.assertEqual((board["summary"]["pending"], board["summary"]["blocked"]), (1, 1))
        self.assertEqual(board["rows"][0]["unit"], "c")
        self.assertEqual(board["rows"][0]["job"], job["job"])
        self.assertIn("已派 1  运行中 0  待派 1  阻塞 1", self.board("--all"))

    def test_old_record_missing_fields_shows_unrecorded(self):
        rec = self.dispatch(initiator_session="s1")
        for key in ("agent", "model", "created"):
            rec.pop(key, None)
        rd.save_job(rec)
        row = self.data("--all")["rows"][0]
        for key in ("harness", "model", "created", "elapsed_seconds"):
            self.assertEqual(row[key], "未记录", key)
        self.assertIn("未记录", self.board("--all"))

    def test_row_shows_reclaim_block_and_regenerable_sizes_when_present(self):
        rec = self.dispatch(project="demo", initiator_session="s1")
        self.plan("add", "next", "--after", rec["job"])
        row = self.data("--all")["rows"][0]
        self.assertEqual(row["reclaim_block"]["code"], "plan_referenced")
        self.assertNotIn("regenerable_sizes", row)
        receipt = dict(schema=1, job=rec["job"], round=1, host="fixture", receipt_id="r", captured_at="x",
                       execution_state="done", report=None, regenerable_sizes={"target": 5242880})
        rd.messages.atomic_json(rd.result_path(rec, 1), receipt)
        row = self.data("--all")["rows"][0]
        self.assertEqual(row["regenerable_sizes"], {"target": 5242880})
        self.assertIn("target 5MB", self.board("--all"))

    def test_board_does_not_write_state(self):
        self.dispatch(initiator_session="s1")
        self.plan("add", "a")

        def snapshot():
            return sorted((os.path.join(d, f), os.path.getmtime(os.path.join(d, f)))
                          for d, _, files in os.walk(rd.STATE) for f in files)
        before = snapshot()
        directories = sorted(d for d, _, _ in os.walk(rd.STATE))
        self.board("--json", "--all")
        self.board("--initiator-session", "s1")
        self.assertEqual(snapshot(), before)
        self.assertEqual(sorted(d for d, _, _ in os.walk(rd.STATE)), directories)


if __name__ == "__main__":
    unittest.main()
