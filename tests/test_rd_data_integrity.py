"""数据正确性：假终态不落盘、keep 持锁、产物按轮次取回。"""
import contextlib
import io
import os
import subprocess
import tarfile
import unittest
from unittest.mock import patch

from remote_dispatch import rd_artifacts as artifacts
from remote_dispatch import rd_lock
from remote_dispatch import rd_query as query
from test_rd import rd
from test_rd_artifact_fingerprint import Fixture


class FalseTerminalTests(Fixture):
    def query(self):
        return query.Query(rd.STATE, states=lambda recs: {}, report=lambda *a: None, receiver=lambda rec: None)

    def result(self, stage, state):
        rec = dict(job="job-a", host="h", rounds=1, user_id="u", dispatch_state=stage)
        return self.query().result(rec, 1, {"state": state, "round": "1", "exit": "0"})

    def test_preparing_never_yields_a_terminal_receipt(self):
        for state in ("missing", "dead", "done"):
            with self.subTest(state=state):
                self.assertIsNone(self.result("preparing", state))

    def test_launching_withholds_missing_and_dead_but_trusts_done(self):
        self.assertIsNone(self.result("launching", "missing"))
        self.assertIsNone(self.result("launching", "dead"))
        self.assertEqual(self.result("launching", "done")["execution_state"], "done")

    def test_started_and_legacy_records_still_get_receipts(self):
        for stage in ("started", None):
            self.assertEqual(self.result(stage, "missing")["execution_state"], "missing")

    def test_collect_result_does_not_cache_while_preparing(self):
        record = self.t.dispatch()
        record["dispatch_state"] = "preparing"
        rd.collect_result(self.t.cfg, record, {"state": "missing", "round": "1"})
        self.assertFalse(rd.result_path(record, 1).exists())
        record["dispatch_state"] = "started"
        self.assertIsNotNone(rd.collect_result(self.t.cfg, record, {"state": "done", "round": "1", "exit": "0"}))
        self.assertTrue(rd.result_path(record, 1).exists())

    def test_run_holds_the_job_lock_until_started(self):
        seen = []

        def launch(host, rec, *args):
            try:
                rd_lock.JobLock(rd.STATE, rec["job"]).acquire()
                seen.append("free")
            except rd_lock.JobBusy:
                seen.append("busy")
            return self.t.finish(host, rec, *args)
        with patch.object(rd, "launch", side_effect=launch):
            record = self.t.dispatch()
        self.assertEqual(seen, ["busy"])
        rd_lock.JobLock(rd.STATE, record["job"]).acquire().release()  # 派单结束后锁已释放


class StuckLaunchingTests(Fixture):
    def stuck(self, stage="launching"):
        record = self.t.dispatch()
        record["dispatch_state"] = stage
        rd.save_job(record)
        return record

    def collect(self, record, snapshot, fresh=None):
        """snapshot 是拿锁前的旧快照；锁内重新查询的远端状态取 fresh（缺省同 snapshot）。"""
        answer = dict(snapshot if fresh is None else fresh)
        with patch.object(rd, "remote_state", side_effect=lambda host, jobs: {record["job"]: dict(answer)}):
            return rd.collect_result(self.t.cfg, record, dict(snapshot))

    def test_state_is_rechecked_under_the_lock_not_taken_from_the_stale_snapshot(self):
        record = self.stuck()  # 旧快照没有痕迹，但拿锁后远端已经启动
        self.collect(record, {"state": "starting", "round": "0"}, fresh={"state": "running", "round": "1"})
        self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "started")
        record = self.stuck()  # 旧快照有痕迹，锁内复查却连不上：不动
        self.collect(record, {"state": "dead", "round": "1"}, fresh={"state": "unreachable"})
        self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "launching")

    def test_preparing_left_by_a_killed_run_becomes_preparation_failed(self):
        record = self.stuck("preparing")
        self.collect(record, {"state": "starting", "round": "0"})
        self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "preparation_failed")

    def test_preparing_with_launch_traces_or_held_lock_is_not_failed(self):
        record = self.stuck("preparing")
        self.collect(record, {"state": "starting", "round": "0"}, fresh={"state": "running", "round": "1"})
        self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "started")
        record = self.stuck("preparing")  # 派单仍持锁（进行中）：不动
        with rd_lock.JobLock(rd.STATE, record["job"]):
            self.collect(record, {"state": "starting", "round": "0"})
        self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "preparing")

    def test_remote_traces_of_a_launch_correct_the_state_to_started(self):
        for snapshot in ({"state": "dead", "round": "1"}, {"state": "running", "round": "1"},
                         {"state": "starting", "round": "1"}, {"state": "done", "round": "1", "exit": "0"}):
            with self.subTest(snapshot=snapshot):
                record = self.stuck()
                self.collect(record, snapshot)
                self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "started")
                self.assertEqual(record["dispatch_state"], "started")

    def test_dead_after_correction_yields_a_receipt_so_reclaim_is_not_blocked_forever(self):
        record = self.stuck()
        receipt = self.collect(record, {"state": "dead", "round": "1", "exit": ""})
        self.assertEqual(receipt["execution_state"], "dead")

    def test_no_launch_traces_means_preparation_failed(self):
        for snapshot in ({"state": "starting", "round": "0"}, {"state": "missing"}):
            with self.subTest(snapshot=snapshot):
                record = self.stuck()
                self.collect(record, snapshot)
                self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "preparation_failed")

    def test_unreachable_or_busy_job_is_left_alone(self):
        record = self.stuck()
        self.collect(record, {"state": "unreachable"})
        self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "launching")
        with rd_lock.JobLock(rd.STATE, record["job"]):
            self.collect(record, {"state": "dead", "round": "1"})
        self.assertEqual(rd.load_job(record["job"])["dispatch_state"], "launching")


class KeepLockTests(Fixture):
    def test_keep_waits_for_the_lock_and_keeps_concurrent_writes(self):
        record = self.t.dispatch()
        holder = rd_lock.JobLock(rd.STATE, record["job"]).acquire()
        args = type("A", (), dict(job=record["job"], hours=2))()
        with patch.dict(os.environ, {"RD_LOCK_WAIT_SECONDS": "0"}), self.assertRaises(SystemExit) as stop:
            rd.cmd_keep(args, self.t.cfg)
        self.assertEqual(stop.exception.code, 4)
        self.assertNotIn("keep_until", rd.load_job(record["job"]))
        # 持锁方写入新轮次后释放；keep 必须读到新轮次，而不是用旧快照覆盖。
        newer = rd.load_job(record["job"])
        newer["rounds"] = 2
        rd.save_job(newer)
        holder.release()
        rd.cmd_keep(args, self.t.cfg)
        saved = rd.load_job(record["job"])
        self.assertEqual(saved["rounds"], 2)
        self.assertIn("keep_until", saved)


class ArtifactRoundTests(Fixture):
    def test_next_round_pulls_again_and_keeps_per_round_state(self):
        record = self.t.dispatch(artifact=["out.txt"])
        wt = self.worktree(record)
        (wt / "out.txt").write_text("round one")
        snapshot = {"state": "done", "round": "1"}
        first = rd.auto_pull_artifacts(self.t.cfg, record, snapshot)
        self.assertEqual(first[0]["status"], "fetched")
        self.assertEqual((self.repo / "out.txt").read_text(), "round one")
        self.assertFalse(artifacts.artifacts_pending(rd.load_job(record["job"])))
        # 续轮：第 1 轮的已取回不能顶替第 2 轮。
        record = rd.load_job(record["job"])
        record["rounds"] = 2
        rd.save_job(record)
        self.assertTrue(artifacts.artifacts_pending(record))
        (wt / "out.txt").write_text("round two, longer")
        second = rd.auto_pull_artifacts(self.t.cfg, record, {"state": "done", "round": "2"})
        self.assertEqual(second[0]["status"], "fetched")
        self.assertEqual((self.repo / "out.txt").read_text(), "round two, longer")
        saved = rd.load_job(record["job"])
        self.assertEqual(sorted(saved["artifacts_rounds"]), ["1", "2"])
        self.assertEqual(saved["artifacts_rounds"]["2"]["round"], 2)
        self.assertEqual(saved["artifacts_rounds"]["1"]["paths"], ["out.txt"])
        self.assertFalse(artifacts.artifacts_pending(saved))

    def test_legacy_state_without_round_counts_for_round_one_only(self):
        rec = dict(artifact_paths=["a"], rounds=1,
                   artifacts_state=dict(results=[dict(path="a", status="fetched")], at="x"))
        self.assertFalse(artifacts.artifacts_pending(rec))
        rec["rounds"] = 2
        self.assertTrue(artifacts.artifacts_pending(rec))


if __name__ == "__main__":
    unittest.main()
