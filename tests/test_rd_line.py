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

    def run_item(self, item, keep_session=True, **overrides):
        """以 line 模式派一个工作项；返回 (新增的派单列表, 标准输出)。"""
        before = {r["job"] for r in rd.all_jobs()}
        values = dict(project="demo", plan_item=item, agent="codex", cmd=None, prompt_file=self.prompt(),
                      dispatch_mode="line", initiator_session="sender")
        values.update(overrides)
        output = io.StringIO()
        with patch.object(rd, "agent_preflight", return_value=None), contextlib.redirect_stdout(output):
            rd.cmd_run(self.args(**values), self.cfg)
        if keep_session:
            # 换新会话后真实 harness 会在日志里给出新会话 ID；夹具没有日志，这里补上。
            for record in rd.all_jobs():
                if record.get("line") and not record.get("receiver_session"):
                    record["receiver_session"] = "receiver"
                    rd.save_job(record)
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

    def patch_receipt(self, record, number, **fields):
        """改写已落盘的回执 report，模拟不健康或带用量的上一轮。"""
        path = rd.result_path(record, number)
        data = messages.read_json(path)
        data["report"].update(fields)
        messages.atomic_json(path, data, limit=messages.RECEIPT_BYTES)

    def second_round_prompt(self, record):
        return rd.load_job(record["job"])["round_prompts"]["2"]

    def test_default_policy_rotates_session_with_a_handoff_pack(self):
        record, head = self.accepted_first()
        prompts = []

        def spy(host, rec, number, command, prompt, timeout):
            prompts.append((number, prompt))
            return self.finish(host, rec, number, command, prompt, timeout)

        with patch.object(rd, "launch", side_effect=spy):
            self.run_item("b", keep_session=False)
        line = rd.load_job(record["job"])
        self.assertIsNone(line["receiver_session"])
        history = line["context_history"]
        self.assertEqual([(h["reason"], h["from_round"], h["receiver_session"]) for h in history],
                         [("policy", 1, "receiver")])
        self.assertIn("receipt_id", history[0])
        (number, launched), = prompts
        self.assertEqual(number, 2)
        for expected in ("上下文包", "来源 " + record["job"], "实现本工作项"):
            self.assertIn(expected, launched)
        self.assertNotIn("故障", launched.split("上下文包")[0])

    def test_policy_and_fault_reasons_are_recorded_separately(self):
        record, _ = self.accepted_first()
        self.run_item("b")
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        self.patch_receipt(record, 2, runtime_failure=dict(kind="context", work_observed=True, scan_complete=True))
        changed = rd.load_job(record["job"])
        changed["receiver_session"] = "receiver2"
        rd.save_job(changed)
        resume = argparse.Namespace(job=record["job"], prompt_file=self.prompt(), new_context=True, agent=None,
                                    session=None, host=None, model=None, effort=None, timeout=None, force_host=True,
                                    retry_after_repair=None, permission_mode=None, initiator_session="sender")
        with patch.object(rd, "agent_preflight", return_value=None):
            self.capture(rd.cmd_resume, resume)
        reasons = [h["reason"] for h in rd.load_job(record["job"])["context_history"]]
        self.assertEqual(reasons, ["policy", "context"])

    def test_policy_rotation_does_not_trigger_the_fault_loop_guard(self):
        record, _ = self.accepted_first()
        self.run_item("b")
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        self.patch_receipt(record, 2, runtime_failure=dict(kind="context", work_observed=False, scan_complete=True))
        changed = rd.load_job(record["job"])
        changed["receiver_session"] = "receiver2"
        rd.save_job(changed)
        resume = argparse.Namespace(job=record["job"], prompt_file=self.prompt(), new_context=True, agent=None,
                                    session=None, host=None, model=None, effort=None, timeout=None, force_host=True,
                                    retry_after_repair=None, permission_mode=None, initiator_session="sender")
        # 历史里最后一条是策略轮换（from_round=1），当前轮 3；即使相邻也不得被当作故障重建。
        history = changed["context_history"]
        history[-1]["from_round"] = changed["rounds"] - 1
        rd.save_job(changed)
        with patch.object(rd, "agent_preflight", return_value=None):
            self.capture(rd.cmd_resume, resume)
        self.assertEqual([h["reason"] for h in rd.load_job(record["job"])["context_history"]], ["policy", "context"])

    def test_unhealthy_previous_receipt_refuses_policy_rotation_with_reason(self):
        record, _ = self.accepted_first()
        self.patch_receipt(record, 1, runtime_failure=dict(kind="context", work_observed=True, scan_complete=True))
        added, output = self.run_item("b")
        self.assertEqual(len(added), 1)
        self.assertIn("上一轮回执不健康", output)
        self.assertIn("context", output)
        line = rd.load_job(record["job"])
        self.assertEqual(line["rounds"], 1)
        self.assertNotIn("context_history", line)

    def test_thresholds_keep_the_session_when_satisfied(self):
        record, _ = self.accepted_first()
        self.patch_receipt(record, 1, rollout_calls=dict(count=4, input_tokens=40000, cached_input_tokens=0, output_tokens=10))
        self.cfg.cp.add_section("line:codex")
        self.cfg.cp.set("line:codex", "reuse_max_context_tokens", "20000")
        self.cfg.cp.set("line:codex", "reuse_max_interval_seconds", "3600")
        self.run_item("b")
        line = rd.load_job(record["job"])
        self.assertEqual((line["rounds"], line["receiver_session"]), (2, "receiver"))
        self.assertNotIn("context_history", line)

    def test_thresholds_exceeded_or_missing_data_still_rotate(self):
        record, _ = self.accepted_first()
        self.cfg.cp.add_section("line:codex")
        self.cfg.cp.set("line:codex", "reuse_max_context_tokens", "5000")
        self.patch_receipt(record, 1, rollout_calls=dict(count=4, input_tokens=40000, cached_input_tokens=0, output_tokens=10))
        self.run_item("b")
        self.assertEqual([h["reason"] for h in rd.load_job(record["job"])["context_history"]], ["policy"])

    def test_missing_usage_data_rotates_when_a_context_threshold_is_set(self):
        record, _ = self.accepted_first()
        self.cfg.cp.add_section("line:codex")
        self.cfg.cp.set("line:codex", "reuse_max_context_tokens", "5000")
        self.run_item("b")
        self.assertEqual([h["reason"] for h in rd.load_job(record["job"])["context_history"]], ["policy"])

    def test_a_busy_job_lock_rejects_without_creating_a_job(self):
        record, _ = self.accepted_first()
        count = len(rd.all_jobs())
        with JobLock(rd.STATE, record["job"]), self.assertRaises(SystemExit) as caught:
            self.run_item("b")
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(len(rd.all_jobs()), count)

    def fetch(self, record):
        bare = self.worker / record["bare"]
        self.git("fetch", "-q", str(bare), f"+{record['branch']}:refs/remotes/rd/{record['branch']}")

    def two_items(self):
        """a、b 两个工作项都已验收；返回 (派单, a 的 head, b 的 head)。"""
        record, head_a = self.accepted_first()
        self.run_item("b")
        head_b = self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        self.fetch(record)
        return rd.load_job(record["job"]), head_a, head_b

    def line_cmd(self, action, record, *items, **overrides):
        values = dict(line_action=action, job=record["job"], items=list(items), branch=None,
                      initiator_session="sender")
        values.update(overrides)
        return self.capture(rd.cmd_line, argparse.Namespace(**values))

    def branch_head(self, name):
        return self.git("rev-parse", name).strip()

    def test_prefix_branch_holds_the_accepted_head_of_the_kth_item(self):
        record, head_a, head_b = self.two_items()
        self.line_cmd("prefix", record, "a")
        self.assertEqual(self.branch_head(record["branch"] + "-prefix-1"), head_a)
        self.line_cmd("prefix", record, "a", "b")
        second = record["branch"] + "-prefix-2"
        self.assertEqual(self.branch_head(second), head_b)
        self.git("merge-base", "--is-ancestor", head_a, second)
        self.assertEqual(self.branch_head(record["branch"] + "-prefix-1"), head_a)

    def test_skipping_or_unaccepted_items_are_rejected_with_exit_code_4(self):
        record, _, _ = self.two_items()
        self.run_item("c")
        for items in (("b",), ("a", "c"), ("c",), ("a", "b", "c"), ("nope",), ()):
            with self.subTest(items=items), self.assertRaises(SystemExit) as caught:
                self.line_cmd("prefix", record, *items)
            self.assertEqual(caught.exception.code, 4)
        self.assertEqual(self.git("branch", "--list", "*-prefix-*").strip(), "")

    def test_gate_runs_against_the_prefix_branch(self):
        record, _, head_b = self.two_items()
        self.line_cmd("prefix", record, "a", "b")
        name = record["branch"] + "-prefix-2"
        with open(rd.CONFIG, "a") as stream:
            stream.write("\n[project:app]\ntest_full=echo full\n")
        self.cfg = rd.Config()
        before = {r["job"] for r in rd.all_jobs()}
        values = dict(dir=str(self.repo), ref=name, host="fixture", project="app", timeout=15,
                      initiator_session=None, force_host=False, tag=[], include=[], name=None, branch=None, wip=False)
        self.capture(rd.cmd_gate, argparse.Namespace(**values))
        (gate,) = [r for r in rd.all_jobs() if r["job"] not in before]
        self.assertEqual(gate["gate"], {"ref": name, "commit": head_b})

    def test_open_line_is_not_merged_for_clean_until_closed(self):
        record, _, head_b = self.two_items()
        self.plan("cancel", "c")
        self.git("merge", "--ff-only", "-q", head_b)
        self.acknowledge_all(record)
        dry = dict(job=None, merged=True, force=False, delete_branch=False, discard_reports=False,
                   initiator_session="sender", into=None, dry_run=True, project="demo", dir=str(self.repo))
        before = self.capture(rd.cmd_clean, argparse.Namespace(**dry))
        self.assertNotIn("would clean", before)
        self.assertIn(record["job"], before)
        open_rec = rd.load_job(record["job"])
        snapshot = rd.job_states(self.cfg, [open_rec])[open_rec["job"]]
        code, detail, _, _ = rd.reclaim_check(self.cfg, open_rec, snapshot, rd.job_host(self.cfg, open_rec))
        self.assertEqual(code, rd.BLOCK_NOT_MERGED)
        self.assertIn("未关闭", detail)
        self.line_cmd("close", record)
        self.assertTrue(rd.load_job(record["job"])["line"]["closed"])
        after = self.capture(rd.cmd_clean, argparse.Namespace(**dry))
        self.assertIn("would clean " + record["job"], after)

    def acknowledge_all(self, record):
        for number in (1, 2):
            rec = rd.load_job(record["job"])
            rec["rounds"] = number
            snapshot = rd.job_states(self.cfg, [rec])[rec["job"]]
            receipt = rd.queries(self.cfg).result(rec, number, snapshot) if number == record["rounds"] else None
            receipt = receipt or messages.read_json(rd.result_path(rec, number))
            if receipt:
                rd.cmd_ack(argparse.Namespace(job=rec["job"], round=number, receipt_id=receipt["receipt_id"],
                                              initiator_session="sender"), self.cfg)

    def test_close_is_rejected_while_a_round_is_running(self):
        record, _, _ = self.two_items()
        for state in ("running", "starting"):
            with self.subTest(state), patch.object(rd, "job_states", return_value={record["job"]: dict(state=state)}), \
                    self.assertRaises(SystemExit) as caught:
                self.line_cmd("close", record)
            self.assertEqual(caught.exception.code, 4)
        self.assertNotIn("closed", rd.load_job(record["job"])["line"])

    def test_closed_line_takes_no_new_items(self):
        record, _, _ = self.two_items()
        self.line_cmd("close", record)
        added, output = self.run_item("c")
        self.assertEqual(len(added), 1)
        self.assertIn("已关闭", output)


if __name__ == "__main__":
    unittest.main()
