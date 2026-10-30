"""suggest 与建议令牌：经 CLI 接缝验证建议条件、令牌拒绝、接受接线、常驻同意与拒绝后不再建议。"""
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from test_rd import rd
from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_suggest as suggestions
from tests import test_rd_dispatch as dispatch
from tests import test_rd_line as lines
from tests import test_rd_plan as planning

HEALTHY = dict(reachable=True, load1="0.1", ncpu="8", memfree="80", disk="200", running="0", heavy="0")


class SuggestTests(unittest.TestCase):
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
    prompt = lines.LineTests.prompt
    first_job = lines.LineTests.first_job
    finish_round = lines.LineTests.finish_round
    review = lines.LineTests.review
    accepted_first = lines.LineTests.accepted_first
    reset_item = lines.LineTests.reset_item
    run_item = lines.LineTests.run_item

    def patch_probe(self, info=HEALTHY):
        patcher = patch.object(rd, "probe", return_value=info)
        patcher.start()
        self.addCleanup(patcher.stop)

    def values(self, item, **overrides):
        values = dict(project="demo", plan_item=item, agent="codex", cmd=None, prompt_file=self.prompt(),
                      dispatch_mode="suggest", initiator_session="sender", json=True)
        values.update(overrides)
        return values

    def suggest(self, item="b", **overrides):
        """调用 suggest --json；返回解析后的结构。"""
        output = io.StringIO()
        with patch.object(rd, "agent_preflight", return_value=None), contextlib.redirect_stdout(output):
            rd.cmd_suggest(self.args(**self.values(item, **overrides)), self.cfg)
        return json.loads(output.getvalue())

    def rerun(self, item, expect=None, **overrides):
        """suggest 模式下 run；返回 (新增派单, 输出, 错误输出)。expect 给出时断言以该退出码退出。"""
        before = {r["job"] for r in rd.all_jobs()}
        out, err = io.StringIO(), io.StringIO()
        values = self.values(item, **overrides)
        values.pop("json")
        with patch.object(rd, "agent_preflight", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if expect:
                with self.assertRaises(SystemExit) as caught:
                    rd.cmd_run(self.args(**values), self.cfg)
                self.assertEqual(caught.exception.code, expect)
            else:
                rd.cmd_run(self.args(**values), self.cfg)
        # 工作线策略轮换后真实 harness 会在日志里给出新会话 ID；夹具没有日志，这里补上。
        for record in rd.all_jobs():
            if record.get("line") and not record.get("receiver_session"):
                record["receiver_session"] = "receiver"
                rd.save_job(record)
        return [r for r in rd.all_jobs() if r["job"] not in before], out.getvalue(), err.getvalue()

    def token_files(self):
        return list((Path(rd.STATE) / "suggest").rglob("*.json"))

    def accept(self, item="b", token=None, **overrides):
        return self.rerun(item, line="a", accept=token, **overrides)

    # ------------------------------------------------------------ 建议内容

    def test_suggestion_lists_work_items_line_host_branch_permission_and_a_token(self):
        self.patch_probe()
        record, head = self.accepted_first()
        result = self.suggest()
        self.assertTrue(result["suggest"])
        self.assertEqual(result["items"], dict(completed=["a"], current="b", pending=["c"]))
        self.assertEqual((result["line"], result["previous"], result["host"], result["branch"]),
                         ("a", record["job"], "fixture", record["branch"]))
        self.assertEqual((result["permission_mode"], result["base"]), (None, head))
        self.assertTrue(result["token"] and result["expires_at"] and result["fingerprint"])
        self.assertIn("--no-line", result["decline"])
        self.assertEqual(len(self.token_files()), 1)

    def test_suggest_is_read_only_and_prints_text_without_json(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        jobs = len(rd.all_jobs())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rd.cmd_suggest(self.args(**self.values("b", json=False)), self.cfg)
        text = output.getvalue()
        for expected in ("建议接入工作线 a", "--accept", "--no-line", "后续待派 c"):
            self.assertIn(expected, text)
        self.assertEqual((len(rd.all_jobs()), rd.load_job(record["job"])["rounds"]), (jobs, 1))
        self.assertNotIn("line", rd.load_job(record["job"]))

    # ------------------------------------------------------------ 建议条件反例

    def refused(self, reason, item="b", **overrides):
        before = len(self.token_files())
        result = self.suggest(item, **overrides)
        self.assertFalse(result["suggest"])
        self.assertIn(reason, result["reason"])
        self.assertNotIn("token", result)
        self.assertEqual(len(self.token_files()), before)

    def test_predecessor_not_accepted_gives_no_suggestion(self):
        self.patch_probe()
        record = self.first_job()
        self.finish_round(record, 1, "a.txt")
        self.refused("尚未验收通过")

    def test_predecessor_must_be_unique(self):
        self.patch_probe()
        self.accepted_first()
        row = rd.plans.PlanStore(rd.STATE, "employee_a", "demo")
        row.update("b", after=["employee_a-demo-x-1", "employee_a-demo-y-2"])
        self.refused("前序派单不唯一")

    def test_missing_handoff_protocol_gives_no_suggestion(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        record["handoff_version"] = None
        rd.save_job(record)
        self.refused("没有交接协议版本")

    def test_reclaimed_worktree_gives_no_suggestion(self):
        self.patch_probe()
        self.accepted_first()
        with patch.object(rd, "job_states", return_value={}):
            self.refused("已回收或目标机不可达")

    def test_running_predecessor_gives_no_suggestion(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        with patch.object(rd, "job_states", return_value={record["job"]: dict(state="running")}):
            self.refused("仍在运行")

    def test_other_user_id_gives_no_suggestion(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        record["user_id"] = "employee_b"
        rd.save_job(record)
        self.refused("不属于当前用户 ID")

    def test_offline_target_gives_no_suggestion(self):
        self.accepted_first()
        self.patch_probe(dict(reachable=False, error="timeout"))
        self.refused("离线或暂时无法接单")

    def test_busy_target_gives_no_suggestion(self):
        self.accepted_first()
        self.patch_probe(dict(HEALTHY, load1="99"))
        self.refused("离线或暂时无法接单")

    def test_strict_wiring_conditions_are_reused_not_rewritten(self):
        self.patch_probe()
        self.accepted_first()
        for overrides, reason in ((dict(wip=True), "--wip"), (dict(model="other-model"), "模型与线不一致"),
                                  (dict(agent="dsh"), "harness 与线不一致"), (dict(branch="rd/employee_a/x"), "不是线分支")):
            with self.subTest(reason):
                self.refused(reason, **overrides)

    def test_no_plan_item_gives_no_suggestion(self):
        self.patch_probe()
        self.accepted_first()
        self.refused("需要 --plan-item", item=None)

    # ------------------------------------------------------------ 令牌

    def issue(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        return record, self.suggest()["token"]

    def assert_rejected_and_unwired(self, record, token, message=None, **overrides):
        _, _, err = self.rerun("b", expect=4, line="a", accept=token, **overrides)
        if message:
            self.assertIn(message, err)
        self.assertEqual(rd.load_job(record["job"])["rounds"], 1)

    def test_token_rejections_exit_4_and_do_not_wire(self):
        record, token = self.issue()
        path = Path(rd.STATE) / "suggest" / "employee_a" / (token + ".json")
        saved = messages.read_json(path, limit=None)
        # 过期
        messages.atomic_json(path, dict(saved, expires=1.0), limit=None)
        self.assert_rejected_and_unwired(record, token, "已过期")
        # 已用
        messages.atomic_json(path, dict(saved, used=True), limit=None)
        self.assert_rejected_and_unwired(record, token, "已使用过")
        messages.atomic_json(path, saved, limit=None)
        # 指纹不符：建议之后待派登记变了
        self.plan("add", "d", "--depends", "b")
        self.assert_rejected_and_unwired(record, token, "不符")
        self.plan("remove", "d")
        # 发起会话不符
        self.assert_rejected_and_unwired(record, token, initiator_session="someone-else")
        # 用户 ID 不符
        with patch.dict("os.environ", {"RD_USER_ID": "employee_b"}):
            self.assert_rejected_and_unwired(record, token)
        # 乱写的令牌
        self.assert_rejected_and_unwired(record, "not-a-token")
        # 以上都没有消耗令牌：原样接受仍可用
        added, output, _ = self.accept(token=token)
        self.assertEqual(added, [])
        self.assertEqual(rd.load_job(record["job"])["rounds"], 2)

    def test_token_is_single_use(self):
        record, token = self.issue()
        self.accept(token=token)
        self.assertEqual(rd.load_job(record["job"])["rounds"], 2)
        # 令牌已用：重置工作项后再次拿它接受仍被拒绝
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        self.reset_item("b", None)
        saved = messages.read_json(next(iter(self.token_files())), limit=None)
        self.assertTrue(saved["used"])
        with self.assertRaises(suggestions.TokenRejected):
            suggestions.TokenStore(rd.STATE, "employee_a").consume(token, saved["fingerprint"], "sender")

    # ------------------------------------------------------------ 接受与直接接线

    def test_accepting_a_suggestion_wires_the_item_into_the_line(self):
        record, token = self.issue()
        added, output, _ = self.accept(token=token)
        self.assertEqual(added, [])
        line = rd.load_job(record["job"])
        self.assertEqual((line["rounds"], line["line"]["items"]), (2, {"a": [1], "b": [2]}))
        for expected in ("工作线 a", record["branch"]):
            self.assertIn(expected, output)

    def test_line_without_a_valid_token_or_consent_is_rejected_with_exit_4(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        _, _, err = self.rerun("b", expect=4, line="a")
        self.assertIn("建议令牌", err)
        self.rerun("b", expect=4, line="wrong-line", accept="x")
        self.assertEqual(rd.load_job(record["job"])["rounds"], 1)

    # ------------------------------------------------------------ 常驻同意

    def test_standing_consent_skips_the_token_but_still_checks_strict_conditions(self):
        record, token = self.issue()
        _, output, _ = self.accept(token=token, accept_chain=True)
        self.assertIn("常驻同意", output)
        self.assertTrue(self.plan("chain", "a", "--json") and json.loads(self.plan("chain", "a", "--json"))["accept_all"])
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        # 免令牌直接接线
        added, output, _ = self.rerun("c", line="a")
        self.assertEqual(added, [])
        self.assertEqual(rd.load_job(record["job"])["line"]["items"], {"a": [1], "b": [2], "c": [3]})

    def test_standing_consent_falls_back_to_default_dispatch_with_the_reason(self):
        record, token = self.issue()
        self.accept(token=token, accept_chain=True)
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        added, output, _ = self.rerun("c", line="a", model="other-model")
        self.assertEqual(len(added), 1)
        self.assertIn("模型与线不一致", output)
        self.assertEqual(rd.load_job(record["job"])["rounds"], 2)

    def test_revoking_consent_requires_a_token_again(self):
        record, token = self.issue()
        self.accept(token=token, accept_chain=True)
        self.plan("chain", "a", "--revoke-accept")
        self.finish_round(record, 2, "b.txt")
        self.review(record, 2)
        self.rerun("c", expect=4, line="a")

    # ------------------------------------------------------------ 拒绝

    def test_no_line_dispatches_by_default_and_stops_suggesting_until_cleared(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        files = len(self.token_files())
        added, output, _ = self.rerun("b", no_line=True)
        self.assertEqual(len(added), 1)
        self.assertIn("本链 a 不再建议", output)
        unchanged = rd.load_job(record["job"])
        self.assertEqual((unchanged["rounds"], "line" in unchanged), (1, False))
        self.assertEqual(len(self.token_files()), files)
        setting = json.loads(self.plan("chain", "a", "--json"))
        self.assertEqual((setting["no_suggest"], setting["rejections"]), (True, 1))
        # 工作项登记不受影响（chains 与 items 并存）
        self.assertIn("b", self.rows())
        self.reset_item("b", added[0])
        refused = self.suggest()
        self.assertFalse(refused["suggest"])
        self.assertIn("本链不再建议", refused["reason"])
        # 清除后恢复建议，拒绝次数归零
        self.plan("chain", "a", "--clear")
        self.assertEqual(json.loads(self.plan("chain", "a", "--json"))["rejections"], 0)
        self.assertTrue(self.suggest()["suggest"])

    def test_repeated_declines_count_up(self):
        self.patch_probe()
        self.accepted_first()
        first, _, _ = self.rerun("b", no_line=True)
        self.reset_item("b", first[0])
        self.rerun("b", no_line=True)
        self.assertEqual(json.loads(self.plan("chain", "a", "--json"))["rejections"], 2)

    def test_flag_combinations_are_validated(self):
        self.patch_probe()
        self.accepted_first()
        self.rerun("b", expect=4, line="a", no_line=True)
        self.rerun("b", expect=4, accept="x")
        self.rerun("b", expect=4, line="a", accept_chain=True)
        self.rerun("b", expect=4, no_line=True, plan_item=None)


if __name__ == "__main__":
    unittest.main()
