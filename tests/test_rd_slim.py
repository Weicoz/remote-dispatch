"""可再生子路径瘦身：本机派发的 CLI 接缝测试，不连接远端，不调用模型。"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd
from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_native as native
from remote_dispatch import rd_slim as slim

COMMAND = ("mkdir -p target node_modules; echo built > target/a.bin; echo dep > node_modules/b.js; "
           "echo log > kept.ignored; echo note > notes.txt")


class SlimFixture(unittest.TestCase):
    declared = "target; node_modules"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-slim-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text("*.ignored\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture base")
        self.config_path = self.root / "config.ini"
        self.config_path.write_text(
            f"[host:self]\nssh = local\nworkdir = {self.root / 'worker'}\nsync = direct\n"
            f"path = {os.environ['PATH']}\ntimeout = 30\n"
            f"[features]\nwarm_worktree = false\n"
            f"[project:repo]\nsetup = echo run >> \"$RD_JOB_DIR/setup-runs\"\n"
            f"regenerable_paths = {self.declared}\n", encoding="utf-8")
        for mock in (
            patch.object(rd, "CONFIG", str(self.config_path)),
            patch.object(rd, "STATE", str(self.root / "state")),
            patch.dict(os.environ, {"RD_USER_ID": "employee_a"}),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            mock.__enter__()
            self.addCleanup(mock.__exit__, None, None, None)
        self.cfg = rd.Config()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def dispatch(self, command=COMMAND):
        args = argparse.Namespace(dir=str(self.repo), agent=None, prompt_file=None, cmd=command,
                                  test=False, heavy=False, host="self", force_host=True, tag=[], ref="HEAD",
                                  wip=False, branch=None, name="slim", project=None, include=[], model=None,
                                  effort=None, timeout=15, initiator_session=None, workdir=None)
        rd.cmd_run(args, self.cfg)
        rec = rd.all_jobs()[0]
        self.wait_done(rec)
        return rec

    def wait_done(self, rec):
        deadline = time.time() + 20
        while time.time() < deadline:
            if rd.job_states(self.cfg, [rec])[rec["job"]].get("state") == "done":
                return
            time.sleep(0.1)
        self.fail("本机派单未在 20 秒内完成")

    def paths(self, rec):
        return Path(rec["worker_root"], "wt", rec["job"]), Path(rec["worker_root"], "jobs", rec["job"])

    def slim_args(self, rec=None, **options):
        values = dict(job=rec["job"] if rec else None, force=False, delete_branch=False, discard_reports=False,
                      merged=False, expired=False, into=None, dry_run=False, json=False, project=None,
                      initiator_session=None, slim=True)
        values.update(options)
        return argparse.Namespace(**values)

    def run_slim(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rd.cmd_clean(args, self.cfg)
        return out.getvalue()


class DeclarationTest(SlimFixture):
    def test_invalid_declarations_are_rejected_when_config_loads(self):
        for bad in (".", "./", "..", "a/../..", "/abs/target", "~/target", "C:/target", "target/evidence",
                    "reports", "a/Reports/b", ".git", "ta*get"):
            with self.subTest(bad):
                self.config_path.write_text(f"[project:repo]\nregenerable_paths = {bad}\n", encoding="utf-8")
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    rd.Config().project("repo")
        self.config_path.write_text("[project:repo]\nregenerable_paths = a/b\\c; target\n", encoding="utf-8")
        self.assertEqual(slim.parse_paths(rd.Config().project("repo")["regenerable_paths"]), ["a/b/c", "target"])


class SlimTest(SlimFixture):
    def test_slim_removes_only_declared_paths_and_setup_marker(self):
        rec = self.dispatch()
        wt, job = self.paths(rec)
        self.assertTrue((job / "setup.done").exists())
        self.run_slim(self.slim_args(rec))
        self.assertFalse((wt / "target").exists())
        self.assertFalse((wt / "node_modules").exists())
        self.assertFalse((job / "setup.done").exists())
        self.assertEqual((wt / "kept.ignored").read_text(), "log\n")
        self.assertEqual((wt / "notes.txt").read_text(), "note\n")
        self.assertTrue((wt / "tracked.txt").exists())
        self.assertTrue((job / "reports").exists())
        self.assertEqual(len(rd.all_jobs()), 1)

    def test_slim_keeps_declared_directory_holding_tracked_files(self):
        (self.repo / "target").mkdir()
        (self.repo / "target" / "tracked.rs").write_text("fn main() {}\n")
        self.git("add", "-f", "target/tracked.rs")
        self.git("commit", "-qm", "track target file")
        rec = self.dispatch()
        wt, _ = self.paths(rec)
        output = self.run_slim(self.slim_args(rec))
        self.assertTrue((wt / "target" / "tracked.rs").exists())
        self.assertIn("target", output)
        self.assertFalse((wt / "node_modules").exists())

    def test_slim_refuses_symlink_escape_without_touching_outside(self):
        rec = self.dispatch("mkdir -p ../../../outside && echo keep > ../../../outside/f && ln -s ../../../outside target")
        wt, _ = self.paths(rec)
        outside = Path(rec["worker_root"]).parent / "outside"
        self.assertTrue((outside / "f").exists())
        self.run_slim(self.slim_args(rec))
        self.assertEqual((outside / "f").read_text(), "keep\n")
        self.assertFalse(os.path.lexists(wt / "target"))

    def test_slim_refuses_running_job(self):
        rec = self.dispatch()
        with patch.object(rd, "remote_state", return_value={rec["job"]: {"state": "running"}}), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rd.cmd_clean(self.slim_args(rec), self.cfg)
        wt, _ = self.paths(rec)
        self.assertTrue((wt / "target").exists())

    def test_dry_run_changes_nothing_and_lists_sizes(self):
        rec = self.dispatch()
        wt, job = self.paths(rec)
        output = self.run_slim(self.slim_args(rec, dry_run=True))
        self.assertIn("target", output)
        self.assertTrue((wt / "target").exists())
        self.assertTrue((job / "setup.done").exists())

    def test_batch_slims_finished_jobs_in_current_repo(self):
        rec = self.dispatch()
        wt, _ = self.paths(rec)
        output = self.run_slim(self.slim_args(None, json=True, dry_run=True, dir=str(self.repo)))
        rows = json.loads(output)
        self.assertEqual([(row["job"], row["slimmable"]) for row in rows], [(rec["job"], True)])
        self.run_slim(self.slim_args(None, dir=str(self.repo)))
        self.assertFalse((wt / "target").exists())

    def test_job_without_declaration_is_refused(self):
        rec = self.dispatch()
        rd.save_job(dict(rd.load_job(rec["job"]), regenerable_paths=[]))
        with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            rd.cmd_clean(self.slim_args(rec), self.cfg)
        self.assertIn("regenerable_paths", errors.getvalue())

    def test_next_round_runs_setup_again_after_slim(self):
        rec = self.dispatch()
        wt, job = self.paths(rec)
        self.run_slim(self.slim_args(rec))
        rd.launch(rd.scoped_host(self.cfg.host("self"), "employee_a"), rd.load_job(rec["job"]), 2, "echo round2", None, 15)
        deadline = time.time() + 20
        while time.time() < deadline:
            if (job / "setup-runs").read_text().count("run") >= 2 and (job / "exit_code").exists():
                break
            time.sleep(0.1)
        self.assertEqual((job / "setup-runs").read_text().count("run"), 2)
        self.assertTrue((job / "setup.done").exists())
        self.assertEqual((job / "exit_code").read_text().strip(), "0")


class SizeRecordTest(SlimFixture):
    def test_receipt_records_declared_path_sizes(self):
        rec = self.dispatch()
        _, job = self.paths(rec)
        report = json.loads((job / "reports" / "result.r1.json").read_text())
        sizes = report["regenerable_kb"]
        self.assertEqual(set(sizes), {"target", "node_modules"})
        self.assertGreaterEqual(sizes["target"], 1)

    def test_phase_timings_ignores_corrupt_size_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "timing.r1").write_text(
                "setup_seconds=1\npath_size_kb=12\ttarget\npath_size_kb=abc\tbad\npath_size_kb=-3\tneg\n"
                "path_size_kb=5\n")
            self.assertEqual(messages.phase_timings(directory, 1),
                             {"setup_seconds": 1, "regenerable_kb": {"target": 12}})


class NativeSlimTest(unittest.TestCase):
    """Windows 原生目标机的 slim 操作：用同一份 rd_native 请求处理器，在临时目录运行。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-slim-native-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.wt = self.root / "wt" / "j1"
        self.job = self.root / "jobs" / "j1"
        self.wt.mkdir(parents=True)
        self.job.mkdir(parents=True)
        for command in (["init", "-q"], ["config", "user.name", "F"], ["config", "user.email", "f@example.invalid"]):
            subprocess.run(["git", "-C", str(self.wt), *command], check=True)
        (self.wt / "tracked.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.wt), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.wt), "commit", "-qm", "base"], check=True)
        (self.wt / "target").mkdir()
        (self.wt / "target" / "a.bin").write_text("built")
        (self.wt / "kept.txt").write_text("keep")
        (self.job / "setup.done").touch()

    def test_native_slim_matches_posix_behaviour(self):
        reply = native.slim_job(self.root, "j1", ["target", "missing"])
        self.assertFalse((self.wt / "target").exists())
        self.assertTrue((self.wt / "kept.txt").exists())
        self.assertFalse((self.job / "setup.done").exists())
        self.assertEqual([row["path"] for row in reply["removed"]], ["target"])


if __name__ == "__main__":
    unittest.main()
