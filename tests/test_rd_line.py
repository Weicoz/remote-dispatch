"""工作线：经 CLI 接缝验证接线条件、工作项与轮次对应、逐项验收、按工作项的 RD_BASE。"""
import argparse
import contextlib
import io
import unittest
from unittest.mock import patch

from test_rd import rd, native
from remote_dispatch import rd_messages as messages
from remote_dispatch.rd_lock import JobLock
from tests import test_rd_dispatch as dispatch
from tests import test_rd_plan as planning


class LineTests(unittest.TestCase):
    setUp = dispatch.DispatchTests.setUp
    git = dispatch.DispatchTests.git
    local_shell = dispatch.DispatchTests.local_shell
    local_rpc = dispatch.DispatchTests.local_rpc
    finish = dispatch.DispatchTests.finish
    args = dispatch.DispatchTests.args
    capture = dispatch.DispatchTests.capture
    remote_head = dispatch.DispatchTests.remote_head
    plan = planning.PlanTests.plan
    rows = planning.PlanTests.rows

    def prompt(self):
        path = self.root / "prompt.md"
        path.write_text("实现本工作项")
        return str(path)

    def run_item(self, item, **overrides):
        """以 line 模式派一个工作项；返回 (新增的派单列表, 标准输出)。"""
        before = {r["job"] for r in rd.all_jobs()}
        values = dict(project="demo", plan_item=item, agent="codex", cmd=None, prompt_file=self.prompt(),
                      dispatch_mode="line", initiator_session="sender")
        values.update(overrides)
        output = io.StringIO()
        with patch.object(rd, "agent_preflight", return_value=None), contextlib.redirect_stdout(output):
            rd.cmd_run(self.args(**values), self.cfg)
        return [r for r in rd.all_jobs() if r["job"] not in before], output.getvalue()

    def first_job(self):
        self.plan("add", "a")
        self.plan("add", "b", "--depends", "a")
        self.plan("add", "c", "--depends", "b")
        (record,), _ = self.run_item("a")
        record["receiver_session"] = "receiver"
        rd.save_job(record)
        return record

    def finish_round(self, record, number, name):
        """假装目标机在该轮提交了一个文件并写好结果回执；返回提交号。"""
        wt = self.worker / "wt" / record["job"]
        (wt / name).write_text(name)
        native.git(wt, "add", name)
        native.git(wt, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", name)
        head = native.git(wt, "rev-parse", "HEAD").stdout.strip()
        directory = self.worker / "jobs" / record["job"]
        (directory / "head").write_text(head)
        messages.report(directory, record["job"], number, "result", summary=name, outcome="completed", checks="passed")
        return head

    def review(self, record, number, reject=None):
        args = argparse.Namespace(job=record["job"], round=number, reject=reject,
                                  review_action=None if reject else "accepted", reviewed_by="sender", via="cli",
                                  initiator_session="sender", host=None, agent=None, session=None, model=None,
                                  effort=None, timeout=None, force_host=True)
        self.capture(rd.cmd_review, args)

    def accepted_first(self):
        record = self.first_job()
        head = self.finish_round(record, 1, "a.txt")
        self.review(record, 1)
        return rd.load_job(record["job"]), head

    def run_script(self, record, number):
        return rd.build_run_script(self.cfg.host("fixture"), record, number, 10)

    def test_accepted_predecessor_is_continued_as_a_new_round_of_the_same_job(self):
        record, head = self.accepted_first()
        jobs_before = len(rd.all_jobs())
        added, output = self.run_item("b", model=None)
        self.assertEqual((added, len(rd.all_jobs())), ([], jobs_before))
        line = rd.load_job(record["job"])
        self.assertEqual((line["rounds"], line["line"]["name"], line["line"]["items"]), (2, "a", {"a": [1], "b": [2]}))
        self.assertEqual(self.rows()["b"]["jobs"], [record["job"]])
        for expected in ("工作线 a", record["branch"], "权限档位 默认", head[:10]):
            self.assertIn(expected, output)

    def test_same_branch_as_the_line_still_wires(self):
        record, _ = self.accepted_first()
        added, _ = self.run_item("b", branch=record["branch"])
        self.assertEqual((added, rd.load_job(record["job"])["rounds"]), ([], 2))

    def test_each_strict_condition_has_a_counter_example_with_the_reason(self):
        cases = {
            "harness": (dict(agent="dsh"), "harness 与线不一致"),
            "model": (dict(model="other-model"), "模型与线不一致"),
            "include": (dict(include=["tracked.txt"]), "--include 与线不一致"),
            "branch": (dict(branch="rd/employee_a/other"), "不是线分支"),
        }
        record, _ = self.accepted_first()
        for name, (overrides, reason) in cases.items():
            with self.subTest(name):
                added, output = self.run_item("b", **overrides)
                self.assertEqual(len(added), 1)
                self.assertIn(reason, output)
                self.assertEqual(rd.load_job(record["job"])["rounds"], 1)
                self.assertNotIn("line", rd.load_job(record["job"]))
                self.reset_item("b", added[0])

    def reset_item(self, item, job):
        """把工作项恢复为只依赖 a 且无派单，便于同一线上继续做反例。"""
        path = self.root / "state" / "plan"
        for file in path.rglob("demo.json"):
            data = messages.read_json(file, limit=None)
            data["items"][item]["jobs"] = []
            messages.atomic_json(file, data, limit=None)

    def test_wip_and_permission_and_handoff_and_user_conditions(self):
        record, _ = self.accepted_first()
        (self.repo / "tracked.txt").write_text("changed\n")
        added, output = self.run_item("b", wip=True)
        self.assertEqual(len(added), 1)
        self.assertIn("--wip", output)
        self.reset_item("b", added[0])
        self.git("checkout", "--", "tracked.txt")
        for field, value, reason in (("permission_mode", "plan", "权限档位与线不一致"),
                                     ("handoff_version", None, "没有交接协议版本"),
                                     ("user_id", "employee_b", "不属于当前用户 ID")):
            with self.subTest(field):
                changed = rd.load_job(record["job"])
                changed[field] = value
                rd.save_job(changed)
                added, output = self.run_item("b")
                self.assertEqual(len(added), 1)
                self.assertIn(reason, output)
                self.reset_item("b", added[0])
                changed[field] = record.get(field)
                rd.save_job(changed)

    def test_predecessor_must_be_accepted_before_the_next_item_starts(self):
        record = self.first_job()
        self.finish_round(record, 1, "a.txt")
        added, output = self.run_item("b")
        self.assertEqual(len(added), 1)
        self.assertIn("尚未验收通过", output)
        self.assertEqual(rd.load_job(record["job"])["rounds"], 1)

    def test_other_modes_never_wire(self):
        record, _ = self.accepted_first()
        for mode in ("suggest", "ticket"):
            with self.subTest(mode):
                added, output = self.run_item("b", dispatch_mode=mode)
                self.assertEqual(len(added), 1)
                self.assertNotIn("工作线 a：工作项", output)  # 接线成功提示；suggest 的「本可接入」提示不算
                self.assertEqual(rd.load_job(record["job"])["rounds"], 1)
                self.reset_item("b", added[0])

    def test_reject_only_resumes_the_current_item(self):
        record, _ = self.accepted_first()
        self.run_item("b")
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2, reject="补测试")
        line = rd.load_job(record["job"])
        self.assertEqual((line["rounds"], line["line"]["items"]), (3, {"a": [1], "b": [2, 3]}))
        self.assertEqual(line["line"]["current"], "b")

    def test_rd_base_follows_the_work_item(self):
        record, first_head = self.accepted_first()
        self.run_item("b")
        second_head = self.finish_round(record, 2, "b.txt")
        self.review(record, 2, reject="补测试")
        self.finish_round(record, 3, "b2.txt")
        self.review(record, 3)
        self.run_item("c")
        line = rd.load_job(record["job"])
        self.assertEqual(line["line"]["items"], {"a": [1], "b": [2, 3], "c": [4]})
        self.assertIn(f" RD_BASE={self.base}\n", self.run_script(line, 1))
        for number in (2, 3):
            self.assertIn(f" RD_BASE={first_head}\n", self.run_script(line, number))
        third_base = line["line"]["bases"]["c"]
        self.assertNotIn(third_base, (self.base, first_head, second_head))
        self.assertIn(f" RD_BASE={third_base}\n", self.run_script(line, 4))

    def test_feature_off_blocks_new_lines_but_existing_lines_continue(self):
        record, _ = self.accepted_first()
        self.cfg.cp.add_section("features")
        self.cfg.cp.set("features", "line", "false")
        added, output = self.run_item("b")
        self.assertEqual(len(added), 1)
        self.assertIn("[features] line 已关闭", output)
        self.reset_item("b", added[0])
        self.assertNotIn("line", rd.load_job(record["job"]))
        self.cfg.cp.set("features", "line", "true")
        self.run_item("b")
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        self.cfg.cp.set("features", "line", "false")
        added, _ = self.run_item("c")
        self.assertEqual(added, [])
        self.assertEqual(rd.load_job(record["job"])["line"]["items"], {"a": [1], "b": [2], "c": [3]})

    def test_a_busy_job_lock_rejects_without_creating_a_job(self):
        record, _ = self.accepted_first()
        count = len(rd.all_jobs())
        with JobLock(rd.STATE, record["job"]), self.assertRaises(SystemExit) as caught:
            self.run_item("b")
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(len(rd.all_jobs()), count)


if __name__ == "__main__":
    unittest.main()
