"""列表缓存通过真实 HTTP/SSE 及合成状态验证，不使用用户状态目录。"""
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_events as events
from remote_dispatch.rd_projects import ProjectIndex, project_id
from tests import test_rd_serve as base


class ListCacheTests(unittest.TestCase):
    setUp = base.ServeTests.setUp
    start = base.ServeTests.start
    request = base.ServeTests.request
    eventually = base.ServeTests.eventually
    open_events = base.ServeTests.open_events
    next_event = base.ServeTests.next_event

    def test_idle_connections_do_not_starve_list_requests(self):
        self.start(poll_interval=3600)
        for _ in range(32):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(connection.close)
            connection.connect(str(self.path))
            connection.sendall(b"GET /v1/projects HTTP/1.1\r\n")
        self.assertEqual(self.request("/v1/projects")[0], 200)

    def test_current_jobs_remain_readable_when_unrelated_retained_receipt_is_corrupt(self):
        cli.save_job(dict(job="current", user_id="fixture", host="removed", rounds=1))
        directory = self.state / "results" / "old"
        directory.mkdir(parents=True)
        (directory / "r1.json").write_text("{invalid")
        self.start(poll_interval=3600)
        status, body = self.request("/v1/jobs")
        self.assertEqual(status, 200)
        self.assertEqual([row["job"] for row in body["jobs"]], ["current"])
        cli.save_job(dict(job="current", user_id="fixture", host="removed", rounds=2))
        own = self.state / "results" / "current"
        own.mkdir()
        (own / "r1.json").write_text("{invalid")
        cli.messages.atomic_json(own / "r2.json", dict(job="current", round=2,
            receipt_id="current-round", execution_state="done", executor_exit=0))
        status, body = self.request("/v1/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(body["jobs"][0]["execution_state"], "done")

    def test_same_round_state_events_are_visible_before_the_receipt_arrives(self):
        rec = dict(job="current", user_id="fixture", host="removed", rounds=1, project="demo")
        cli.save_job(rec)
        with patch.object(cli, "job_states", return_value={}):
            service = self.start(poll_interval=3600)
            self.assertTrue(service.reports.ready.wait(5))
            self.request("/v1/projects")
            self.request("/v1/jobs")
            for state in ("running", "done"):
                events.emit(self.state, "job.state_changed", events.job_data(rec, execution_state=state))
                # A later progress event must not erase the authoritative state event.
                events.emit(self.state, "progress.updated", events.job_data(rec, summary="new progress"))
                self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0]["execution_state"], state)
                cell = self.request("/v1/projects")[1]["projects"][0]["hosts"]["removed"]
                self.assertEqual(cell["execution_state"], state)
            events.emit(self.state, "job.state_changed", events.job_data(rec, execution_state="running"))
            self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0]["execution_state"], "running")
            cli.messages.atomic_json(cli.result_path(rec, 1), dict(job="current", round=1,
                receipt_id="finished", execution_state="done", executor_exit=0))
            self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0]["execution_state"], "done")

    def test_large_project_index_and_unrelated_user_files_preserve_list_contract(self):
        index = ProjectIndex(self.state, "fixture")
        rows = {}
        for number in range(700):
            name = f"project-{number:03}-" + "x" * 80
            identity = project_id(name, "")
            rows[identity] = dict(id=identity, name=name, origin="", hidden=False,
                                  dispatch_count=0, first_dispatch_at=None, last_dispatch_at=None)
        cli.messages.atomic_json(index.path, dict(schema=1, projects=rows), limit=None)
        (self.state / "projects" / "other-user.json").write_text("{invalid")
        self.start(poll_interval=3600)
        status, body = self.request("/v1/projects")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["projects"]), 700)

    def test_realistic_lists_remain_fast_with_ten_clients_and_sse(self):
        for index in range(511):
            rec = dict(job=f"job-{index:03}", user_id="fixture", host="removed", rounds=3,
                       project=f"project-{index % 30:02}", origin="",
                       created="2026-10-25T00:00:00Z", dispatch_state="started")
            if index < 200:
                cli.save_job(rec)
            directory = self.state / "results" / rec["job"]
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "dispatch.json").write_text(json.dumps(rec))
            for number in range(1, 4):
                receipt = dict(rec, round=number, receipt_id=f"receipt-{index}-{number}",
                               execution_state="done", executor_exit=0,
                               report=dict(outcome="completed", checks="passed"))
                (directory / f"r{number}.json").write_text(json.dumps(receipt))
                (directory / f"r{number}.ack.json").write_text(json.dumps(dict(
                    receipt_id=receipt["receipt_id"], received_at="2026-10-25T01:00:00Z")))
                (directory / f"r{number}.review.json").write_text(json.dumps(dict(
                    review_state="accepted", reviewed_at="2026-10-25T02:00:00Z")))
        with (self.state / "events.jsonl").open("w") as stream:
            for index in range(15000):
                stream.write(json.dumps(dict(seq=index + 1, time="2026-10-25T03:00:00Z",
                    type="job.state_changed", data=dict(job=f"job-{index % 511:03}",
                    user_id="fixture", execution_state="done"))) + "\n")
        with patch.object(cli, "job_states", return_value={}):
            service = self.start(poll_interval=3600)
            self.assertTrue(service.reports.ready.wait(10))
            stream = self.open_events("/v1/events?after=15000")
            self.addCleanup(stream.close)
            self.assertEqual(stream.readline(), b": connected\n")
            for path, key, count in (("/v1/projects", "projects", 30), ("/v1/jobs", "jobs", 200)):
                status, body = self.request(path)
                self.assertEqual((status, len(body[key])), (200, count))
                started = time.monotonic()
                self.assertEqual(self.request(path)[0], 200)
                warm = time.monotonic() - started
                self.assertLess(warm, 0.3, f"{path} warm={warm:.3f}s")
                started = time.monotonic()
                # Each worker owns its connection; no unittest cleanup mutation in workers.
                def read(_):
                    connection = base.UnixHTTPConnection(self.path)
                    try:
                        connection.request("GET", path)
                        response = connection.getresponse()
                        return response.status, len(json.loads(response.read())[key])
                    finally:
                        connection.close()
                with ThreadPoolExecutor(max_workers=10) as clients:
                    self.assertEqual(list(clients.map(read, range(10))), [(200, count)] * 10)
                concurrent = time.monotonic() - started
                print(f"{path}: warm={warm:.4f}s ten_clients={concurrent:.4f}s", flush=True)
                self.assertLess(concurrent, 1.0, f"{path} ten_clients={concurrent:.3f}s")
            event = events.emit(self.state, "progress.updated", dict(job="job-000", user_id="fixture", summary="new"))
            while self.next_event(stream)[2]["seq"] != event["seq"]:
                pass
            projects = self.request("/v1/projects")[1]["projects"]
            changed = next(row for row in projects if row["name"] == "project-00")
            self.assertEqual(changed["recent_event"]["seq"], event["seq"])
            receipt_path = self.state / "results" / "job-000" / "r3.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["report"]["outcome"] = "blocked"
            cli.messages.atomic_json(receipt_path, receipt)
            jobs = self.request("/v1/jobs")[1]["jobs"]
            self.assertEqual(next(row for row in jobs if row["job"] == "job-000")["outcome"], "blocked")

    def test_warm_lists_show_next_round_receipt_ack_review_event_and_deletion_immediately(self):
        rec = dict(job="changing", user_id="fixture", host="removed", rounds=1,
                   project="demo", origin="", dispatch_state="started")
        cli.save_job(rec)
        with patch.object(cli, "job_states", return_value={}):
            service = self.start(poll_interval=3600)
            self.assertTrue(service.reports.ready.wait(5))
            self.request("/v1/projects")
            self.request("/v1/jobs")
            cli.save_job(dict(rec, rounds=2))
            row = self.request("/v1/jobs")[1]["jobs"][0]
            self.assertEqual((row["round"], row["execution_state"]), (2, "starting"))
            path = cli.result_path(rec, 2)
            receipt = dict(job="changing", round=2, host="removed", user_id="fixture",
                           project="demo", receipt_id="new-receipt", execution_state="done",
                           executor_exit=0, report=dict(outcome="blocked"))
            cli.messages.atomic_json(path, receipt)
            self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0]["outcome"], "blocked")
            self.assertEqual(self.request("/v1/projects")[1]["projects"][0]["blocked_count"], 1)
            cli.messages.atomic_json(path.with_suffix(".ack.json"), dict(
                receipt_id="new-receipt", received_at="2026-10-26T01:00:00Z"))
            self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0]["delivery_state"], "received")
            cli.messages.atomic_json(path.with_suffix(".review.json"), dict(review_state="voided"))
            self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0]["review_state"], "voided")
            self.assertEqual(self.request("/v1/projects")[1]["projects"][0]["hosts"]["removed"]["state"], "blocked")
            event = events.emit(self.state, "progress.updated", dict(job="changing", user_id="fixture", summary="fresh"))
            recent = self.request("/v1/projects")[1]["projects"][0]["recent_event"]
            self.assertEqual((recent["seq"], recent["data"]["summary"]), (event["seq"], "fresh"))
            # In-place edits must invalidate too, even without a new event.
            receipt["report"]["outcome"] = "failed"
            path.write_text(json.dumps(receipt))
            self.assertEqual(self.request("/v1/jobs")[1]["jobs"][0]["outcome"], "failed")
            path.unlink()
            self.assertIsNone(self.request("/v1/jobs")[1]["jobs"][0]["outcome"])
