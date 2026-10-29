"""上下文包：run --context-from 的 CLI 接缝，以及拼装模块的截断规则。不调用模型。"""
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from remote_dispatch import rd_context_pack as pack
from remote_dispatch import rd_messages as messages
from test_rd import rd
import test_rd_dispatch


class ContextFromTests(unittest.TestCase):
    def setUp(self):
        # 复用派单夹具（临时仓库、本机 shell 模拟），但不继承它的用例。
        self.fixture = test_rd_dispatch.DispatchTests("setUp")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def dispatch(self, **kwargs):
        return self.fixture.dispatch(**kwargs)

    def prior(self, summary="完成了解析器", user="employee_a", receipt=True, review=None, **report):
        if receipt:
            rec = self.dispatch(name="prior")
        else:
            # 没有收尾的派单：远端没有结果，本地也没有回执。
            with patch.object(rd, "launch"):
                rec = self.dispatch(name="prior")
        if user != "employee_a":
            rec["user_id"] = user
            rd.save_job(rec)
        if receipt:
            body = dict(schema=1, kind="result", job=rec["job"], round=1, summary=summary,
                        outcome="completed", checks="passed", **report)
            rd.persist_receipt(rec, dict(job=rec["job"], round=1, receipt_id="r-1", head="a" * 40,
                                         execution_state="done", executor_exit=0, report=body))
        if review:
            path = rd.reviews.review_path(rd.STATE, rec, 1)
            messages.atomic_json(path, dict(review_state=review, reviewed_by="x", reviewed_at="t", review_note=""))
        return rec

    def agent_dispatch(self, prompt_text, **kwargs):
        prompt = self.root / "prompt.md"
        prompt.write_text(prompt_text, encoding="utf-8")
        sent = []
        with patch.object(rd, "agent_preflight", return_value=None), \
             patch.object(rd, "launch", side_effect=lambda host, rec, n, cmd, text, t: sent.append(text)):
            record = self.dispatch(agent="codex", cmd=None, prompt_file=str(prompt), name="next", **kwargs)
        return record, sent[0]

    def refused(self, **kwargs):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            self.agent_dispatch("新任务", **kwargs)
        self.assertEqual(caught.exception.code, 4)
        return errors.getvalue()

    def test_pack_goes_before_prompt(self):
        prior = self.prior(decisions=["用递归下降"], unfinished=["补错误位置"], review="accepted")
        record, sent = self.agent_dispatch("新任务正文", context_from=[prior["job"]])
        self.assertTrue(sent.startswith("# 上下文包"))
        for text in (prior["job"], "完成了解析器", "用递归下降", "补错误位置", "验收 accepted"):
            self.assertIn(text, sent)
        self.assertLess(sent.index("完成了解析器"), sent.index("新任务正文"))
        self.assertIn("上下文包", record["round_prompts"]["1"])

    def test_several_sources_are_all_included(self):
        first, second = self.prior("第一个摘要"), self.prior("第二个摘要")
        _, sent = self.agent_dispatch("正文", context_from=[first["job"], second["job"]])
        self.assertIn("第一个摘要", sent)
        self.assertIn("第二个摘要", sent)

    def test_oversized_pack_is_truncated_with_notice(self):
        prior = self.prior("摘要保留", commits=None, unfinished=["未完成保留"],
                           changed_files=[{"path": f"src/package/module_dir/file{n}.py", "additions": 1, "deletions": 0} for n in range(500)])
        _, sent = self.agent_dispatch("正文", context_from=[prior["job"]])
        self.assertIn("摘要保留", sent)
        self.assertIn("未完成保留", sent)
        self.assertIn("已截断", sent)
        self.assertIn("改动文件", sent.split("已截断")[1])
        self.assertLessEqual(pack.estimate_tokens(sent.split("\n\n")[0]), pack.LIMIT_TOKENS + 200)

    def test_other_users_job_is_refused(self):
        prior = self.prior(user="employee_b")
        self.assertIn("不属于当前用户", self.refused(context_from=[prior["job"]]))

    def test_job_without_receipt_is_refused(self):
        prior = self.prior(receipt=False)
        self.assertIn("没有回执", self.refused(context_from=[prior["job"]]))
        self.assertEqual(len(rd.all_jobs()), 1)

    def test_command_dispatch_cannot_take_context(self):
        prior = self.prior()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.dispatch(context_from=[prior["job"]])
        self.assertEqual(caught.exception.code, 4)


class BuildTests(unittest.TestCase):
    def source(self, **kwargs):
        return dict(dict(job="j1", round=2, summary="摘要", unfinished=["u1"], decisions=["d1"],
                         commits=["c1"], files=["f1"]), **kwargs)

    def test_small_pack_is_complete_and_not_marked_truncated(self):
        text = pack.build([self.source()])
        for part in ("摘要", "u1", "d1", "c1", "f1", "第 2 轮"):
            self.assertIn(part, text)
        self.assertNotIn("已截断", text)

    def test_files_are_dropped_before_commits_before_decisions(self):
        source = self.source(files=[f"path/{n}" for n in range(200)], commits=[f"c{n}" for n in range(200)])
        text = pack.build([source], limit=400)
        self.assertIn("已截断", text)
        self.assertIn("摘要", text)
        self.assertIn("d1", text)
        self.assertIn("u1", text)
        self.assertLessEqual(pack.estimate_tokens(text), 400)
        self.assertGreater(text.count("path/"), -1)
        self.assertLessEqual(text.count("path/"), text.count("- c"))

    def test_empty_sources_give_empty_pack(self):
        self.assertEqual(pack.build([]), "")


if __name__ == "__main__":
    unittest.main()
