"""公开写接口：HTTP 响应、查询读回、SSE；仅替换目标机系统边界。"""
import json
import os
from pathlib import Path
import runpy
import subprocess
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch.rd_lock import JobLock
import unittest

from tests import test_rd_serve as serve_tests


class ServeWriteTests(unittest.TestCase):
    setUp = serve_tests.ServeTests.setUp
    start = serve_tests.ServeTests.start
    seed = serve_tests.ServeTests.seed
    request = serve_tests.ServeTests.request
    eventually = serve_tests.ServeTests.eventually
    open_events = serve_tests.ServeTests.open_events
    next_event = serve_tests.ServeTests.next_event
    assert_structure = serve_tests.ServeTests.assert_structure

    def post(self, path, body, key="write-1"):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.request(path, "POST", json.dumps(body), headers)

    def ready(self):
        return self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/receipt"),
                               lambda result: result[0] == 200)[1]["receipt"]

    def test_ack_requires_key_and_replays_first_result_after_restart_without_another_event(self):
        service = self.seed(poll_interval=3600)
        receipt = self.ready()
        path = "/v1/jobs/fixture-job/rounds/1/ack"
        body = dict(receipt_id=receipt["receipt_id"], acked_by="App MacBook")
        self.assertEqual(self.post(path, body, None),
                         (400, {"error": {"reason": "idempotency_key_required"}}))
        first = self.post(path, body)
        self.assertEqual(first[0], 200, first)
        self.assert_contract("write-ack", first)
        ack = self.ready()["acknowledgement"]
        self.assertEqual((ack["acked_by"], ack["via"]), ("App MacBook", "serve"))
        service.close()
        self.start(poll_interval=3600)
        self.assertEqual(self.post(path, body), first)
        self.assertEqual(self.post(path, dict(body, extra="different")),
                         (422, {"error": {"reason": "idempotency_key_reused"}}))
        self.assertEqual(self.ready()["acknowledgement"], ack)
        response = self.open_events()
        # The next explicit review acts as a barrier after the replayed ack.
        self.assertEqual(self.post("/v1/jobs/fixture-job/rounds/1/review",
                                   dict(review_state="accepted", reviewed_by="reviewer"), "review")[0], 200)
        kinds = []
        while True:
            _, kind, _ = self.next_event(response)
            kinds.append(kind)
            if kind == "review.updated":
                break
        self.assertEqual(kinds.count("receipt.acked"), 1)

    def agent_seed(self, **overrides):
        self.config.write_text("[host:target]\nssh=fixture.invalid\nplatform=windows\nworkdir=C:/fixture\nsync=direct\n")
        self.target_path = self.root / "target.json"
        initial_state = overrides.pop("initial_state", "done")
        exit_code = overrides.pop("exit_code", "0")
        self.target_data = dict(states={"fixture-job": dict(state=initial_state, round="1", exit=exit_code, head="base", dirty="0")}, **overrides)
        cli.messages.atomic_json(self.target_path, self.target_data)
        fixture = Path(__file__).parent / "fixtures/serve_write_target.py"
        target = runpy.run_path(str(fixture))["request"]
        original_run = subprocess.run
        def run(argv, *args, **kwargs):
            if isinstance(argv, list) and len(argv) == 3 and argv[1] == "-c" and "execute_cli" in argv[2]:
                argv = [argv[0], str(fixture)]
            return original_run(argv, *args, **kwargs)
        for mock in (patch.dict(os.environ, {"RD_TEST_TARGET": str(self.target_path),
                                            "RD_PROFILE_DIR": str(self.root / "absent-profile")}),
                     patch.object(cli.native, "request", side_effect=target),
                     patch.object(subprocess, "run", side_effect=run)):
            mock.start()
            self.addCleanup(mock.stop)
        self.rec = dict(job="fixture-job", user_id="fixture", host="target", rounds=1,
                        project="demo", origin="", repo=str(self.root), agent="codex", isolated=True,
                        initiator_session="original-session", receiver_session="receiver",
                        handoff_version=1, base="base", branch="ticket/demo", sync="direct")
        cli.save_job(self.rec)
        service = self.start(poll_interval=3600)
        self.ready()
        return service

    def test_review_auto_ack_conflicts_include_original_actor_time_and_result(self):
        self.agent_seed()
        path = "/v1/jobs/fixture-job/rounds/1/review"
        status, body = self.post(path, dict(review_state="accepted", reviewed_by="App MacBook"))
        self.assertEqual(status, 200, body)
        self.assert_contract("write-review", (status, body))
        self.assertEqual(self.post(path, dict(review_state="accepted", reviewed_by="App MacBook")), (status, body))
        self.assertEqual(self.post(path, dict(review_state="voided", reviewed_by="changed")),
                         (422, {"error": {"reason": "idempotency_key_reused"}}))
        receipt = self.ready()
        self.assertEqual(receipt["review_state"], "accepted")
        self.assertEqual((receipt["acknowledgement"]["acked_by"], receipt["acknowledgement"]["via"]),
                         ("App MacBook", "serve"))
        status, body = self.post(path, dict(review_state="voided", reviewed_by="other"), "conflict")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["reason"], "already_reviewed")
        self.assert_contract("write-already-reviewed", (status, body))
        for name in ("reviewed_by", "reviewed_at", "review_state"):
            self.assertEqual(body["error"][name], receipt[name])
        held = JobLock(self.state, "fixture-job").acquire(blocking=True)
        self.addCleanup(held.release)
        try:
            status, body = self.post(path, dict(review_state="accepted", reviewed_by="other"), "locked")
        finally:
            held.release()
        self.assertEqual((status, body["error"]["reason"]), (409, "round_in_progress"))
        self.assert_contract("write-round-in-progress", (status, body))

    def test_failed_outcome_cannot_be_accepted_and_empty_rejection_is_a_conflict(self):
        self.agent_seed(outcome="failed")
        for state in ("accepted", "rejected"):
            result = self.post("/v1/jobs/fixture-job/rounds/1/review",
                               dict(review_state=state, reviewed_by="App"), state)
            self.assertEqual((result[0], result[1]["error"]["reason"]), (409, "review_not_allowed"))
            self.assert_contract("write-review-not-allowed", result)
        self.assertEqual(self.ready()["review_state"], "pending")
        self.assertIsNone(self.ready()["acknowledgement"])

    def test_failed_rejection_returns_422_and_keeps_round_review_and_ack_unchanged(self):
        self.agent_seed(fail="launch")
        result = self.post("/v1/jobs/fixture-job/rounds/1/review",
                           dict(review_state="rejected", reviewed_by="App", review_note="继续"))
        self.assertEqual((result[0], result[1]["error"]["reason"]), (422, "resume_launch_failed"), result)
        self.assert_contract("write-resume-launch-failed", result)
        self.assertEqual(self.ready()["review_state"], "pending")
        self.assertIsNone(self.ready()["acknowledgement"])
        self.assertEqual(len(self.request("/v1/jobs/fixture-job")[1]["rounds"]), 1)

    def test_resume_creates_one_pending_round_for_concurrent_duplicate_requests(self):
        self.agent_seed()
        from concurrent.futures import ThreadPoolExecutor
        body = dict(prompt="继续", model="new-model", harness="codex")
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.post("/v1/jobs/fixture-job/resume", body), range(2)))
        self.assertEqual(replies[0][0], 200, replies)
        self.assertEqual(replies[0], replies[1])
        self.assert_contract("write-resume", replies[0])
        detail = self.request("/v1/jobs/fixture-job")[1]
        self.assertEqual(len(detail["rounds"]), 2)
        self.assertEqual(detail["rounds"][1]["review"]["review_state"], "pending")
        self.assertEqual(detail["job"]["model"], "new-model")
        self.assertIsNone(detail["prompt"])
        self.assertIsNone(detail["rounds"][0]["prompt"])
        self.assertEqual(detail["rounds"][1]["prompt"], "继续")
        response = self.open_events()
        while True:
            _, kind, row = self.next_event(response)
            if kind == "job.state_changed" and row["data"].get("round") == 2:
                break

    def test_stop_creates_failed_suppressed_receipt_and_auto_acknowledges_as_stopper(self):
        self.agent_seed()
        saved = json.loads(self.target_path.read_text())
        saved["states"]["fixture-job"] = dict(state="running", round="1")
        cli.messages.atomic_json(self.target_path, saved)
        result = self.post("/v1/jobs/fixture-job/stop", dict(stopped_by="App stopper"))
        self.assertEqual(result[0], 200, result)
        self.assert_contract("write-stop", result)
        self.assertEqual(self.post("/v1/jobs/fixture-job/stop", dict(stopped_by="App stopper")), result)
        self.assertEqual(self.post("/v1/jobs/fixture-job/stop", dict(stopped_by="changed")),
                         (422, {"error": {"reason": "idempotency_key_reused"}}))
        receipt = self.ready()
        self.assertEqual(receipt["executor_exit"], 143)
        self.assertEqual(receipt["report"]["outcome"], "failed")
        self.assertTrue(receipt["suppress_push"])
        self.assertTrue(receipt["needs_initiator_review"])
        self.assertEqual(receipt["review_state"], "pending")
        self.assertEqual((receipt["acknowledgement"]["acked_by"], receipt["acknowledgement"]["via"]),
                         ("App stopper", "serve"))
        response = self.open_events()
        seen = set()
        while "receipt.acked" not in seen:
            _, kind, row = self.next_event(response)
            if row["data"].get("suppress_push"):
                seen.add(kind)
        self.assertTrue({"job.state_changed", "receipt.ready", "receipt.acked"} <= seen)

    def prepare_run_project(self):
        service = self.agent_seed()
        repo = self.root / "repo"
        repo.mkdir()
        for argv in (["init", "-q"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"], ["commit", "--allow-empty", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
        self.rec.update(repo=str(repo), created="2026-10-25 00:00:00")
        cli.projects.ProjectIndex(self.state, "fixture").register(self.rec)
        Path(cli.job_path(self.rec["job"])).unlink()
        # An independently prepared portable profile; no host rules or login state.
        profile_dir = self.root / "profile"
        profile_dir.mkdir()
        (profile_dir / "config.toml").write_text('approval_policy = "never"\n')
        (profile_dir / "AGENTS.md").write_text("fixture rules")
        profile, _ = cli.profiles.bundle(profile_dir, "fixture", self.state)
        target = json.loads(self.target_path.read_text())
        target["profile"] = profile
        cli.messages.atomic_json(self.target_path, target)
        return service

    def test_runs_dispatches_retained_project_after_job_cleanup_with_defaults_and_is_idempotent(self):
        self.prepare_run_project()
        body = dict(project="demo", prompt="请实现此功能", harness="codex", host="target",
                    initiator_session="app:device", model="test-model")
        reply = self.post("/v1/runs", body)
        self.assertEqual(reply[0], 201, reply)
        self.assert_contract("write-runs", reply)
        self.assertEqual(self.post("/v1/runs", body), reply)
        job = reply[1]["job"]
        detail = self.request("/v1/jobs/" + job)[1]
        self.assertEqual(detail["job"]["initiator_session"], "app:device")
        self.assertEqual(detail["job"]["model"], "test-model")
        self.assertEqual(detail["job"]["project"], "demo")
        self.assertEqual(detail["prompt"], "请实现此功能")
        self.assertEqual(detail["rounds"][0]["prompt"], "请实现此功能")
        response = self.open_events()
        while True:
            _, kind, event = self.next_event(response)
            if kind == "job.created" and event["data"]["job"] == job:
                break
        self.assertEqual(event["data"]["round"], 1)
        # Omitted harness / target / initiator use project history, auto and App.
        identity = cli.projects.project_id("demo", "")
        automatic = self.post("/v1/runs", dict(project=identity, prompt="再派一次"), "automatic")
        self.assertEqual(automatic[0], 201, automatic)
        card = self.request("/v1/jobs/" + automatic[1]["job"])[1]["job"]
        self.assertEqual((card["initiator_session"], card["host"]), ("app", "target"))

    def test_runs_uses_the_same_legacy_project_inference_as_queries(self):
        service = self.prepare_run_project()
        service.close()
        cli.messages.atomic_json(self.state / "projects/fixture.json", dict(schema=1, projects={}))
        (self.state / "results/fixture-job/dispatch.json").unlink()
        legacy = dict(self.rec, job="fixture-demo-legacy")
        legacy.pop("project")
        cli.save_job(legacy)
        cli.save_job(dict(self.rec, job="fixture-demo-explicit", repo=str(self.root / "absent"),
                          created="2026-10-24 00:00:00"))
        self.start(poll_interval=3600)
        detail = self.request("/v1/jobs/fixture-demo-legacy")[1]
        self.assertEqual(detail["job"]["project"], "demo")
        result = self.post("/v1/runs", dict(project=detail["job"]["project_id"], prompt="继续", host="target"))
        self.assertEqual(result[0], 201, result)
        self.assertEqual(self.request("/v1/jobs/" + result[1]["job"])[1]["job"]["project"], "demo")

    def assert_contract(self, name, result):
        example = json.loads((serve_tests.CONTRACTS / (name + ".json")).read_text())
        self.assertEqual(result[0], example["response"]["status"], result)
        self.assert_structure(result[1], example["response"]["body"])
        if example["events"]:
            response = self.open_events()
            remaining = list(example["events"])
            while remaining:
                _, kind, event = self.next_event(response)
                expected = remaining[0]
                if (kind == expected["type"] and set(expected["data"]) <= set(event["data"])
                        and event["data"].get("round") == expected["data"]["round"]
                        and event["data"].get("job") == result[1].get("job", "fixture-job")):
                    self.assert_structure(event, remaining.pop(0))
            response.close()

    def test_all_write_routes_require_keys_and_invalid_bodies_never_write(self):
        self.agent_seed()
        paths = ["/v1/jobs/fixture-job/rounds/1/ack", "/v1/jobs/fixture-job/rounds/1/review",
                 "/v1/jobs/fixture-job/stop", "/v1/jobs/fixture-job/resume", "/v1/runs"]
        for path in paths:
            with self.subTest(path=path):
                result = self.post(path, {}, None)
                self.assert_contract("write-key-required", result)
                self.assertEqual(self.post(path, [], "invalid-" + path)[0], 400)
        for action, body in (("ack", dict(receipt_id="wrong", acked_by="App")),
                             ("review", dict(review_state="unknown", reviewed_by="App"))):
            self.assertNotEqual(self.post("/v1/jobs/fixture-job/rounds/1/" + action, body, action)[0], 200)
        self.assertIsNone(self.ready()["acknowledgement"])
        self.assertEqual(self.ready()["review_state"], "pending")
        cli.save_job(dict(self.rec, job="private", user_id="another-user"))
        for action in ("stop", "resume", "rounds/1/review", "rounds/1/ack"):
            self.assertEqual(self.post("/v1/jobs/private/" + action, {}, "private-" + action)[0], 404)
        self.assertEqual(self.post("/v1/runs", dict(project="unknown", prompt="继续"), "unknown")[0], 422)

    def test_successful_reject_is_atomic_and_emits_ack_review_then_new_round(self):
        self.agent_seed()
        result = self.post("/v1/jobs/fixture-job/rounds/1/review",
                           dict(review_state="rejected", reviewed_by="App", review_note="继续", model="next-model"))
        self.assertEqual(result[0], 200, result)
        receipt = self.ready()
        self.assertEqual((receipt["review_state"], receipt["review_note"]), ("rejected", "继续"))
        detail = self.request("/v1/jobs/fixture-job")[1]
        self.assertEqual(len(detail["rounds"]), 2)
        self.assertEqual(detail["rounds"][1]["prompt"], "继续")
        self.assertEqual(detail["rounds"][1]["review"]["review_state"], "pending")
        response = self.open_events()
        seen = []
        while True:
            _, kind, event = self.next_event(response)
            if kind in ("receipt.acked", "review.updated"):
                seen.append((kind, event["data"]["round"]))
            if kind == "job.state_changed" and event["data"]["round"] == 2:
                break
        self.assertEqual(seen, [("receipt.acked", 1), ("review.updated", 1)])

    def test_rejection_resource_and_host_failures_leave_existing_ack_and_review_pending(self):
        self.agent_seed()
        receipt = self.ready()
        self.assertEqual(self.post("/v1/jobs/fixture-job/rounds/1/ack",
                                   dict(receipt_id=receipt["receipt_id"], acked_by="original"), "ack")[0], 200)
        ack = self.ready()["acknowledgement"]
        cases = [(dict(fail="probe"), "resume_host_unavailable"),
                 (dict(probe=dict(memfree="1", disk="100")), "resume_resource_check_failed")]
        for override, reason in cases:
            saved = dict(self.target_data, **override)
            cli.messages.atomic_json(self.target_path, saved)
            result = self.post("/v1/jobs/fixture-job/rounds/1/review",
                               dict(review_state="rejected", reviewed_by="App", review_note="继续"), reason)
            self.assertEqual((result[0], result[1]["error"]["reason"]), (422, reason), result)
            self.assertEqual(self.ready()["acknowledgement"], ack)
            self.assertEqual(self.ready()["review_state"], "pending")
            self.assertEqual(len(self.request("/v1/jobs/fixture-job")[1]["rounds"]), 1)

    def test_running_round_and_rejection_of_history_return_round_in_progress(self):
        self.agent_seed()
        cli.save_job(dict(self.rec, rounds=2))
        target = dict(self.target_data)
        target["states"]["fixture-job"] = dict(state="running", round="2")
        cli.messages.atomic_json(self.target_path, target)
        for number, body in [(2, dict(review_state="accepted", reviewed_by="App")),
                             (1, dict(review_state="rejected", reviewed_by="App", review_note="继续"))]:
            result = self.post(f"/v1/jobs/fixture-job/rounds/{number}/review", body, str(number))
            self.assertEqual((result[0], result[1]["error"]["reason"]), (409, "round_in_progress"), result)

    def test_unreachable_target_without_local_receipt_returns_retryable_503(self):
        self.agent_seed()
        receipt = self.ready()
        cli.result_path(self.rec, 1).unlink(missing_ok=True)
        cli.messages.atomic_json(self.target_path, dict(self.target_data, fail="state"))
        for path, body in [("/v1/jobs/fixture-job/rounds/1/ack", dict(receipt_id=receipt["receipt_id"], acked_by="App")),
                           ("/v1/jobs/fixture-job/rounds/1/review", dict(review_state="accepted", reviewed_by="App"))]:
            result = self.post(path, body, path)
            self.assertEqual((result[0], result[1]["error"]["reason"]), (503, "receipt_host_unavailable"), result)
            self.assertEqual(result[1]["error"]["exit_code"], 4)

    def test_resume_cannot_change_harness_and_replays_failure_after_target_changes(self):
        self.agent_seed()
        body = dict(prompt="继续", harness="dsh")
        first = self.post("/v1/jobs/fixture-job/resume", body)
        self.assertEqual((first[0], first[1]["error"]["reason"]), (422, "harness_change_not_allowed"))
        self.assertEqual(self.post("/v1/jobs/fixture-job/resume", body), first)
        self.assertEqual(self.post("/v1/jobs/fixture-job/resume", dict(prompt="changed", harness="codex")),
                         (422, {"error": {"reason": "idempotency_key_reused"}}))
        self.assertEqual(len(self.request("/v1/jobs/fixture-job")[1]["rounds"]), 1)

    def test_result_publication_failure_is_queryable_and_never_reexecutes_the_same_key(self):
        self.agent_seed()
        receipt = self.ready()
        real_replace = os.replace
        publications = []
        def fail_final_result(source, destination):
            if Path(destination).parent.name == "serve-writes":
                publications.append(destination)
                if len(publications) == 2:
                    raise OSError("fixture result storage full")
            return real_replace(source, destination)
        body = dict(receipt_id=receipt["receipt_id"], acked_by="first")
        path = "/v1/jobs/fixture-job/rounds/1/ack"
        with patch.object(os, "replace", side_effect=fail_final_result):
            first = self.post(path, body)
        self.assertEqual(first, (503, {"error": {"reason": "write_outcome_unknown"}}))
        ack = self.ready()["acknowledgement"]
        self.assertEqual(ack["acked_by"], "first")
        self.assertEqual(self.post(path, body), first)
        self.assertEqual(self.post(path, dict(body, extra="different")),
                         (422, {"error": {"reason": "idempotency_key_reused"}}))
        self.assertEqual(self.ready()["acknowledgement"], ack)

    def test_runs_rejects_changed_repository_origin_instead_of_creating_a_project(self):
        self.agent_seed()
        repo = self.root / "changed-repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/new.git"], check=True)
        self.rec.update(repo=str(repo), origin="https://example.invalid/old.git")
        cli.save_job(self.rec)
        identity = cli.projects.project_id("demo", self.rec["origin"])
        result = self.post("/v1/runs", dict(project=identity, prompt="继续", host="target"))
        self.assertEqual((result[0], result[1]["error"]["reason"]), (422, "project_unavailable"), result)

    def test_shutdown_drains_an_accepted_write_and_persists_its_result_for_restart(self):
        service = self.agent_seed()
        receipt = self.ready()
        from concurrent.futures import ThreadPoolExecutor
        import threading
        entered, release = threading.Event(), threading.Event()
        real_run = subprocess.run
        def wait_for_release(argv, *args, **kwargs):
            if isinstance(argv, list) and len(argv) == 3 and argv[1] == "-c" and "execute_cli" in argv[2]:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("写入没有收到测试释放信号")
            return real_run(argv, *args, **kwargs)
        path = "/v1/jobs/fixture-job/rounds/1/ack"
        body = dict(receipt_id=receipt["receipt_id"], acked_by="App")
        with patch.object(subprocess, "run", side_effect=wait_for_release), ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(self.post, path, body)
            try:
                self.assertTrue(entered.wait(5))
                shutdown = pool.submit(service.close)
                self.assertTrue(service.stop.wait(5))
            finally:
                release.set()
            first = write.result(timeout=5)
            shutdown.result(timeout=5)
        self.assertEqual(first[0], 200, first)
        self.assertFalse(self.path.exists())
        self.start(poll_interval=3600)
        self.assertEqual(self.post(path, body), first)
        self.assertEqual(self.post(path, dict(body, extra="different")),
                         (422, {"error": {"reason": "idempotency_key_reused"}}))
        self.assertEqual(self.ready()["acknowledgement"]["acked_by"], "App")

    SUGGESTION = dict(suggest=True, reason=None, line="a", previous="fixture-job", host="target", token="tok-1",
                      fingerprint="f" * 64, expires_at="2026-10-30 00:00:00",
                      items=dict(completed=["a"], current="b", pending=[]))

    def fake_cli(self, suggestion):
        """只替换 serve 子进程里的 suggest/run 边界：记录 argv，返回约定输出；其余命令照常执行。"""
        calls = []
        original = subprocess.run

        def run(argv, *args, **kwargs):
            request = json.loads(kwargs["input"]) if isinstance(kwargs.get("input"), str) and kwargs["input"].startswith("{") else {}
            command = (request.get("argv") or [None])[0]
            if command == "suggest":
                calls.append(request["argv"])
                return subprocess.CompletedProcess(argv, 0, json.dumps(suggestion), "")
            if command == "run":
                calls.append(request["argv"])
                if "--line=a" in request["argv"]:
                    return subprocess.CompletedProcess(argv, 0, "文案可以任意改动\nRD_LINE_WIRED {\"job\": \"fixture-job\", \"line\": \"a\"}\n", "")
                return subprocess.CompletedProcess(argv, 0, "started. job id: fixture-new\n", "")
            return original(argv, *args, **kwargs)
        patcher = patch.object(subprocess, "run", side_effect=run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def run_body(self, **extra):
        return dict(project="demo", prompt="做 b", host="target", plan_item="b", **extra)

    def test_runs_without_a_suggestion_dispatches_as_before(self):
        self.prepare_run_project()
        calls = self.fake_cli(dict(suggest=False, reason="前序派单未验收"))
        reply = self.post("/v1/runs", self.run_body())
        self.assertEqual(reply, (201, dict(job="fixture-new", round=1)))
        self.assertEqual([call[0] for call in calls], ["suggest", "run"])
        self.assertIn("--plan-item=b", calls[1])
        self.assertFalse([a for a in calls[1] if a.startswith(("--line", "--accept", "--no-line"))])

    def test_runs_with_a_suggestion_returns_it_first_without_dispatching(self):
        self.prepare_run_project()
        calls = self.fake_cli(self.SUGGESTION)
        status, body = self.post("/v1/runs", self.run_body())
        self.assertEqual(status, 200, body)
        self.assertEqual(body["suggestion"], self.SUGGESTION)
        self.assert_contract("write-runs-suggestion", (status, body))
        self.assertNotIn("job", body)
        self.assertEqual([call[0] for call in calls], ["suggest"])

    def test_accepting_with_the_token_wires_the_line_under_a_new_key(self):
        self.prepare_run_project()
        calls = self.fake_cli(self.SUGGESTION)
        self.assertEqual(self.post("/v1/runs", self.run_body(), "first")[0], 200)
        cli.save_job(self.rec)  # 前序派单仍在（prepare_run_project 模拟的是清理后的项目）
        accept = self.run_body(suggestion=dict(action="accept", line="a", token="tok-1"))
        status, body = self.post("/v1/runs", accept, "second")
        self.assertEqual((status, body["job"], body["line"]), (201, "fixture-job", "a"), body)
        self.assertEqual(body["round"], 1)
        self.assertEqual(calls[-1][0], "run")
        self.assertIn("--line=a", calls[-1])
        self.assertIn("--accept=tok-1", calls[-1])
        self.assertNotIn("--no-line", calls[-1])

    def test_prose_without_the_marker_line_is_not_treated_as_wired(self):
        self.prepare_run_project()
        self.fake_cli(self.SUGGESTION)
        self.post("/v1/runs", self.run_body(), "first")
        original = subprocess.run

        def run(argv, *args, **kwargs):
            if isinstance(kwargs.get("input"), str) and '"run"' in kwargs["input"]:
                return subprocess.CompletedProcess(argv, 0, "工作线 a：工作项 b 接入 fixture-job（分支 x）\n", "")
            return original(argv, *args, **kwargs)
        with patch.object(subprocess, "run", side_effect=run):
            accept = self.run_body(suggestion=dict(action="accept", line="a", token="tok-1"))
            self.assertEqual(self.post("/v1/runs", accept, "second")[1]["error"]["reason"], "write_outcome_unknown")

    def test_declining_dispatches_the_default_way_and_records_no_line(self):
        self.prepare_run_project()
        calls = self.fake_cli(self.SUGGESTION)
        status, body = self.post("/v1/runs", self.run_body(suggestion=dict(action="decline")), "decline")
        self.assertEqual((status, body), (201, dict(job="fixture-new", round=1)))
        self.assertEqual([call[0] for call in calls], ["run"])
        self.assertIn("--no-line", calls[0])
        self.assertFalse([a for a in calls[0] if a.startswith(("--line", "--accept"))])

    def test_reusing_the_key_for_the_accept_request_is_rejected_and_bad_decisions_are_invalid(self):
        self.prepare_run_project()
        calls = self.fake_cli(self.SUGGESTION)
        self.assertEqual(self.post("/v1/runs", self.run_body(), "same")[0], 200)
        accept = self.run_body(suggestion=dict(action="accept", line="a", token="tok-1"))
        self.assertEqual(self.post("/v1/runs", accept, "same"),
                         (422, dict(error=dict(reason="idempotency_key_reused"))))
        self.assertEqual([call[0] for call in calls], ["suggest"])
        for bad in (dict(action="accept", line="a"), dict(action="other"), "accept"):
            self.assertEqual(self.post("/v1/runs", self.run_body(suggestion=bad), "bad-" + str(bad))[0], 400)
        self.assertEqual(self.post("/v1/runs", dict(project="demo", prompt="x", suggestion=dict(action="decline")),
                                   "no-item")[0], 400)


    def test_large_cli_error_has_a_bounded_display_message_and_replays_first_result(self):
        self.prepare_run_project()
        body = dict(project="demo", prompt="继续", host="target", harness="unknown-" + "x" * 140000)
        first = self.post("/v1/runs", body)
        self.assertEqual(first[0], 422, first[0])
        self.assertTrue(first[1]["error"]["message_truncated"])
        self.assertLessEqual(len(first[1]["error"]["message"]), 600)
        replay = self.post("/v1/runs", body)
        self.assertEqual(replay[0], first[0])
        self.assertEqual(replay[1], first[1])


def outcome_case(outcome, action, execution="done", exit_code="0"):
    def test(self):
        self.agent_seed(outcome=outcome, initial_state=execution, exit_code=exit_code)
        result = self.post("/v1/jobs/fixture-job/rounds/1/review",
                           dict(review_state=action, reviewed_by="App", review_note="继续"))
        allowed = action != "accepted" or (outcome in ("completed", "partial") and execution == "done" and exit_code == "0")
        if allowed:
            self.assertEqual(result[0], 200, result)
            self.assertEqual(self.ready()["review_state"], action)
            self.assertEqual(self.ready()["acknowledgement"]["via"], "serve")
            if action in ("accepted", "voided"):
                resumed = self.post("/v1/jobs/fixture-job/resume", dict(prompt="再继续"), "after-review")
                self.assertEqual(resumed[0], 200, resumed)
            self.assertEqual(self.request("/v1/jobs/fixture-job")[1]["rounds"][-1]["review"]["review_state"], "pending")
        else:
            self.assertEqual((result[0], result[1]["error"]["reason"]), (409, "review_not_allowed"), result)
            self.assertIsNone(self.ready()["acknowledgement"])
            self.assertEqual(self.ready()["review_state"], "pending")
    return test


for label, outcome, execution, exit_code in [
    ("completed", "completed", "done", "0"), ("partial", "partial", "done", "0"),
    ("blocked", "blocked", "done", "0"), ("failed", "failed", "done", "0"),
    ("dead", "failed", "dead", ""), ("missing", "failed", "missing", ""),
    ("nonzero_exit", "completed", "done", "7"),
]:
    for action in ("accepted", "rejected", "voided"):
        setattr(ServeWriteTests, "test_review_rules_" + label + "_" + action,
                outcome_case(outcome, action, execution, exit_code))
