"""回收去阻塞：可再生目录不算未提交、按项目 ID 选择派单。本机派发的 CLI 接缝测试，不连接远端，不调用模型。"""
import argparse
import contextlib
import io
import json
import unittest

from remote_dispatch import cli as rd
from test_rd_slim import SlimFixture

REGEN_ONLY = "mkdir -p target node_modules; echo built > target/a.bin; echo dep > node_modules/b.js"
WITH_STRAY = REGEN_ONLY + "; echo note > notes.txt"


class ReclaimFixture(SlimFixture):
    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", "https://example.invalid/team/repo.git")

    def dispatch(self, command=REGEN_ONLY):
        known = {rec["job"] for rec in rd.all_jobs()}
        args = argparse.Namespace(dir=str(self.repo), agent=None, prompt_file=None, cmd=command,
                                  test=False, heavy=False, host="self", force_host=True, tag=[], ref="HEAD",
                                  wip=False, branch=None, name="reclaim", project=None, include=[], model=None,
                                  effort=None, timeout=15, initiator_session=None, workdir=None)
        rd.cmd_run(args, self.cfg)
        rec = next(r for r in rd.all_jobs() if r["job"] not in known)
        self.wait_done(rec)
        snapshot = rd.job_states(self.cfg, [rec])[rec["job"]]
        receipt = rd.collect_result(self.cfg, rec, snapshot)
        rd.cmd_ack(argparse.Namespace(job=rec["job"], round=receipt["round"], receipt_id=receipt["receipt_id"],
                                      initiator_session=None), self.cfg)
        return rec

    def clean_args(self, **options):
        values = dict(job=None, force=False, delete_branch=False, discard_reports=False, merged=False, expired=True,
                      into=None, dry_run=True, json=True, project=None, initiator_session=None, slim=False,
                      dir=str(self.repo))
        values.update(options)
        return argparse.Namespace(**values)

    def run_clean(self, **options):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rd.cmd_clean(self.clean_args(**options), self.cfg)
        return out.getvalue()

    def rows(self, **options):
        return {row["job"]: row for row in json.loads(self.run_clean(**options))}


class RegenerableUnblockTest(ReclaimFixture):
    def test_regenerable_only_worktree_is_reclaimable(self):
        rec = self.dispatch(REGEN_ONLY)
        row = self.rows()[rec["job"]]
        self.assertTrue(row["reclaimable"], row)
        self.run_clean(dry_run=False, json=False)
        self.assertFalse(self.paths(rec)[0].exists())

    def test_undeclared_untracked_file_still_blocks(self):
        rec = self.dispatch(WITH_STRAY)
        row = self.rows()[rec["job"]]
        self.assertEqual(row["code"], rd.BLOCK_DIRTY, row)
        self.assertIn("notes.txt", row["reason"])
        self.assertNotIn("target", row["reason"])

    def test_tracked_change_under_declared_path_still_blocks(self):
        rec = self.dispatch("mkdir -p target; echo x > target/a.bin; echo y >> tracked.txt")
        self.assertEqual(self.rows()[rec["job"]]["code"], rd.BLOCK_DIRTY)

    def test_single_clean_ignores_regenerable_but_not_stray(self):
        clean = self.dispatch(REGEN_ONLY)
        stray = self.dispatch(WITH_STRAY)
        rd.cmd_clean(self.clean_args(job=clean["job"], expired=False, dry_run=False, json=False), self.cfg)
        self.assertFalse(self.paths(clean)[0].exists())
        with self.assertRaises(SystemExit):
            rd.cmd_clean(self.clean_args(job=stray["job"], expired=False, dry_run=False, json=False), self.cfg)
        self.assertTrue(self.paths(stray)[0].exists())


class ProjectScopeTest(ReclaimFixture):
    def test_job_dispatched_from_another_worktree_is_reclaimable_from_main_repo(self):
        other = self.root / "integration"
        self.git("worktree", "add", "-q", "-b", "integration", str(other))
        main_job = self.dispatch(REGEN_ONLY)
        self.repo, main_repo = other, self.repo
        try:
            side_job = self.dispatch(REGEN_ONLY)
        finally:
            self.repo = main_repo
        self.assertEqual(side_job["project"], main_job["project"])
        rows = self.rows()
        self.assertEqual({main_job["job"], side_job["job"]}, set(rows))
        self.assertTrue(all(row["reclaimable"] for row in rows.values()), rows)

    def test_other_users_jobs_are_untouched(self):
        mine = self.dispatch(REGEN_ONLY)
        theirs = self.dispatch(REGEN_ONLY)
        rd.save_job(dict(rd.load_job(theirs["job"]), user_id="employee_b"))
        self.assertEqual(set(self.rows()), {mine["job"]})
        self.run_clean(dry_run=False, json=False)
        self.assertFalse(self.paths(mine)[0].exists())
        self.assertTrue(self.paths(theirs)[0].exists())

    def test_other_projects_are_not_selected(self):
        mine = self.dispatch(REGEN_ONLY)
        rd.save_job(dict(rd.load_job(mine["job"]), project="elsewhere"))
        self.assertEqual(self.rows(), {})
        self.assertEqual(set(self.rows(project="elsewhere")), {mine["job"]})


if __name__ == "__main__":
    unittest.main()
