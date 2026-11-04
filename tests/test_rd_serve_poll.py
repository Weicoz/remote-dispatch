"""serve 轮询：回执与进度错误边界分离、远端调用有界、按目标机并发、错误日志聚合限频。"""
import contextlib
import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_events as events
from remote_dispatch import rd_serve_poll as poll
from remote_dispatch.rd_serve import Service


class PollErrorsTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.errors = poll.PollErrors(interval=600, clock=lambda: self.now[0])

    def lines(self, action):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action()
        return err.getvalue().splitlines()

    def test_first_error_is_logged_with_job_stage_class_and_reason(self):
        lines = self.lines(lambda: self.errors.failed("job=j1", "progress", ValueError("连接中断")))
        self.assertEqual(lines, ["rdispatch: poll_error: job=j1 stage=progress error=ValueError reason=连接中断"])

    def test_repeats_are_counted_and_summarised_once_per_interval(self):
        error = ValueError("x")
        first = self.lines(lambda: self.errors.failed("job=j1", "result", error))
        self.assertEqual(len(first), 1)
        for _ in range(50):
            self.now[0] += 5
            self.assertEqual(self.lines(lambda: self.errors.failed("job=j1", "result", error)), [])
        self.now[0] = 700
        summary = self.lines(lambda: self.errors.failed("job=j1", "result", error))
        self.assertEqual(len(summary), 1)
        self.assertIn("poll_error_repeated", summary[0])
        self.assertIn("count=51", summary[0])
        # 不同异常类型是新的键，立即记录。
        other = self.lines(lambda: self.errors.failed("job=j1", "result", OSError("io")))
        self.assertEqual(len(other), 1)

    def test_recovery_is_logged_once_and_resets_the_key(self):
        error = ValueError("x")
        self.lines(lambda: self.errors.failed("job=j1", "state", error))
        self.lines(lambda: self.errors.failed("job=j1", "state", error))
        recovered = self.lines(lambda: self.errors.ok("job=j1", "state"))
        self.assertEqual(len(recovered), 1)
        self.assertIn("poll_recovered", recovered[0])
        self.assertIn("after=2", recovered[0])
        self.assertEqual(self.lines(lambda: self.errors.ok("job=j1", "state")), [])
        again = self.lines(lambda: self.errors.failed("job=j1", "state", error))
        self.assertEqual(len(again), 1)

    def test_deleted_job_entries_are_forgotten_and_commit_stage_recovers(self):
        self.lines(lambda: self.errors.failed("job=gone", "commit", ValueError("x")))
        self.lines(lambda: self.errors.failed("host=old", "unit", ValueError("x")))
        self.errors.forget_missing(set(), set())
        self.assertEqual(self.errors.active, {})
        self.lines(lambda: self.errors.failed("job=j1", "commit", ValueError("x")))
        self.assertIn("poll_recovered", self.lines(lambda: self.errors.ok("job=j1", "commit"))[0])

    def test_reason_never_includes_command_line_or_secrets(self):
        timeout = subprocess.TimeoutExpired(["ssh", "-i", "SECRET-KEY", "host"], 30)
        line = self.lines(lambda: self.errors.failed("job=j1", "state", timeout))[0]
        self.assertIn("timeout after 30s", line)
        self.assertNotIn("SECRET", line)
        errors = poll.PollErrors(lambda text: text.replace("TOKEN123", "[redacted]"))
        line = self.lines(lambda: errors.failed("job=j1", "state", ValueError("bad TOKEN123 here\nsecond line")))[0]
        self.assertNotIn("TOKEN123", line)
        self.assertEqual(len(line.splitlines()), 1)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-poll-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.config.write_text("[host:fast]\nssh=local\nworkdir=%s/fast\nsync=direct\n"
                               "[host:slow]\nssh=local\nworkdir=%s/slow\nsync=direct\n" % (self.root, self.root),
                               encoding="utf-8")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(cli, "STATE", str(self.state)))
        stack.enter_context(patch.object(cli, "CONFIG", str(self.config)))
        stack.enter_context(patch.dict(os.environ, {"RD_USER_ID": "fixture"}))
        stack.enter_context(patch.object(cli, "probe", lambda host: dict(host=host["name"], reachable=True)))
        stack.enter_context(patch.object(cli, "assess", lambda host, info: []))
        self.states = {}

    def job(self, name, host="fast", rounds=1, **extra):
        rec = dict(job=name, user_id="fixture", host=host, rounds=rounds, project="demo", origin="",
                   initiator_session="owner", agent=None, sync="direct", base="base", branch="b",
                   created="2026-10-25 00:00:00", **extra)
        cli.save_job(rec)
        self.states[name] = dict(state="done", round=str(rounds), exit="0")
        return rec

    def service(self):
        service = Service(cli.Config(), self.root / "serve.sock", poll_interval=3600)
        self.addCleanup(service.collector.close)
        return service

    def cycle(self, service):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            service.collector.cycle(events.EventLog(self.state))
        return err.getvalue()

    def job_states(self, cfg, recs):
        return {rec["job"]: dict(self.states[rec["job"]]) for rec in recs}

    def receipt_exists(self, rec):
        return cli.result_path(rec, 1).exists()

    def test_progress_failure_does_not_block_receipt_and_is_logged_by_stage(self):
        rec = self.job("job-a", handoff_version=1)

        def report(host, rec_, number, kind):
            if kind == "progress":
                raise ValueError("progress 读取失败")
            return None
        service = self.service()
        with patch.object(cli, "job_states", self.job_states), patch.object(cli, "remote_report", report):
            text = self.cycle(service)
        self.assertTrue(self.receipt_exists(rec))
        self.assertEqual(service.snapshots["job-a"]["state"], "done")
        self.assertIn("poll_error: job=job-a stage=progress error=ValueError", text)
        self.assertNotIn("stage=result", text)

    def test_history_failure_does_not_block_receipt_either(self):
        rec = self.job("job-h", handoff_version=1, rounds=2)

        def report(host, rec_, number, kind):
            if kind == "progress" and number == 1:
                raise ValueError("历史读取失败")
            return None
        service = self.service()
        with patch.object(cli, "job_states", self.job_states), patch.object(cli, "remote_report", report):
            text = self.cycle(service)
        self.assertTrue(cli.result_path(rec, 2).exists())
        self.assertIn("stage=history", text)

    def test_result_failure_keeps_old_snapshot_and_retries_next_cycle(self):
        rec = self.job("job-r")
        service = self.service()
        calls = []

        def broken(config, rec_, snapshot):
            calls.append(1)
            raise ValueError("回执读取失败")
        with patch.object(cli, "job_states", self.job_states), patch.object(cli, "collect_result", broken):
            first = self.cycle(service)
            second = self.cycle(service)
        self.assertIn("stage=result error=ValueError", first)
        self.assertNotIn("poll_error", second)  # 同键重复不再逐行记录
        self.assertEqual(len(calls), 2)
        self.assertNotIn("job-r", service.snapshots)
        with patch.object(cli, "job_states", self.job_states):
            text = self.cycle(service)
        self.assertIn("poll_recovered: job=job-r stage=result", text)
        self.assertTrue(self.receipt_exists(rec))

    def test_session_lookup_timeout_is_a_session_stage_error_not_a_hang(self):
        self.job("job-s")
        service = self.service()
        with patch.object(cli, "job_states", self.job_states), \
                patch.object(cli, "collect_result", side_effect=cli.SessionLookupError("会话查找超时")):
            text = self.cycle(service)
        self.assertIn("stage=session error=SessionLookupError", text)

    def test_slow_host_does_not_delay_other_hosts_and_keeps_serial_jobs_within_host(self):
        fast = self.job("fast-job", "fast")
        self.job("slow-job", "slow")
        release = threading.Event()
        order = []

        def states(cfg, recs):
            host = recs[0]["host"]
            order.append(host)
            if host == "slow":
                release.wait(10)
            return self.job_states(cfg, recs)
        service = self.service()
        worker = threading.Thread(target=self.cycle, args=(service,))
        with patch.object(cli, "job_states", states):
            worker.start()
            deadline = time.monotonic() + 5
            while "fast-job" not in service.snapshots and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("fast-job", service.snapshots)
            self.assertNotIn("slow-job", service.snapshots)
            self.assertTrue(self.receipt_exists(fast))
            release.set()
            worker.join(10)
        self.assertIn("slow-job", service.snapshots)

    def test_concurrency_is_bounded_by_setting(self):
        self.config.write_text(self.config.read_text() + "[serve]\npoll_concurrency=1\n", encoding="utf-8")
        self.job("fast-job", "fast")
        self.job("slow-job", "slow")
        active, peak, lock = [0], [0], threading.Lock()

        def states(cfg, recs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1
            return self.job_states(cfg, recs)
        service = self.service()
        self.assertEqual(service.collector.concurrency, 1)
        with patch.object(cli, "job_states", states):
            self.cycle(service)
        self.assertEqual(peak[0], 1)

    def test_settled_job_is_probed_less_often_but_still_visible_until_acknowledged(self):
        rec = self.job("job-settled")
        service = self.service()
        calls = []

        def states(cfg, recs):
            calls.append([r["job"] for r in recs])
            return self.job_states(cfg, recs)
        with patch.object(cli, "job_states", states):
            self.cycle(service)
            self.assertEqual(calls, [["job-settled"]])
            self.assertTrue(service.collector.is_settled(rec))
            self.cycle(service)
            self.cycle(service)
        self.assertEqual(calls, [["job-settled"]])
        self.assertEqual(service.snapshots["job-settled"]["state"], "done")
        # 降频周期过去后再探测一次。
        service.collector.settled["job-settled"] = (service.generation(rec), service.collector.clock() - 10 ** 6)
        with patch.object(cli, "job_states", states):
            self.cycle(service)
        self.assertEqual(len(calls), 2)

    def test_new_round_leaves_settled_state_immediately(self):
        rec = self.job("job-next")
        service = self.service()
        with patch.object(cli, "job_states", self.job_states):
            self.cycle(service)
        self.assertTrue(service.collector.is_settled(rec))
        resumed = dict(rec, rounds=2)
        cli.save_job(resumed)
        self.states["job-next"] = dict(state="running", round="2")
        self.assertFalse(service.collector.is_settled(resumed))
        with patch.object(cli, "job_states", self.job_states):
            self.cycle(service)
        self.assertEqual(service.snapshots["job-next"]["state"], "running")


class HostRowTests(CollectorTests):
    def test_unit_crash_keeps_already_published_host_rows(self):
        self.job("job-a", "fast")
        service = self.service()
        self.cycle(service)
        before = {row["name"] for row in service.hosts}
        self.assertEqual(before, {"fast", "slow"})
        original = service.collector.unit

        def crashing(config, log, name, enabled_hosts, enabled, group):
            if name == "slow":
                raise KeyError("缺陷")
            return original(config, log, name, enabled_hosts, enabled, group)
        with patch.object(service.collector, "unit", crashing):
            text = self.cycle(service)
        self.assertEqual({row["name"] for row in service.hosts}, before)
        self.assertIn("host=slow stage=unit error=KeyError", text)


class RemoteTimeoutTests(unittest.TestCase):
    host = dict(name="h", platform="posix", ssh="local", workdir="/w", path="", ssh_opts="")

    def test_find_session_ssh_is_bounded_and_timeout_is_an_error(self):
        seen = {}

        def fake_ssh(host, script, **kwargs):
            seen.update(kwargs)
            return None
        agent = dict(session_regex="id=(.+)")
        with patch.object(cli, "ssh", fake_ssh), self.assertRaises(cli.SessionLookupError):
            cli.find_session(self.host, dict(job="j", rounds=1), agent)
        self.assertEqual(seen["timeout"], cli.REMOTE_READ_TIMEOUT)

    def test_remote_report_calls_are_bounded(self):
        seen = []

        def fake_ssh(host, script, **kwargs):
            seen.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess([], 0, "", "")

        def fake_native(host, operation, data=None, **kwargs):
            seen.append(kwargs.get("timeout"))
            return None
        rec = dict(job="j", rounds=1)
        with patch.object(cli, "ssh", fake_ssh), patch.object(cli.native, "request", fake_native):
            cli.remote_report(self.host, rec, 1, "result")
            cli.remote_report(self.host, rec, 1, "progress")
            cli.remote_report(dict(self.host, platform="windows"), rec, 1, "result")
        self.assertEqual(seen, [cli.REMOTE_READ_TIMEOUT] * 3)

    def test_remote_state_is_bounded(self):
        seen = []

        def fake_ssh(host, script, **kwargs):
            seen.append(kwargs.get("timeout"))
            return None
        with patch.object(cli, "ssh", fake_ssh):
            states = cli.remote_state(dict(self.host, python="python3"), ["j"])
        self.assertEqual(states, {"j": {"state": "unreachable"}})
        self.assertTrue(seen and all(timeout for timeout in seen))


if __name__ == "__main__":
    unittest.main()
