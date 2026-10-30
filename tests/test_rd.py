"""本地临时 worker 回归；不连接真实设备，不调用模型。"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import getpass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from remote_dispatch import rd_profiles as profiles
from remote_dispatch import rd_native as native

from remote_dispatch import cli as rd


class FixtureBase:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rd-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "config.toml").write_text('approval_policy="never"\nsandbox_mode="workspace-write"\nmodel="fixture-a"\n')
        (self.source / "AGENTS.md").write_text("公司规范\nEMPLOYEE_RULE_MARKER\n")
        (self.source / "git.json").write_text(json.dumps({"name": "RD Fixture", "email": "fixture@example.invalid"}))
        self.state = self.root / "state"

    def make_bundle(self, uid="employee_a"):
        return profiles.bundle(self.source, uid, self.state)

    def install(self, uid="employee_a"):
        manifest, payload = self.make_bundle(uid)
        worker = self.root / "worker" / "users" / uid
        destination = worker / "profiles" / manifest["fingerprint"]
        destination.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-xf", "-", "-C", str(destination)], input=payload, check=True)
        job = worker / "jobs" / "fixture-job"
        job.mkdir(parents=True, exist_ok=True)
        return manifest, job


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.records = {name: {"job": name, "user_id": "employee_a", "host": "fixture", "project": name}
                        for name in ("first", "second")}

    def watch(self, states, ticks=(0, 0), timeout=900, jobs=None, cfg=None):
        output = io.StringIO()
        with patch.object(rd, "require_user", return_value="employee_a"), \
             patch.object(rd, "load_job", side_effect=self.records.__getitem__), \
             patch.object(rd, "job_states", side_effect=states) as checks, \
             patch.object(rd.time, "monotonic", side_effect=ticks), \
             patch.object(rd.time, "sleep") as sleep, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit) as result:
            rd.cmd_watch(argparse.Namespace(jobs=jobs or list(self.records), timeout=timeout), cfg)
        payload = json.loads(output.getvalue()) if output.getvalue() else None
        return result.exception.code, payload, checks, sleep

    def test_one_initiator_deadline_checks_multiple_jobs_and_wakes_at_fifteen_minutes(self):
        pending = {name: {"state": "running"} for name in self.records}
        code, event, checks, sleep = self.watch([pending] * 3, ticks=(0, 0, 899, 900))
        self.assertEqual(code, 2)
        self.assertEqual(event["event"], "progress_due")
        self.assertEqual([x["job"] for x in event["jobs"]], ["first", "second"])
        self.assertEqual(checks.call_count, 3)
        for call in checks.call_args_list:
            self.assertEqual(call.args[1], list(self.records.values()))
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [30, 1])

    def test_completion_wakes_initiator_early_and_preserves_pending_jobs_without_raw_logs(self):
        states = {"first": {"state": "done", "exit": "0", "last": "PRIVATE_LOG_MARKER"},
                  "second": {"state": "running", "last": "PRIVATE_LOG_MARKER"}}
        code, event, checks, sleep = self.watch([states])
        self.assertEqual(code, 0)
        self.assertEqual(event["event"], "task_finished")
        self.assertEqual(event["finished"], ["first"])
        self.assertEqual(event["jobs"][1]["state"], "running")
        self.assertNotIn("PRIVATE_LOG_MARKER", json.dumps(event))
        sleep.assert_not_called()

    def test_failure_and_unreachable_wake_as_alerts_without_claiming_completion(self):
        for status, expected in [(dict(state="done", exit="124"), "task_finished"),
                                 (dict(state="unreachable"), "monitor_error")]:
            with self.subTest(status=status):
                code, event, _, _ = self.watch([{"first": status}], jobs=["first"])
                self.assertEqual(code, 1)
                self.assertEqual(event["event"], expected)
                self.assertEqual(event["finished"], ["first"] if status["state"] == "done" else [])

    def test_early_wake_can_reuse_remaining_shared_interval(self):
        code, event, _, sleep = self.watch([{"second": {"state": "running"}}] * 2,
                                          ticks=(0, 0, 240), timeout=240, jobs=["second"])
        self.assertEqual((code, event["event"]), (2, "progress_due"))
        sleep.assert_called_once_with(30)

    def test_other_employee_is_rejected_before_any_remote_check(self):
        self.records["second"]["user_id"] = "employee_b"
        code, event, checks, _ = self.watch([])
        self.assertEqual(code, 4)
        self.assertIsNone(event)
        checks.assert_not_called()

    def test_invalid_timeout_is_rejected_before_any_remote_check(self):
        code, event, checks, _ = self.watch([], timeout=0)
        self.assertEqual(code, 4)
        self.assertIsNone(event)
        checks.assert_not_called()

    def test_configured_interval_is_used_when_no_single_wait_override(self):
        cfg = object.__new__(rd.Config)
        cfg.cp = rd.configparser.ConfigParser()
        cfg.cp.read_dict({"monitor": {"report_interval_seconds": "60"}})
        with patch.dict(os.environ, {"RD_REPORT_INTERVAL_SECONDS": ""}):
            code, event, _, _ = self.watch([{"first": {"state": "running"}}],
                                           ticks=(0, 60), timeout=None, jobs=["first"], cfg=cfg)
        self.assertEqual((code, event["event"]), (2, "progress_due"))

    def test_single_wait_override_precedes_interval_configuration(self):
        cfg = object.__new__(rd.Config)
        with patch.object(cfg, "report_interval", side_effect=AssertionError("must not read interval config")) as interval:
            code, event, _, _ = self.watch([{"first": {"state": "running"}}],
                                           ticks=(0, 60), timeout=60, jobs=["first"], cfg=cfg)
        self.assertEqual((code, event["event"]), (2, "progress_due"))
        interval.assert_not_called()


class AssessSwapTests(unittest.TestCase):
    def info(self, swap, swapfree="4096.00M", swapio="0", memfree="50"):
        return {"reachable": True, "os": "Darwin", "ncpu": "10", "load1": "1",
                "memfree": memfree, "disk": "100", "swap": swap, "swapfree": swapfree, "swapio": swapio}

    def test_cold_swap_used_high_passes_with_note(self):
        info = self.info("18944.00M", swapio="52")
        self.assertEqual(rd.assess(dict(rd.DEFAULTS), info), [])
        self.assertTrue(rd.notes(dict(rd.DEFAULTS), info)[0].startswith("swap 18.5 GB used"))
        self.assertIn("not counted as busy", rd.fmt_probe(dict(rd.DEFAULTS, name="m4", ssh="m4"), info, []))

    def test_swap_under_threshold_has_no_note(self):
        info = self.info("1043.94M")
        self.assertEqual(rd.assess(dict(rd.DEFAULTS), info), [])
        self.assertEqual(rd.notes(dict(rd.DEFAULTS), info), [])

    def test_memory_pressure_blocks(self):
        why = rd.assess(dict(rd.DEFAULTS), self.info("1043.94M", memfree="5"))
        self.assertTrue(any(w.startswith("only 5% memory free") for w in why), why)

    def test_heavy_paging_blocks(self):
        why = rd.assess(dict(rd.DEFAULTS), self.info("1043.94M", swapio=str(20 * 1024)))
        self.assertTrue(any(w.startswith("swapping 20.0 MB/s") for w in why), why)

    def test_swap_nearly_full_while_paging_blocks(self):
        why = rd.assess(dict(rd.DEFAULTS), self.info("18944.00M", swapfree="512.00M", swapio=str(2 * 1024)))
        self.assertTrue(any(w.startswith("swap nearly full (0.5 GB free)") for w in why), why)

    def test_swap_nearly_full_but_idle_passes(self):
        self.assertEqual(rd.assess(dict(rd.DEFAULTS), self.info("18944.00M", swapfree="512.00M", swapio="40")), [])

    def test_swap_unknown_does_not_block(self):
        self.assertEqual(rd.assess(dict(rd.DEFAULTS), self.info("", swapfree="", swapio="")), [])

    def test_thresholds_follow_host_config(self):
        host = dict(rd.DEFAULTS, max_swap_io_mb_s="1")
        self.assertTrue(rd.assess(host, self.info("1043.94M", swapio=str(2 * 1024))))

    def test_windows_pagefile_probe(self):
        done = lambda out: subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch.object(native.subprocess, "run", return_value=done("16384\r\n")):
            self.assertEqual(native.win_pagefile_used(), "16384M")
        with patch.object(native.subprocess, "run", return_value=done("")):
            self.assertEqual(native.win_pagefile_used(), "?")
        with patch.object(native.subprocess, "run", side_effect=OSError("no powershell")):
            self.assertEqual(native.win_pagefile_used(), "?")
        info = {"reachable": True, "os": "Windows", "ncpu": "8", "cpu_pct": "20",
                "memfree": "50", "disk": "100", "swap": "16384M"}
        self.assertEqual(rd.assess(dict(rd.DEFAULTS), info), [])
        self.assertIn("paging rate unknown", rd.notes(dict(rd.DEFAULTS), info)[0])


class MonitorConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = object.__new__(rd.Config)
        self.cfg.cp = rd.configparser.ConfigParser()

    def test_interval_defaults_and_environment_overrides_local_config(self):
        with patch.dict(os.environ, {"RD_REPORT_INTERVAL_SECONDS": ""}):
            self.assertEqual(self.cfg.report_interval(), 900)
            self.cfg.cp.read_dict({"monitor": {"report_interval_seconds": "300"}})
            self.assertEqual(self.cfg.report_interval(), 300)
        with patch.dict(os.environ, {"RD_REPORT_INTERVAL_SECONDS": "600"}):
            self.assertEqual(self.cfg.report_interval(), 600)

    def test_bad_environment_or_config_intervals_stop_cleanly(self):
        for value in ("0", "-5", "abc", "1.5"):
            for source in ("environment", "config"):
                with self.subTest(value=value, source=source):
                    self.cfg.cp.read_dict({"monitor": {"report_interval_seconds": value if source == "config" else "900"}})
                    with patch.dict(os.environ, {"RD_REPORT_INTERVAL_SECONDS": value if source == "environment" else ""}), \
                         contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        self.cfg.report_interval()
                    self.assertEqual(error.exception.code, 4)


class ProfileTests(FixtureBase, unittest.TestCase):
    def test_permission_config_serialization_preserves_nested_settings(self):
        data = {"model": "fixture-a", "agents": {"reviewer": {"description": "中文代理", "config_file": "agents/reviewer.toml"}},
                "skills": {"config": [{"path": "C:\\目录\\SKILL.md", "enabled": False}]}, "effort": 0.5}
        text = "\n".join(json.dumps(k) + "=" + profiles.toml_value(v) for k, v in data.items())
        self.assertEqual(profiles.tomllib.loads(text), data)

    def test_workspace_permissions_allow_task_git_queue_and_default_network(self):
        seed = self.root / "seed"
        seed.mkdir()
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
        for label, access in (("default", None), ("offline", False), ("online", True)):
            with self.subTest(network=label):
                text = 'approval_policy="never"\nsandbox_mode="workspace-write"\nmodel="fixture-a"\n'
                if access is not None:
                    text += '[sandbox_workspace_write]\nnetwork_access=' + str(access).lower() + '\n'
                (self.source / "config.toml").write_text(text)
                manifest, job = self.install("employee_" + label)
                worker = job.parent.parent
                bare = worker / "repos/fixture.git"
                bare.parent.mkdir()
                subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
                wt = worker / "wt" / job.name
                wt.parent.mkdir()
                subprocess.run(["git", "-C", str(bare), "worktree", "add", "-q", "-b", "fixture-task", str(wt)], check=True)
                with patch.object(profiles, "host_skill_paths", return_value=[]):
                    home = profiles.prepare_runtime(job, manifest)
                config = profiles.tomllib.loads((home / "config.toml").read_text())
                self.assertEqual(config["model"], "fixture-a")
                self.assertNotIn("sandbox_mode", config)
                permission = config["permissions"][config["default_permissions"]]
                self.assertEqual(permission["extends"], ":workspace")
                self.assertTrue(permission["network"]["enabled"])
                expected = {str(bare.resolve()), str((bare / "worktrees" / job.name).resolve()), str((self.root / "worker/locks").resolve())}
                self.assertEqual(permission["filesystem"], dict.fromkeys(expected, "write"))

    def test_id_is_explicit_and_path_safe(self):
        for value in ("", "../other", "alice/bob", "a b", "a;cmd", "a" * 65, "Employee_A", "con", "lpt1"):
            with self.assertRaises(ValueError):
                profiles.user_id(value)
        self.assertEqual(profiles.user_id("employee_a-123"), "employee_a-123")

    def test_runtime_rejects_job_outside_employee_users_namespace(self):
        manifest, job = self.install()
        users = job.parent.parent.parent
        moved = users.with_name("employees")
        users.rename(moved)
        job = moved / job.relative_to(users)
        with patch.object(profiles, "host_skill_paths", return_value=[]), self.assertRaises(ValueError):
            profiles.prepare_runtime(job, manifest)

    def test_invalid_default_id_stops_before_any_repo_or_remote_work(self):
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(rd, "CONFIG", str(self.root / "config.ini")), \
             patch.object(getpass, "getuser", return_value="中文 !!!"), \
             patch.object(rd, "local_repo") as repo:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                rd.cmd_run(argparse.Namespace(), None)
            self.assertEqual(failure.exception.code, 4)
            repo.assert_not_called()

    def test_one_week_boundary_and_content_equality(self):
        remote = {"fingerprint": "a", "source_updated_at": 100}
        local = {"fingerprint": "b", "source_updated_at": 100 + profiles.WEEK}
        self.assertEqual(profiles.comparison(local, remote), "different")
        local["source_updated_at"] += 1
        self.assertEqual(profiles.comparison(local, remote), "stale")
        local["fingerprint"] = "a"
        self.assertEqual(profiles.comparison(local, remote), "same")
        local.update(fingerprint="b", source_updated_at=99)
        self.assertEqual(profiles.comparison(local, remote), "remote-newer")
        self.assertEqual(profiles.comparison(None, remote), "unknown")

    def test_unchanged_mtime_does_not_change_version_and_deletion_does(self):
        tool = self.source / "tools" / "fixture.txt"
        tool.parent.mkdir()
        tool.write_text("fixture")
        first, _ = self.make_bundle()
        os.utime(tool, (time.time(), time.time()))
        second, _ = self.make_bundle()
        self.assertEqual(first, second)
        tool.unlink()
        third, _ = self.make_bundle()
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])
        self.assertNotIn("tools/fixture.txt", third["files"])

    def test_credentials_and_symlinks_cannot_enter_bundle(self):
        auth = self.source / "auth.json"
        auth.write_text('{"fixture":"not-a-real-token"}')
        with self.assertRaises(ValueError):
            self.make_bundle()
        auth.unlink()
        link = self.source / "tools"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.make_bundle()

    def test_local_paths_and_inline_secrets_are_rejected(self):
        for extra in ('[mcp_servers.fixture]\ncommand="/Users/alice/tool"\n', '[model_providers.fixture]\napi_key="synthetic-fixture"\n'):
            (self.source / "config.toml").write_text('approval_policy="never"\n' + extra)
            with self.assertRaises(ValueError):
                self.make_bundle()

    def test_target_paths_and_resume_snapshot_stay_separate(self):
        (self.source / "config.toml").write_text('approval_policy="never"\n[mcp_servers.fixture]\ncommand="python3"\nargs=["@RD_PROFILE@/tools/fixture.py"]\n')
        tools = self.source / "tools"
        tools.mkdir()
        (tools / "fixture.py").write_text("pass\n")
        a, job_a = self.install("employee_a")
        b, job_b = self.install("employee_b")
        with patch.object(profiles, "host_skill_paths", return_value=["/host/skills/private/SKILL.md"]):
            home_a = profiles.prepare_runtime(job_a, a)
            home_b = profiles.prepare_runtime(job_b, b)
        config = profiles.tomllib.loads((home_a / "config.toml").read_text())
        self.assertNotEqual(home_a, home_b)
        self.assertEqual(config["mcp_servers"]["fixture"]["args"], [str(home_a / "tools/fixture.py")])
        self.assertFalse(config["skills"]["config"][0]["enabled"])
        self.assertEqual(config["sqlite_home"], str(home_a))
        (self.source / "AGENTS.md").write_text("公司规范\nNEW_RULE_MARKER\n")
        new, _ = self.install("employee_a")
        self.assertEqual(profiles.prepare_runtime(job_a, a), home_a)
        self.assertNotIn("NEW_RULE_MARKER", (home_a / "AGENTS.md").read_text())
        with self.assertRaises(ValueError):
            profiles.prepare_runtime(job_a, new)

    def test_unlisted_remote_files_are_not_copied(self):
        manifest, job = self.install()
        version = job.parent.parent / "profiles" / manifest["fingerprint"]
        (version / "auth.json").write_text('{"fixture":true}')
        with patch.object(profiles, "host_skill_paths", return_value=[]):
            runtime = profiles.prepare_runtime(job, manifest)
        self.assertFalse((runtime / "auth.json").exists())

    def test_manifest_rejects_other_user_and_traversal(self):
        manifest, _ = self.make_bundle()
        with self.assertRaises(ValueError):
            profiles.validate_manifest(manifest, "employee_b")
        manifest["files"]["../../outside"] = next(iter(manifest["files"].values()))
        with self.assertRaises(ValueError):
            profiles.validate_manifest(manifest, "employee_a")


    def test_missing_auth_can_be_fixed_without_partial_runtime(self):
        manifest, job = self.install()
        auth = self.root / "approved-auth.json"
        with self.assertRaises(ValueError):
            profiles.prepare_runtime(job, manifest, str(auth))
        self.assertFalse((job / "codex").exists())
        auth.write_text('{"fixture":true}')
        with patch.object(profiles, "host_skill_paths", return_value=[]):
            home = profiles.prepare_runtime(job, manifest, str(auth))
        self.assertEqual((home / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_resume_keeps_auth_and_refresh_overwrites_only_auth(self):
        manifest, job = self.install()
        auth = self.root / "approved-auth.json"
        auth.write_text('{"token":"first"}')
        with patch.object(profiles, "host_skill_paths", return_value=[]):
            home = profiles.prepare_runtime(job, manifest, str(auth))
            (home / "AGENTS.md").write_text("员工副本已改")
            auth.write_text('{"token":"second"}')
            profiles.prepare_runtime(job, manifest, str(auth))
            self.assertEqual((home / "auth.json").read_text(), '{"token":"first"}')
            profiles.prepare_runtime(job, manifest, str(auth), refresh=True)
        self.assertEqual((home / "auth.json").read_text(), '{"token":"second"}')
        self.assertEqual((home / "auth.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((home / "AGENTS.md").read_text(), "员工副本已改")

    def test_first_round_without_auth_needs_refresh_to_copy_later(self):
        manifest, job = self.install()
        auth = self.root / "approved-auth.json"
        auth.write_text('{"token":"late"}')
        with patch.object(profiles, "host_skill_paths", return_value=[]):
            home = profiles.prepare_runtime(job, manifest)
            profiles.prepare_runtime(job, manifest, str(auth))
            self.assertFalse((home / "auth.json").exists())
            profiles.prepare_runtime(job, manifest, str(auth), refresh=True)
        self.assertTrue((home / "auth.json").exists())

    def test_agent_resource_paths_are_mapped_to_target_job(self):
        (self.source / "config.toml").write_text('approval_policy="never"\n[agents.fixture]\nconfig_file="agents/fixture.toml"\n')
        agents = self.source / "agents"
        agents.mkdir()
        (agents / "fixture.toml").write_text('developer_instructions="Use @RD_PROFILE@/tools/fixture.py"\n')
        manifest, job = self.install()
        with patch.object(profiles, "host_skill_paths", return_value=[]):
            home = profiles.prepare_runtime(job, manifest)
        self.assertIn(str(home / "tools/fixture.py"), (home / "agents/fixture.toml").read_text())


class HeavyQueueTests(FixtureBase, unittest.TestCase):
    def test_permission_denied_pid_probe_does_not_steal_live_slot(self):
        directory = self.root / "locks"
        slot = directory / "heavy.1"
        slot.mkdir(parents=True)
        (slot / "pid").write_text(str(os.getpid()))
        marker = self.root / "started"
        helper = Path(rd.HERE, "bin", "rd-heavy")
        command = 'kill() { echo "Operation not permitted" >&2; return 1; }; . "$1" sh -c \'touch "$1"\' fixture "$2"'
        process = subprocess.Popen(["sh", "-c", command, "fixture", str(helper), str(marker)],
                                   env=dict(os.environ, RD_LOCK_DIR=str(directory), RD_MAX_HEAVY="1"),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 0.5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(marker.exists(), "live slot was stolen after an inconclusive PID probe")
            self.assertEqual((slot / "pid").read_text(), str(os.getpid()))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)

    def test_confirmed_dead_holder_releases_slot(self):
        dead = subprocess.Popen(["sh", "-c", "exit 0"])
        dead.wait(timeout=5)
        directory = self.root / "locks"
        slot = directory / "heavy.1"
        slot.mkdir(parents=True)
        (slot / "pid").write_text(str(dead.pid))
        helper = Path(rd.HERE, "bin", "rd-heavy")
        result = subprocess.run(["sh", str(helper), "printf", "queue-ready"],
                                env=dict(os.environ, RD_LOCK_DIR=str(directory), RD_MAX_HEAVY="1", LC_ALL="C"),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "queue-ready")
        self.assertFalse(slot.exists())


class NativeTests(FixtureBase, unittest.TestCase):
    def test_background_launch_failure_is_reported_as_failed(self):
        root = self.root / "worker/users/employee_a"
        spec = {"job": "employee_a-failed", "round": 1, "profile": None}
        q = {"operation": "launch", "workdir": str(root), "shared_workdir": str(self.root / "worker"), "path": "", "spec": spec,
             "prompt": "", "profile_code": Path(profiles.__file__).read_text(), "worker_code": Path(native.__file__).read_text()}
        with patch.object(native.subprocess, "Popen", side_effect=OSError("fixture denied")), self.assertRaises(ValueError):
            native.handle(q, io.BytesIO())
        status = native.job_state(root, spec["job"])
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["exit"], "98")

    def test_windows_wire_preserves_binary_without_shell_interpolation(self):
        import base64
        payload = bytes(range(256)) * 300
        host = {"workdir": "C:/rd/users/employee_a", "path": "", "python": "C:/Program Files/Python/python.exe", "ssh": "fixture", "ssh_opts": "-o BatchMode=yes"}
        def transport(cmd, stdin, stdout, **kwargs):
            header = json.loads(stdin.readline())
            self.assertEqual(header["payload_encoding"], "base64")
            self.assertEqual(b"".join(base64.b64decode(line.strip(), validate=True) for line in stdin), payload)
            self.assertNotIn(host["workdir"], " ".join(cmd))
            stdout.write(json.dumps({"ok": True, "result": None, "binary_encoding": "base64"}).encode() + b"\n")
            stdout.write(base64.b64encode(payload) + b"\n")
            return subprocess.CompletedProcess(cmd, 0, None, b"")
        output = io.BytesIO()
        with patch.object(native.subprocess, "run", side_effect=transport):
            native.request(host, "fixture", payload=payload, output=output)
        self.assertEqual(output.getvalue(), payload)

    def test_powershell_uses_utf16_and_quotes_path_arguments(self):
        script = "& " + native.ps_quote("C:\\员工 目录\\it's python.exe") + " 'argument'"
        encoded = native.powershell(script)
        self.assertEqual(encoded[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
        import base64
        self.assertEqual(base64.b64decode(encoded[4]).decode("utf-16-le"), script)
        self.assertIn("it''s", script)

    def test_native_sync_preserves_utf8_and_id_namespace(self):
        manifest, payload = self.make_bundle()
        worker = self.root / "worker space 中文" / "users" / "employee_a"
        q = {"workdir": str(worker), "shared_workdir": str(self.root / "worker space 中文"), "path": "",
             "operation": "profile-sync", "expected": None, "manifest": manifest, "user_id": "employee_a"}
        native.handle(q, io.BytesIO(payload))
        loaded = native.handle(dict(q, operation="profile-read"), io.BytesIO())
        self.assertEqual(loaded["root"], str(worker.resolve()))
        self.assertEqual(loaded["manifest"], manifest)
        self.assertIn("公司规范", (worker / "profiles" / manifest["fingerprint"] / "AGENTS.md").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            native.handle(q, io.BytesIO(payload))

    def test_native_archive_cannot_escape_employee_directory(self):
        import tarfile
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            entry = tarfile.TarInfo("../../outside")
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
        payload.seek(0)
        with self.assertRaises(ValueError):
            native.extract(payload, self.root / "employee")
        self.assertFalse((self.root.parent / "outside").exists())

    def test_windows_environment_keeps_required_os_variables(self):
        manifest, job = self.install()
        with patch.object(profiles, "host_skill_paths", return_value=[]):
            home = profiles.prepare_runtime(job, manifest)
        env = profiles.runtime_environment(home, {"SYSTEMROOT": "C:\\Windows", "PATHEXT": ".EXE;.CMD", "USERPROFILE": "C:\\Users\\fixture", "UNRELATED_SECRET": "synthetic-fixture"})
        self.assertEqual(env["SYSTEMROOT"], "C:\\Windows")
        self.assertEqual(env["PATHEXT"], ".EXE;.CMD")
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertEqual(env["CODEX_HOME"], str(home))

    def test_native_git_bundle_and_background_command_protocol(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
        sha = rd.git(["rev-parse", "HEAD"], str(repo)).stdout.strip()
        root = self.root / "native worker 中文" / "users" / "employee_a"
        shared = self.root / "native worker 中文"
        host = {"workdir": str(root), "shared_workdir": str(shared), "platform": "windows", "path": ""}
        rec = {"job": "employee_a-fixture", "bare": "repos/fixture.git", "branch": "rd/employee_a/fixture", "sync": "direct"}
        def local_rpc(h, operation, data=None, payload=None, **kwargs):
            q = dict(data or {}, operation=operation, workdir=h["workdir"], shared_workdir=h["shared_workdir"], path="")
            return native.handle(q, payload or io.BytesIO())
        with patch.object(native, "request", side_effect=local_rpc):
            rd.native_init(host, rec, str(repo), sha, None)
        self.assertNotIn("refs/rd-transfer/", rd.git(["show-ref"], str(repo), check=False).stdout)
        self.assertEqual(native.git(root / rec["bare"], "rev-parse", rec["branch"]).stdout.strip(), sha)
        spec = dict(rec, round=1, user_id="employee_a", profile=None, timeout=10, max_heavy=1, cmd='printf "完成"', heavy=False)
        q = {"operation": "launch", "workdir": str(root), "shared_workdir": str(shared), "path": "", "spec": spec,
             "prompt": "", "profile_code": Path(profiles.__file__).read_text(), "worker_code": Path(native.__file__).read_text()}
        children = []
        start = subprocess.Popen
        def capture_child(*args, **kwargs):
            child = start(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(native.subprocess, "Popen", side_effect=capture_child):
            launched = native.handle(q, io.BytesIO())
        deadline = time.monotonic() + 20
        directory = root / "jobs" / rec["job"]
        while not (directory / "exit_code").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual((directory / "exit_code").read_text(), "0")
        self.assertIn("完成", (directory / "stdout.r1.log").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((directory / "run.r1.json").read_text())["lock_dir"], str(shared.resolve() / "locks"))
        self.assertEqual(children[0].wait(timeout=5), 0)
        self.assertTrue((root / "bin/rd-heavy.cmd").exists())
        capture = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch.object(sys, "stdout", capture):
            native.serve({"operation": "fetch-bundle", "workdir": str(root), "bare": rec["bare"], "branch": rec["branch"]})
        capture.flush()
        capture.buffer.seek(0)
        self.assertTrue(json.loads(capture.buffer.readline())["ok"])
        result_bundle = self.root / "result.bundle"
        import base64
        result_bundle.write_bytes(b"".join(base64.b64decode(line.strip(), validate=True) for line in capture.buffer))
        self.assertEqual(subprocess.run(["git", "bundle", "verify", str(result_bundle)], cwd=repo, capture_output=True).returncode, 0)


class WorkerTests(FixtureBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        fake = self.bin / "codex"
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
if "--help" in sys.argv:
    print("--disable --permission-profile")
    sys.exit(0)
if sys.argv[1:] == ["features", "list"]:
    print("hooks stable true\\nplugins stable true\\nmemories stable true\\nexternal_agent_memory_import development false")
    sys.exit(0)
home = pathlib.Path(os.environ["CODEX_HOME"])
record = {"id":os.environ["RD_USER_ID"], "home":str(home), "config":(home/"config.toml").read_text(), "args":sys.argv[1:]}
pathlib.Path("result.json").write_text(json.dumps(record))
subprocess.run(["git","add","result.json"],check=True)
subprocess.run(["git","commit","-qm","fixture"],check=True)
print(json.dumps({"session_id":"fixture-session"}))
''')
        fake.chmod(0o755)
        config = self.root / "config.ini"
        config.write_text(f"[host:fixture]\nssh=not-a-real-host\nworkdir={self.root / 'worker'}\nsync=direct\npath={self.bin}:/usr/bin:/bin:/opt/homebrew/bin\n")
        self.env = patch.dict(os.environ, {"RD_USER_ID": "employee_a", "RD_PROFILE_DIR": str(self.source)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config_patch = patch.object(rd, "CONFIG", str(config))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.state_patch = patch.object(rd, "STATE", str(self.state))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.cfg = rd.Config()
        self.ssh_patch = patch.object(rd, "ssh", self.local_shell)
        self.ssh_patch.start()
        self.addCleanup(self.ssh_patch.stop)
        self.native_patch = patch.object(native, "request", side_effect=self.local_rpc)
        self.native_patch.start()
        self.addCleanup(self.native_patch.stop)

    def local_rpc(self, host, operation, data=None, **kwargs):
        return native.handle(dict(data or {}, operation=operation, workdir=host["workdir"],
                                  shared_workdir=host.get("shared_workdir", host["workdir"]), path=host["path"]),
                             io.BytesIO())

    def local_shell(self, host, script, check=True, stdin_data=None, stdin=None, timeout=None):
        shared = dict(host, workdir=host.get("shared_workdir", host["workdir"]))
        pre = f'export PATH="{host["path"]}:$PATH"; ROOT={rd.root_expr(host)}; SHARED_ROOT={rd.root_expr(shared)}; '
        p = subprocess.run(["sh", "-c", pre + script], input=stdin_data, stdin=stdin, capture_output=True,
                           text=stdin_data is not None, timeout=timeout or 20)
        out = p.stdout if isinstance(p.stdout, str) else p.stdout.decode()
        err = p.stderr if isinstance(p.stderr, str) else p.stderr.decode()
        if check and p.returncode:
            raise AssertionError(f"fixture worker shell failed ({p.returncode}): {err}")
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def sync(self):
        manifest, _ = self.make_bundle()
        with contextlib.redirect_stdout(io.StringIO()):
            rd.cmd_profile(argparse.Namespace(action="sync", host="fixture", confirm_user_id="employee_a", confirm_version=manifest["fingerprint"]), self.cfg)
        return manifest

    def test_sync_requires_matching_id_and_reviewed_version(self):
        for uid, version in [(None, None), ("employee_b", "a" * 64), ("employee_a", "a" * 64)]:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                rd.cmd_profile(argparse.Namespace(action="sync", host="fixture", confirm_user_id=uid, confirm_version=version), self.cfg)
            self.assertEqual(failure.exception.code, 4)
        self.assertFalse((self.root / "worker").exists())

    def test_sync_versions_keep_old_files_and_do_not_touch_global_config(self):
        first = self.sync()
        (self.source / "AGENTS.md").write_text("公司规范\nVERSION_TWO\n")
        second = self.sync()
        root = self.root / "worker" / "users" / "employee_a"
        self.assertTrue((root / "profiles" / first["fingerprint"] / "AGENTS.md").exists())
        self.assertIn("VERSION_TWO", (root / "profiles" / second["fingerprint"] / "AGENTS.md").read_text())
        self.assertEqual(json.loads((root / "profiles/current.json").read_text())["fingerprint"], second["fingerprint"])
        self.assertFalse((self.root / "worker" / "config.toml").exists())
        self.assertFalse((root / "profiles/.sync-lock").exists())

    def wait_success(self, job):
        with self.assertRaises(SystemExit) as complete:
            rd.cmd_wait(argparse.Namespace(job=job, timeout=10, interval=0.05), self.cfg)
        self.assertEqual(complete.exception.code, 0)

    def test_default_user_uses_own_provider_and_other_controller_warns_on_resume(self):
        config = self.source / "config.toml"
        config.write_text(config.read_text() + '\nmodel_provider="fixture"\n[model_providers.fixture]\nbase_url="https://model.example.invalid/v1"\n')
        curl = self.bin / "curl"
        curl.write_text('#!/bin/sh\ncase "$*" in *https://model.example.invalid*|*https://pypi.org*) printf 403;; *) exit 7;; esac\n')
        curl.chmod(0o755)
        self.sync()
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
        prompt = self.root / "task.md"
        prompt.write_text("Fixture only; no model API request.\n")
        args = argparse.Namespace(dir=str(repo), agent="codex", prompt_file=str(prompt), cmd=None,
            test=False, heavy=False, host="fixture", force_host=False, tag=[], ref="HEAD", wip=False,
            branch=None, name="fixture", project=None, include=[], model=None, effort=None, timeout=15,
            initiator_session="fixture-origin")
        def worker(host, script, **kwargs):
            if script == rd.PROBE:
                return subprocess.CompletedProcess([], 0, "ncpu=4 load1=0 memfree=80 disk=100", "")
            return self.local_shell(host, script, **kwargs)
        with patch.dict(os.environ, {"RD_USER_ID": ""}), \
             patch.object(getpass, "getuser", return_value="Employee_A"), \
             patch.object(rd, "ssh", side_effect=worker), \
             patch.object(profiles, "host_skill_paths", return_value=[]), \
             patch.object(rd, "scp_path", side_effect=lambda h, rel: str(Path(h["workdir"]) / rel)), \
             contextlib.redirect_stdout(io.StringIO()):
            rd.cmd_run(args, self.cfg)
            record = rd.all_jobs()[0]
            self.assertEqual(record["user_id"], "employee_a")
            self.wait_success(record["job"])
            other_config = self.root / "other-controller/config.ini"
            other_config.parent.mkdir()
            other_config.write_text(Path(rd.CONFIG).read_text())
            errors, output = io.StringIO(), io.StringIO()
            with patch.object(rd, "CONFIG", str(other_config)), \
                 contextlib.redirect_stderr(errors), contextlib.redirect_stdout(output):
                rd.cmd_resume(argparse.Namespace(job=record["job"], session=None, prompt_file=str(prompt),
                    force_host=False, model=None, effort=None, timeout=15, initiator_session="fixture-origin"), self.cfg)
                self.wait_success(record["job"])
            self.assertIn("其他控制端", errors.getvalue())
            self.assertIn("resumed ", output.getvalue())
            self.assertEqual(rd.load_job(record["job"])["rounds"], 2)

    def test_real_git_and_worker_scripts_run_fetch_and_resume_original_profile(self):
        first = self.sync()
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
        prompt = self.root / "task.md"
        prompt.write_text("Fixture only; no model API request.\n")
        args = argparse.Namespace(agent="codex", prompt_file=str(prompt), cmd=None, test=False, heavy=False,
            host="fixture", force_host=True, tag=[], ref="HEAD", wip=False, branch=None, name="fixture",
            project=None, include=[], model="override-fixture", effort="low", timeout=15, initiator_session="fixture-origin")
        with patch.object(rd, "local_repo", return_value=str(repo)), patch.object(rd, "scp_path", side_effect=lambda h, rel: str(Path(h["workdir"]) / rel)), contextlib.redirect_stdout(io.StringIO()):
            rd.cmd_run(args, self.cfg)
            rec = rd.all_jobs()[0]
            self.wait_success(rec["job"])
            rd.cmd_fetch(argparse.Namespace(job=rec["job"]), self.cfg)
            result = json.loads(rd.git(["show", "rd/" + rec["branch"] + ":result.json"], str(repo)).stdout)
            self.assertEqual(result["id"], "employee_a")
            self.assertIn("/users/employee_a/jobs/", result["home"])
            self.assertIn("override-fixture", result["args"])
            (self.source / "AGENTS.md").write_text("公司规范\nCONFIG_FOR_NEW_JOBS\n")
            second = self.sync()
            self.assertNotEqual(first["fingerprint"], second["fingerprint"])
            rd.cmd_resume(argparse.Namespace(job=rec["job"], session=None, prompt_file=str(prompt), force_host=True, model=None, effort=None, timeout=15, initiator_session="fixture-origin"), self.cfg)
            self.wait_success(rec["job"])
            rd.cmd_fetch(argparse.Namespace(job=rec["job"]), self.cfg)
            home = Path(result["home"])
            self.assertEqual(json.loads((home / "rd-profile.json").read_text())["fingerprint"], first["fingerprint"])
            self.assertNotIn("CONFIG_FOR_NEW_JOBS", (home / "AGENTS.md").read_text())
            permission = profiles.tomllib.loads((home / "config.toml").read_text())["permissions"]["rd_workspace"]["filesystem"]
            self.assertEqual(permission[str((home.parent / "reports").resolve())], "write")
            self.assertNotIn(str(home.parent.resolve()), permission)
            after = json.loads(rd.git(["show", "rd/" + rec["branch"] + ":result.json"], str(repo)).stdout)
            self.assertIn("override-fixture", after["args"])
            continued = rd.load_job(rec["job"])
            self.assertEqual(continued["initiator_session"], "fixture-origin")
            self.assertEqual(continued["receiver_session"], "fixture-session")
            self.assertEqual(continued["rounds"], 2)
            self.assertTrue(rd.messages.read_json(rd.result_path(continued, 1))["needs_initiator_review"])
        host = rd.job_host(self.cfg, rec)
        script = rd.build_run_script(host, rec, 1, 15)
        self.assertIn(str(self.root / "worker") + "/locks", script)
        self.assertNotIn(str(self.root / "worker/users/employee_a/locks"), script)
        with patch.dict(os.environ, {"RD_USER_ID": "employee_b"}), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rd.job_host(self.cfg, rec)

    def test_local_repo_with_custom_dir_and_precedence(self):
        repo_dir = self.root / "custom_repo"
        repo_dir.mkdir()
        rd.run(["git", "init", "-q", str(repo_dir)])
        resolved = rd.local_repo(repo_dir)
        self.assertEqual(resolved, str(repo_dir.resolve()))

        # Non-repo dir raises SystemExit
        non_repo = self.root / "not_a_repo"
        non_repo.mkdir()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rd.local_repo(non_repo)

        # get_dir precedence: sub dir over top dir
        ns = argparse.Namespace(dir="/sub", top_dir="/top")
        self.assertEqual(rd.get_dir(ns), "/sub")
        ns2 = argparse.Namespace(dir=None, top_dir="/top")
        self.assertEqual(rd.get_dir(ns2), "/top")

    def test_run_with_custom_workdir_override(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
        custom_remote = self.root / "custom_remote"
        args = argparse.Namespace(agent=None, prompt_file=None, cmd="echo test", test=False, heavy=False,
            host="fixture", force_host=True, tag=[], ref="HEAD", wip=False, branch=None, name="fixture",
            project=None, include=[], model=None, effort=None, timeout=15, initiator_session="fixture-origin",
            workdir=str(custom_remote), dir=str(repo))
        with patch.object(rd, "local_repo", return_value=str(repo)), \
             patch.object(rd, "scp_path", side_effect=lambda h, rel: str(Path(h["workdir"]) / rel)), \
             contextlib.redirect_stdout(io.StringIO()):
            rd.cmd_run(args, self.cfg)
            rec = rd.all_jobs()[0]
            self.assertEqual(rec["worker_root"], str(custom_remote / "users" / "employee_a"))
            self.assertEqual(rec["shared_root"], str(custom_remote))
            # 等后台 worker 退出，避免临时目录清理与它写文件竞态。
            self.wait_success(rec["job"])


if __name__ == "__main__":
    unittest.main()

