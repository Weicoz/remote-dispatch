"""每轮 setup 与主体耗时：运行真实生成脚本和 Windows 原生 runner，不连接设备或模型。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_rd import rd
from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_native as native
from remote_dispatch import rd_timings as timings

SRC = Path(rd.HERE)


class JobFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-timings-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / "jobs" / "fixture"
        self.wt = self.root / "wt" / "fixture"
        self.job.mkdir(parents=True)
        self.wt.mkdir(parents=True)
        (self.job / "rd_messages.py").write_text((SRC / "rd_messages.py").read_text())

    def receipt(self, number=1):
        return json.loads((self.job / "reports" / f"result.r{number}.json").read_text())


@unittest.skipUnless(os.name == "posix", "POSIX runner only")
class PosixTimingTests(JobFixture, unittest.TestCase):
    def run_script(self, command, setup=None, setup_done=False):
        if setup is not None:
            (self.job / "setup.sh").write_text(setup + "\n")
        if setup_done:
            (self.job / "setup.done").touch()
        (self.job / "cmd.r1.sh").write_text(command + "\n")
        host = dict(rd.DEFAULTS, workdir=str(self.root))
        rec = dict(job="fixture", sync="direct", handoff_version=1, agent=None)
        script = self.job / "run.r1.sh"
        script.write_text(rd.build_run_script(host, rec, 1, 30))
        subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=30, check=False)
        return self.receipt()

    def test_normal_setup_and_command_are_timed_separately(self):
        # setup 睡 1 秒、命令睡 4 秒；只断言各自下限与相对大小，不依赖绝对墙钟上限（高负载下秒级取整会超）。
        data = self.run_script("sleep 4", setup="sleep 1")
        self.assertGreaterEqual(data["setup_seconds"], 1)
        self.assertGreaterEqual(data["body_seconds"], 4)
        self.assertLess(data["setup_seconds"], data["body_seconds"])
        self.assertNotIn("setup_note", data)

    def test_skipped_setup_is_zero_and_noted(self):
        for kwargs in (dict(setup="sleep 3", setup_done=True), dict()):
            with self.subTest(kwargs):
                data = self.run_script("true", **kwargs)
                self.assertEqual(data["setup_seconds"], 0)
                self.assertEqual(data["setup_note"], "已跳过")
                self.assertIn("body_seconds", data)
                for name in ("setup.done", "setup.sh"):
                    (self.job / name).unlink(missing_ok=True)

    def test_failed_command_still_records_body_time(self):
        data = self.run_script("sleep 1; exit 3", setup="true")
        self.assertEqual(data["executor_exit"], 3)
        self.assertEqual(data["outcome"], "failed")
        self.assertGreaterEqual(data["body_seconds"], 1)
        self.assertEqual(data["setup_seconds"], 0)

    def test_failed_setup_records_setup_time_and_no_body(self):
        data = self.run_script("true", setup="sleep 1; exit 1")
        self.assertEqual(data["executor_exit"], 97)
        self.assertGreaterEqual(data["setup_seconds"], 1)
        self.assertNotIn("body_seconds", data)

    def test_rollout_switch_is_off_by_default(self):
        rec = dict(job="f", sync="direct", handoff_version=1)
        script = rd.build_run_script(dict(rd.DEFAULTS, workdir=str(self.root)), rec, 1, 5)
        self.assertNotIn("RD_ROLLOUT_STATS", script)
        enabled = rd.build_run_script(dict(rd.DEFAULTS, workdir=str(self.root), rollout_stats="1"), rec, 1, 5)
        self.assertIn("RD_ROLLOUT_STATS=1", enabled)


@unittest.skipUnless(os.name == "posix", "sh shell_command")
class NativeTimingTests(JobFixture, unittest.TestCase):
    def run_native(self, command, setup=None, setup_done=False):
        if setup is not None:
            (self.job / "setup.ps1").write_text(setup)
        if setup_done:
            (self.job / "setup.done").touch()
        (self.job / "prompt.r1.md").write_text("")
        spec = dict(directory=str(self.job), wt=str(self.wt), round=1, user_id="u", job="fixture", lock_dir=str(self.root / "locks"),
                    max_heavy=1, bin=str(self.root), cmd=command, timeout=30, sync="direct", branch="b", handoff_version=1, agent=None)
        path = self.root / "spec.json"
        path.write_text(json.dumps(spec))
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with patch.object(native, "git", return_value=subprocess.CompletedProcess([], 0, "", "")):
            native.run_job(str(path))
        return self.receipt()

    def test_native_records_setup_and_body(self):
        data = self.run_native("sleep 1", setup="sleep 1")
        self.assertGreaterEqual(data["setup_seconds"], 1)
        self.assertGreaterEqual(data["body_seconds"], 1)

    def test_native_skipped_setup_and_failed_command(self):
        data = self.run_native("exit 5", setup="true", setup_done=True)
        self.assertEqual(data["setup_seconds"], 0)
        self.assertEqual(data["setup_note"], "已跳过")
        self.assertEqual(data["executor_exit"], 5)
        self.assertIn("body_seconds", data)


class RolloutTests(JobFixture, unittest.TestCase):
    def finish(self, enabled=True):
        env = dict(os.environ)
        env.pop("RD_ROLLOUT_STATS", None)
        if enabled:
            env["RD_ROLLOUT_STATS"] = "1"
        with patch.dict(os.environ, env, clear=True):
            return messages.finish(self.job, "fixture", 1, 0, agent="codex")

    def rollout(self, text):
        folder = self.job / "codex" / "sessions" / "2026" / "10" / "29"
        folder.mkdir(parents=True)
        (folder / "rollout-x.jsonl").write_bytes(text)

    def test_corrupt_rollout_records_unavailable_and_does_not_break_finish(self):
        self.rollout(b"{not json\n\x00\xff garbage")
        data = self.finish()
        self.assertEqual(data["rollout_calls"], "未记录")
        self.assertEqual(data["executor_exit"], 0)

    def test_missing_rollout_records_unavailable(self):
        self.assertEqual(self.finish()["rollout_calls"], "未记录")

    def test_disabled_by_default_adds_nothing(self):
        self.rollout(b"{}\n")
        self.assertNotIn("rollout_calls", self.finish(enabled=False))

    def test_valid_rollout_summarizes_calls(self):
        def row(i, o):
            return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": i, "cached_input_tokens": 1, "output_tokens": o}}}})
        self.rollout(("\n".join([row(10, 2), "garbage", row(5, 3)]) + "\n").encode())
        calls = self.finish()["rollout_calls"]
        self.assertEqual(calls, dict(count=2, input_tokens=15, cached_input_tokens=2, output_tokens=5))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-history-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.recs = [
            dict(job="a", project="p1", host="m4", cmd="make test", test=True, rounds=1),
            dict(job="b", project="p1", host="m6", cmd="make test", test=True, rounds=1),
            dict(job="c", project="p2", host="m4", cmd="echo", rounds=1),
            dict(job="d", project="p1", host="m4", agent="codex", rounds=1),
            dict(job="e", project="p1", host="m4", cmd="make test", test=True, rounds=1),
        ]
        body = {"a": 600, "b": 900, "c": 1, "d": 77}
        for rec in self.recs:
            if rec["job"] in body:
                self.save(rec, dict(body_seconds=body[rec["job"]], setup_seconds=30, executor_exit=0))

    def save(self, rec, report):
        path = Path(self.state) / "results" / rec["job"] / "r1.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(job=rec["job"], round=1, host=rec["host"], report=report)))

    def test_filters_by_project_host_and_kind(self):
        rows = timings.history(self.state, self.recs, project="p1", host="m4", kind="test")
        self.assertEqual([(r["job"], r["body_seconds"]) for r in rows], [("a", 600)])
        self.assertEqual({r["job"] for r in timings.history(self.state, self.recs, project="p1")}, {"a", "b", "d"})
        self.assertEqual([r["job"] for r in timings.history(self.state, self.recs, kind="command")], ["c"])

    def test_missing_receipt_or_timing_is_skipped_and_median_per_host(self):
        rows = timings.history(self.state, self.recs, project="p1", kind="test")
        self.assertEqual({r["job"] for r in rows}, {"a", "b"})
        self.assertEqual(timings.median_body_by_host(rows), {"m4": 600, "m6": 900})


class ConfigAndCommandTests(HistoryTests):
    def test_rollout_flag_defaults_off_and_reads_features(self):
        cfg = rd.Config()
        cfg.cp.read_string("[host:x]\nssh = local\nworkdir = ~/w\n")
        self.assertEqual(cfg.host("x")["rollout_stats"], "0")
        cfg.cp.read_string("[features]\nrollout_stats = true\n")
        self.assertEqual(cfg.host("x")["rollout_stats"], "1")

    def test_timings_command_prints_rows_and_medians(self):
        import argparse, contextlib, io
        out = io.StringIO()
        args = argparse.Namespace(project="p1", host=None, kind="test")
        with patch.object(rd, "STATE", str(self.state)), patch.object(rd, "require_user", return_value="u"), \
             patch.object(rd, "all_jobs", return_value=self.recs), contextlib.redirect_stdout(out):
            rd.cmd_timings(args, None)
        data = json.loads(out.getvalue())
        self.assertEqual({r["job"] for r in data["rows"]}, {"a", "b"})
        self.assertEqual(data["median_body_seconds_by_host"], {"m4": 600, "m6": 900})


if __name__ == "__main__":
    unittest.main()
