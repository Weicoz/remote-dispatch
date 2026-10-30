"""接受建议时同步 integration 新头：经 CLI 接缝验证无新提交不同步、预检、同步后接线、冲突回退。"""
import subprocess
import unittest
from unittest.mock import patch

from test_rd import rd, native
from remote_dispatch import rd_board as board
from tests import test_rd_suggest as suggesting


class LineSyncTests(unittest.TestCase):
    """复用建议测试的夹具，不继承其用例。"""
    S = suggesting.SuggestTests
    setUp, git, local_shell, local_rpc, finish, args, capture, remote_head = (
        S.setUp, S.git, S.local_shell, S.local_rpc, S.finish, S.args, S.capture, S.remote_head)
    plan, rows, prompt, first_job, finish_round, review, accepted_first, reset_item, run_item = (
        S.plan, S.rows, S.prompt, S.first_job, S.finish_round, S.review, S.accepted_first, S.reset_item, S.run_item)
    patch_probe, values, suggest, rerun, token_files, accept, refused = (
        S.patch_probe, S.values, S.suggest, S.rerun, S.token_files, S.accept, S.refused)

    def advance_integration(self, name, text):
        """在控制端的 integration（测试里是 baseline 分支）上再提交一个文件。"""
        self.git("checkout", "-q", "baseline")
        (self.repo / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-qm", "integration " + name)
        self.git("checkout", "-q", "-")
        return self.git("rev-parse", "baseline").strip()

    def worktree_head(self, record):
        return native.git(self.worker / "wt" / record["job"], "rev-parse", "HEAD").stdout.strip()

    def test_no_new_integration_commit_means_no_sync(self):
        self.patch_probe()
        record, head = self.accepted_first()
        result = self.suggest()
        self.assertTrue(result["suggest"])
        self.assertEqual(result["sync"], dict(needed=False))
        _, output, _ = self.accept(token=result["token"])
        self.assertNotIn("已同步 integration", output)
        line = rd.load_job(record["job"])["line"]
        self.assertEqual((line["bases"]["b"], "syncs" in line), (head, False))
        self.assertEqual(self.worktree_head(record), head)

    def test_new_integration_commit_is_prechecked_then_synced_and_wired(self):
        self.patch_probe()
        record, head = self.accepted_first()
        integration = self.advance_integration("other.txt", "integration\n")
        result = self.suggest()
        self.assertTrue(result["suggest"])
        self.assertEqual(result["sync"], dict(needed=True, integration=integration, from_head=head, precheck="clean"))
        # 建议阶段只读：目标机 worktree 与 bare 都没动。
        self.assertEqual(self.worktree_head(record), head)
        bare = self.worker / "repos"
        self.assertEqual(subprocess.run(["git", "-C", str(next(bare.iterdir())), "for-each-ref", "refs/rd-sync"],
                                        capture_output=True, text=True).stdout, "")
        added, output, _ = self.accept(token=result["token"])
        self.assertEqual(added, [])
        merged = self.worktree_head(record)
        self.assertNotEqual(merged, head)
        self.assertTrue((self.worker / "wt" / record["job"] / "other.txt").exists())
        self.assertTrue((self.worker / "wt" / record["job"] / "a.txt").exists())
        self.assertIn("已同步 integration", output)
        saved = rd.load_job(record["job"])
        self.assertEqual((saved["rounds"], saved["line"]["bases"]["b"]), (2, merged))
        sync = saved["line"]["syncs"]["b"]
        self.assertEqual((sync["integration"], sync["from_head"], sync["head"]), (integration, head, merged))
        # 看板与回执都带同步记录。
        self.assertEqual(rd.workline.sync_for_round(saved, 2)["head"], merged)
        self.assertEqual(rd.workline.sync_for_round(saved, 1), None)
        row = board.row_for(rd.queries(self.cfg), saved, {"state": "done", "round": 2}, {}, "b", [], 0)
        self.assertEqual(row["integration_sync"]["head"], merged)
        self.assertIn("已同步 integration", board.render(dict(summary=dict(dispatched=1, running=0, pending=0, blocked=0),
                                                              rows=[row])))
        receipt = rd.queries(self.cfg).result(saved, 2, {"state": "done", "round": 2, "head": merged})
        self.assertEqual(receipt["integration_sync"]["integration"], integration)

    def test_retry_after_resume_failure_reuses_the_already_synced_worktree(self):
        """同步（写）成功后 resume 失败：重试不能被「worktree 已偏离验收提交」拒绝，也不重复合并。"""
        self.patch_probe()
        record, head = self.accepted_first()
        self.advance_integration("other.txt", "integration\n")
        token = self.suggest()["token"]
        with patch.object(rd, "resume_job", side_effect=SystemExit(1)):
            self.rerun("b", expect=1, line="a", accept=token)
        merged = self.worktree_head(record)
        self.assertNotEqual(merged, head)
        self.assertEqual(rd.load_job(record["job"])["rounds"], 1)
        retry = self.suggest()
        self.assertTrue(retry["suggest"], retry)
        added, output, _ = self.accept(token=retry["token"])
        self.assertEqual(added, [])
        self.assertNotIn("偏离验收提交", output)
        self.assertEqual(self.worktree_head(record), merged)
        saved = rd.load_job(record["job"])
        self.assertEqual((saved["rounds"], saved["line"]["bases"]["b"]), (2, merged))

    def test_precheck_conflict_gives_no_suggestion_and_names_the_files(self):
        self.patch_probe()
        self.accepted_first()
        self.advance_integration("a.txt", "integration version\n")
        self.refused("预检有冲突")
        self.assertIn("a.txt", self.suggest()["reason"])

    def test_real_merge_conflict_restores_the_worktree_and_dispatches_by_default(self):
        self.patch_probe()
        record, _ = self.accepted_first()
        head = self.worktree_head(record)
        self.advance_integration("a.txt", "integration version\n")
        # 让预检误判为通过，模拟「预检通过但实际合并冲突」；实际合并仍由目标机真实 git 执行。
        original = rd.git

        def lying_precheck(args, repo, *rest, **kw):
            if args[:1] == ["merge-tree"]:
                return subprocess.CompletedProcess(args, 0, "tree\n", "")
            return original(args, repo, *rest, **kw)
        # 预检被骗过，才会给出建议并取得令牌。
        with patch.object(rd, "git", side_effect=lying_precheck):
            token = self.suggest()["token"]
            added, output, _ = self.accept(token=token)
        self.assertEqual(len(added), 1)
        self.assertIn("实际合并有冲突", output)
        self.assertIn("按默认方式派出", output)
        self.assertEqual(self.worktree_head(record), head)
        status = native.git(self.worker / "wt" / record["job"], "status", "--porcelain", "--untracked-files=no").stdout
        self.assertEqual(status, "")
        merging = subprocess.run(["git", "-C", str(self.worker / "wt" / record["job"]), "rev-parse", "-q", "--verify",
                                  "MERGE_HEAD"], capture_output=True)
        self.assertNotEqual(merging.returncode, 0)
        saved = rd.load_job(record["job"])
        self.assertEqual((saved["rounds"], "b" in saved["line"]["items"] if saved.get("line") else False), (1, False))


if __name__ == "__main__":
    unittest.main()
