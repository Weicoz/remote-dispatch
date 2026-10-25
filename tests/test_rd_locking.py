"""CLI concurrency against a file-backed fake target RPC, using real processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from remote_dispatch import rd_messages as messages


TARGET = r'''
import json, os, sys
from pathlib import Path
from remote_dispatch import cli as rd
root = Path(os.environ["FIXTURE_ROOT"])
def request(host, operation, data=None, **kwargs):
    if operation == os.environ.get("FIXTURE_HOLD"):
        (root / "entered").touch()
        sys.stdin.readline()
    if operation == os.environ.get("FIXTURE_FAIL"):
        raise ValueError("fixture target failed")
    if operation == "state":
        return {job: json.loads((root / "target.json").read_text()) for job in data["jobs"]}
    if operation == "probe":
        return dict(memfree="90", disk="100")
    if operation == "sessions":
        return '{"session_id":"receiver"}'
    if operation == "report-read":
        return None
    if operation == "upload":
        return None
    if operation in ("launch", "stop"):
        number = data["spec"]["round"] if operation == "launch" else 1
        with (root / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(dict(operation=operation, round=number)) + "\n")
        rd.messages.atomic_json(root / "target.json", dict(state="running" if operation == "launch" else "done",
                                                          round=str(number), exit="143"))
        return None
    raise AssertionError(operation)
rd.native.request = request
rd.main()
'''


class JobLockingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.record = dict(job="fixture-job", user_id="fixture", host="fixture", rounds=1,
                           agent="codex", isolated=True, receiver_session="receiver",
                           initiator_session="sender", model=None, effort=None, sync="direct", base="base")
        messages.atomic_json(self.state / "jobs/fixture-job.json", self.record)
        messages.atomic_json(self.root / "target.json", dict(state="done", round="1", exit="0"))
        (self.root / "prompt.md").write_text("Continue the task.")
        config = self.root / "config.ini"
        config.write_text("[host:fixture]\nssh=fixture.invalid\nplatform=windows\nworkdir=C:/fixture\n")
        self.env = dict(os.environ, RD_CONFIG=str(config), RD_STATE=str(self.state), RD_USER_ID="fixture",
                        RD_INITIATOR_SESSION="sender", FIXTURE_ROOT=str(self.root))

    def command(self, name, **env):
        args = [name] if name == "inbox" else [name, "fixture-job"]
        if name == "status":
            args += ["--json"]
        if name == "resume":
            args += ["--prompt-file", str(self.root / "prompt.md")]
        if name == "ack":
            receipt = self.run_command("result")
            args += ["--round", "1", "--receipt-id", json.loads(receipt.stdout)["receipt_id"]]
        process = subprocess.Popen([sys.executable, "-c", TARGET, *args], env=dict(self.env, **env),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.communicate()

    def run_command(self, name, **env):
        process = self.command(name, **env)
        stdout, stderr = process.communicate(timeout=10)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def wait_until_held(self, process):
        deadline = time.monotonic() + 5
        while not (self.root / "entered").exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail("first command did not reach target boundary")
            time.sleep(0.01)

    def test_concurrent_resumes_only_launch_one_new_round(self):
        first = self.command("resume", FIXTURE_HOLD="launch")
        self.wait_until_held(first)
        second = self.run_command("resume")
        first_stdout, first_stderr = first.communicate("release\n", timeout=10)
        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertIn("round 2", first_stdout)
        self.assertEqual(second.returncode, 4, second.stdout + second.stderr)
        self.assertIn("round in progress", second.stderr)
        events = [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events, [dict(operation="launch", round=2)])
        status = self.run_command("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("running", status.stdout)
        self.assertEqual(json.loads(status.stdout)[0]["rounds"], 2)

    def assert_excludes(self, holder, boundary, contenders):
        first = self.command(holder, FIXTURE_HOLD=boundary)
        self.wait_until_held(first)
        for name in contenders:
            with self.subTest(holder=holder, contender=name):
                other = self.run_command(name)
                self.assertEqual(other.returncode, 4, other.stdout + other.stderr)
                self.assertIn("round in progress", other.stderr)
        _, stderr = first.communicate("release\n", timeout=10)
        self.assertEqual(first.returncode, 0, stderr)

    def test_resume_excludes_ack_and_stop_before_reading_target_state(self):
        self.assert_excludes("resume", "state", ["ack", "stop"])

    def test_ack_excludes_resume_and_stop(self):
        self.assert_excludes("ack", "state", ["resume", "stop"])
        receipt = self.run_command("result")
        self.assertEqual(json.loads(receipt.stdout)["delivery_state"], "received")

    def test_stop_excludes_resume_and_ack(self):
        self.assert_excludes("stop", "stop", ["resume", "ack"])

    def test_running_round_rejects_a_later_resume_without_incrementing(self):
        first = self.run_command("resume")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_command("resume")
        self.assertEqual(second.returncode, 4, second.stderr)
        self.assertIn("round in progress", second.stderr)
        status = json.loads(self.run_command("status").stdout)
        self.assertEqual(status[0]["rounds"], 2)

    def test_target_failure_releases_lock_for_a_subsequent_resume(self):
        failed = self.run_command("resume", FIXTURE_FAIL="probe")
        self.assertNotEqual(failed.returncode, 0)
        resumed = self.run_command("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(self.run_command("status").stdout)[0]["rounds"], 2)

    def test_process_exit_releases_lock_for_another_command(self):
        first = self.command("resume", FIXTURE_HOLD="launch")
        self.wait_until_held(first)
        first.kill()
        first.communicate(timeout=10)
        stopped = self.run_command("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

    def test_launch_failure_releases_lock_for_stop(self):
        failed = self.run_command("resume", FIXTURE_FAIL="launch")
        self.assertNotEqual(failed.returncode, 0)
        stopped = self.run_command("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

    def test_different_jobs_do_not_contend(self):
        from remote_dispatch.rd_lock import JobLock
        with JobLock(self.state, "other-job"):
            resumed = self.run_command("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_inbox_collecting_an_old_result_cannot_rewind_a_resumed_job(self):
        self.record.pop("receiver_session")
        messages.atomic_json(self.state / "jobs/fixture-job.json", self.record)
        inbox = self.command("inbox", FIXTURE_HOLD="sessions")
        self.wait_until_held(inbox)
        resumed = self.run_command("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        _, stderr = inbox.communicate("release\n", timeout=10)
        self.assertEqual(inbox.returncode, 0, stderr)
        self.assertEqual(json.loads(self.run_command("status").stdout)[0]["rounds"], 2)


if __name__ == "__main__":
    unittest.main()
