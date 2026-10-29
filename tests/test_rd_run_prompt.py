"""run 的终端询问、「本可接线」提示与看板工作线视图：经 CLI 接缝验证。"""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from test_rd import rd
from tests import test_rd_dispatch as dispatch
from tests import test_rd_line as lines
from tests import test_rd_plan as planning
from tests import test_rd_suggest as suggest


class Terminal(io.StringIO):
    def isatty(self):
        return True


class RunPromptTests(unittest.TestCase):
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
    patch_probe = suggest.SuggestTests.patch_probe
    values = suggest.SuggestTests.values

    def ask(self, item, answers, tty=True):
        """suggest 模式下 run（不带 --line／--no-line）；tty 时模拟终端并喂入回答。返回 (新增派单, 输出)。"""
        before = {r["job"] for r in rd.all_jobs()}
        out = Terminal() if tty else io.StringIO()
        stdin = Terminal(answers) if tty else io.StringIO(answers)
        values = self.values(item)
        values.pop("json")
        with patch.object(rd, "agent_preflight", return_value=None), patch("sys.stdin", stdin), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rd.cmd_run(self.args(**values), self.cfg)
        return [r for r in rd.all_jobs() if r["job"] not in before], out.getvalue()

    def board(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rd.main(["board", "--all", *argv])
        return output.getvalue()

    def test_non_terminal_does_not_block_and_attaches_hint(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        added, output = self.ask("b", "", tty=False)
        self.assertEqual(len(added), 1)
        self.assertIn("本可接入工作线 a", output)
        self.assertEqual(added[0]["line_hint"]["line"], "a")
        self.assertEqual(rd.load_job(record["job"])["rounds"], 1)
        row = next(r for r in json.loads(self.board("--json"))["rows"] if r["job"] == added[0]["job"])
        self.assertIn("本可接入工作线 a", row["line_hint"]["message"])
        self.assertIn("本可接入工作线 a", self.board())

    def test_terminal_yes_wires_like_accept(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        added, output = self.ask("b", "y\n")
        self.assertEqual(added, [])
        wired = rd.load_job(record["job"])
        self.assertEqual((wired["rounds"], wired["line"]["items"]), (2, {"a": [1], "b": [2]}))
        chain = json.loads(self.board("--json"))["chains"][0]
        self.assertEqual((chain["line"], chain["suggested"], chain["accepted"], chain["rejected"]), ("a", 1, 1, 0))

    def test_terminal_no_dispatches_by_default_like_no_line(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        added, output = self.ask("b", "maybe\nn\n")
        self.assertEqual(len(added), 1)
        self.assertNotIn("line_hint", added[0])
        self.assertEqual(rd.load_job(record["job"])["rounds"], 1)
        setting = json.loads(self.plan("chain", "a", "--json"))
        self.assertEqual((setting["no_suggest"], setting["rejections"]), (True, 1))

    def test_no_prompt_when_wiring_is_not_possible(self):
        self.patch_probe()
        record = self.first_job()
        self.finish_round(record, 1, "a.txt")  # 前序未验收：不可接线
        added, output = self.ask("b", "y\n")
        self.assertEqual(len(added), 1)
        self.assertNotIn("接入这条工作线", output)
        self.assertNotIn("line_hint", added[0])

    def test_board_shows_line_info_and_counts(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        self.ask("b", "y\n")
        data = json.loads(self.board("--json"))
        row = next(r for r in data["rows"] if r["job"] == record["job"])
        self.assertEqual((row["mode"], row["line"], row["line_items"], row["pending_items"], row["shared_branch"]),
                         ("line", "a", ["a", "b"], 1, True))
        self.assertEqual(row["branch"], record["branch"])
        text = self.board()
        self.assertIn("a -> b", text)
        self.assertIn("共用任务分支", text)
        self.assertIn("建议 1 次，接受 1 次", text)

    def test_single_item_row_has_mode_single(self):
        self.patch_probe()
        record = self.first_job()
        row = next(r for r in json.loads(self.board("--json"))["rows"] if r["job"] == record["job"])
        self.assertEqual((row["mode"], row["line"], row["shared_branch"]), ("single", None, False))


if __name__ == "__main__":
    unittest.main()
