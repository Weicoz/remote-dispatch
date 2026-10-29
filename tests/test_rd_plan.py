"""待派登记 plan：经 CLI 接缝验证增删改查、依赖成环、关联派单与回收保留。"""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from test_rd import rd
from tests import test_rd_dispatch as dispatch


class PlanTests(unittest.TestCase):
    setUp = dispatch.DispatchTests.setUp
    git = dispatch.DispatchTests.git
    local_shell = dispatch.DispatchTests.local_shell
    local_rpc = dispatch.DispatchTests.local_rpc
    finish = dispatch.DispatchTests.finish
    args = dispatch.DispatchTests.args
    bulk_args = dispatch.DispatchTests.bulk_args
    capture = dispatch.DispatchTests.capture
    remote_head = dispatch.DispatchTests.remote_head
    acknowledge = dispatch.DispatchTests.acknowledge

    def plan(self, *argv, expect=0):
        """调用 rdispatch plan；返回标准输出，非 0 退出码用 expect 断言。"""
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            if expect:
                with self.assertRaises(SystemExit) as caught:
                    rd.main(["plan", "-C", str(self.repo), "--project", "demo", *argv])
                self.assertEqual(caught.exception.code, expect)
            else:
                rd.main(["plan", "-C", str(self.repo), "--project", "demo", *argv])
        return output.getvalue()

    def rows(self):
        return {row["id"]: row for row in json.loads(self.plan("list", "--json"))["items"]}

    def run_item(self, item, **overrides):
        return self.dispatch(project="demo", plan_item=item, **overrides)

    def dispatch(self, **kwargs):
        before = {r["job"] for r in rd.all_jobs()}
        rd.cmd_run(self.args(**kwargs), self.cfg)
        return next(r for r in rd.all_jobs() if r["job"] not in before)

    def test_add_list_update_done_cancel_and_remove(self):
        self.plan("add", "api", "--title", "接口")
        self.plan("add", "ui", "--title", "界面", "--depends", "api")
        rows = self.rows()
        self.assertEqual((rows["api"]["state"], rows["api"]["title"]), ("pending", "接口"))
        self.assertEqual((rows["ui"]["state"], rows["ui"]["blocked_by"]), ("blocked", ["api"]))
        self.plan("set", "ui", "--title", "新界面")
        self.assertEqual(self.rows()["ui"]["title"], "新界面")
        self.plan("done", "api")
        rows = self.rows()
        self.assertEqual((rows["api"]["state"], rows["ui"]["state"]), ("done", "pending"))
        self.plan("cancel", "ui")
        self.assertEqual(self.rows()["ui"]["state"], "cancelled")
        self.plan("remove", "ui")
        self.assertEqual(set(self.rows()), {"api"})
        self.plan("add", "api", expect=4)
        self.plan("done", "missing", expect=4)

    def test_json_summary_counts_dispatched_pending_and_blocked(self):
        self.plan("add", "a")
        self.plan("add", "b", "--depends", "a")
        self.plan("add", "c")
        self.run_item("c")
        data = json.loads(self.plan("list", "--json"))
        self.assertEqual(data["project"], "demo")
        self.assertEqual(data["summary"], dict(dispatched=1, pending=1, blocked=1, done=0, cancelled=0))
        self.assertEqual({row["id"]: row["state"] for row in data["items"]},
                         dict(a="pending", b="blocked", c="dispatched"))

    def test_dependency_cycle_is_rejected_with_exit_code_4(self):
        self.plan("add", "a")
        self.plan("add", "b", "--depends", "a")
        self.plan("set", "a", "--depends", "b", expect=4)
        self.plan("set", "a", "--depends", "a", expect=4)
        self.plan("add", "c", "--depends", "missing", expect=4)
        self.assertEqual(self.rows()["a"]["depends"], [])

    def test_run_with_plan_item_marks_item_dispatched(self):
        self.plan("add", "api")
        record = self.run_item("api")
        row = self.rows()["api"]
        self.assertEqual((row["state"], row["jobs"]), ("dispatched", [record["job"]]))
        self.assertEqual(record["plan_item"], "api")

    def test_run_rejects_unknown_item_and_after_without_item(self):
        with self.assertRaises(SystemExit) as caught:
            self.dispatch(project="demo", plan_item="nope")
        self.assertEqual(caught.exception.code, 4)
        with self.assertRaises(SystemExit) as caught:
            self.dispatch(project="demo", after=["x"])
        self.assertEqual(caught.exception.code, 4)

    def test_plans_are_separated_by_project_and_user(self):
        self.plan("add", "api")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rd.main(["plan", "-C", str(self.repo), "--project", "other", "list", "--json"])
        self.assertEqual(json.loads(output.getvalue())["items"], [])
        with patch.dict("os.environ", {"RD_USER_ID": "employee_b"}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rd.main(["plan", "-C", str(self.repo), "--project", "demo", "list", "--json"])
        self.assertEqual(json.loads(output.getvalue())["items"], [])

    def verdict(self, record):
        with patch.object(rd, "disk_low", return_value=False):
            rows = json.loads(self.capture(rd.cmd_clean, self.bulk_args(dry_run=True, json=True, project="demo", dir=None)))
        return {row["job"]: row for row in rows}[record["job"]]

    def test_job_referenced_by_pending_item_is_not_reclaimed(self):
        self.plan("add", "api")
        self.plan("add", "ui", "--depends", "api")
        record = self.run_item("api")
        self.remote_head(record)
        self.acknowledge(record)
        row = self.verdict(record)
        self.assertEqual((row["reclaimable"], row["code"]), (False, "plan_referenced"))
        self.assertIn("ui", row["reason"])
        text = self.capture(rd.cmd_clean, self.bulk_args(project="demo", dir=None))
        self.assertIn("plan_referenced", text)
        self.assertTrue((self.worker / "wt" / record["job"]).exists())
        # 后续工作项取消后不再引用，派单可回收
        self.plan("cancel", "ui")
        self.assertEqual(self.verdict(record)["code"], "ok")

    def test_after_job_is_referenced_until_the_item_is_dispatched(self):
        first = self.dispatch(project="demo")
        self.remote_head(first)
        self.acknowledge(first)
        self.assertEqual(self.verdict(first)["code"], "ok")
        self.plan("add", "next")
        self.plan("set", "next", "--after", first["job"])
        self.assertEqual(self.verdict(first)["code"], "plan_referenced")
        second = self.run_item("next", after=[first["job"]])
        self.assertEqual(self.rows()["next"]["state"], "dispatched")
        self.assertEqual(self.verdict(first)["code"], "ok")
        self.assertEqual(self.rows()["next"]["jobs"], [second["job"]])

    def test_single_clean_blocks_referenced_job_unless_forced(self):
        self.plan("add", "api")
        self.plan("add", "ui", "--depends", "api")
        record = self.run_item("api")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as error:
            rd.cmd_clean(self.bulk_args(job=record["job"], merged=False, project=None), self.cfg)
        self.assertIn("ui", error.getvalue())
        self.assertTrue((self.worker / "wt" / record["job"]).exists())


if __name__ == "__main__":
    unittest.main()
