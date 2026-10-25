"""Project CLI and query behavior against isolated controller state."""
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd
from remote_dispatch.rd_lock import JobLock


class ProjectHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.config.write_text("[project:app]\n[project:app-tools]\n")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(rd, "STATE", str(self.state)))
        stack.enter_context(patch.object(rd, "CONFIG", str(self.config)))
        stack.enter_context(patch.dict(os.environ, {"RD_USER_ID": "alice-team"}))
        stack.enter_context(patch.object(rd, "job_states", return_value={}))

    def command(self, *args):
        with patch.object(sys, "argv", ["rdispatch", *args]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            rd.main()
        return json.loads(output.getvalue())

    def snapshot(self):
        return {str(path.relative_to(self.state)): (path.stat().st_mtime_ns, path.read_bytes())
                for path in self.state.rglob("*") if path.is_file()}

    def test_legacy_queries_prefer_field_then_longest_prefix_then_unassigned_without_writes(self):
        records = [
            dict(job="alice-team-app-tools-task-20261024-090000", project="explicit"),
            dict(job="alice-team-app-tools-task-20261024-100000"),
            dict(job="alice-team-unknown-task-20261024-110000"),
            dict(job="alice-team-application-task-20261024-120000"),
        ]
        for rec in records:
            rd.save_job(dict(rec, host="fixture", rounds=1))
        before = self.snapshot()
        rows = {row["job"]: row for row in self.command("status", "--json")}
        expected = ["explicit", "app-tools", "未归属", "未归属"]
        self.assertEqual([rows[rec["job"]]["project"] for rec in records], expected)
        self.assertTrue(all(rows[rec["job"]]["project_id"] for rec in records))
        self.assertEqual(self.snapshot(), before)

    def test_rebuild_uses_old_jobs_and_receipts_once_and_excludes_other_users(self):
        old = dict(job="alice-team-app-tools-task-20261024-100000", rounds=2, host="fixture")
        rd.save_job(old)
        receipts = [
            dict(job=old["job"], round=1),
            dict(job=old["job"], round=2),
            dict(job="old-unknown", round=1, user_id="alice-team"),
            dict(job="other-app-job", round=1, project="private", user_id="other"),
            dict(job="field-job", round=1, project="explicit", created="2026-10-23 08:00:00"),
        ]
        for rec in receipts:
            rd.messages.atomic_json(rd.result_path(rec, rec["round"]), rec)
        before = self.snapshot()
        rows = {row["name"]: row for row in self.command("project", "rebuild")}
        self.assertEqual(set(rows), {"app-tools", "explicit", "未归属"})
        self.assertEqual(rows["app-tools"]["dispatch_count"], 1)
        self.assertEqual(rows["app-tools"]["first_dispatch_at"], "2026-10-24 10:00:00")
        self.assertEqual(rows["explicit"]["first_dispatch_at"], "2026-10-23 08:00:00")
        self.assertIsNone(rows["未归属"]["first_dispatch_at"])
        after = self.snapshot()
        self.assertEqual({key: after[key] for key in before}, before)

    def test_project_commands_are_user_scoped_and_unknown_id_does_not_write(self):
        rd.save_job(dict(job="alice-job", user_id="alice-team", project="app"))
        row = self.command("project", "rebuild")[0]
        before = self.snapshot()
        with patch.dict(os.environ, {"RD_USER_ID": "other"}), \
             contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as result:
            self.command("project", "hide", row["id"])
        self.assertEqual(result.exception.code, 4)
        self.assertIn("unknown project", error.getvalue())
        self.assertEqual({key: self.snapshot()[key] for key in before}, before)
        self.assertEqual(rd.queries(rd.Config()).projects("other"), [])

    def test_index_writers_contend_across_processes_without_sharing_job_or_other_user_locks(self):
        rd.save_job(dict(job="alice-job", user_id="alice-team", project="app"))
        with JobLock(self.state, "alice-team"):
            row = self.command("project", "rebuild")[0]
        env = dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(self.config))
        # Pause at the filesystem replacement boundary while the CLI holds the
        # index lock; the parent checks observable behavior of a second CLI.
        script = '''
import os, sys
from pathlib import Path
from remote_dispatch import cli as rd
replace = os.replace
def held_replace(source, destination):
    if Path(destination) == Path(rd.STATE) / "projects/alice-team.json":
        print("holding", flush=True)
        sys.stdin.readline()
    return replace(source, destination)
os.replace = held_replace
rd.main()
'''
        first = subprocess.Popen([sys.executable, "-c", script, "project", "hide", row["id"]],
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        def reap():
            if first.poll() is None:
                first.kill()
            first.communicate()
        self.addCleanup(reap)
        self.assertEqual(first.stdout.readline().strip(), "holding")
        def invoke(*args, **extra_env):
            return subprocess.run([sys.executable, "-m", "remote_dispatch", "project", *args],
                                  env=dict(env, **extra_env), capture_output=True, text=True, timeout=10)
        for args in (("show", row["id"]), ("rebuild",)):
            result = invoke(*args)
            self.assertEqual(result.returncode, 4)
            self.assertIn("project index busy", result.stderr)
        other = invoke("rebuild", RD_USER_ID="other")
        self.assertEqual(other.returncode, 0, other.stderr)
        first.communicate(input="release\n", timeout=10)
        self.assertEqual(first.returncode, 0)
        self.assertTrue(rd.queries(rd.Config()).projects("alice-team")[0]["hidden"])
        self.assertEqual(invoke("show", row["id"]).returncode, 0)

    def test_rebuild_repairs_invalid_json_or_structure_from_dispatch_history(self):
        rd.save_job(dict(job="alice-job", user_id="alice-team", project="app"))
        expected = self.command("project", "rebuild")
        path = self.state / "projects/alice-team.json"
        for invalid in ('{"schema":', '{"schema":1,"projects":{"bad":{}}}'):
            with self.subTest(invalid=invalid):
                path.write_text(invalid)
                self.assertEqual(self.command("project", "rebuild"), expected)

    def test_project_filter_includes_inferred_legacy_records_without_rewriting_them(self):
        rd.save_job(dict(job="alice-team-app-tools-task", host="fixture", rounds=1))
        rd.save_job(dict(job="other-app-task", user_id="other", project="app-tools"))
        before = self.snapshot()
        rows = self.command("status", "--project", "app-tools", "--json")
        self.assertEqual([row["job"] for row in rows], ["alice-team-app-tools-task"])
        self.assertEqual(rows[0]["project"], "app-tools")
        self.assertEqual(self.snapshot(), before)

    def test_project_commands_reuse_the_persisted_default_user_id(self):
        rd.save_job(dict(job="default-job", user_id="alice-smith", project="app"))
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(getpass, "getuser", return_value=" Alice Smith "):
            row = self.command("project", "rebuild")[0]
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(getpass, "getuser", return_value="Changed User"):
            self.assertTrue(self.command("project", "hide", row["id"])["hidden"])
        self.assertTrue(rd.queries(rd.Config()).projects("alice-smith")[0]["hidden"])
        self.assertEqual(rd.queries(rd.Config()).projects("changed-user"), [])
