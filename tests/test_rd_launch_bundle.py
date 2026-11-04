"""SSH 连接复用与启动打包上传。"""
import contextlib
import io
import os
import subprocess
import tarfile
import unittest
from unittest.mock import patch

from remote_dispatch import rd_artifacts as artifacts
from remote_dispatch import rd_lock
from remote_dispatch import rd_query as query
from test_rd import rd
from test_rd_artifact_fingerprint import Fixture


class SshOptionTests(Fixture):
    def test_defaults_gain_multiplexing_with_short_control_path(self):
        opts = rd.with_ssh_multiplexing(rd.DEFAULTS["ssh_opts"])
        for part in ("ControlMaster=auto", "ControlPersist=60", "ControlPath=~/.ssh/rd-%C"):
            self.assertIn(part, opts)
        self.assertNotIn("%h", opts)  # 不用会变长的主机名占位

    def test_user_written_control_options_are_never_overridden(self):
        for text in ("-o ControlMaster=no", "-o controlpath=/tmp/x", "-o ControlPersist=5m -o BatchMode=yes"):
            self.assertEqual(rd.with_ssh_multiplexing(text), text)

    def test_custom_ssh_opts_without_control_get_multiplexing_appended(self):
        opts = rd.with_ssh_multiplexing("-o ConnectTimeout=3 -i /key")
        self.assertTrue(opts.startswith("-o ConnectTimeout=3 -i /key "))
        self.assertIn("ControlMaster=auto", opts)

    def test_windows_initiator_skips_multiplexing(self):
        with patch.object(rd.os, "name", "nt"):
            self.assertEqual(rd.with_ssh_multiplexing("-o BatchMode=yes"), "-o BatchMode=yes")

    def test_host_config_applies_it_to_ssh_git_and_rsync_paths(self):
        with open(rd.CONFIG, "a") as stream:
            stream.write("\n[host:plain]\nssh=plain.invalid\n[host:own]\nssh=own.invalid\nssh_opts=-o ControlMaster=no\n")
        cfg = rd.Config()
        plain, own = cfg.host("plain"), cfg.host("own")
        self.assertIn("ControlMaster=auto", plain["ssh_opts"])
        self.assertIn("ControlMaster=auto", rd.git_ssh_env(plain)["GIT_SSH_COMMAND"])
        self.assertEqual(own["ssh_opts"], "-o ControlMaster=no")


class LaunchBundleTests(Fixture):
    def counted_launch(self, record, **changes):
        """用真实 launch 派出，并统计 launch 阶段的 ssh 调用与脚本。"""
        calls = []

        def ssh(host, script, **kwargs):
            calls.append(script)
            return self.t.local_shell(host, script, **kwargs)
        host = self.t.cfg.host("fixture")
        host = rd.scoped_host(host, "employee_a")
        record.update(changes)
        with patch.object(rd, "ssh", side_effect=ssh):
            self.t.real_launch(host, record, 1, "true", changes.get("_prompt"), 10)
        return calls

    def dispatched(self):
        with patch.object(rd, "launch", side_effect=self.t.finish):
            return self.t.dispatch()

    def test_command_launch_is_one_ssh_once_libs_are_on_the_target(self):
        first = self.dispatched()
        calls = self.counted_launch(first)
        self.assertLessEqual(len(calls), 2)  # 首次：库缺失 -> 带脚本重试
        second = self.dispatched()
        self.assertEqual(len(self.counted_launch(second)), 1)  # 之后：同摘要不重传
        job = self.worker / "jobs" / second["job"]
        self.assertEqual(os.stat(job / "rd_messages.py").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(job / "run.r1.sh").st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(self.worker / "bin" / "rd-heavy").st_mode & 0o777, 0o755)
        self.assertTrue((job / "reports" / "dispatch.r1.json").is_file())
        self.assertEqual((job / "cmd.r1.sh").read_text(), "true\n")
        self.assertEqual((job / "rd_messages.py").read_bytes(), rd.read_text_file("rd_messages.py"))
        self.assertEqual([p.name for p in self.worker.glob(".up.*")], [])  # 临时目录已清理

    def test_agent_launch_with_profile_is_still_one_ssh_after_warmup(self):
        warm = self.dispatched()
        self.counted_launch(warm, agent="codex", isolated=True, profile=dict(model="m"))
        record = self.dispatched()
        calls = self.counted_launch(record, agent="codex", isolated=True, profile=dict(model="m"),
                                    _prompt="hello")
        self.assertEqual(len(calls), 1)
        job = self.worker / "jobs" / record["job"]
        for name in ("profile.json", "rd_profiles.py", "prompt.r1.md", "rd_cache.py"):
            self.assertTrue((job / name).is_file(), name)
        self.assertTrue(record["profile_script_uploaded"])

    def test_stale_lib_size_is_repaired_by_retry(self):
        warm = self.dispatched()
        self.counted_launch(warm)
        for lib in (self.worker / "lib").rglob("rd_messages.py"):
            lib.write_text("truncated")
        record = self.dispatched()
        calls = self.counted_launch(record)
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.worker / "jobs" / record["job"] / "rd_messages.py").read_bytes(),
                         rd.read_text_file("rd_messages.py"))

    def test_failed_upload_dies_without_starting_the_job(self):
        record = self.dispatched()
        host = rd.scoped_host(self.t.cfg.host("fixture"), "employee_a")
        done = subprocess.CompletedProcess([], 1, "", "disk full")
        with patch.object(rd, "ssh", return_value=done), self.assertRaises(SystemExit):
            self.t.real_launch(host, record, 1, "true", None, 10)
        self.assertFalse((self.worker / "jobs" / record["job"] / "pid").exists())

    def test_tar_stream_keeps_modes_and_names(self):
        with rd.tar_stream([("jobs/x/run.sh", b"echo", "755"), ("a b/中.txt", b"1", "600")]) as stream:
            with tarfile.open(fileobj=stream) as archive:
                modes = {m.name: m.mode for m in archive.getmembers()}
        self.assertEqual(modes, {"jobs/x/run.sh": 0o755, "a b/中.txt": 0o600})

    def test_windows_target_keeps_per_file_native_uploads(self):
        record = self.dispatched()
        host = dict(rd.scoped_host(self.t.cfg.host("fixture"), "employee_a"), platform="windows")
        uploads = []

        def request(host, operation, data=None, **kwargs):
            uploads.append(operation if operation != "upload" else data["relative"])
        with patch.object(rd.native, "request", side_effect=request), patch.object(rd, "ssh") as ssh:
            self.t.real_launch(host, record, 1, "", "p", 10)
        ssh.assert_not_called()
        job = record["job"]
        self.assertEqual(uploads, [f"jobs/{job}/rd_messages.py", f"jobs/{job}/rd_cache.py",
                                   f"jobs/{job}/reports/dispatch.r1.json", "launch"])


if __name__ == "__main__":
    unittest.main()
