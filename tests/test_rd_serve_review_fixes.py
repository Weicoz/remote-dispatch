"""桌面 v3 审查回归；仅临时状态、假 Herdr／发送器与回环随机端口。"""
import base64
import io
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli, rd_events as events, rd_native
from remote_dispatch.rd_serve import Service
from remote_dispatch.rd_serve_writes import ROUTE
from tests import test_rd_serve as serve_base
from tests import test_rd_serve_network as network_base
from tests import test_rd_serve_sessions as sessions_base


class NetworkFixTests(unittest.TestCase):
    setUp = network_base.NetworkTests.setUp
    start = network_base.NetworkTests.start
    request = network_base.NetworkTests.request
    configure_network = network_base.NetworkTests.configure_network
    network_info = network_base.NetworkTests.network_info
    tls = network_base.NetworkTests.tls
    pair = network_base.NetworkTests.pair

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture requires openssl")
    def test_pair_secrets_are_never_cached_and_replay_is_closed_across_restart(self):
        service = self.start()
        window = self.request("/v1/pairing-window", "POST", {}, key="window")[1]
        storage = self.state / "network/devices.json"
        self.assertNotIn(window["code"], storage.read_text())
        body = dict(code=window["code"], name="Phone")
        first = self.request("/v1/pair", "POST", body, key="pair")
        self.assertEqual(first[0], 200)
        for secret in (window["code"], first[1]["device_token"]):
            self.assertNotIn(secret, storage.read_text())
        example = json.loads((serve_base.CONTRACTS / "network-pair-replay.json").read_text())
        closed = example["response"]["status"], example["response"]["body"]
        for restart in (False, True):
            if restart:
                service.close()
                self.start()
            self.assertEqual(self.request("/v1/pair", "POST", body, key="pair"), closed)
            self.assertEqual(self.request("/v1/pairing-window", "POST", {}, key="window"), closed)
            self.assertNotIn("device_token", json.dumps(self.request("/v1/pair", "POST", body, key="pair")))

    def test_start_scrubs_legacy_pairing_cache(self):
        path = self.state / "network/devices.json"
        cli.messages.atomic_json(path, dict(devices={}, requests={
            "old-pair": [200, dict(device_token="legacy-device-secret")],
            "old-window": [200, dict(code="legacy-pair-code")]}))
        self.start()
        saved = path.read_text()
        self.assertNotIn("legacy-device-secret", saved)
        self.assertNotIn("legacy-pair-code", saved)

    def test_network_key_is_bound_to_method_path_and_canonical_body(self):
        service = self.start()
        net = service.network
        body = dict(listen_addresses=[])
        first = net.write("/v1/network", body, "used-key")
        self.assertEqual(first[0], 200)
        self.assertEqual(net.write("/v1/network", body, "used-key"), first)
        example = json.loads((serve_base.CONTRACTS / "network-idempotency-reused.json").read_text())
        expected = example["response"]["status"], example["response"]["body"]
        for path, method, changed in (("/v1/devices/unknown", "POST", body),
                                      ("/v1/network", "DELETE", body),
                                      ("/v1/network", "POST", dict(listen_addresses=[], extra=True))):
            self.assertEqual(net.write(path, changed, "used-key", method=method), expected)
        self.assertEqual(net.write("/v1/network", body, "used-key", device_id="other"), first)

    def test_push_failure_backs_off_without_rereading_and_success_resumes_cursor(self):
        service = Service(cli.Config(), self.path)
        self.addCleanup(service.close)
        log = events.EventLog(self.state)
        first = log.append("job.created", dict(job="one", user_id="fixture"))
        waits, batches = [], []
        class Stop:
            def is_set(self):
                return len(waits) >= 12
            def wait(self, delay):
                waits.append(delay)
        class Push:
            def dispatch(self, rows, devices, *, now):
                batches.append([row["seq"] for row in rows])
                if len(batches) <= 9:
                    raise OSError("synthetic transport failure")
                if len(batches) == 10:
                    log.append("job.created", dict(job="two", user_id="fixture"))
        service.network.push = Push()
        with patch.object(service, "stop", Stop()), patch.object(events.EventCursor, "read", autospec=True,
                side_effect=events.EventCursor.read) as read, patch("sys.stderr", io.StringIO()):
            service.network.push_loop()
        self.assertEqual(waits[:3], [0.2, 0.4, 0.8])
        self.assertEqual(waits[8:], [30.0, 0.1, 0.1, 0.1])
        self.assertEqual(batches[:10], [[first["seq"]]] * 10)
        self.assertEqual(batches[10:], [[2], []])
        self.assertEqual(read.call_count, 3)
        self.assertEqual(service.network.push_thread, None)
        service.network.push = None

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture requires openssl")
    def test_tls_handshake_request_read_and_connection_slots_are_bounded(self):
        self.configure_network()
        with self.config.open("a") as stream:
            stream.write("tls_handshake_timeout=0.2\nrequest_timeout=0.2\nmax_connections=1\n")
        service = self.start()
        info = self.network_info()
        endpoint = "127.0.0.1", info["candidates"][0]["port"]
        pending = socket.create_connection(endpoint, timeout=2)
        self.addCleanup(pending.close)
        deadline = time.monotonic() + 2
        while not service.network.connections and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(service.network.connections), 1)
        rejected = socket.create_connection(endpoint, timeout=2)
        self.addCleanup(rejected.close)
        self.assertEqual(rejected.recv(1), b"")
        self.assertEqual(pending.recv(1), b"")  # handshake deadline reclaimed the slot
        context = ssl._create_unverified_context()
        idle = context.wrap_socket(socket.create_connection(endpoint, timeout=2), server_hostname="localhost")
        self.addCleanup(idle.close)
        idle.sendall(b"POST /v1/pair HTTP/1.1\r\nHost: localhost\r\nContent-Length: 9\r\n")
        self.assertEqual(idle.recv(1), b"")  # incomplete headers time out
        body_idle = context.wrap_socket(socket.create_connection(endpoint, timeout=2), server_hostname="localhost")
        self.addCleanup(body_idle.close)
        body_idle.sendall(("POST /v1/pair HTTP/1.1\r\nHost: localhost\r\nX-Rdispatch-SPKI: " +
            info["spki_sha256"] + "\r\nContent-Type: application/json\r\nIdempotency-Key: slow\r\nContent-Length: 9\r\n\r\n{").encode())
        body_idle.settimeout(2)
        response = b""
        while chunk := body_idle.recv(4096):
            response += chunk
        self.assertIn(b"503", response.split(b"\r\n")[0])
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info))[0], 403)


class WriteFixTests(unittest.TestCase):
    setUp = serve_base.ServeTests.setUp
    start = serve_base.ServeTests.start
    seed = serve_base.ServeTests.seed
    request = serve_base.ServeTests.request
    eventually = serve_base.ServeTests.eventually

    def test_write_key_reuse_does_not_execute_and_is_bound_to_device_and_method(self):
        service = self.start(poll_interval=3600)
        writes = service.writes
        body = dict(stopped_by="App", extra="value")
        route = ROUTE.fullmatch("/v1/jobs/one/stop")
        with patch.object(writes, "command", return_value=(200, {"accepted": True})) as command:
            first = writes.write("used-key", route, body)
            self.assertEqual(writes.write("used-key", route, dict(reversed(list(body.items())))), first)
            example = json.loads((serve_base.CONTRACTS / "write-idempotency-reused.json").read_text())
            expected = example["response"]["status"], example["response"]["body"]
            for target, method, changed in ((ROUTE.fullmatch("/v1/jobs/one/resume"), "POST", body),
                    (route, "DELETE", body), (route, "POST", dict(stopped_by="other"))):
                self.assertEqual(writes.write("used-key", target, changed, method=method), expected)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(writes.write("used-key", route, body, device_id="other"), first)
            self.assertEqual(command.call_count, 2)
        service.close()
        service = self.start(poll_interval=3600)
        with patch.object(service.writes, "command") as command:
            self.assertEqual(service.writes.write("used-key", route, body), first)
            self.assertEqual(service.writes.write("used-key", route, {}), expected)
            command.assert_not_called()

    def test_execute_timeout_kills_child_releases_writer_and_caches_unknown_result(self):
        self.config.write_text("[serve]\nexecute_timeout=0.05\n")
        service = self.start(poll_interval=3600)
        request = dict(argv=["ack", "one"], user_id="fixture", config=str(self.config),
                       cwd=str(self.root), origin={}, stdin="")
        real_run = subprocess.run
        def slow(argv, **kwargs):
            return real_run([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)
        with patch("remote_dispatch.rd_serve.subprocess.run", side_effect=slow):
            before = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                service.execute(request)
            self.assertLess(time.monotonic() - before, 2)
            self.assertTrue(service.writer.acquire(blocking=False))
            service.writer.release()
            route = ROUTE.fullmatch("/v1/jobs/one/stop")
            with patch.object(service.writes, "command", side_effect=lambda *_: service.execute(request)) as command:
                first = service.writes.write("timeout", route, {})
                self.assertEqual(first, (503, {"error": {"reason": "write_outcome_unknown"}}))
                self.assertEqual(service.writes.write("timeout", route, {}), first)
                self.assertEqual(command.call_count, 1)

    def test_slow_job_probe_does_not_block_another_jobs_ack(self):
        service = self.seed(poll_interval=3600)
        receipt = self.eventually(lambda: self.request("/v1/jobs/fixture-job/rounds/1/receipt"),
                                  lambda result: result[0] == 200)[1]["receipt"]
        service.close()
        # Poll slow-job; the completed fixture job remains independently writable.
        slow = dict(self.rec, job="slow-job")
        cli.save_job(slow)
        entered, release = threading.Event(), threading.Event()
        states = cli.job_states
        def slow_states(config, records):
            if records[0]["job"] == "slow-job":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("slow probe was not released")
                return {"slow-job": {"state": "unreachable"}}
            return states(config, records)
        with patch.object(cli, "job_states", side_effect=slow_states):
            service = self.start(poll_interval=3600)
            try:
                self.assertTrue(entered.wait(3))
                payload = dict(argv=["ack", "fixture-job", "--round", "1", "--receipt-id", receipt["receipt_id"]],
                    user_id="fixture", config=str(self.config), cwd=str(self.root), origin={}, stdin="")
                from concurrent.futures import ThreadPoolExecutor
                pool = ThreadPoolExecutor(max_workers=1)
                pending = pool.submit(self.request, "/v1/jobs/slow-job/rounds/1/ack", "POST",
                    json.dumps(dict(receipt_id="not-ready", acked_by="App")),
                    {"Content-Type": "application/json", "Idempotency-Key": "slow-write"})
                time.sleep(0.05)
                self.assertFalse(pending.done())
                before = time.monotonic()
                status, response = self.request("/_cli/v1", "POST", json.dumps(payload), {"Content-Type": "application/json"})
                self.assertEqual((status, response["exit_code"]), (200, 0), response)
                self.assertLess(time.monotonic() - before, 2)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                if "pool" in locals():
                    pool.shutdown(wait=True)
            service.close()


class SessionFixTests(unittest.TestCase):
    setUp = sessions_base.SessionTests.setUp
    start = sessions_base.SessionTests.start
    request = sessions_base.SessionTests.request
    agent = sessions_base.SessionTests.agent
    save_fixture = sessions_base.SessionTests.save_fixture
    transcript = sessions_base.SessionTests.transcript
    saved_session = sessions_base.SessionTests.saved_session
    open_events = sessions_base.SessionTests.open_events
    eventually = sessions_base.SessionTests.eventually

    def test_terminal_redacts_decoded_secret_and_preserves_ansi_and_binary_bytes(self):
        raw = "\x1b[31maccess_token=terminal-private\x1b[0m\r\nvery-\x1b[1msecret\x1b[0m\n\x1b]0;token=osc-private\x07".encode() + b"\xff"
        self.data["frames"] = [json.dumps(dict(type="terminal.frame", seq=7,
            data=base64.b64encode(raw).decode())), '{"type":"terminal.closed","reason":"closed"}']
        self.save_fixture()
        service = self.start(poll_interval=3600)
        service.sensitive.add("very-secret")
        response = self.open_events("/v1/sessions/w1:p1/terminal")
        self.assertEqual(response.readline(), b"event: terminal.frame\n")
        frame = json.loads(response.readline()[6:])
        decoded = base64.b64decode(frame["data"])
        self.assertNotIn(b"terminal-private", decoded)
        self.assertNotIn(b"very-", decoded)
        self.assertEqual(decoded.count(b"[redacted]"), 3)
        self.assertNotIn(b"osc-private", decoded)
        self.assertIn(b"\x1b]0;[redacted]\x07", decoded)
        self.assertEqual(frame["seq"], 7)
        for code in (b"\x1b[31m", b"\x1b[0m", b"\x1b[1m"):
            self.assertEqual(decoded.count(code), raw.count(code))
        self.assertTrue(decoded.endswith(b"\x07\xff"))
        response.close()

    def test_transcript_parses_only_appended_lines_with_stable_ids_and_resets_on_replace(self):
        path = self.transcript()
        self.saved_session()
        service = Service(cli.Config(), self.path)
        self.addCleanup(service.close)
        sessions = service.sessions
        item = dict(row=dict(harness="codex"), transcript=dict(path=path))
        from remote_dispatch.rd_transcripts import convert_transcript
        parsed = []
        def convert(harness, lines, **kwargs):
            parsed.append(len(lines))
            return convert_transcript(harness, lines, **kwargs)
        with patch("remote_dispatch.rd_serve_sessions.convert_transcript", side_effect=convert):
            original = sessions.messages(item)
            self.assertEqual(sessions.messages(item), original)
            self.assertEqual(parsed, [2])
            call = dict(type="response_item", timestamp="now", payload=dict(type="function_call",
                        name="shell", call_id="call", arguments="pwd"))
            result = dict(type="response_item", timestamp="now", payload=dict(type="function_call_output",
                          call_id="call", output="done"))
            with path.open("a") as stream:
                stream.write(json.dumps(call) + "\n")
            sessions.messages(item)
            with path.open("a") as stream:
                stream.write(json.dumps(result) + "\n")
                stream.write('{"type":')
            current = sessions.messages(item)
            self.assertEqual(parsed, [2, 1, 1])
            self.assertEqual(current["messages"][0], original["messages"][0])
            self.assertEqual(current["messages"][-1]["tool"], "shell")
            self.assertEqual(len({row["id"] for row in current["messages"]}), 3)
            self.assertEqual(sessions.messages(item), current)
            self.assertEqual(parsed, [2, 1, 1])
            replacement = path.with_suffix(".tmp")
            replacement.write_text('{"type":"unknown"}\n')
            replacement.replace(path)
            self.assertFalse(sessions.messages(item)["supported"])
            path.write_text('{"type":"session_meta"}\n')
            self.assertEqual(sessions.messages(item), dict(supported=True, messages=[]))

    def test_discovery_keeps_incremental_event_cursor_without_query_writes(self):
        self.transcript()
        self.saved_session()
        service = Service(cli.Config(), self.path)
        self.addCleanup(service.close)
        sessions = service.sessions
        sessions.collect()
        before = (self.state / "events.jsonl").read_bytes()
        with patch.object(events, "read_after", side_effect=AssertionError("full history read")):
            first = sessions.discover()
            self.assertTrue(first)
            seq = sessions.event_reader.seq
            self.assertGreater(seq, 0)
            for _ in range(3):
                self.assertEqual(sessions.discover(), first)
                self.assertEqual(sessions.event_reader.seq, seq)
        self.assertEqual((self.state / "events.jsonl").read_bytes(), before)


class ReaderAndMigrationFixTests(unittest.TestCase):
    def test_event_cursor_skips_parsed_prefixes_and_survives_multiple_rotations(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            log = events.EventLog(state, max_bytes=1000000)
            log.append_batch([("job.created", dict(job=str(number)), False) for number in range(200)])
            reader = events.EventCursor(state)
            self.assertEqual(len(reader.read()), 200)
            loads = json.loads
            with patch.object(events.json, "loads", wraps=loads) as parse:
                self.assertEqual(reader.read(), [])
                self.assertLessEqual(parse.call_count, 1)
            log.append("job.created", dict(job="new"))
            with patch.object(events.json, "loads", wraps=loads) as parse:
                self.assertEqual([row["seq"] for row in reader.read()], [201])
                self.assertLessEqual(parse.call_count, 2)
            log = events.EventLog(state, max_bytes=1)
            for number in range(3):
                log.append("job.created", dict(job="rotation"))
            self.assertEqual([row["seq"] for row in reader.read()], [202, 203, 204])
            self.assertEqual(reader.read(), [])

    def test_resume_archive_excludes_nested_untracked_credentials_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wt = root / "wt/job"
            wt.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(wt)], check=True)
            (wt / "main.py").write_text("source\n")
            rd_native.git(wt, "add", "main.py")
            rd_native.git(wt, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                          "commit", "-qm", "fixture")
            (wt / "main.py").write_text("modified source\n")
            (wt / "note.txt").write_text("business file")
            for name in (".env", ".env.local", "nested/.env.prod", "nested/CERT.PEM", "server.key", "id_ed25519", ".ssh/config"):
                path = wt / name
                path.parent.mkdir(exist_ok=True)
                path.write_text("synthetic-credential-marker")
            target = root / "resume.tar"
            rd_native.export_resume(root, "job", target)
            with tarfile.open(target) as archive:
                names = archive.getnames()
                self.assertIn("source.bundle", names)
                self.assertIn("index", names)
                self.assertIn("workspace/main.py", names)
                self.assertIn("workspace/note.txt", names)
                self.assertEqual(archive.extractfile("workspace/main.py").read(), b"modified source\n")
                for name in names:
                    if archive.getmember(name).isfile() and name.startswith("workspace/"):
                        self.assertNotIn(b"synthetic-credential-marker", archive.extractfile(name).read())
                self.assertNotIn("workspace/.ssh", names)
            self.assertTrue((wt / ".env").exists())
