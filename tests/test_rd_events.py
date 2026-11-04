"""事件的公开持久化／补读边界；所有数据均在临时 RD_STATE。"""
import tempfile
import os
import subprocess
import sys
import time
from unittest.mock import patch
from pathlib import Path
import unittest

from remote_dispatch import rd_events as events


class EventLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-events-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve() / "state"

    def test_appended_display_fields_can_be_read_after_a_sequence_without_credentials(self):
        log = events.EventLog(self.state)
        self.assertEqual(log.read_after(0), [])
        self.assertFalse(self.state.exists())
        log.append("job.created", dict(job="fixture", round=1, user_id="user", host="target",
                                      env_file="secret", agent_auth_file="secret", ssh_opts="secret",
                                      profile={"token": "secret"}))
        log.append("job.state_changed", dict(job="fixture", round=2, execution_state="running"))
        rows = log.read_after(0)
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["type"], "job.created")
        self.assertTrue(rows[0]["time"])
        self.assertEqual(rows[0]["data"], dict(job="fixture", round=1, user_id="user", host="target"))
        self.assertEqual(log.read_after(1), [rows[1]])
        self.assertEqual(log.read_after(2), [])

    def test_rotation_preserves_every_cursor_and_continues_sequence_after_restart(self):
        log = events.EventLog(self.state, max_bytes=1)
        for number in range(5):
            log.append("job.state_changed", dict(job="fixture", round=number + 1))
        self.assertGreater(len(list(self.state.glob("events.*.jsonl"))), 1)
        for cursor in range(7):
            self.assertEqual([row["seq"] for row in events.read_after(self.state, cursor)],
                             list(range(cursor + 1, 6)))
        restarted = events.EventLog(self.state, max_bytes=1)
        restarted.append("receipt.ready", dict(job="fixture", round=5, receipt_id="receipt"))
        self.assertEqual([row["seq"] for row in restarted.read_after(4)], [5, 6])

    def test_processes_append_unique_strictly_increasing_sequences_during_rotation(self):
        script = """
import sys
from remote_dispatch.rd_events import EventLog
log = EventLog(sys.argv[1], max_bytes=400)
for number in range(20):
    log.append('job.state_changed', dict(job=sys.argv[2], round=number + 1))
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.state), str(number)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=dict(os.environ, PYTHONPATH="src", RD_EVENT_LOG_MAX_ARCHIVES="100")) for number in range(2)]
        try:
            cursor = 0
            deadline = time.monotonic() + 15
            while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
                batch = events.read_after(self.state, cursor)
                if batch:
                    self.assertEqual([row["seq"] for row in batch], list(range(cursor + 1, batch[-1]["seq"] + 1)))
                    cursor = batch[-1]["seq"]
                time.sleep(0.001)
            for process in processes:
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, (out, err))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        rows = events.read_after(self.state, 0)
        self.assertEqual([row["seq"] for row in rows], list(range(1, 41)))
        self.assertEqual({row["data"]["job"] for row in rows}, {"0", "1"})

    def test_background_observations_publish_state_progress_and_host_only_when_changed(self):
        log = events.EventLog(self.state, max_bytes=1)
        rec = dict(job="fixture", user_id="user", rounds=1, host="target")
        snapshot = dict(state="running", round="1", observed_at="one", last="private output")
        progress = dict(phase="implementing", summary="实现中", updated_at="one", env_file="secret")
        host = dict(name="target", ssh="private", env_file="secret")
        info = dict(reachable=True, load1="1", ncpu="8", memfree="90", disk="100")
        log.observe_job(rec, snapshot, progress)
        log.observe_host(host, info, full=False)
        # A service restart and rotation must not produce duplicate observations.
        restarted = events.EventLog(self.state, max_bytes=1)
        restarted.observe_job(rec, dict(snapshot, observed_at="two"), progress)
        restarted.observe_host(host, info, full=False)
        rows = restarted.read_after(0)
        self.assertEqual([row["type"] for row in rows],
                         ["job.state_changed", "progress.updated", "host.changed"])
        self.assertEqual(rows[1]["data"]["summary"], "实现中")
        self.assertNotIn("last", rows[0]["data"])
        self.assertNotIn("ssh", rows[2]["data"])
        restarted.observe_job(rec, dict(snapshot, state="unreachable"), progress)
        restarted.observe_host(host, dict(info, reachable=False), full=False)
        self.assertEqual([row["type"] for row in restarted.read_after(3)], ["job.state_changed", "host.changed"])

    def test_interrupted_append_is_ignored_by_queries_and_repaired_by_next_writer(self):
        log = events.EventLog(self.state)
        log.append("job.created", dict(job="fixture", round=1))
        path = self.state / "events.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"seq":2,"data":"' + b"x" * 20000)
        before = path.read_bytes()
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1])
        self.assertEqual(path.read_bytes(), before)
        log.append("receipt.ready", dict(job="fixture", round=1))
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1, 2])

    def test_restart_between_rename_and_append_uses_last_archived_sequence(self):
        log = events.EventLog(self.state, max_bytes=1)
        log.append("job.created", dict(job="fixture", round=1))
        (self.state / "events.jsonl").rename(self.state / "events.00000000000000000001.jsonl")
        events.EventLog(self.state, max_bytes=1).append("receipt.ready", dict(job="fixture", round=1))
        self.assertEqual([row["seq"] for row in events.read_after(self.state, 0)], [1, 2])

    def test_environment_configures_rotation_size_and_rejects_non_positive_values(self):
        with patch.dict(os.environ, {"RD_EVENT_LOG_MAX_BYTES": "1"}):
            events.emit(self.state, "job.created", dict(job="fixture"))
            events.emit(self.state, "job.state_changed", dict(job="fixture", round=2))
        self.assertEqual(len(list(self.state.glob("events.*.jsonl"))), 1)
        for value in ("0", "-1", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {"RD_EVENT_LOG_MAX_BYTES": value}):
                with self.assertRaisesRegex(ValueError, "RD_EVENT_LOG_MAX_BYTES"):
                    events.emit(self.state, "job.created", dict(job="fixture"))

    def test_event_append_does_not_share_a_job_or_project_lock(self):
        from remote_dispatch.rd_lock import JobLock
        from remote_dispatch.rd_projects import ProjectLock
        with JobLock(self.state, "events"), ProjectLock(self.state, "events"):
            result = subprocess.run([sys.executable, "-c",
                                     "from remote_dispatch.rd_events import emit; import sys; "
                                     "emit(sys.argv[1], 'job.created', dict(job='fixture'))", str(self.state)],
                                    capture_output=True, text=True, timeout=10,
                                    env=dict(os.environ, PYTHONPATH="src"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(events.read_after(self.state, 0)), 1)

    def test_failed_batch_after_rotation_publishes_nothing_and_retry_keeps_sequence(self):
        log = events.EventLog(self.state, max_bytes=1)
        log.append("job.created", dict(job="fixture"))
        before = log.read_after(0)
        write_active = events.write_active
        def fail_publish(path, body):
            if Path(path) == self.state / "events.jsonl":
                raise OSError("fixture publish failed")
            return write_active(path, body)
        with patch.object(events, "write_active", side_effect=fail_publish), self.assertRaisesRegex(ValueError, "event_write_failed"):
            log.append_batch([("receipt.ready", dict(job="fixture"), False),
                              ("receipt.acked", dict(job="fixture"), False)])
        self.assertEqual(log.read_after(0), before)
        log.append("receipt.ready", dict(job="fixture"))
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1, 2])
