"""票 45：配置、资源探针与主机显示接口的行为。"""
import os
import contextlib
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_native as native


class HostConfigTests(unittest.TestCase):
    def test_warning_defaults_and_host_overrides_are_available_without_changing_pressure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.ini"
            config.write_text("[host:default]\nssh=local\n"
                              "[host:custom]\nssh=local\nwarn_load_per_core=0.8\n"
                              "warn_free_mem_pct=30\nwarn_free_disk_gb=50\nwarn_cpu_pct=60\n")
            with patch.object(cli, "CONFIG", str(config)):
                cfg = cli.Config()
                default, custom = cfg.host("default"), cfg.host("custom")
        keys = ("warn_load_per_core", "warn_free_mem_pct", "warn_free_disk_gb", "warn_cpu_pct")
        self.assertEqual([default[key] for key in keys], ["1.0", "20", "20", "70"])
        self.assertEqual([custom[key] for key in keys], ["0.8", "30", "50", "60"])
        self.assertEqual([custom[key] for key in ("max_load_per_core", "min_free_mem_pct",
                                                  "min_free_disk_gb", "max_cpu_pct")],
                         ["1.5", "10", "10", "85"])

    def test_invalid_warning_values_fail_with_a_chinese_configuration_error(self):
        for key, value in (("warn_load_per_core", "nope"), ("warn_load_per_core", "NaN"),
                           ("warn_free_mem_pct", "101"), ("warn_free_disk_gb", "-1"),
                           ("warn_cpu_pct", "inf")):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.ini"
                config.write_text(f"[host:target]\nssh=local\n{key}={value}\n")
                error = io.StringIO()
                with patch.object(cli, "CONFIG", str(config)), contextlib.redirect_stderr(error), \
                     self.assertRaises(SystemExit) as stopped:
                    cli.Config().host("target")
                self.assertEqual(stopped.exception.code, 4)
                self.assertIn(key, error.getvalue())
                self.assertIn("必须", error.getvalue())


class DiskProbeTests(unittest.TestCase):
    def test_posix_probe_reads_total_and_free_from_same_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            for name, script in {
                "uname": "echo Linux",
                "sleep": ":",
                "df": "printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n"
                      "fixture 524288000 419430400 104857600 80%% /fixture\\n'",
            }.items():
                path = binary / name
                path.write_text("#!/bin/sh\n" + script + "\n")
                path.chmod(0o700)
            host = dict(cli.DEFAULTS, name="fixture", ssh="local", workdir=str(root))
            with patch.dict(os.environ, {"RD_USER_ID": "fixture",
                                         "PATH": str(binary) + os.pathsep + os.environ["PATH"]}):
                info = cli.probe(host)
            self.assertTrue(info["reachable"])
            self.assertEqual((info["disk"], info["disk_total"]), ("100", "500"))

    def test_windows_probe_reports_disk_total_in_same_unit_as_free(self):
        class Kernel:
            ticks = 0

            def GlobalMemoryStatusEx(self, pointer):
                pointer._obj.total, pointer._obj.available = 100, 80
                return True

            def GetSystemTimes(self, idle, system, user):
                self.ticks += 1
                idle._obj.dwLowDateTime = self.ticks * 80
                system._obj.dwLowDateTime = self.ticks * 100
                return True

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(native.ctypes, "WinDLL", return_value=Kernel(), create=True), \
             patch.object(native.time, "sleep"), \
             patch.object(native.shutil, "disk_usage", return_value=SimpleNamespace(
                 total=500 * 1024 ** 3, free=100 * 1024 ** 3)), \
             patch.object(native.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 0, stdout="0", stderr="")):
            info = native.win_metrics(Path(directory), Path(directory))
        self.assertEqual((info["disk"], info["disk_total"]), ("100", "500"))
