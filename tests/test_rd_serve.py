"""Unix socket 上的 HTTP/SSE 契约与 CLI 行为；不监听 TCP。"""
import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import time
import tempfile
import threading
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_events as events
from remote_dispatch.rd_serve import Service


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "v1"


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class ServeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-serve-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.config.write_text("", encoding="utf-8")
        self.path = self.root / "serve.sock"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(cli, "STATE", str(self.state)))
        stack.enter_context(patch.object(cli, "CONFIG", str(self.config)))
        stack.enter_context(patch.dict(os.environ, {"RD_USER_ID": "fixture"}))
        # Session collection must never contact a user's real Herdr in these tests.
        binary = self.root / "bin"
        binary.mkdir()
        herdr = binary / "herdr"
        herdr.write_text("#!" + sys.executable + "\nimport sys\n"
                         "if sys.argv[1:] != ['agent', 'list']: sys.exit(70)\n"
                         "print('{\"id\":\"cli:agent:list\",\"result\":{\"agents\":[]}}')\n")
        herdr.chmod(0o700)
        stack.enter_context(patch.dict(os.environ, {
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "HERDR_CONFIG_PATH": str(self.root / "herdr" / "config.toml"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"), "CODEX_HOME": str(self.root / "codex")}))

    def start(self, **options):
        service = Service(cli.Config(), self.path, **options)
        service.start()
        self.addCleanup(service.close)
        return service

    def request(self, path, method="GET", body=None, headers=None):
        connection = UnixHTTPConnection(self.path)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def seed(self, poll_interval=0.05, running=False):
        self.config.write_text(f"[host:target]\nssh=local\nworkdir={self.root / 'worker'}\nsync=direct\n"
                               "env_file=/private/fixture-env\ncodex_auth_file=/private/fixture-auth\n"
                               "ssh_opts=-o SecretOption=fixture-ssh\n", encoding="utf-8")
        # Only the OS/resource boundary is replaced; job/report/log reads use real shell + files.
        probe = patch.object(cli, "PROBE", 'echo "os=Darwin ncpu=8 load1=1 memfree=80 disk=100 running=0 heavy=0"')
        probe.start()
        self.addCleanup(probe.stop)
        self.rec = dict(job="fixture-job", user_id="fixture", host="target", rounds=1,
                        project="demo", origin="", initiator_session="original-session",
                        initiator_harness="codex", initiator_host="controller", agent=None,
                        handoff_version=1, sync="direct", base="base", branch="ticket/demo",
                        created="2026-10-25 00:00:00", env_file="/private/fixture-env",
                        agent_auth_file="/private/fixture-auth", ssh_opts="fixture-ssh")
        cli.save_job(self.rec)
        self.worker = self.root / "worker" / "users" / "fixture" / "jobs" / "fixture-job"
        self.worker.mkdir(parents=True)
        (self.worker / "run.r1.sh").write_text("")
        (self.worker / "started.r1").write_text("2026-10-25T00:00:01Z")
        if running:
            (self.worker / "pid").write_text(str(os.getpid()))
        else:
            (self.worker / "exit_code").write_text("0")
        (self.worker / "head").write_text("base")
        (self.worker / "dirty").write_text("0")
        (self.worker / "stdout.r1.log").write_text("first\nsecond\nthird\n", encoding="utf-8")
        (self.worker / "stderr.r1.log").write_text("error line\n", encoding="utf-8")
        cli.messages.report(self.worker, "fixture-job", 1, "result", summary="完成", checks="passed")
        if running:
            cli.messages.report(self.worker, "fixture-job", 1, "progress", phase="implementing", summary="实现中")
        other = dict(self.rec, job="other-job", user_id="other", initiator_session="other-session")
        cli.save_job(other)
        return self.start(poll_interval=poll_interval)

    def eventually(self, read, predicate):
        deadline = time.monotonic() + 5
        value = None
        while time.monotonic() < deadline:
            value = read()
            if predicate(value):
                return value
            time.sleep(0.02)
        self.fail(f"未等到后台采集结果：{value!r}")

    def snapshot(self):
        return {str(p.relative_to(self.state)): (p.stat().st_mtime_ns, p.read_bytes())
                for p in self.state.rglob("*") if p.is_file()}

    def test_current_user_can_read_cards_details_historical_receipt_and_log_tail_without_writes(self):
        service = self.seed()
        cards = self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                                lambda rows: rows and rows[0]["outcome"] == "completed")
        self.assertEqual([row["job"] for row in cards], ["fixture-job"])
        self.assertEqual((cards[0]["round"], cards[0]["delivery_state"], cards[0]["review_state"]),
                         (1, "pending", "pending"))
        # Pause collection via its public lifecycle, then restart with a long poll interval.
        service.close()
        service = self.start(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                        lambda rows: rows and rows[0]["outcome"] == "completed")
        before = self.snapshot()
        detail = self.request("/v1/jobs/fixture-job")[1]
        receipt = self.request("/v1/jobs/fixture-job/rounds/1/receipt")[1]
        logs = self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/logs?stream=out&tail=2")[1],
                               lambda row: row.get("snapshot_at") is not None)
        self.assertEqual(detail["job"]["initiator_session"], "original-session")
        self.assertEqual(detail["rounds"][0]["receipt"]["receipt_id"], receipt["receipt"]["receipt_id"])
        self.assertEqual(receipt["receipt"]["report"]["summary"], "完成")
        self.assertEqual(logs["text"], "second\nthird\n")
        error_log = self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/logs?stream=err&tail=1")[1],
                                    lambda row: row.get("snapshot_at") is not None)
        self.assertEqual(error_log["text"], "error line\n")
        self.assertEqual(self.request("/v1/jobs/other-job")[0], 404)
        self.assertEqual(self.request("/v1/jobs/other-job/rounds/1/receipt")[0], 404)
        self.assertEqual(self.request("/v1/jobs/other-job/rounds/1/logs")[0], 404)
        self.assertEqual(self.snapshot(), before)

    def open_events(self, path="/v1/events", headers=None):
        connection = UnixHTTPConnection(self.path)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        return response

    def test_detail_replays_sorted_progress_from_rotated_log_for_each_round(self):
        service = self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/receipt"),
                        lambda result: result[0] == 200)
        service.close()
        receipt_path = cli.result_path(self.rec, 1)
        receipt = cli.messages.read_json(receipt_path)
        receipt["report"]["started_at"] = "2026-10-25T00:00:01Z"
        cli.messages.atomic_json(receipt_path, receipt)
        cli.save_job(dict(self.rec, rounds=2))
        cli.messages.atomic_json(receipt_path.with_suffix(".review.json"), dict(
            review_state="rejected", reviewed_by="App", reviewed_at="2026-10-25T00:01:00Z",
            review_note="请补充测试"))
        log = events.EventLog(self.state, max_bytes=1)
        for job, user, number, phase, stamp in (
                ("fixture-job", "fixture", 1, "later", "2026-10-25T00:00:20Z"),
                ("other-job", "other", 1, "private", "2026-10-25T00:00:05Z"),
                ("fixture-job", "other", 1, "wrong-user", "2026-10-25T00:00:06Z"),
                ("fixture-job", "fixture", 1, "earlier", "2026-10-25T00:00:10Z"),
                ("fixture-job", "fixture", 2, "resuming", "2026-10-25T00:01:10Z")):
            with patch.object(events.messages, "utc_now", return_value=stamp):
                log.append("progress.updated", dict(job=job, user_id=user, round=number,
                                                   phase=phase, summary=phase, updated_at=stamp))
        self.start(poll_interval=3600)
        detail = self.request("/v1/jobs/fixture-job")[1]
        first, second = detail["rounds"]
        self.assertEqual(first["started_at"], "2026-10-25T00:00:01Z")
        self.assertEqual(second["prompt"], "请补充测试")
        self.assertEqual([row["data"]["phase"] for row in first["progress_events"]], ["earlier", "later"])
        self.assertEqual([row["data"]["phase"] for row in second["progress_events"]], ["resuming"])
        response = self.open_events()
        replay = {}
        while len(replay) < 3:
            seq, kind, row = self.next_event(response)
            if kind == "progress.updated":
                replay[seq] = row
        for row in first["progress_events"] + second["progress_events"]:
            self.assertEqual(row, replay[row["seq"]])

    def test_receipt_and_detail_return_file_changes_or_explicitly_mark_missing_data(self):
        service = self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/receipt"),
                        lambda result: result[0] == 200)
        path = cli.result_path(self.rec, 1)
        for value, expected, available in (
                (None, [], False),
                ([], [], True),
                ([dict(path="src/demo.py", additions=12, deletions=3, token="hidden"),
                  dict(path="image.png", additions=None, deletions=None)],
                 [dict(path="src/demo.py", additions=12, deletions=3),
                  dict(path="image.png", additions=None, deletions=None)], True)):
            receipt = cli.messages.read_json(path)
            if value is not None:
                receipt["report"]["changed_files"] = value
            cli.messages.atomic_json(path, receipt)
            with self.subTest(value=value):
                for row in (self.request("/v1/jobs/fixture-job/rounds/1/receipt")[1]["receipt"],
                            self.request("/v1/jobs/fixture-job")[1]["rounds"][0]["receipt"]):
                    self.assertEqual(row["changed_files"], expected)
                    self.assertEqual(row["changed_files_available"], available)
                if available and expected:
                    example = json.loads((CONTRACTS / "receipt-changes.json").read_text())
                    status, body = self.request(example["request"]["path"])
                    self.assertEqual(status, example["response"]["status"])
                    self.assert_structure(body, example["response"]["body"])
        service.close()
        self.start(poll_interval=3600)
        self.assertEqual(self.request("/v1/jobs/fixture-job/rounds/1/receipt")[1]["receipt"]["changed_files"], expected)

    def test_running_detail_preserves_full_prompt_and_start_time_without_a_receipt(self):
        self.seed(poll_interval=3600, running=True)
        prompt = "请实现功能\n" * 200
        cli.save_job(dict(self.rec, round_prompts={"1": prompt}))
        detail = self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1],
                                 lambda row: row["rounds"][0]["started_at"] is not None)
        self.assertEqual(detail["prompt"], prompt)
        self.assertEqual(detail["rounds"][0]["prompt"], prompt)
        self.assertEqual(detail["rounds"][0]["started_at"], "2026-10-25T00:00:01Z")
        self.assertIsNone(detail["rounds"][0]["receipt"])
        self.assertEqual(detail["rounds"][0]["progress_events"][0]["data"]["summary"], "实现中")
        example = json.loads((CONTRACTS / "job-details-timeline.json").read_text())
        self.assert_structure(detail, example["response"]["body"])

    def next_event(self, response):
        fields = {}
        while True:
            raw = response.readline()
            self.assertTrue(raw, "SSE 在收到下一条事件前关闭")
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if "data" in fields:
                    return int(fields["id"]), fields["event"], json.loads(fields["data"])
                continue
            if not line.startswith(":"):
                key, value = line.split(":", 1)
                fields[key] = value.lstrip()

    def test_sse_delivers_new_events_and_reconnect_replays_every_missing_sequence(self):
        self.start()
        response = self.open_events()
        log = events.EventLog(self.state, max_bytes=1)
        log.append("job.created", dict(job="fixture", user_id="fixture", round=1))
        first = self.next_event(response)
        self.assertEqual(first[:2], (1, "job.created"))
        self.assertEqual(first[2]["seq"], 1)
        response.close()
        log.append("job.created", dict(job="private", user_id="other", round=1))
        log.append("receipt.ready", dict(job="fixture", user_id="fixture", round=1, receipt_id="receipt"))
        log.append("receipt.acked", dict(job="fixture", user_id="fixture", round=1, receipt_id="receipt", via="serve"))
        response = self.open_events("/v1/events?after=0", {"Last-Event-ID": "1"})
        self.assertEqual([self.next_event(response)[0] for _ in range(2)], [3, 4])
        log.append("review.updated", dict(job="fixture", user_id="fixture", round=1, review_state="accepted"))
        self.assertEqual(self.next_event(response)[0], 5)
        self.assertEqual(self.request("/v1/events", headers={"Last-Event-ID": "bad"})[0], 400)
        self.assertEqual(self.request("/v1/events?after=-1")[0], 400)

    def command(self, *argv, input=None, cwd=None, **environment):
        env = dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(self.config),
                   RD_USER_ID="fixture", RD_SERVE_SOCKET=str(self.path),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), **environment)
        return subprocess.run([sys.executable, "-m", "remote_dispatch", *argv], env=env,
                              capture_output=True, text=True, timeout=10, input=input, cwd=cwd)

    def test_cli_ack_is_written_by_serve_then_falls_back_after_shutdown_with_same_job_lock(self):
        service = self.seed(poll_interval=3600)
        receipt = self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/receipt"),
                                  lambda result: result[0] == 200)[1]["receipt"]
        argv = ["ack", "fixture-job", "--round", "1", "--receipt-id", receipt["receipt_id"], "--via", "cli"]
        result = self.command(*argv, RD_INITIATOR_SESSION="cli-session")
        self.assertEqual(result.returncode, 0, result.stderr)
        ack = self.request("/v1/jobs/fixture-job/rounds/1/receipt")[1]["receipt"]["acknowledgement"]
        self.assertEqual((ack["acked_by"], ack["via"]), ("cli-session", "serve"))
        example = json.loads((CONTRACTS / "cli-ack.json").read_text())
        request = example["request"]["body"]
        request.update(cwd=str(self.root), config=str(self.config), initiator_session="cli-session")
        request["argv"][-1] = receipt["receipt_id"]
        status, reply = self.request(example["request"]["path"], "POST", json.dumps(request),
                                     {"Content-Type": "application/json"})
        self.assertEqual(status, example["response"]["status"])
        self.assert_structure(reply, example["response"]["body"])
        self.assertEqual(reply["exit_code"], 0)
        response = self.open_events()
        while True:
            _, kind, event = self.next_event(response)
            if kind == "receipt.acked":
                break
        self.assert_structure(event, example["events"][0])
        response.close()
        from remote_dispatch.rd_lock import JobBusy, JobLock
        held = JobLock(self.state, "fixture-job")
        def take_lock():
            try:
                return held.acquire()
            except JobBusy:
                return None
        self.eventually(take_lock, bool)
        with contextlib.ExitStack() as lock_scope:
            lock_scope.callback(held.release)
            result = self.command(*argv)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("round_in_progress", result.stderr)
        service.close()
        self.assertFalse(self.path.exists())
        with JobLock(self.state, "fixture-job"):
            result = self.command(*argv)
        self.assertEqual(result.returncode, 4)
        result = self.command(*argv, RD_INITIATOR_SESSION="cli-session")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.command("result", "fixture-job", "--initiator-session", "original-session")
        self.assertEqual(json.loads(result.stdout)["acknowledgement"]["via"], "cli")

    def assert_structure(self, actual, example, nullable=False):
        if example is None:
            return  # Null examples represent optional/nullable display fields.
        if actual is None:
            self.assertTrue(nullable, "必需的契约对象或字段不能为 null")
            return
        self.assertIsInstance(actual, type(example))
        if isinstance(example, dict):
            self.assertTrue(set(example) <= set(actual), (set(example), set(actual)))
            for key, value in example.items():
                self.assert_structure(actual[key], value, nullable=key in {
                    "report", "progress", "execution_state", "load1", "ncpu", "memfree", "disk", "running", "heavy",
                    "additions", "deletions", "snapshot_at"})
        elif isinstance(example, list) and example:
            self.assertTrue(actual)
            for row in actual:
                self.assert_structure(row, example[0])

    def test_http_and_sse_responses_follow_the_shared_v1_examples(self):
        self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                        lambda rows: rows and rows[0]["outcome"] == "completed")
        self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"], bool)
        for name in ("hosts-populated", "jobs", "job-details", "receipt", "logs-out", "logs-err", "not-found", "invalid-request"):
            with self.subTest(contract=name):
                example = json.loads((CONTRACTS / (name + ".json")).read_text())
                request = example["request"]
                status, body = self.request(request["path"], request["method"])
                self.assertEqual(status, example["response"]["status"])
                self.assert_structure(body, example["response"]["body"])
        example = json.loads((CONTRACTS / "events.json").read_text())
        response = self.open_events(example["request"]["path"], example["request"]["headers"])
        while True:
            _, kind, body = self.next_event(response)
            if kind == "receipt.ready":
                break
        self.assert_structure(body, example["events"][0])

    def test_every_read_response_and_event_omits_configuration_and_scrubs_secrets_in_text(self):
        self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/receipt"),
                        lambda result: result[0] == 200)
        path = cli.result_path(self.rec, 1)
        receipt = cli.messages.read_json(path)
        receipt.update(env_file="private-marker", agent_auth_file="private-marker", ssh_opts="private-marker")
        receipt["report"].update(token="private-marker", evidence={"checks": [], "artifacts": [],
                                "apiKey": "private-marker", "agent_auth_file": "private-marker"})
        cli.messages.atomic_json(path, receipt)
        private_text = ("env_file=/private/fixture-env agent_auth_file=/private/fixture-auth "
                        "ssh_opts='-o SecretOption=fixture-ssh' token=private-marker private_key=private-marker\n"
                        "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-marker\n-----END OPENSSH PRIVATE KEY-----")
        cli.save_job(dict(self.rec, rounds=2, round_prompts={"1": private_text, "2": private_text}))
        cli.messages.atomic_json(path.with_suffix(".review.json"), dict(
            review_state="rejected", reviewed_by="App", reviewed_at="2026-10-25T00:01:00Z",
            review_note=private_text))
        receipt["report"]["changed_files"] = [dict(path="/private/fixture-env", additions=1, deletions=0,
                                                 agent_auth_file="private-marker")]
        cli.messages.atomic_json(path, receipt)
        events.EventLog(self.state).append("progress.updated", dict(
            job="fixture-job", user_id="fixture", round=1, phase=private_text, summary=private_text,
            updated_at="2026-10-25T00:00:10Z", agent_auth_file="private-marker"))
        (self.worker / "stdout.r1.log").write_text(
            "env_file=/private/fixture-env agent_auth_file=/private/fixture-auth "
            "ssh_opts=-o SecretOption=fixture-ssh\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-marker\n-----END OPENSSH PRIVATE KEY-----\n")
        for route in ("/v1/hosts", "/v1/jobs", "/v1/jobs/fixture-job",
                      "/v1/jobs/fixture-job/rounds/1/receipt",
                      "/v1/jobs/fixture-job/rounds/1/logs?stream=out",
                      "/v1/jobs/fixture-job/rounds/1/logs?stream=err",
                      "/v1/jobs/other-job", "/unknown", "/v1/jobs/fixture-job/rounds/1/logs?tail=-1"):
            with self.subTest(route=route):
                if "/logs?stream=" in route:
                    body = self.eventually(lambda: self.request(route)[1],
                                           lambda row: row.get("snapshot_at") is not None)
                    if "stream=out" in route:
                        self.assertIn("[redacted]", body["text"])
                else:
                    body = self.request(route)[1]
                encoded = json.dumps(body).lower()
                for forbidden in ("env_file", "auth_file", "ssh", "private-marker", "/private/fixture", "apikey", "private key"):
                    self.assertNotIn(forbidden, encoded)
        response = self.open_events()
        _, _, event = self.next_event(response)
        self.assertNotIn("private-marker", json.dumps(event))

    def test_forwarded_run_preserves_cwd_stdin_and_origin_and_stop_review_project_writes(self):
        self.config.write_text(f"[host:target]\nssh=local\nworkdir={self.root / 'worker'}\nsync=direct\n")
        repo = self.root / "repo with spaces"
        repo.mkdir()
        for argv in (["init", "-q"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"], ["commit", "--allow-empty", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
        self.start(poll_interval=3600)
        result = self.command("-C", str(repo), "run", "--cmd", "sleep 60", "--host", "target", "--force-host",
                              "--project", "demo", RD_INITIATOR_SESSION="sender", HERDR_PANE_ID="pane-19")
        self.assertEqual(result.returncode, 0, result.stderr)
        cards = self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"], bool)
        job = cards[0]["job"]
        self.addCleanup(self.command, "stop", job)
        self.assertEqual(cards[0]["initiator_session"], "sender")
        self.assertEqual(cards[0]["initiator_herdr"]["pane_id"], "pane-19")
        project = cards[0]["project_id"]
        for action in ("hide", "show"):
            result = self.command("project", action, project)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["hidden"], action == "hide")
        result = self.command("stop", job, RD_INITIATOR_SESSION="stopper")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.request(f"/v1/jobs/{job}/rounds/1/receipt")[1]["receipt"]
        self.assertEqual((receipt["stopped_by"], receipt["acknowledgement"]["via"]), ("stopper", "serve"))
        result = self.command("review", job, "--round", "1", "--void", RD_INITIATOR_SESSION="reviewer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.request(f"/v1/jobs/{job}/rounds/1/receipt")[1]["receipt"]["review_state"], "voided")

    def test_queries_do_not_attach_a_previous_round_snapshot_or_receipt_to_a_new_round(self):
        self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                        lambda rows: rows and rows[0]["outcome"] == "completed")
        # The local record can advance before the next remote observation arrives.
        cli.save_job(dict(self.rec, rounds=2))
        card = self.request("/v1/jobs")[1]["jobs"][0]
        self.assertEqual((card["round"], card["execution_state"], card["outcome"]), (2, "starting", None))
        self.assertIsNone(self.request("/v1/jobs/fixture-job")[1]["rounds"][1]["receipt"])
        self.assertEqual(self.request("/v1/jobs/fixture-job/rounds/1/receipt")[0], 200)

    def test_cli_forwards_resume_stdin_and_exit_code_without_retry_after_connection_loss(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen()
        self.addCleanup(listener.close)
        requests = []
        def accept(reply):
            with listener.accept()[0] as client:
                stream = client.makefile("rb")
                with stream:
                    start = stream.readline()
                    headers = {}
                    while True:
                        line = stream.readline()
                        if line == b"\r\n":
                            break
                        key, value = line.decode().split(":", 1)
                        headers[key.lower()] = value.strip()
                    requests.append((start, json.loads(stream.read(int(headers["content-length"])))))
                if reply:
                    body = json.dumps(dict(exit_code=7, stdout="forwarded\n", stderr="busy\n")).encode()
                    client.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        for reply in (True, False):
            worker = threading.Thread(target=accept, args=(reply,), daemon=True)
            worker.start()
            result = self.command("resume", "fixture-job", "--prompt-file", "-", input="请继续\n第二行", cwd=self.root,
                                  RD_INITIATOR_SESSION="caller")
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            if reply:
                self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "forwarded\n", "busy\n"))
            else:
                self.assertEqual(result.returncode, 4)
                self.assertIn("cli_forward_failed", result.stderr)
                self.assertNotIn("unknown job", result.stderr)
        self.assertEqual(requests[0][0], b"POST /_cli/v1 HTTP/1.1\r\n")
        self.assertEqual(requests[0][1]["stdin"], "请继续\n第二行")
        self.assertEqual(requests[0][1]["cwd"], str(self.root))
        self.assertEqual(requests[0][1]["initiator_session"], "caller")

    def test_serve_cli_starts_and_terminates_cleanly_and_rejects_invalid_intervals(self):
        env = dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(self.config), RD_USER_ID="fixture",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        process = subprocess.Popen([sys.executable, "-m", "remote_dispatch", "serve", "--socket", str(self.path)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            def ready():
                try:
                    return self.request("/v1/hosts")
                except (OSError, http.client.HTTPException):
                    return None
            self.eventually(ready, bool)
            process.terminate()
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, (out, err))
            self.assertFalse(self.path.exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        for value in ("0", "-1", "nan", "inf"):
            result = self.command("serve", "--poll-interval", value)
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn("invalid_poll_interval", result.stderr)

    def test_forwarded_command_without_a_session_does_not_inherit_the_service_session(self):
        self.seed(poll_interval=3600)
        receipt = self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/receipt"),
                                  lambda result: result[0] == 200)[1]["receipt"]
        with patch.dict(os.environ, {"RD_INITIATOR_SESSION": "service-session"}):
            result = self.command("ack", "fixture-job", "--round", "1", "--receipt-id", receipt["receipt_id"],
                                  RD_INITIATOR_SESSION="", CODEX_THREAD_ID="", CLAUDE_CODE_SESSION_ID="",
                                  DSH_SESSION_ID="", ANTIGRAVITY_CONVERSATION_ID="")
        self.assertEqual(result.returncode, 0, result.stderr)
        acknowledgement = self.request("/v1/jobs/fixture-job/rounds/1/receipt")[1]["receipt"]["acknowledgement"]
        self.assertEqual(acknowledgement["acked_by"], "cli")

    def test_background_collection_exposes_running_progress_and_publishes_the_finished_receipt(self):
        self.seed(running=True)
        detail = self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1],
                                 lambda row: row["job"]["execution_state"] == "running")
        self.assertEqual(detail["rounds"][0]["execution_state"], "running")
        self.assertEqual(detail["rounds"][0]["progress"]["summary"], "实现中")
        response = self.open_events()
        (self.worker / "exit_code").write_text("0")
        kinds = []
        while True:
            _, kind, _ = self.next_event(response)
            kinds.append(kind)
            if kind == "receipt.ready":
                break
        self.assertIn("progress.updated", kinds)
        receipt = self.request("/v1/jobs/fixture-job/rounds/1/receipt")[1]["receipt"]
        self.assertEqual(receipt["report"]["checks"], "passed")

    def test_progress_between_polls_is_complete_sorted_and_survives_restart(self):
        service = self.seed(poll_interval=3600, running=True)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1],
                        lambda row: row["rounds"][0]["progress"] is not None)
        with patch.object(cli.messages, "utc_now", return_value="2026-10-25T00:00:03Z"):
            cli.messages.report(self.worker, "fixture-job", 1, "progress", "checking", "检查中")
        with patch.object(cli.messages, "utc_now", return_value="2026-10-25T00:00:02Z"):
            cli.messages.report(self.worker, "fixture-job", 1, "progress", "building", "构建中")
        service.wake.set()
        path = "/v1/jobs/fixture-job/rounds/1/progress"
        status, body = self.eventually(lambda: self.request(path),
                                       lambda row: row[0] == 200 and len(row[1]["progress"]) == 3)
        self.assertEqual(status, 200)
        self.assertEqual([row["phase"] for row in body["progress"]], ["building", "checking", "implementing"])
        example = json.loads((CONTRACTS / "progress.json").read_text())
        self.assert_structure(body, example["response"]["body"])
        detail = self.request("/v1/jobs/fixture-job")[1]
        self.assertEqual([row["data"]["phase"] for row in detail["rounds"][0]["progress_events"]],
                         ["building", "checking", "implementing"])
        before = len(events.read_after(self.state, 0))
        service.close()
        self.start(poll_interval=3600)
        self.assertEqual(self.request(path), (status, body))
        self.assertEqual(self.request("/v1/jobs/other-job/rounds/1/progress")[0], 404)
        self.assertEqual(self.request("/v1/jobs/fixture-job/rounds/2/progress")[0], 404)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1],
                        lambda row: row["rounds"][0]["progress"] is not None)
        self.assertEqual(len(events.read_after(self.state, 0)), before)

    def test_progress_legacy_latest_and_no_progress_are_read_without_remote_queries(self):
        service = self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1],
                        lambda row: row["job"]["outcome"] == "completed")
        path = "/v1/jobs/fixture-job/rounds/1/progress"
        self.assertEqual(self.request(path), (200, {"progress": []}))
        latest = cli.messages.report(self.worker, "fixture-job", 1, "progress", "legacy", "旧任务最新进度")
        (self.worker / "reports/progress.r1.jsonl").unlink()
        service.wake.set()
        expected = {"progress": [dict(updated_at=latest["updated_at"], phase="legacy", summary="旧任务最新进度")]}
        self.eventually(lambda: self.request(path), lambda row: row == (200, expected))
        service.close()
        with patch.object(cli.native, "request", side_effect=ValueError("offline")):
            self.start(poll_interval=3600)
            before = self.snapshot()
            self.assertEqual(self.request(path), (200, expected))
            self.assertEqual(self.snapshot(), before)

    def test_progress_history_new_fields_are_projected_and_sensitive_text_is_scrubbed(self):
        service = self.seed(poll_interval=3600, running=True)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1],
                        lambda row: row["rounds"][0]["progress"] is not None)
        secret = "token=private-marker env_file=/private/fixture-env"
        cli.messages.report(self.worker, "fixture-job", 1, "progress", secret, secret)
        path = self.worker / "reports/progress.r1.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1].update(auth_file="private-marker", percentage=50)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        service.wake.set()
        body = self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/progress")[1],
                               lambda row: len(row["progress"]) == 2)
        for row in body["progress"]:
            self.assertEqual(set(row), {"updated_at", "phase", "summary"})
        self.assertIn("[redacted]", body["progress"][-1]["summary"])
        cache = self.state / "serve-progress/fixture-job/r1.json"
        for value in (body, cli.messages.read_json(cache)):
            for forbidden in ("token", "env_file", "auth_file", "private-marker", "/private/fixture", "percentage"):
                self.assertNotIn(forbidden, json.dumps(value))

    def test_large_history_remains_available_for_prior_round_after_resume(self):
        service = self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs/fixture-job")[1],
                        lambda row: row["job"]["outcome"] == "completed")
        service.close()
        for index in range(7):
            cli.messages.report(self.worker, "fixture-job", 1, "progress", str(index), "中" * 8000)
        # Start collecting after a resume, with no progress yet in the new round.
        cli.save_job(dict(self.rec, rounds=2))
        (self.worker / "run.r2.sh").write_text("")
        self.start(poll_interval=3600)
        path = "/v1/jobs/fixture-job/rounds/1/progress"
        expected = self.eventually(lambda: self.request(path),
                                   lambda row: row[0] == 200 and len(row[1]["progress"]) == 7)
        self.assertEqual([row["phase"] for row in expected[1]["progress"]], ["0", "1", "2", "3", "4", "5", "6"])
        self.assertEqual(self.request("/v1/jobs/fixture-job/rounds/2/progress"), (200, {"progress": []}))

    def test_resume_collects_final_progress_of_an_already_cached_prior_round(self):
        service = self.seed(poll_interval=3600, running=True)
        path = "/v1/jobs/fixture-job/rounds/1/progress"
        self.eventually(lambda: self.request(path), lambda row: row[0] == 200 and len(row[1]["progress"]) == 1)
        cli.messages.report(self.worker, "fixture-job", 1, "progress", "finished", "前轮完成")
        cli.save_job(dict(self.rec, rounds=2))
        (self.worker / "run.r2.sh").write_text("")
        service.wake.set()
        result = self.eventually(lambda: self.request(path),
                                 lambda row: row[0] == 200 and len(row[1]["progress"]) == 2)
        self.assertEqual([row["phase"] for row in result[1]["progress"]], ["implementing", "finished"])

    def test_log_endpoint_does_not_follow_a_link_to_a_private_file(self):
        self.seed(poll_interval=3600)
        private = self.root / "private-data"
        private.write_text("private-marker")
        log = self.worker / "stdout.r1.log"
        log.unlink()
        log.symlink_to(private)
        status, body = self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/logs"),
                                       lambda row: row[0] == 503)
        self.assertEqual(status, 503)
        self.assertNotIn("private-marker", json.dumps(body))

    def test_service_rejects_a_second_writer_and_recovers_a_stale_socket_without_deleting_files(self):
        service = self.start()
        with self.assertRaisesRegex(ValueError, "serve_already_running"):
            Service(cli.Config(), self.path).start()
        self.assertEqual(self.request("/v1/hosts"), (200, {"hosts": []}))
        service.close()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()
        recovered = self.start()
        self.assertEqual(self.request("/v1/hosts")[0], 200)
        recovered.close()
        self.path.write_text("keep me")
        with self.assertRaisesRegex(ValueError, "socket_path_in_use"):
            Service(cli.Config(), self.path).start()
        self.assertEqual(self.path.read_text(), "keep me")

    def test_hosts_contract_is_readable_and_service_removes_its_private_socket(self):
        service = self.start()
        contract = json.loads((CONTRACTS / "hosts.json").read_text())
        self.assertEqual(self.request(contract["request"]["path"]),
                         (contract["response"]["status"], contract["response"]["body"]))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        service.close()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
