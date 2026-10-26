"""项目与统计通过临时 Unix HTTP/SSE 服务的外部行为。"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch.rd_projects import ProjectIndex
from tests import test_rd_serve as support


class ProjectsStatsTests(unittest.TestCase):
    setUp = support.ServeTests.setUp
    start = support.ServeTests.start
    request = support.ServeTests.request
    eventually = support.ServeTests.eventually
    snapshot = support.ServeTests.snapshot
    open_events = support.ServeTests.open_events
    next_event = support.ServeTests.next_event
    command = support.ServeTests.command

    def prepare(self):
        self.states = {}
        boundary = patch.object(cli, "job_states", side_effect=lambda cfg, recs: {
            rec["job"]: self.states.get(rec["job"], {"state": "running", "round": str(rec["rounds"])})
            for rec in recs})
        boundary.start()
        self.addCleanup(boundary.stop)

    def dispatch(self, job, state="running", outcome=None, received=False, review=None,
                 host="target", user="fixture", created="2026-10-25 00:00:00", project="demo"):
        rec = dict(job=job, user_id=user, project=project, origin="", host=host,
                   rounds=1, agent="codex", created=created)
        ProjectIndex(self.state, user).register(rec)
        self.states[job] = dict(state=state, round="1")
        if outcome:
            receipt = dict(schema=1, job=job, round=1, host=host, receipt_id=job,
                           captured_at=created, execution_state="done", executor_exit=0,
                           report=dict(outcome=outcome, updated_at=created))
            path = cli.result_path(rec, 1)
            cli.messages.atomic_json(path, receipt)
            if received:
                cli.messages.atomic_json(path.with_suffix(".ack.json"), dict(receipt_id=job, received_at=created))
            if review:
                cli.messages.atomic_json(path.with_suffix(".review.json"),
                                         dict(review_state=review, reviewed_at=created, reviewed_by="fixture"))
        return rec

    def test_projects_choose_each_priority_and_newest_tie_and_expose_summary_without_writes(self):
        self.prepare()
        cases = [("needs_reply", "done", "blocked", False),
                 ("interrupted", "dead", None, False),
                 ("failed_pending", "done", "failed", False),
                 ("pending_receipt", "done", "partial", False),
                 ("pending_review", "done", "completed", True),
                 ("running", "running", None, False),
                 ("idle", "done", "completed", True)]
        for index, (expected, state, outcome, received) in enumerate(cases):
            host = "host-" + str(index)
            for _, lower_state, lower_outcome, lower_received in cases[index:]:
                # Each host receives all priorities from its expected winner down.
                self.dispatch(f"job-{index}-{lower_state}-{lower_outcome}", lower_state, lower_outcome,
                              lower_received, review="accepted" if lower_received and lower_state == "done" and _ == "idle" else None,
                              host=host)
            self.dispatch("newest-" + str(index), state, outcome, received,
                          review="accepted" if expected == "idle" else None, host=host,
                          created="2026-10-25 01:00:00")
        self.dispatch("offline", "unreachable", host="offline")
        self.dispatch("lost", "missing", host="lost")
        self.dispatch("private", user="other", project="private")
        service = self.start(poll_interval=3600)
        rows = self.eventually(lambda: self.request("/v1/projects")[1].get("projects", []),
                               lambda rows: rows and rows[0].get("hosts", {}).get("offline", {}).get("state") == "interrupted")
        self.assertEqual([row["name"] for row in rows], ["demo"])
        row = rows[0]
        for index, (expected, *rest) in enumerate(cases):
            with self.subTest(expected=expected):
                self.assertEqual(row["hosts"]["host-" + str(index)]["state"], expected)
                self.assertEqual(row["hosts"]["host-" + str(index)]["job"], "newest-" + str(index))
        self.assertEqual(row["hosts"]["offline"]["state"], "interrupted")
        self.assertEqual(row["hosts"]["lost"]["state"], "interrupted")
        self.assertGreater(row["needs_attention_count"], 0)
        self.assertGreater(row["in_progress_count"], 0)
        self.assertEqual(row["last_harness"], "codex")
        self.assertEqual(row["last_accepted_at"], "2026-10-25 01:00:00")
        self.assertFalse(row["session_only"])
        self.assertEqual(row["sessions"], [])
        # The HTTP assertions above run with collection active. Quiesce all
        # background writers before isolating the query's no-write guarantee.
        service.close()
        before = self.snapshot()
        self.assertEqual(service.read("/v1/projects")[0], 200)
        self.assertEqual(self.snapshot(), before)

    def test_http_hide_show_replay_first_response_after_restart_and_new_dispatch_restores_project(self):
        self.prepare()
        rec = self.dispatch("first")
        service = self.start(poll_interval=3600)
        row = self.request("/v1/projects")[1]["projects"][0]
        path = "/v1/projects/" + row["id"]
        self.assertEqual(self.request(path + "/hide", "POST", "{}", {"Content-Type": "application/json"})[0], 400)
        headers = {"Content-Type": "application/json", "Idempotency-Key": "hide-once"}
        first = self.request(path + "/hide", "POST", "{}", headers)
        self.assertEqual(first[0], 200)
        self.assertTrue(first[1]["project"]["hidden"])
        stream = self.open_events()
        changed = []
        while not changed or not changed[-1]["data"].get("hidden"):
            _, kind, event = self.next_event(stream)
            if kind == "project.changed":
                changed.append(event)
        self.assertEqual([item["data"]["hidden"] for item in changed], [False, True])
        stream.close()
        restored = self.request(path + "/show", "POST", "{}", dict(headers, **{"Idempotency-Key": "show"}))
        self.assertFalse(restored[1]["project"]["hidden"])
        self.assertEqual(self.request(path + "/hide", "POST", "{}", headers), first)
        self.assertFalse(self.request("/v1/projects")[1]["projects"][0]["hidden"])
        service.close()
        self.start(poll_interval=3600)
        self.assertEqual(self.request(path + "/hide", "POST", "{}", headers), first)
        self.request(path + "/hide", "POST", "{}", dict(headers, **{"Idempotency-Key": "hide-again"}))
        # Registering a new dispatch uses the same CLI implementation and lock.
        self.dispatch("second")
        self.assertFalse(self.request("/v1/projects")[1]["projects"][0]["hidden"])
        foreign = ProjectIndex(self.state, "other")
        foreign.register(dict(rec, job="foreign", user_id="other", project="private"))
        foreign_id = next(iter(foreign.read()))
        self.assertEqual(self.request("/v1/projects/" + foreign_id + "/hide", "POST", "{}",
                                     dict(headers, **{"Idempotency-Key": "foreign"}))[0], 404)

    def test_project_history_pagination_keeps_snapshot_when_new_jobs_arrive_and_retains_cleaned_jobs(self):
        self.prepare()
        records = [self.dispatch("history-" + str(n), "done", "completed", True, "accepted",
                                 created=f"2026-10-25 0{n}:00:00") for n in range(4)]
        self.dispatch("running")
        self.dispatch("different", "done", "completed", True, "accepted", project="elsewhere")
        # clean removes the registry; retained dispatch metadata and receipt remain.
        Path(cli.job_path(records[1]["job"])).unlink()
        self.start(poll_interval=3600)
        identity = next(row["id"] for row in self.request("/v1/projects")[1]["projects"] if row["name"] == "demo")
        base = "/v1/jobs?project=" + identity + "&state=history&limit=2"
        first = self.request(base)
        self.assertEqual(first[0], 200)
        self.assertEqual([row["job"] for row in first[1]["jobs"]], ["history-3", "history-2"])
        self.assertTrue(first[1]["next_cursor"])
        self.dispatch("new-history", "done", "completed", True, "accepted", created="2026-10-25 05:00:00")
        self.dispatch("new-backdated", "done", "completed", True, "accepted", created="2026-10-25 00:30:00")
        second = self.request(base + "&cursor=" + first[1]["next_cursor"])
        self.assertEqual([row["job"] for row in second[1]["jobs"]], ["history-1", "history-0"])
        self.assertIsNone(second[1]["next_cursor"])
        self.assertEqual(self.request(base + "&cursor=" + first[1]["next_cursor"]), second)
        self.assertEqual(self.request(base)[1]["jobs"][0]["job"], "new-history")
        self.assertEqual(self.request("/v1/jobs?project=" + identity + "&state=active")[1]["jobs"][0]["job"], "running")
        self.assertEqual(self.request("/v1/jobs?project=unknown")[1]["jobs"], [])
        for suffix in ("&cursor=bad", "&limit=0", "&limit=201"):
            self.assertEqual(self.request(base + suffix)[0], 400)

    def test_harness_statistics_count_rounds_durations_streaks_and_emit_only_changed_values(self):
        self.prepare()
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        clock = patch("remote_dispatch.rd_harness_stats.datetime", wraps=datetime)
        clock_source = clock.start()
        clock_source.now.return_value = now
        self.addCleanup(clock.stop)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rec = self.dispatch("sequence", created=midnight.isoformat())
        for number, (seconds, decision) in enumerate(((60, "rejected"), (120, "accepted"), (180, "accepted")), 1):
            start = midnight + timedelta(minutes=10 * number)
            finish = start + timedelta(seconds=seconds)
            path = cli.result_path(rec, number)
            cli.messages.atomic_json(path, dict(job=rec["job"], round=number, host="target", receipt_id=str(number),
                execution_state="done", executor_exit=0, captured_at=finish.isoformat(),
                report=dict(outcome="completed", checks="passed", started_at=start.isoformat(), finished_at=finish.isoformat())))
            cli.messages.atomic_json(path.with_suffix(".review.json"), dict(review_state=decision, reviewed_at=finish.isoformat()))
        cli.save_job(dict(rec, rounds=4))
        self.states[rec["job"]] = dict(state="running", round="4", started_at=(now - timedelta(seconds=30)).isoformat())
        self.dispatch("private-stats", "done", "failed", user="other")
        service = self.start(poll_interval=0.05)
        rows = self.eventually(lambda: self.request("/v1/harness-stats?recent=2")[1].get("harness_stats", []),
                               lambda rows: rows and rows[0].get("current_rounds") and rows[0]["current_rounds"][0]["elapsed_seconds"] is not None)
        self.assertEqual(len(rows), 1)
        stats = rows[0]
        self.assertEqual(stats["harness"], "codex")
        self.assertEqual(stats["today_round_count"], 3)
        self.assertEqual(stats["average_duration_seconds"], 150.0)
        self.assertEqual(stats["duration_sample_count"], 2)
        self.assertEqual((stats["consecutive_accepted"], stats["consecutive_rejected"]), (2, 0))
        self.assertEqual((stats["last_result"], stats["last_round"]), ("completed", 3))
        self.assertEqual(stats["current_rounds"][0]["round"], 4)
        self.assertGreaterEqual(stats["current_rounds"][0]["elapsed_seconds"], 30)
        stream = self.open_events()
        while True:
            _, kind, event = self.next_event(stream)
            if kind == "harness.stats_changed":
                break
        self.assertEqual(event["data"]["today_round_count"], 3)
        stream.close()
        # Repeated poll ticks and changing elapsed time must not emit statistics again.
        clock_source.now.return_value = now + timedelta(seconds=10)
        elapsed = self.request("/v1/harness-stats")[1]["harness_stats"][0]["current_rounds"][0]["elapsed_seconds"]
        self.assertEqual(elapsed, 40)
        import time
        time.sleep(0.15)
        from remote_dispatch import rd_events as events
        changed = [row for row in events.read_after(self.state, 0) if row["type"] == "harness.stats_changed"]
        self.assertEqual(len(changed), 1)
        service.close()
        self.start(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/harness-stats")[1]["harness_stats"], lambda rows: rows and rows[0]["current_rounds"])
        self.assertEqual(len([row for row in events.read_after(self.state, 0) if row["type"] == "harness.stats_changed"]), 1)
        for suffix in ("recent=0", "recent=bad", "recent=201"):
            self.assertEqual(self.request("/v1/harness-stats?" + suffix)[0], 400)

    def test_real_worker_timestamps_feed_statistics_and_survive_job_cleanup(self):
        service = support.ServeTests.seed(self, poll_interval=3600)
        service.close()
        rec = dict(self.rec, agent="codex", receiver_session="receiver")
        cli.save_job(rec)
        # The target's existing started.rN marker is the source of elapsed time.
        start = datetime.now(timezone.utc) - timedelta(seconds=90)
        (self.worker / "started.r1").write_text(start.isoformat())
        cli.messages.finish(self.worker, rec["job"], 1, 0)
        Path(cli.result_path(rec, 1)).unlink(missing_ok=True)
        self.start(poll_interval=3600)
        stats = self.eventually(lambda: self.request("/v1/harness-stats")[1].get("harness_stats", []),
                                lambda rows: rows and rows[0]["average_duration_seconds"] is not None)[0]
        self.assertGreaterEqual(stats["average_duration_seconds"], 90)
        self.assertLess(stats["average_duration_seconds"], 95)
        Path(cli.job_path(rec["job"])).unlink()
        cleaned = self.request("/v1/harness-stats")[1]["harness_stats"][0]
        self.assertEqual(cleaned["average_duration_seconds"], stats["average_duration_seconds"])
        self.assertEqual(cleaned["harness"], "codex")

    def test_project_write_rolls_back_on_event_failure_and_reports_index_lock_conflict(self):
        import os
        from remote_dispatch.rd_projects import ProjectLock
        self.prepare()
        self.dispatch("rollback")
        self.start(poll_interval=3600)
        identity = self.request("/v1/projects")[1]["projects"][0]["id"]
        path = "/v1/projects/" + identity + "/hide"
        headers = {"Content-Type": "application/json", "Idempotency-Key": "retry"}
        with ProjectLock(self.state, "fixture"):
            self.assertEqual(self.request(path, "POST", "{}", headers), (409, {"error": {"reason": "project_busy"}}))
        replace = os.replace
        def fail_event(source, destination):
            if Path(destination) == self.state / "events.jsonl":
                raise OSError("fixture disk full")
            return replace(source, destination)
        with patch.object(os, "replace", side_effect=fail_event):
            self.assertEqual(self.request(path, "POST", "{}", headers), (503, {"error": {"reason": "project_write_failed"}}))
        self.assertFalse(self.request("/v1/projects")[1]["projects"][0]["hidden"])
        self.assertTrue(self.request(path, "POST", "{}", headers)[1]["project"]["hidden"])

    def test_legacy_projects_and_unknown_timings_remain_read_only_and_user_scoped(self):
        self.prepare()
        self.config.write_text("[project:app]\n[project:app-tools]\n")
        for job in ("fixture-app-tools-task", "fixture-mystery-task"):
            cli.save_job(dict(job=job, host="target", rounds=1, agent="codex"))
        self.start(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                        lambda rows: len(rows) == 2 and all(row["execution_state"] == "running" for row in rows))
        before = self.snapshot()
        projects = self.request("/v1/projects")[1]["projects"]
        self.assertEqual([row["name"] for row in projects], ["app-tools", "未归属"])
        self.assertTrue(all(row["recent_event"] and row["recent_event"]["type"] == "job.state_changed" for row in projects))
        stats = self.request("/v1/harness-stats")[1]["harness_stats"][0]
        self.assertEqual(stats["today_round_count"], 0)
        self.assertIsNone(stats["average_duration_seconds"])
        self.assertIsNone(stats["current_rounds"][0]["elapsed_seconds"])
        self.assertEqual(self.snapshot(), before)

    assert_structure = support.ServeTests.assert_structure

    def test_new_http_and_sse_contract_examples_match_requests_responses_and_events(self):
        self.prepare()
        rec = self.dispatch("fixture-job", "done", "completed", True, "accepted")
        path = cli.result_path(rec, 1)
        receipt = cli.messages.read_json(path)
        now = datetime.now(timezone.utc)
        receipt["report"].update(started_at=(now - timedelta(seconds=60)).isoformat(), finished_at=now.isoformat(), checks="passed")
        cli.messages.atomic_json(path, receipt)
        self.start(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1]["job"],
                        lambda row: row["execution_state"] == "done")
        self.eventually(lambda: self.request("/v1/projects")[1]["projects"][0],
                        lambda row: row["recent_event"]["data"].get("accepted_round_count") == 1)
        identity = self.request("/v1/projects")[1]["projects"][0]["id"]
        for name in ("projects-summary", "jobs-project", "jobs-history", "harness-stats", "projects-key-required", "projects-hide", "projects-show"):
            with self.subTest(contract=name):
                example = json.loads((support.CONTRACTS / (name + ".json")).read_text())
                request = example["request"]
                target = request["path"].replace("3e15c8030cfe7c2cd22b", identity)
                status, body = self.request(target, request["method"],
                    json.dumps(request["body"]) if "body" in request else None, request.get("headers"))
                self.assertEqual(status, example["response"]["status"])
                self.assert_structure(body, example["response"]["body"])
                if example["events"]:
                    stream = self.open_events()
                    for expected in example["events"]:
                        while True:
                            _, kind, event = self.next_event(stream)
                            if (kind == expected["type"] and (kind != "project.changed" or
                                    (event["data"].get("hidden") == expected["data"]["hidden"] and
                                     event["data"].get("host") == expected["data"].get("host")))):
                                break
                        self.assert_structure(event, expected)
                    stream.close()

    def test_acked_blocked_receipt_becomes_pending_review_and_does_not_override_interruptions(self):
        self.prepare()
        self.dispatch("blocked", "done", "blocked", True, host="blocked-only")
        self.dispatch("blocked-shared", "done", "blocked", True, host="shared")
        self.dispatch("interrupted-shared", "unreachable", host="shared")
        self.start(poll_interval=3600)
        row = self.eventually(lambda: self.request("/v1/projects")[1]["projects"][0],
                              lambda row: row["hosts"]["shared"]["state"] == "interrupted")
        self.assertEqual(row["hosts"]["blocked-only"]["state"], "pending_review")

    def test_legacy_receipts_do_not_expose_jobs_owned_by_another_current_user(self):
        self.prepare()
        self.dispatch("own", "done", "completed", True, "accepted")
        foreign = dict(job="foreign-old", host="target", rounds=1, user_id="other", project="private", agent="claude")
        cli.save_job(foreign)
        cli.messages.atomic_json(cli.result_path(foreign, 1), dict(job=foreign["job"], round=1,
            host="target", agent="claude", receipt_id="old", execution_state="done", report=dict(outcome="completed")))
        self.start(poll_interval=3600)
        self.assertEqual([row["name"] for row in self.request("/v1/projects")[1]["projects"]], ["demo"])
        self.assertEqual([row["harness"] for row in self.request("/v1/harness-stats")[1]["harness_stats"]], ["codex"])
        self.assertNotIn("foreign-old", [row["job"] for row in self.request("/v1/jobs?state=history")[1]["jobs"]])

    def test_equal_priority_uses_latest_state_change_instead_of_dispatch_time(self):
        self.prepare()
        self.dispatch("old-dispatch", "running", created="2026-10-20 00:00:00")
        self.dispatch("new-dispatch", "unreachable", created="2026-10-25 00:00:00")
        self.start(poll_interval=0.05)
        self.eventually(lambda: self.request("/v1/projects")[1]["projects"][0]["hosts"]["target"],
                        lambda row: row["job"] == "new-dispatch" and row["state"] == "interrupted")
        self.states["old-dispatch"] = dict(state="unreachable", round="1")
        self.eventually(lambda: self.request("/v1/projects")[1]["projects"][0]["hosts"]["target"],
                        lambda row: row["job"] == "old-dispatch" and row["state"] == "interrupted")

    def test_harnesses_keep_independent_rejection_streaks_and_weekly_counts_ignore_resume(self):
        self.prepare()
        now = datetime.now().astimezone()
        rec = self.dispatch("codex-sequence", "done", "failed", True, "rejected", created=now.isoformat())
        cli.messages.atomic_json(cli.result_path(rec, 2), dict(job=rec["job"], round=2,
            host="target", receipt_id="second", execution_state="done", executor_exit=1,
            captured_at=now.isoformat(), report=dict(outcome="failed")))
        cli.messages.atomic_json(cli.result_path(rec, 2).with_suffix(".review.json"),
                                 dict(review_state="rejected", reviewed_at=now.isoformat()))
        cli.save_job(dict(rec, rounds=2))
        second = self.dispatch("claude-sequence", "done", "failed", True, "rejected", created=now.isoformat())
        cli.save_job(dict(second, agent="claude"))
        self.dispatch("previous-week", "done", "completed", True, "accepted", created=(now - timedelta(days=8)).isoformat())
        self.start(poll_interval=3600)
        stats = {row["harness"]: row for row in self.request("/v1/harness-stats")[1]["harness_stats"]}
        self.assertEqual(set(stats), {"claude", "codex"})
        self.assertEqual(stats["codex"]["consecutive_rejected"], 2)
        self.assertEqual(stats["claude"]["consecutive_rejected"], 1)
        self.assertEqual(stats["codex"]["last_result"], "failed")
        self.assertIsNone(stats["codex"]["average_duration_seconds"])
        self.assertEqual(self.request("/v1/projects")[1]["projects"][0]["week_dispatch_count"], 2)
        stream = self.open_events()
        harnesses = set()
        while len(harnesses) < 2:
            _, kind, event = self.next_event(stream)
            if kind == "harness.stats_changed":
                harnesses.add(event["data"]["harness"])
        self.assertEqual(harnesses, {"claude", "codex"})
        stream.close()

    def test_live_round_elapsed_time_uses_real_target_marker_without_query_writes(self):
        service = support.ServeTests.seed(self, poll_interval=3600, running=True)
        service.close()
        cli.save_job(dict(self.rec, agent="codex"))
        start = datetime.now(timezone.utc) - timedelta(seconds=45)
        (self.worker / "started.r1").write_text(start.isoformat())
        service = self.start(poll_interval=3600)
        stats = self.eventually(lambda: self.request("/v1/harness-stats")[1]["harness_stats"],
            lambda rows: rows and rows[0]["current_rounds"] and rows[0]["current_rounds"][0]["elapsed_seconds"] is not None)
        self.assertGreaterEqual(stats[0]["current_rounds"][0]["elapsed_seconds"], 45)
        self.assertEqual(stats[0]["today_round_count"], 0)
        self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"], bool)
        before = self.snapshot()
        self.request("/v1/harness-stats")
        self.assertEqual(self.snapshot(), before)
        service.close()
        self.assertFalse(self.path.exists())

    def test_cumulative_harness_rounds_survive_cleanup_and_do_not_depend_on_recent_limit(self):
        self.prepare()
        rec = self.dispatch("history", "done", "completed", True, "rejected")
        for number in (2, 3):
            cli.messages.atomic_json(cli.result_path(rec, number), dict(
                job=rec["job"], round=number, host="target", receipt_id=str(number),
                execution_state="done", captured_at="2026-10-24T10:00:00Z",
                report=dict(outcome="failed")))
        cli.save_job(dict(rec, rounds=4))
        self.dispatch("running-only", host="second")
        self.dispatch("foreign-history", "done", "completed", user="other")
        service = self.start(poll_interval=3600)
        self.assertEqual(self.request("/v1/harness-stats?recent=1")[1]["harness_stats"][0]["total_round_count"], 3)
        service.close()
        Path(cli.job_path(rec["job"])).unlink()
        for path in self.state.glob("events*.jsonl"):
            path.unlink()
        self.start(poll_interval=3600)
        stats = self.request("/v1/harness-stats?recent=200")[1]["harness_stats"][0]
        self.assertEqual(stats["total_round_count"], 3)

    def test_host_history_uses_fourteen_local_dates_including_dst_and_receipt_host(self):
        self.prepare()
        original_timezone = os.environ.get("TZ")
        def restore_timezone():
            if original_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_timezone
            time.tzset()
        self.addCleanup(restore_timezone)
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        with patch("remote_dispatch.rd_harness_stats.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.config.write_text("[host:unused]\nssh=local\n")
            rec = self.dispatch("migrated", host="new-target")
            stamps = ["2026-10-20T03:59:59Z", "2026-10-20T04:00:00Z",
                      "2026-11-01T03:59:59Z", "2026-11-01T05:30:00Z",
                      "2026-11-01T06:30:00Z", "2026-11-02T04:59:59Z",
                      "2026-11-02T05:00:00Z", "2026-11-02T13:00:00Z", "invalid"]
            for number, stamp in enumerate(stamps, 1):
                cli.messages.atomic_json(cli.result_path(rec, number), dict(
                    job=rec["job"], round=number, host="old-target", receipt_id=str(number),
                    captured_at=stamp, report=dict(outcome="completed", finished_at=stamp)))
            cli.save_job(dict(rec, rounds=10))
            self.dispatch("foreign-daily", "done", "completed", user="other", host="private-target", created=now.isoformat())
            service = self.start(poll_interval=3600)
            body = self.request("/v1/harness-stats")[1]
            hosts = {row["host"]: row for row in body["host_stats"]}
            self.assertEqual(body["date_basis"], "controller_local")
            self.assertEqual(set(hosts), {"old-target", "new-target", "unused"})
            days = hosts["old-target"]["daily_round_counts"]
            self.assertEqual(len(days), 14)
            self.assertEqual(days[0], dict(date="2026-10-20", round_count=1))
            self.assertEqual(days[-3:], [dict(date="2026-10-31", round_count=1),
                dict(date="2026-11-01", round_count=3), dict(date="2026-11-02", round_count=1)])
            self.assertEqual(sum(day["round_count"] for day in days), 6)
            self.assertTrue(all(day["round_count"] == 0 for day in hosts["new-target"]["daily_round_counts"]))
            self.assertTrue(all(day["round_count"] == 0 for day in hosts["unused"]["daily_round_counts"]))
            service.close()

    def test_project_host_counts_only_accepted_rounds_and_keeps_original_hosts_after_cleanup(self):
        self.prepare()
        self.config.write_text("[host:unused]\nssh=local\n")
        rec = self.dispatch("accepted-old", "done", "completed", True, "accepted", host="old")
        for number, decision in ((2, "rejected"), (3, "voided"), (4, "pending"), (5, "accepted")):
            cli.messages.atomic_json(cli.result_path(rec, number), dict(job=rec["job"], round=number,
                host="new", receipt_id=str(number), report=dict(outcome="completed")))
            if decision != "pending":
                cli.messages.atomic_json(cli.result_path(rec, number).with_suffix(".review.json"),
                    dict(review_state=decision, reviewed_at="2026-10-25T01:00:00Z"))
        cli.save_job(dict(rec, rounds=5, host="new"))
        self.dispatch("automatic-completed", "done", "completed", True, host="new")
        self.dispatch("foreign-accepted", "done", "completed", True, "accepted", user="other", host="new")
        self.dispatch("another-project", "done", "completed", True, "accepted", project="other", host="new")
        service = self.start(poll_interval=3600)
        projects = {row["name"]: row for row in self.request("/v1/projects")[1]["projects"]}
        self.assertEqual(projects["demo"]["hosts"]["old"]["accepted_round_count"], 1)
        self.assertEqual(projects["demo"]["hosts"]["new"]["accepted_round_count"], 1)
        self.assertEqual(projects["demo"]["hosts"]["unused"]["accepted_round_count"], 0)
        self.assertEqual(projects["other"]["hosts"]["new"]["accepted_round_count"], 1)
        service.close()
        Path(cli.job_path(rec["job"])).unlink()
        for path in self.state.glob("events*.jsonl"):
            path.unlink()
        self.start(poll_interval=3600)
        demo = next(row for row in self.request("/v1/projects")[1]["projects"] if row["name"] == "demo")
        self.assertEqual(demo["hosts"]["old"]["accepted_round_count"], 1)
        self.assertEqual(demo["hosts"]["new"]["accepted_round_count"], 1)

    def test_history_stat_events_include_changed_counts_and_deduplicate_across_restart_and_rotation(self):
        self.prepare()
        from remote_dispatch import rd_events as events
        with patch.dict(os.environ, {"RD_EVENT_LOG_MAX_BYTES": "1"}):
            self.dispatch("first", "done", "completed", True, "accepted", project="one")
            self.dispatch("second", "done", "completed", True, "accepted", project="two")
            service = self.start(poll_interval=0.05)
            stream = self.open_events()
            accepted = {}
            while len(accepted) < 2:
                _, kind, event = self.next_event(stream)
                if kind == "project.changed" and event["data"].get("accepted_round_count") is not None:
                    accepted[event["data"]["name"]] = event["data"]["accepted_round_count"]
            self.assertEqual(accepted, {"one": 1, "two": 1})
            stream.close()
            service.close()
            before = events.read_after(self.state, 0)
            stats = [row for row in before if row["type"] == "harness.stats_changed"]
            self.assertEqual(stats[-1]["data"]["total_round_count"], 2)
            self.assertEqual(stats[-1]["data"]["host_stats"][0]["host"], "target")
            service = self.start(poll_interval=0.05)
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: all(row["execution_state"] == "done" for row in rows))
            time.sleep(0.2)
            self.assertEqual(events.read_after(self.state, 0), before)
            self.dispatch("third", "done", "completed", True, "accepted", project="one")
            stream = self.open_events("/v1/events?after=" + str(before[-1]["seq"]))
            changed = {}
            while len(changed) < 2:
                _, kind, event = self.next_event(stream)
                if kind == "project.changed" and event["data"].get("accepted_round_count") == 2:
                    changed["project"] = event["data"]
                if kind == "harness.stats_changed" and event["data"].get("total_round_count") == 3:
                    changed["harness"] = event["data"]
            self.assertEqual(changed["project"]["name"], "one")
            stream.close()
            service.close()

    def test_statistics_and_events_exclude_sensitive_fields_and_support_command_only_history(self):
        self.prepare()
        now = datetime.now().astimezone()
        rec = self.dispatch("command-history", "done", "completed", True, "accepted", created=now.isoformat())
        marker = "fixture-do-not-publish-41"
        cli.save_job(dict(rec, agent=None, env_file=marker, profile=dict(token=marker)))
        path = cli.result_path(rec, 1)
        receipt = cli.messages.read_json(path)
        receipt.update(agent=None, password=marker, agent_auth_file=marker)
        receipt["report"].update(token=marker, ssh_opts=marker)
        cli.messages.atomic_json(path, receipt)
        self.start(poll_interval=3600)
        stream = self.open_events()
        while True:
            _, kind, event = self.next_event(stream)
            if kind == "harness.stats_changed":
                break
        self.assertIsNone(event["data"]["harness"])
        self.assertEqual(sum(day["round_count"] for day in event["data"]["host_stats"][0]["daily_round_counts"]), 1)
        stream.close()
        body = self.request("/v1/harness-stats")[1]
        self.assertEqual(body["harness_stats"], [])
        self.assertEqual(body["host_stats"], event["data"]["host_stats"])
        projects = self.request("/v1/projects")[1]
        encoded = json.dumps([body, projects, event])
        for value in (marker, "env_file", "agent_auth_file", "ssh_opts", "password", '"token"', '"profile"'):
            self.assertNotIn(value, encoded)

    def test_local_midnight_advances_daily_window_and_publishes_stats_without_new_rounds(self):
        self.prepare()
        now = datetime.now().astimezone().replace(hour=23, minute=59, second=0, microsecond=0)
        self.dispatch("midnight", "done", "completed", created=now.isoformat())
        with patch("remote_dispatch.rd_harness_stats.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.start(poll_interval=0.05)
            stream = self.open_events()
            while True:
                _, kind, event = self.next_event(stream)
                if kind == "harness.stats_changed":
                    break
            cursor = event["seq"]
            first = event["data"]["host_stats"][0]["daily_round_counts"]
            self.assertEqual(first[-1]["round_count"], 1)
            clock.now.return_value = now + timedelta(minutes=2)
            stream.close()
            stream = self.open_events("/v1/events?after=" + str(cursor))
            while True:
                _, kind, event = self.next_event(stream)
                if kind == "harness.stats_changed":
                    break
            second = event["data"]["host_stats"][0]["daily_round_counts"]
            self.assertEqual(second[:-1], first[1:])
            self.assertEqual(second[-1], dict(date=(now.date() + timedelta(days=1)).isoformat(), round_count=0))
            self.assertEqual(event["data"]["total_round_count"], 1)
            stream.close()
