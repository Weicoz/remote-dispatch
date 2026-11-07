"""serve 定时回收的接缝测试：Service.reclaim.run_once 与状态、事件、看板、/v1/reclaim 输出。
本机目标机，不连接远端，不调用模型。"""
import contextlib
import io
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd
from remote_dispatch import rd_board as board
from remote_dispatch import rd_events as events
from remote_dispatch import rd_serve
from remote_dispatch.rd_serve import Service

# 测试不连接真实 hc；hc 后端的测试自行注入假 hc。
os.environ.setdefault("RD_HC_BIN", "")
from test_rd_reclaim_unblock import ReclaimFixture

# 每个 worktree 里 target 约 1.5 MB（可再生），big.bin 约 1.5 MB（已跟踪，瘦身后仍在）。
BUILD = "mkdir -p target; head -c 1500000 /dev/zero > target/a.bin"
STRAY = BUILD + "; echo note > notes.txt"
DAY = 86400
T0 = 1_800_000_000.0


class ScheduleFixture(ReclaimFixture):
    declared = "target"
    # 两个 worktree 合计约 6 MB；上限 4 MB 时瘦身后恰为 3 MB，不需要删除。
    limit = "0.004"

    def setUp(self):
        super().setUp()
        # 用非零内容：新版 git checkout 会把连续零写成稀疏空洞，du 只计几 KB。
        (self.repo / "big.bin").write_bytes(b"x" * 1_500_000)
        self.git("add", ".")
        self.git("commit", "-qm", "big tracked file")
        self.services = []
        # 命令任务默认确认后立即过期；默认不过期，需要过期的用例用 age() 回溯确认时间。
        retain = patch.dict(os.environ, {"RD_RETAIN_COMMAND_HOURS": "10"})
        retain.start()
        self.addCleanup(retain.stop)
        # 选机探测替换为空闲读数，避免用例随本机真实负载（swap、load）被判离线而失败。
        idle = dict(reachable=True, memfree="80", disk="100", load1="0.1", ncpu="8", heavy="0")
        patcher = patch.object(rd, "probe", return_value=idle)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, limit=None, enabled=True):
        text = self.config_path.read_text(encoding="utf-8")
        text = text.replace("sync = direct\n", f"sync = direct\nmax_worktree_gb = {limit or self.limit}\n", 1)
        text = text.replace("[features]\n", "[features]\nscheduled_reclaim = " + ("true" if enabled else "false") + "\n", 1)
        self.config_path.write_text(text, encoding="utf-8")
        self.cfg = rd.Config()

    def age(self, rec, hours=20):
        """把回执确认时间回溯 hours 小时，使命令任务超过保留期。"""
        old = time.time() - hours * 3600
        for path in rd.result_path(rec, 1).parent.glob("r*.ack.json"):
            os.utime(path, (old, old))

    def service(self):
        service = Service(self.cfg, self.root / f"serve{len(self.services)}.sock", poll_interval=3600)
        self.services.append(service)
        return service

    def event_rows(self, kind):
        return [row for row in events.read_after(rd.STATE, 0) if row["type"] == kind]

    def exists(self, rec):
        return self.paths(rec)[0].exists()


class FirstWeekPlanOnlyTest(ScheduleFixture):
    def test_first_week_only_plans_and_start_survives_restart(self):
        first = self.dispatch(BUILD)
        self.configure(limit="0.0025")
        plans = self.service().reclaim.run_once(now=T0)
        plan = plans["self"]
        self.assertEqual(plan["mode"], "plan")
        self.assertEqual([(a["job"], a["action"]) for a in plan["actions"]], [(first["job"], "slim")])
        self.assertTrue((self.paths(first)[0] / "target" / "a.bin").exists())
        planned = self.event_rows("reclaim.planned")
        self.assertEqual((planned[0]["data"]["mode"], planned[0]["data"]["actions"][0]["action"]), ("plan", "slim"))
        self.assertEqual(self.event_rows("reclaim.result"), [])
        # 重启 serve 后第 3 天仍在首周内，起点不重新计时。
        again = self.service().reclaim.run_once(now=T0 + 3 * DAY)["self"]
        self.assertEqual(again["mode"], "plan")
        self.assertTrue((self.paths(first)[0] / "target" / "a.bin").exists())
        # 第 8 天超过起点 + 7 天：执行瘦身。
        done = self.service().reclaim.run_once(now=T0 + 8 * DAY)["self"]
        self.assertEqual(done["mode"], "execute")
        self.assertFalse((self.paths(first)[0] / "target").exists())
        self.assertTrue((self.paths(first)[0] / "big.bin").exists())
        self.assertEqual(len(self.event_rows("reclaim.result")), 1)

    def test_plan_is_visible_on_board_and_reclaim_endpoint(self):
        self.dispatch(BUILD)
        self.configure(limit="0.0025")
        service = self.service()
        service.reclaim.run_once(now=time.time())
        shown = board.build(rd.STATE, service.query(), service.records(), {}, user_id="employee_a")["reclaim_plan"]
        self.assertEqual(shown["mode"], "plan")
        self.assertEqual(shown["hosts"]["self"]["actions"][0]["action"], "slim")
        self.assertIn("定时回收", board.render(dict(scope="all", initiator_session=None, rows=[], reclaim_plan=shown,
                                                    summary=dict(dispatched=0, running=0, pending=0, blocked=0))))
        status, body = service.read("/v1/reclaim")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["reclaim"]["hosts"]["self"]["actions"], shown["hosts"]["self"]["actions"])

    def test_disabled_feature_does_nothing(self):
        self.dispatch(BUILD)
        self.configure(enabled=False)
        service = self.service()
        self.assertIsNone(service.reclaim.run_once(now=T0 + 30 * DAY))
        self.assertEqual(self.event_rows("reclaim.planned"), [])
        self.assertIsNone(service.read("/v1/reclaim")[1]["reclaim"])
        self.assertFalse(service.read("/v1/reclaim")[1]["enabled"])

    def test_offline_host_is_skipped_without_error(self):
        self.dispatch(BUILD)
        self.configure()
        with patch.object(rd, "probe", return_value={"reachable": False}):
            self.assertEqual(self.service().reclaim.run_once(now=T0 + 30 * DAY), {})
        self.assertEqual(self.event_rows("reclaim.planned"), [])

    def test_under_limit_produces_no_plan(self):
        self.dispatch(BUILD)
        self.configure(limit="5")
        self.assertEqual(self.service().reclaim.run_once(now=T0 + 30 * DAY), {})


class ExecuteTest(ScheduleFixture):
    def execute_at(self, service):
        service.reclaim.run_once(now=T0)
        return service.reclaim.run_once(now=T0 + 8 * DAY)

    def test_over_limit_deletes_expired_first_then_slims_the_rest(self):
        old = self.dispatch(BUILD)
        new = self.dispatch(BUILD)
        self.age(old)
        # 过期的 old 先删；剩下的 new 约 3 MB 仍超过 2.4 MB 上限，再瘦身。
        self.configure(limit="0.0023")
        plans = self.execute_at(self.service())
        results = plans["self"]["results"]
        self.assertEqual([(r["action"], r["job"]) for r in results],
                         [("delete", old["job"]), ("slim", new["job"])])
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertFalse(self.exists(old))
        self.assertTrue(self.exists(new))
        self.assertFalse((self.paths(new)[0] / "target").exists())
        result_event = self.event_rows("reclaim.result")[-1]["data"]
        self.assertEqual((result_event["slimmed_count"], result_event["deleted_count"], result_event["failed_count"]), (1, 1, 0))
        json.dumps(result_event)

    def test_expired_jobs_are_reclaimed_even_when_under_the_limit(self):
        old = self.dispatch(BUILD)
        fresh = self.dispatch(BUILD)
        self.age(old)
        self.configure(limit="5")
        plans = self.execute_at(self.service())
        self.assertEqual([(r["action"], r["job"], r["ok"]) for r in plans["self"]["results"]],
                         [("delete", old["job"], True)])
        self.assertFalse(self.exists(old))
        self.assertTrue(self.exists(fresh))
        self.assertTrue((self.paths(fresh)[0] / "target").exists())

    def seed_old_user(self):
        """老用户：早就启用过（enabled_at 很久以前），没有 sweep_enabled_at。"""
        from remote_dispatch import rd_reclaim_plan as store
        store.write(rd.STATE, dict(user_id="employee_a", enabled_at=T0 - 90 * DAY, hosts={}))

    def test_sweep_of_expired_jobs_has_its_own_plan_only_week_for_existing_users(self):
        old = self.dispatch(BUILD)
        self.age(old)
        self.seed_old_user()
        self.configure(limit="5")
        service = self.service()
        plan = service.reclaim.run_once(now=T0)["self"]
        self.assertEqual((plan["mode"], [(a["action"], a["job"]) for a in plan["actions"]]),
                         ("plan", [("delete", old["job"])]))
        self.assertTrue(self.exists(old))
        self.assertEqual(service.reclaim.load()["sweep_enabled_at"], T0)
        again = service.reclaim.run_once(now=T0 + 3 * DAY)["self"]
        self.assertEqual(again["mode"], "plan")
        self.assertTrue(self.exists(old))
        done = service.reclaim.run_once(now=T0 + 8 * DAY)["self"]
        self.assertEqual(done["mode"], "execute")
        self.assertFalse(self.exists(old))

    def test_existing_users_still_get_over_limit_enforcement_during_the_sweep_week(self):
        rec = self.dispatch(BUILD)
        self.seed_old_user()
        self.configure(limit="0.0025")
        plan = self.service().reclaim.run_once(now=T0)["self"]
        self.assertEqual([(r["action"], r["ok"]) for r in plan["results"]], [("slim", True)])
        self.assertFalse((self.paths(rec)[0] / "target").exists())

    def test_expired_reclaim_under_limit_is_plan_only_during_first_week(self):
        old = self.dispatch(BUILD)
        self.age(old)
        self.configure(limit="5")
        plan = self.service().reclaim.run_once(now=T0)["self"]
        self.assertEqual((plan["mode"], [(a["action"], a["job"]) for a in plan["actions"]]),
                         ("plan", [("delete", old["job"])]))
        self.assertTrue(self.exists(old))

    def test_slim_is_enough_when_it_brings_usage_under_limit(self):
        first = self.dispatch(BUILD)
        second = self.dispatch(BUILD)
        self.configure()
        plans = self.execute_at(self.service())
        self.assertEqual({r["action"] for r in plans["self"]["results"]}, {"slim"})
        self.assertTrue(self.exists(first) and self.exists(second))

    def test_blocked_job_is_kept_even_when_oldest(self):
        blocked = self.dispatch(STRAY)
        time.sleep(1.1)
        clean = self.dispatch(BUILD)
        self.age(blocked)
        self.age(clean)
        self.configure(limit="0.0016")
        plans = self.execute_at(self.service())
        plan = plans["self"]
        self.assertIn((blocked["job"], rd.BLOCK_DIRTY), [(b["job"], b["code"]) for b in plan["blocked"]])
        self.assertNotIn(blocked["job"], [r["job"] for r in plan["results"] if r["action"] == "delete"])
        self.assertTrue(self.exists(blocked))
        self.assertEqual((self.paths(blocked)[0] / "notes.txt").read_text(), "note\n")
        self.assertFalse(self.exists(clean))

    def test_other_users_jobs_are_not_touched(self):
        mine = self.dispatch(BUILD)
        theirs = self.dispatch(BUILD)
        rd.save_job(dict(rd.load_job(theirs["job"]), user_id="employee_b"))
        self.age(mine)
        self.age(theirs)
        self.configure(limit="0.0016")
        self.execute_at(self.service())
        self.assertTrue(self.exists(theirs))
        self.assertTrue((self.paths(theirs)[0] / "target").exists())
        self.assertFalse(self.exists(mine))


class ResumedAfterPlanTest(ScheduleFixture):
    def test_job_resumed_between_plan_and_execute_is_not_slimmed(self):
        rec = self.dispatch(BUILD)
        self.configure(limit="0.0012")
        service = self.service()
        service.reclaim.run_once(now=T0)
        real = rd.job_states
        calls = []

        def states(cfg, recs):
            calls.append(1)
            # 第一次是计划阶段的快照；之后任务已被 resume，处于运行中。
            snapshot = real(cfg, recs)
            return snapshot if len(calls) == 1 else {r["job"]: dict(state="running") for r in recs}

        with patch.object(rd, "job_states", states):
            plan = service.reclaim.run_once(now=T0 + 8 * DAY)["self"]
        self.assertGreater(len(calls), 1)
        slim = [r for r in plan["results"] if r["action"] == "slim"]
        self.assertEqual([(r["ok"], r["reason"]) for r in slim], [(False, rd.BLOCK_RUNNING)])
        self.assertTrue((self.paths(rec)[0] / "target" / "a.bin").exists())


class LoopResilienceTest(ScheduleFixture):
    class FakeStop:
        def __init__(self, rounds):
            self.rounds = rounds

        def is_set(self):
            return self.rounds <= 0

        def wait(self, _):
            self.rounds -= 1

    def test_unexpected_exception_does_not_kill_loop_and_is_logged(self):
        self.configure()
        service = self.service()
        service.stop = self.FakeStop(2)
        runs = []

        def run_once(now=None):
            runs.append(1)
            if len(runs) == 1:
                raise KeyError("boom")

        service.reclaim.run_once = run_once
        with contextlib.redirect_stdout(io.StringIO()) as out:
            service.reclaim.loop()
        self.assertEqual(len(runs), 2)
        self.assertIn("KeyError", out.getvalue())


class StopTest(ScheduleFixture):
    def test_run_once_stops_between_hosts_when_service_is_stopping(self):
        self.dispatch(BUILD)
        self.configure(limit="0.0025")
        service = self.service()
        service.stop.set()
        with patch.object(rd, "probe") as probe:
            self.assertEqual(service.reclaim.run_once(now=T0 + 30 * DAY), {})
        probe.assert_not_called()

    def test_close_does_not_wait_forever_for_a_stuck_reclaim_thread(self):
        service = self.service()
        release = threading.Event()
        self.addCleanup(release.set)
        service.reclaim_poller = threading.Thread(target=release.wait, daemon=True)
        service.reclaim_poller.start()
        started = time.monotonic()
        with patch.object(rd_serve, "RECLAIM_JOIN_SECONDS", 0.2):
            service.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNone(service.reclaim_poller)


class SharedVerdictTest(ScheduleFixture):
    def test_scheduled_reclaim_uses_the_same_verdict_as_clean_expired(self):
        rec = self.dispatch(BUILD)
        self.configure(limit="0.0001")
        calls = []
        real = rd.expiry_verdict

        def spy(*args, **kwargs):
            calls.append(args[1]["job"])
            return real(*args, **kwargs)

        service = self.service()
        service.reclaim.run_once(now=T0)
        with patch.object(rd, "expiry_verdict", spy):
            service.reclaim.run_once(now=T0 + 8 * DAY)
        self.assertIn(rec["job"], calls)

    def test_low_disk_tightens_scheduled_retention_like_clean_expired(self):
        rec = self.dispatch(BUILD)
        self.configure(limit="0.0001")
        # 默认保留期内：磁盘不紧张时不删；紧张时按收紧后的保留期，与 clean --expired 一致。
        with patch.object(rd, "disk_low", return_value=True), patch.object(
                rd, "RETENTION_LOW_DISK_HOURS", {"accepted": 0, "unmerged": 0, "command": 0}):
            expected = self.rows()[rec["job"]]
            plan = self.service().reclaim.run_once(now=T0)["self"]
        self.assertTrue(expected["reclaimable"], expected)
        self.assertIn(rec["job"], [r["job"] for r in plan["actions"] if r["action"] == "delete"])

    def rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rd.cmd_clean(self.clean_args(), self.cfg)
        return {row["job"]: row for row in json.loads(out.getvalue())}


if __name__ == "__main__":
    unittest.main()
