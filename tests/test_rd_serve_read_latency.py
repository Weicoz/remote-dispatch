"""读接口延迟回归：真实 Unix HTTP，目标机仅在 RPC 边界替换。"""
import threading
import time
import unittest
import json
import sys
from datetime import datetime
from unittest.mock import patch

from remote_dispatch import cli
from tests import test_rd_serve as base


class ReadLatencyTests(unittest.TestCase):
    setUp = base.ServeTests.setUp
    start = base.ServeTests.start
    request = base.ServeTests.request
    eventually = base.ServeTests.eventually
    open_events = base.ServeTests.open_events
    next_event = base.ServeTests.next_event
    assert_structure = base.ServeTests.assert_structure

    def test_first_hosts_response_lists_all_enabled_targets_before_probe_finishes(self):
        self.config.write_text("[host:target]\nssh=local\n"
                               "[host:remote]\nplatform=windows\nssh=remote.invalid\n"
                               "[host:disabled]\nssh=disabled.invalid\nenabled=false\n")
        entered, release = threading.Event(), threading.Event()

        def delayed_probe(host):
            entered.set()
            if not release.wait(5):
                raise AssertionError("probe was not released")
            raise OSError("synthetic offline")

        with patch.object(cli, "probe", side_effect=delayed_probe):
            service = self.start(poll_interval=3600)
            try:
                self.assertTrue(entered.wait(2))
                status, body = self.request("/v1/hosts")
                self.assertEqual(status, 200)
                example = json.loads((base.CONTRACTS / "hosts-cold-start.json").read_text())
                row = example["response"]["body"]["hosts"][0]
                remote = json.loads(json.dumps(row))
                remote.update(name="remote", local=False)
                remote["metrics"]["cpu"].update(unit="cpu_pct", warning=70.0, pressure=85.0)
                self.assertEqual(body["hosts"], [row, remote])
                release.set()
                rows = self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"],
                                       lambda rows: all(row["probed"] for row in rows))
                self.assertEqual(rows, [dict(row, probed=True),
                                        dict(remote, probed=True)])
            finally:
                release.set()
                service.close()

    def test_host_probe_results_are_visible_while_job_collection_is_delayed(self):
        self.config.write_text("[host:target]\nplatform=windows\nssh=target.invalid\n")
        for number in range(195):
            cli.save_job(dict(job=f"job-{number:03}", user_id="fixture", host="target", rounds=1))
        entered, release = threading.Event(), threading.Event()
        calls = []

        def target(host, action, data=None, **kwargs):
            calls.append(action)
            if action == "probe":
                return dict(load1="1", ncpu="8", memfree="80", disk="100", running="0", heavy="0")
            if action == "state":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("job collection was not released")
                return {data["jobs"][0]: dict(state="unreachable")}
            raise AssertionError(f"unexpected RPC: {action}")

        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            try:
                self.assertTrue(entered.wait(2))
                status, body = self.request("/v1/hosts")
                self.assertEqual(status, 200)
                self.assertEqual(len(body["hosts"]), 1)
                row = body["hosts"][0]
                self.assertTrue(row["probed"])
                self.assertTrue(row["reachable"])
                self.assertEqual(row["load1"], "1")
                self.assertEqual(calls, ["probe", "state"])
            finally:
                service.stop.set()
                release.set()
                service.close()

    def test_two_hundred_jobs_and_thirty_projects_return_while_target_is_slow(self):
        self.config.write_text("[host:slow]\nplatform=windows\nssh=slow.invalid\n")
        for number in range(200):
            cli.save_job(dict(job=f"job-{number:03}", user_id="fixture", host="slow",
                              rounds=1, project=f"project-{number % 30:02}", origin="",
                              dispatch_state="started", created="2026-10-25T00:00:00Z"))
        entered = threading.Event()
        calls = []

        def slow_target(*args, **kwargs):
            calls.append(args[1])
            if args[1] == "probe":
                return dict(os="Windows")
            entered.set()
            time.sleep(5)
            raise ValueError("synthetic unreachable target")

        with patch.object(cli.native, "request", side_effect=slow_target):
            service = self.start(poll_interval=3600)
            self.assertTrue(entered.wait(2), "后台未开始目标机查询")
            for path, key, count in (("/v1/jobs", "jobs", 200), ("/v1/projects", "projects", 30)):
                with self.subTest(path=path):
                    started = time.monotonic()
                    status, body = self.request(path)
                    elapsed = time.monotonic() - started
                    self.assertEqual(status, 200)
                    self.assertEqual(len(body[key]), count)
                    self.assertEqual(calls, ["probe", "state"], "后台请求阻塞期间，读请求不得联系目标机")
                    self.assertLess(elapsed, 0.5, f"{path}: {elapsed:.3f}s")
            for path in ("/v1/jobs/job-000", "/v1/jobs?state=active", "/v1/jobs?state=history",
                         "/v1/harness-stats", "/v1/sessions", "/v1/jobs/job-000/session"):
                self.assertEqual(self.request(path)[0], 200, path)
            self.assertEqual(calls, ["probe", "state"])
            service.close()
        self.assertFalse(self.path.exists())

    def test_background_refresh_is_visible_emits_events_and_survives_restart_without_old_rounds(self):
        self.config.write_text("[host:target]\nplatform=windows\nssh=target.invalid\n")
        rec = dict(job="refresh", user_id="fixture", host="target", rounds=1,
                   project="demo", dispatch_state="started")
        cli.save_job(rec)
        state = "running"
        entered, release = threading.Event(), threading.Event()
        calls = []
        def target(host, action, data=None, **kwargs):
            calls.append(action)
            if action == "state":
                entered.set()
                release.wait(5)
                return {"refresh": dict(state=state, round="1", exit="0")}
            if action == "probe":
                return dict(reachable=False)
            raise ValueError("unexpected RPC")
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            try:
                release.set()
                first = self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"][0],
                                        lambda row: row["execution_state"] == "running")
                self.assertIsNotNone(first["snapshot_at"])
                self.assertIsNotNone(datetime.fromisoformat(first["snapshot_at"].replace("Z", "+00:00")).tzinfo)
                service.close()

                release.clear()
                entered.clear()
                service = self.start(poll_interval=3600)
                self.assertTrue(entered.wait(2))
                before = list(calls)
                self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0], first)
                self.assertEqual(calls, before)
                state = "done"
                release.set()
                finished = self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"][0],
                                           lambda row: row["execution_state"] == "done")
                self.assertGreaterEqual(finished["snapshot_at"], first["snapshot_at"])
                stream = self.open_events()
                states = []
                while "done" not in states:
                    _, kind, event = self.next_event(stream)
                    if kind == "job.state_changed":
                        states.append(event["data"]["execution_state"])
                stream.close()
                self.assertIn("running", states)
                cli.save_job(dict(rec, rounds=2))
                row = self.request("/v1/jobs")[1]["jobs"][0]
                self.assertEqual((row["round"], row["execution_state"], row["snapshot_at"]), (2, "starting", None))
            finally:
                release.set()
                service.close()

    def test_same_round_migration_does_not_return_the_old_target_progress(self):
        self.config.write_text("[host:target]\nplatform=windows\nssh=target.invalid\n"
                               "[host:second]\nplatform=windows\nssh=second.invalid\n")
        rec = dict(job="moving", user_id="fixture", host="target", rounds=1, handoff_version=1)
        cli.save_job(rec)
        def target(host, action, data=None, **kwargs):
            if action == "state":
                return {"moving": dict(state="running", round="1")}
            if action == "probe":
                return dict(reachable=False)
            if action == "report-read":
                return dict(schema=1, job="moving", round=1, kind="progress", summary="old target")
            raise ValueError("unexpected RPC")
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: self.request("/v1/jobs/moving")[1],
                            lambda row: row["rounds"][0]["progress"] is not None)
            cli.save_job(dict(rec, host="second"))
            detail = self.request("/v1/jobs/moving")[1]
            self.assertEqual(detail["job"]["execution_state"], "starting")
            self.assertIsNone(detail["rounds"][0]["progress"])
            self.assertIsNone(detail["job"]["snapshot_at"])
            service.close()

    def test_bad_persisted_snapshots_do_not_prevent_cold_start_from_local_records(self):
        cli.save_job(dict(job="bad-cache", user_id="fixture", host="removed", rounds=1))
        directory = self.state / "serve-snapshots"
        directory.mkdir()
        (directory / "bad-cache.json").write_text("{incomplete")
        (directory / "bad-type.json").write_text("[]")
        (directory / "bad-round.json").write_text(json.dumps(dict(user_id="fixture",
            generation=[1, "removed", None], snapshot=dict(state="running", round="bad"),
            progress=dict(summary="must not load"))))
        (directory / "missing-generation.json").write_text(json.dumps(dict(user_id="fixture", snapshot={})))
        (directory / "linked.json").symlink_to(directory / "bad-cache.json")
        service = self.start(poll_interval=3600)
        row = self.request("/v1/jobs")[1]["jobs"][0]
        self.assertEqual((row["execution_state"], row["snapshot_at"]), ("starting", None))
        service.close()

    def test_old_target_round_is_not_published_as_the_resumed_round(self):
        self.config.write_text("[host:target]\nplatform=windows\nssh=target.invalid\n")
        cli.save_job(dict(job="resumed", user_id="fixture", host="target", rounds=2))
        def target(host, action, data=None, **kwargs):
            if action == "state":
                return {"resumed": dict(state="done", round="1", exit="0")}
            if action == "probe":
                return dict(reachable=False)
            raise ValueError("unexpected RPC")
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"], bool)
            row = self.request("/v1/jobs")[1]["jobs"][0]
            self.assertEqual((row["round"], row["execution_state"], row["snapshot_at"]), (2, "starting", None))
            self.assertEqual(self.request("/v1/jobs/resumed/rounds/2/receipt")[0], 404)
            service.close()

    def test_cold_start_returns_saved_receipt_state_and_unknown_snapshot_time_without_waiting(self):
        self.config.write_text("[host:slow]\nplatform=windows\nssh=slow.invalid\n")
        rec = dict(job="finished", user_id="fixture", host="slow", rounds=1, project="demo")
        cli.save_job(rec)
        cli.messages.atomic_json(cli.result_path(rec, 1), dict(job="finished", round=1,
            receipt_id="saved", execution_state="done", executor_exit=0,
            captured_at="2026-10-25T01:00:00Z", report=dict(outcome="completed")))
        cli.save_job(dict(rec, job="not-observed", dispatch_state="started"))
        entered, release = threading.Event(), threading.Event()
        def target(*args, **kwargs):
            entered.set()
            release.wait(5)
            raise ValueError("synthetic offline")
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            try:
                self.assertTrue(entered.wait(2))
                body = self.request("/v1/jobs")[1]
                rows = {row["job"]: row for row in body["jobs"]}
                self.assertEqual(rows["finished"]["execution_state"], "done")
                self.assertEqual(rows["finished"]["snapshot_at"], "2026-10-25T01:00:00Z")
                self.assertEqual(rows["not-observed"]["execution_state"], "starting")
                self.assertIsNone(rows["not-observed"]["snapshot_at"])
                example = json.loads((base.CONTRACTS / "jobs-cold-start.json").read_text())
                self.assert_structure(body, example["response"]["body"])
            finally:
                release.set()
                service.close()

    def test_session_lists_and_projects_read_last_background_snapshot_without_running_herdr(self):
        from tests import test_rd_serve_sessions as sessions
        # Reuse the external Herdr executable fixture, not an internal adapter mock.
        fixture = sessions.SessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = fixture.start(poll_interval=3600)
        fixture.eventually(lambda: fixture.request("/v1/sessions")[1]["sessions"], bool)
        stream = fixture.open_events()
        fixture.next_event(stream)
        stream.close()
        script = fixture.bin / "herdr"
        script.write_text("#!" + sys.executable + "\n"
                          "from pathlib import Path\n"
                          f"Path({str(fixture.writes)!r}).write_text('unexpected request')\n"
                          "import time\ntime.sleep(5)\nraise SystemExit(1)\n")
        for target, key in (("/v1/sessions", "sessions"), ("/v1/projects", "projects")):
            started = time.monotonic()
            status, body = fixture.request(target)
            self.assertEqual(status, 200)
            self.assertEqual(len(body[key]), 1)
            self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(fixture.writes.exists(), "列表查询不得执行 Herdr")
        service.close()

    def test_log_reads_return_immediately_and_background_refresh_supplies_the_tail(self):
        self.config.write_text("[host:target]\nplatform=windows\nssh=target.invalid\n")
        cli.save_job(dict(job="logging", user_id="fixture", host="target", rounds=1))
        entered, release = threading.Event(), threading.Event()
        callers = []
        def target(host, action, data=None, **kwargs):
            if action == "state":
                return {"logging": dict(state="running", round="1")}
            if action == "probe":
                return dict(reachable=False)
            if action == "logs":
                callers.append(action)
                entered.set()
                release.wait(5)
                return "first\nsecond\nthird\n"
            raise ValueError("unexpected RPC")
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            try:
                route = "/v1/jobs/logging/rounds/1/logs?tail=2"
                started = time.monotonic()
                status, cold = self.request(route)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(status, 200)
                self.assertEqual(cold["text"], "")
                self.assertIsNone(cold["snapshot_at"])
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.request(route)[1], cold)
                self.assertEqual(callers, ["logs"])
                release.set()
                warm = self.eventually(lambda: self.request(route)[1], lambda row: row.get("snapshot_at") is not None)
                self.assertEqual(warm["text"], "second\nthird\n")
            finally:
                release.set()
                service.close()
