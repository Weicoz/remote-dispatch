"""本机模拟网络故障、远端探针及重连巡检；不连接外网、远端或模型。"""
import argparse
import configparser
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from remote_dispatch import rd_health as health
from test_rd import rd, native


RECONNECT = json.dumps({"type": "error", "message": "Reconnecting... waiting for network (2/5)"}) + "\n"


class HealthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rd-health-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.host = dict(rd.DEFAULTS, name="fixture", ssh="fixture.invalid", workdir=str(self.root))
        self.options = health.network_options({})
        self.env = patch.dict(os.environ, {"RD_USER_ID": "employee_a", "OPENAI_BASE_URL": "",
            **{"RD_" + key.upper(): "" for key in self.options}})
        self.env.start()
        self.addCleanup(self.env.stop)

    def curl(self, cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "403", "")

    def test_defaults_include_pypi_model_and_websocket_tls_with_shared_deadline(self):
        with patch.object(health.subprocess, "run", side_effect=self.curl) as calls:
            result = health.network_probe(self.options, self.root)
        self.assertTrue(result["ok"])
        self.assertEqual([x["target"] for x in result["checks"]], [
            "https://pypi.org", "https://chatgpt.com/", "https://chatgpt.com/backend-api/codex/responses"])
        self.assertEqual(len(calls.call_args_list), 3)
        for call in calls.call_args_list:
            cmd = call.args[0]
            self.assertIn("--disable", cmd)
            self.assertLessEqual(float(cmd[cmd.index("--max-time") + 1]), 5)
            self.assertLessEqual(call.kwargs["timeout"], 5.2)
        self.assertTrue(any("Upgrade: websocket" in call.args[0] for call in calls.call_args_list))

    def test_websocket_tls_failure_blocks_even_when_both_https_hosts_work(self):
        def curl(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 35 if "Upgrade: websocket" in cmd else 0,
                                               "000" if "Upgrade: websocket" in cmd else "200", "PRIVATE_ERROR")
        with patch.object(health.subprocess, "run", side_effect=curl):
            network = health.network_probe(self.options, self.root)
        info = dict(reachable=True, memfree="50", disk="100", network=network)
        why = rd.assess(self.host, info)
        self.assertIn("TLS handshake failed", "; ".join(why))
        self.assertNotIn("PRIVATE_ERROR", json.dumps(network))
        self.assertIn("websocket_tls", rd.fmt_probe(self.host, info, why))
        self.assertIn("network unavailable", rd.fmt_probe(self.host, info, why))

    def test_all_https_failures_and_missing_tools_fail_closed(self):
        for failure in (subprocess.CompletedProcess([], 35, "000", "PRIVATE"),
                        FileNotFoundError(), subprocess.TimeoutExpired("curl", 0.1)):
            with self.subTest(failure=type(failure).__name__):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(health.subprocess, "run", **kwargs):
                    self.assertFalse(health.network_probe(self.options, self.root)["ok"])

    def test_upgrade_101_timeout_is_tls_success_and_command_skips_upgrade(self):
        with patch.object(health.subprocess, "run", return_value=subprocess.CompletedProcess([], 28, "101", "")):
            result = health.network_probe(self.options, self.root)
        self.assertTrue(result["checks"][-1]["ok"])
        self.assertFalse(result["checks"][0]["ok"])
        with patch.object(health.subprocess, "run", side_effect=self.curl) as call:
            self.assertTrue(health.network_probe(self.options, self.root, agent="command")["ok"])
        self.assertEqual(call.call_count, 2)

    def test_environment_overrides_host_urls_budget_and_ws_target(self):
        with patch.dict(os.environ, {"RD_NETWORK_URLS": "https://docs.example.invalid,https://status.example.invalid",
                "RD_NETWORK_MODEL_URL": "https://model.example.invalid/v1",
                "RD_NETWORK_WEBSOCKET_URL": "wss://model.example.invalid/v1/responses",
                "RD_NETWORK_TIMEOUT_SECONDS": "0.05"}):
            options = health.network_options({"network_urls": "https://ignored.example.invalid"})
        with patch.object(health.subprocess, "run", side_effect=self.curl):
            result = health.network_probe(options, self.root)
        self.assertEqual(options["network_timeout_seconds"], 0.05)
        self.assertEqual(result["checks"][-1]["target"], "https://model.example.invalid/v1/responses")
        self.assertIn("https://model.example.invalid/", [x["target"] for x in result["checks"]])
        self.assertNotIn("https://pypi.org", [x["target"] for x in result["checks"]])

    def test_probes_run_in_parallel_instead_of_multiplying_budget(self):
        import threading
        barrier = threading.Barrier(3)
        def curl(cmd, **kwargs):
            barrier.wait(timeout=2)
            return self.curl(cmd, **kwargs)
        with patch.object(health.subprocess, "run", side_effect=curl):
            self.assertTrue(health.network_probe(self.options, self.root)["ok"])

    def test_invalid_timeouts_and_credential_urls_fail_without_echoing_values(self):
        for value in ("0", "-1", "nan", "inf", "abc"):
            with self.subTest(value=value), patch.dict(os.environ, {"RD_NETWORK_TIMEOUT_SECONDS": value}), self.assertRaises(ValueError):
                health.network_options({})
        for value in ("http://model.example.invalid", "https://user:PRIVATE@model.example.invalid", "https://model.example.invalid/?token=PRIVATE"):
            with self.subTest(value=value), patch.dict(os.environ, {"RD_NETWORK_MODEL_URL": value}), self.assertRaises(ValueError) as caught:
                health.network_options({})
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_infers_active_provider_from_only_employee_profile(self):
        directory = self.root / "users/employee_a/profiles"
        version = "a" * 64
        (directory / version).mkdir(parents=True)
        (directory / "current.json").write_text(json.dumps({"fingerprint": version}))
        config = directory / version / "config.toml"
        config.write_text('model_provider="fixture"\n[model_providers.fixture]\nbase_url="https://model.example.invalid/v1"\n')
        with patch.object(health.subprocess, "run", side_effect=self.curl):
            result = health.network_probe(self.options, self.root, "employee_a")
        self.assertEqual(result["checks"][-1]["target"], "https://model.example.invalid/v1/responses")
        config.write_text('model_provider="unknown"\n')
        with patch.object(health.subprocess, "run") as call:
            self.assertFalse(health.network_probe(self.options, self.root, "employee_a")["ok"])
        call.assert_not_called()

    def test_resume_uses_pinned_provider_instead_of_new_current_profile(self):
        directory = self.root / "users/employee_a/profiles"
        version = "b" * 64
        (directory / version).mkdir(parents=True)
        (directory / "current.json").write_text(json.dumps({"fingerprint": "a" * 64}))
        (directory / version / "config.toml").write_text('model_provider="fixture"\n[model_providers.fixture]\nbase_url="https://pinned.example.invalid/v1"\n')
        options = dict(self.options, profile_fingerprint=version)
        with patch.object(health.subprocess, "run", side_effect=self.curl):
            result = health.network_probe(options, self.root, "employee_a")
        self.assertEqual(result["checks"][-1]["target"], "https://pinned.example.invalid/v1/responses")

    def test_posix_probe_transports_code_and_uses_remote_proxy_environment(self):
        # 使用假 curl 与真实本机 shell 执行发送给远端的源码。
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        curl = bin_dir / "curl"
        curl.write_text('#!/bin/sh\n[ "$HTTPS_PROXY" = "fixture-proxy" ] || exit 7\nprintf 403\n')
        curl.chmod(0o755)
        env_file = self.root / "probe-env"
        env_file.write_text('HTTPS_PROXY=fixture-proxy\n')
        self.host.update(path=str(bin_dir), env_file=str(env_file))
        def ssh(host, script, **kwargs):
            if script == rd.PROBE:
                return subprocess.CompletedProcess([], 0, "ncpu=4 load1=1 memfree=50 disk=100", "")
            pre = 'export PATH=' + rd.shlex.quote(str(bin_dir)) + ':"$PATH"; SHARED_ROOT=' + rd.shlex.quote(str(self.root)) + '; '
            return subprocess.run(["sh", "-c", pre + script], capture_output=True, text=True, timeout=7)
        with patch.object(rd, "ssh", side_effect=ssh):
            result = rd.probe(self.host)
        self.assertEqual(rd.assess(self.host, result), [])
        self.assertTrue(result["network"]["ok"])

    def test_posix_probe_unavailable_is_not_healthy(self):
        metrics = subprocess.CompletedProcess([], 0, "memfree=50 disk=100", "")
        with patch.object(rd, "ssh", side_effect=[metrics, None]):
            result = rd.probe(self.host)
        self.assertTrue(rd.assess(self.host, result))

    def test_posix_and_native_state_transport_both_log_streams_as_metadata(self):
        directory = self.root / "jobs/fixture"
        directory.mkdir(parents=True)
        for name in ("run.r1.sh", "run.r1.json"):
            (directory / name).write_text("")
        (directory / "pid").write_text(str(os.getpid()))
        (directory / "stdout.r1.log").write_text(RECONNECT)
        (directory / "stderr.r1.log").write_text("PRIVATE_PROGRESS\n")
        def ssh(host, script, **kwargs):
            pre = 'ROOT=' + rd.shlex.quote(str(self.root)) + '; '
            return subprocess.run(["sh", "-c", pre + script], capture_output=True, text=True, timeout=7)
        with patch.object(rd, "ssh", side_effect=ssh):
            posix = rd.remote_state(self.host, ["fixture"])["fixture"]
        windows = native.job_state(self.root, "fixture")
        for state in (posix, windows):
            self.assertEqual(state["state"], "running")
            sample = state["network_sample"]
            self.assertGreater(sample["stdout"]["reconnect"], 0)
            self.assertGreater(sample["stderr"]["progress"], 0)
            self.assertNotIn("PRIVATE_PROGRESS", json.dumps(sample))

    def test_windows_rpc_probe_includes_network_and_configuration(self):
        options = self.options
        q = dict(operation="probe", workdir=str(self.root), shared_workdir=str(self.root), path="",
                 network_options=options, user_id="employee_a", agent="codex")
        with patch.object(native, "win_metrics", return_value={"memfree": "50", "disk": "100"}), \
             patch.object(health.subprocess, "run", side_effect=self.curl):
            self.assertTrue(native.handle(q, io.BytesIO())["network"]["ok"])
        self.host["platform"] = "windows"
        with patch.object(native, "request", return_value={"network": {"ok": False}}) as call:
            self.assertTrue(rd.assess(self.host, rd.probe(self.host)))
        self.assertEqual(call.call_args.args[2]["network_options"], options)

    def test_named_and_auto_host_selection_reject_network_failure(self):
        cfg = object.__new__(rd.Config)
        cfg.cp = configparser.ConfigParser()
        cfg.cp.read_dict({"host:fixture": {"ssh": "fixture.invalid"}})
        bad = dict(reachable=True, disk="100", memfree="50", network={"ok": False, "error": "TLS handshake failed"})
        for name in ("fixture", "auto"):
            with patch.object(rd, "probe", return_value=bad), contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                rd.pick_host(cfg, name, [])
            self.assertEqual(result.exception.code, 3)


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rd-reconnect-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.rec = dict(job="fixture", user_id="employee_a", host="worker", project="fixture")
        self.log = self.root / "stdout.r1.log"
        self.log.write_text(RECONNECT)
        self.state = dict(state="running", round="1", head="fixture", dirty="0")
        self.state_patch = patch.object(rd, "STATE", str(self.root / "state"))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def observe(self, now, threshold=600):
        self.state["network_sample"] = health.log_sample(self.root, 1)
        with patch.object(rd.time, "time", return_value=now):
            return rd.network_stalls([self.rec], {"fixture": self.state}, threshold)

    def append(self, text, path=None):
        with (path or self.log).open("a") as stream:
            stream.write(text)

    def test_reconnecting_without_progress_alerts_across_separate_observations(self):
        self.assertEqual(self.observe(100), [])
        self.append(RECONNECT)
        self.assertEqual(self.observe(699), [])
        self.assertEqual(self.observe(700), ["fixture"])
        self.assertNotIn("Reconnecting", "".join(p.read_text() for p in (self.root / "state/network-watch").glob("*.json")))

    def test_progress_in_either_stream_resets_stall_and_silence_is_not_reconnect(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                self.observe(100)
                self.append("build passed\n", self.root / (stream + ".r1.log"))
                self.assertEqual(self.observe(500), [])
                self.assertEqual(self.observe(1200), [])
                self.append(RECONNECT)
                self.assertEqual(self.observe(1300), [])
                self.assertEqual(self.observe(1900), ["fixture"])

    def test_new_round_truncation_and_unobserved_large_window_reset_timer(self):
        for kind in ("round", "truncate", "window", "commit", "dirty"):
            with self.subTest(kind=kind):
                self.observe(100)
                if kind == "round":
                    self.state["round"] = "2"
                elif kind == "truncate":
                    self.log.write_text("\n")
                elif kind == "window":
                    self.append(RECONNECT * 2000)
                else:
                    self.state["head" if kind == "commit" else "dirty"] = kind
                self.assertEqual(self.observe(800), [])

    def test_plain_and_json_reconnect_classification_excludes_normal_items(self):
        self.assertTrue(health.reconnect_line(RECONNECT))
        self.assertTrue(health.reconnect_line("Reconnecting... waiting for network (1/5)"))
        self.assertFalse(health.reconnect_line(json.dumps({"type": "item.completed", "message": "Reconnecting... waiting for network"})))
        self.assertFalse(health.reconnect_line("已修复 Reconnecting... waiting for network"))

    def test_watch_emits_monitor_error_with_running_job_and_no_logs_even_keep_on_disconnect(self):
        self.observe(100)
        output = io.StringIO()
        with patch.object(rd, "require_user", return_value="employee_a"), \
             patch.object(rd, "load_job", return_value=self.rec), patch.object(rd, "job_states", return_value={"fixture": self.state}), \
             patch.object(rd.time, "time", return_value=700), patch.object(rd.time, "monotonic", return_value=0), \
             patch.object(rd.time, "sleep") as sleep, patch.object(rd, "cmd_stop") as stop, \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            rd.cmd_watch(argparse.Namespace(jobs=["fixture"], timeout=900, keep_on_disconnect=True), None)
        event = json.loads(output.getvalue())
        self.assertEqual((result.exception.code, event["event"]), (1, "monitor_error"))
        self.assertEqual(event["finished"], [])
        self.assertEqual(event["jobs"][0]["state"], "running")
        self.assertEqual(event["jobs"][0]["monitor_reason"], "network_reconnect_stalled")
        self.assertNotIn("Reconnecting", output.getvalue())
        sleep.assert_not_called()
        stop.assert_not_called()

    def test_stall_alert_is_not_hidden_by_another_job_finishing(self):
        self.observe(100)
        other = dict(self.rec, job="other")
        states = {"fixture": self.state, "other": {"state": "done", "exit": "0"}}
        output = io.StringIO()
        with patch.object(rd, "require_user", return_value="employee_a"), \
             patch.object(rd, "load_job", side_effect=[self.rec, other]), patch.object(rd, "job_states", return_value=states), \
             patch.object(rd.time, "time", return_value=700), patch.object(rd.time, "monotonic", return_value=0), \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            rd.cmd_watch(argparse.Namespace(jobs=["fixture", "other"], timeout=900), None)
        event = json.loads(output.getvalue())
        self.assertEqual((result.exception.code, event["event"]), (1, "monitor_error"))
        self.assertEqual(event["finished"], ["other"])

    def test_monitor_threshold_config_and_environment_precedence(self):
        cfg = object.__new__(rd.Config)
        cfg.cp = configparser.ConfigParser()
        with patch.dict(os.environ, {"RD_NETWORK_STALL_SECONDS": ""}):
            self.assertEqual(cfg.network_stall_seconds(), 600)
            cfg.cp.read_dict({"monitor": {"network_stall_seconds": "120"}})
            self.assertEqual(cfg.network_stall_seconds(), 120)
        with patch.dict(os.environ, {"RD_NETWORK_STALL_SECONDS": "30"}):
            self.assertEqual(cfg.network_stall_seconds(), 30)
        with patch.dict(os.environ, {"RD_NETWORK_STALL_SECONDS": "nan"}), self.assertRaises(ValueError):
            cfg.network_stall_seconds()


if __name__ == "__main__":
    unittest.main()
