"""Ticket 72: observe discovery through HTTP; replace only Herdr/target RPCs."""
import threading
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from tests import test_rd_serve_sessions as session_base


class SessionPollIsolationTests(unittest.TestCase):
    setUp = session_base.SessionTests.setUp
    agent = session_base.SessionTests.agent
    save_fixture = session_base.SessionTests.save_fixture
    request = session_base.SessionTests.request
    eventually = session_base.SessionTests.eventually
    start = session_base.SessionTests.start
    sessions = session_base.SessionTests.sessions

    def test_new_pane_appears_within_two_cycles_while_200_jobs_target_hangs(self):
        self.config.write_text("[host:slow]\nssh=slow.invalid\nplatform=windows\n"
                               "[host:fast]\nssh=fast.invalid\nplatform=windows\n")
        for index in range(200):
            cli.save_job(dict(job=f"history-{index}", user_id="fixture", rounds=1,
                              host="slow" if index < 180 else "fast", project="demo"))
        entered, release = threading.Event(), threading.Event()

        def target(host, action, payload, **kwargs):
            if action == "probe":
                return dict(os="Windows")
            if action == "state" and host["name"] == "slow":
                entered.set()
                release.wait(30)  # cancellable fake of the measured 30-second target timeout
            return {job: dict(state="unreachable") for job in payload["jobs"]}

        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=0.5)
            try:
                self.assertTrue(entered.wait(2))
                self.data["agents"].append(self.agent("w1:p2"))
                self.save_fixture()
                deadline = time.monotonic() + 2 * service.poll_interval
                while time.monotonic() < deadline:
                    if "w1:p2" in [row["id"] for row in self.sessions()]:
                        break
                    time.sleep(0.01)
                self.assertIn("w1:p2", [row["id"] for row in self.sessions()])
                self.assertFalse(release.is_set())
            finally:
                release.set()
                service.close()

    def test_collection_failure_is_visible_and_successful_empty_list_recovers(self):
        service = self.start(poll_interval=3600)
        self.data["unavailable"] = True
        self.save_fixture()
        for count in range(1, 4):
            service.wake.set()
            body = self.eventually(lambda: self.request("/v1/sessions")[1],
                lambda body: body.get("collection", {}).get("consecutive_failures") == count)
            self.assertEqual([row["id"] for row in body["sessions"]], ["w1:p1"])
            self.assertEqual(body["collection"]["stale"], count >= 3)
            self.assertFalse(body["collection"]["last_attempt_succeeded"])
            self.assertEqual(body["collection"]["reason"], "OSError")
        self.data.update(unavailable=False, agents=[])
        self.save_fixture()
        service.wake.set()
        body = self.eventually(lambda: self.request("/v1/sessions")[1],
                              lambda body: not body["sessions"])
        self.assertTrue(body["collection"]["last_attempt_succeeded"])
        self.assertFalse(body["collection"]["stale"])
        self.assertEqual(body["collection"]["consecutive_failures"], 0)
        self.assertIsNone(body["collection"]["reason"])

    def test_jobs_are_batched_by_target_without_holding_job_lock_during_rpc(self):
        from remote_dispatch.rd_lock import JobLock
        self.config.write_text("[host:target]\nssh=target.invalid\nplatform=windows\n")
        for index in range(12):
            cli.save_job(dict(job=f"batch-{index}", user_id="fixture", rounds=1,
                              host="target", project="demo"))
        batches = []
        def target(host, action, payload, **kwargs):
            if action == "probe":
                return dict(os="Windows")
            batches.append(payload["jobs"])
            with JobLock(self.state, "batch-0"):
                pass
            return {job: dict(state="running", round="1") for job in payload["jobs"]}
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: len(rows) == 12 and all(row["execution_state"] == "running" for row in rows))
            self.assertEqual(len(batches), 1)
            self.assertEqual(set(batches[0]), {f"batch-{index}" for index in range(12)})
            service.close()

    def test_offline_host_backs_off_and_recovery_refreshes_jobs_in_same_cycle(self):
        self.config.write_text("[host:target]\nssh=target.invalid\nplatform=windows\n")
        cli.save_job(dict(job="offline", user_id="fixture", rounds=1, host="target", project="demo"))
        attempts = []
        state_calls = []
        online = threading.Event()
        def target(host, action, payload, **kwargs):
            if action == "probe":
                attempts.append(time.monotonic())
                if not online.is_set():
                    raise OSError("private terminal text token=must-not-log")
                return dict(os="Windows")
            state_calls.append(payload["jobs"])
            return {job: dict(state="running", round="1") for job in payload["jobs"]}
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=0.1)
            self.eventually(lambda: len(attempts), lambda count: count >= 3)
            before = len(attempts)
            # A local write wake does not bypass an open host circuit.
            for _ in range(4):
                service.wake.set()
                time.sleep(0.02)
            self.assertEqual(len(attempts), before)
            self.assertEqual(state_calls, [])
            online.set()
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: rows[0]["execution_state"] == "running")
            self.assertEqual(state_calls[0], ["offline"])
            recovered = len(attempts)
            service.wake.set()
            self.eventually(lambda: len(attempts), lambda count: count > recovered)
            service.close()

    def test_received_terminal_job_is_not_reprobed_but_new_round_is(self):
        service = session_base.base.ServeTests.seed(self, poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                        lambda rows: rows[0]["outcome"] == "completed")
        service.close()
        receipt = cli.messages.read_json(cli.result_path(self.rec, 1))
        cli.acknowledge_receipt(self.rec, receipt, "original-session", "manual")
        remote = cli.ssh
        state_batches = []
        def transport(host, script, **kwargs):
            if "sample_code=" in script:
                state_batches.append(script)
            return remote(host, script, **kwargs)
        with patch.object(cli, "ssh", side_effect=transport):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: service.reports.ready.is_set(), bool)
            self.assertEqual(state_batches, [])
            cli.save_job(dict(self.rec, rounds=2))
            (self.worker / "run.r2.sh").write_text("")
            service.wake.set()
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: rows[0]["round"] == 2 and rows[0]["snapshot_at"] is not None)
            self.assertEqual(len(state_batches), 1)
            service.close()

    transcript = session_base.SessionTests.transcript

    def test_each_discovery_scans_a_harness_once_for_multiple_panes(self):
        from pathlib import Path
        self.transcript()
        self.data["agents"].append(self.agent("w1:p2"))
        self.save_fixture()
        scan = Path.rglob
        scans = []
        def glob(path, pattern):
            if path == self.codex / "sessions":
                scans.append(pattern)
            return scan(path, pattern)
        with patch.object(Path, "rglob", glob):
            service = self.start(poll_interval=3600)
            self.assertEqual(len(self.sessions()), 2)
            self.assertEqual(scans, ["rollout-*.jsonl"])
            self.transcript("new-session")
            service.wake.set()
            self.eventually(self.sessions, lambda rows: all(row["session_id"] == "new-session" for row in rows))
            self.assertEqual(scans, ["rollout-*.jsonl", "rollout-*.jsonl"])
            service.close()

    def test_bad_transcript_conversion_does_not_freeze_other_panes_or_leak_errors(self):
        import io
        from remote_dispatch import rd_serve_sessions
        self.transcript()
        self.data["agents"].append(self.agent("w1:p2"))
        self.save_fixture()
        # Fault injection at the conversion seam: guard against converter regressions
        # that raise rather than return unsupported_format. No call-shape assertions.
        for error in (KeyError, TypeError):
            with self.subTest(error=error.__name__), \
                 patch.object(rd_serve_sessions, "convert_transcript", side_effect=error("PRIVATE_BODY")), \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr:
                service = self.start(poll_interval=3600)
                self.assertEqual({row["id"] for row in self.sessions()}, {"w1:p1", "w1:p2"})
                body = self.request("/v1/sessions/w1:p1/messages")[1]
                self.assertEqual(body, dict(supported=False, messages=[], reason="unsupported_format", text="不支持"))
                self.assertTrue(self.request("/v1/sessions")[1]["collection"]["last_attempt_succeeded"])
                self.assertNotIn("PRIVATE_BODY", stderr.getvalue())
                service.close()

    def test_disabled_host_existing_jobs_continue_refreshing(self):
        self.config.write_text("[host:target]\nssh=target.invalid\nplatform=windows\nenabled=false\n")
        cli.save_job(dict(job="existing", user_id="fixture", rounds=1, host="target", project="demo"))
        def target(host, action, payload, **kwargs):
            self.assertEqual(action, "state")
            return {"existing": dict(state="running", round="1")}
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: rows[0]["execution_state"] == "running")
            self.assertEqual(self.request("/v1/hosts")[1]["hosts"], [])
            service.close()

    def test_partial_host_response_marks_missing_job_unreachable(self):
        self.config.write_text("[host:target]\nssh=target.invalid\nplatform=windows\n")
        for job in ("present", "omitted"):
            cli.save_job(dict(job=job, user_id="fixture", rounds=1, host="target", project="demo"))
        omit = False
        def target(host, action, payload, **kwargs):
            if action == "probe":
                return dict(os="Windows")
            return {job: dict(state="running", round="1") for job in payload["jobs"]
                    if not (omit and job == "omitted")}
        with patch.object(cli.native, "request", side_effect=target):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: len(rows) == 2 and all(row["execution_state"] == "running" for row in rows))
            omit = True
            service.wake.set()
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: {row["job"]: row["execution_state"] for row in rows}
                            == dict(present="running", omitted="unreachable"))
            service.close()
