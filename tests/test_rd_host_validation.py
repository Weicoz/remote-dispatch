"""目标机配置：读取路径与 CRUD 共用同一套校验；DEFAULTS 与缓存目录解析下沉后 cli 仍兼容。"""
import contextlib
import inspect
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_defaults
from remote_dispatch import rd_host_config as host_config
from remote_dispatch import rd_serve


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.ini"
        patcher = patch.object(cli, "CONFIG", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, **fields):
        body = "".join(f"{key} = {value}\n" for key, value in fields.items())
        self.path.write_text(f"[host:box]\nssh = user@box\n{body}", encoding="utf-8")
        return cli.Config(strict=True)

    def read_rejects(self, **fields):
        try:
            self.config(**fields).host("box")
        except cli.ConfigError:
            return True
        return False


class ReadPathValidationTests(ConfigFixture):
    BAD = [("max_load_per_core", "nan"), ("max_load_per_core", "inf"), ("max_load_per_core", "-1"),
           ("min_free_mem_pct", "nan"), ("max_cpu_pct", "101"), ("warn_free_disk_gb", "-5"),
           ("max_swap_used_gb", "inf"), ("max_heavy", "0"), ("max_heavy", "1.5"), ("timeout", "nan"),
           ("timeout", "0"), ("max_worktree_gb", "-1"), ("max_worktree_gb", "nan"),
           ("sync", "rsync"), ("launcher", "screen"), ("platform", "plan9"),
           ("cache_dirs", "BAD")]
    GOOD = [("max_load_per_core", "0.5"), ("max_cpu_pct", "100"), ("max_heavy", "2"), ("timeout", "60"),
            ("max_worktree_gb", ""), ("max_worktree_gb", "0"), ("enabled", "No"), ("platform", "linux")]

    def test_nan_inf_negative_and_bad_enums_are_rejected_on_read(self):
        for field, value in self.BAD:
            with self.subTest(field=field, value=value):
                self.assertTrue(self.read_rejects(**{field: value}))

    def test_legitimate_values_still_load(self):
        for field, value in self.GOOD:
            with self.subTest(field=field, value=value):
                self.assertFalse(self.read_rejects(**{field: value}))

    def test_read_and_crud_validation_agree(self):
        for field, value in self.BAD + self.GOOD:
            if field == "enabled":
                continue  # 读取路径对 enabled 宽松解析，见 test_enabled_is_lenient_on_read_but_strict_on_write
            with self.subTest(field=field, value=value):
                cp = host_config.parse(f"[host:box]\nssh = user@box\n{field} = {value}\n")
                try:
                    host_config.validate(host_config.effective(cp, "box"))
                    crud_rejects = False
                except host_config.InvalidField:
                    crud_rejects = True
                self.assertEqual(crud_rejects, self.read_rejects(**{field: value}))

    def test_enabled_is_lenient_on_read_but_strict_on_write(self):
        config = self.config(enabled="maybe")
        self.assertEqual(config.hosts(), [])  # 读取：非 1/true/yes 一律视为停用，保持旧行为
        cp = host_config.parse("[host:box]\nssh = user@box\nenabled = maybe\n")
        with self.assertRaises(host_config.InvalidField):  # 写入（CRUD）仍严格
            host_config.validate(host_config.effective(cp, "box"))

    def test_nan_load_threshold_can_no_longer_bypass_the_busy_gate(self):
        self.assertTrue(self.read_rejects(max_load_per_core="nan"))

    def test_missing_ssh_and_local_rules_keep_their_messages(self):
        self.path.write_text("[host:box]\nworkdir = ~/x\n", encoding="utf-8")
        with self.assertRaises(cli.ConfigError) as caught:
            cli.Config(strict=True).host("box")
        self.assertIn("缺少 ssh", str(caught.exception))
        self.path.write_text("[host:box]\nssh = local\nworkdir = relative\n", encoding="utf-8")
        with self.assertRaises(cli.ConfigError) as caught:
            cli.Config(strict=True).host("box")
        self.assertIn("workdir", str(caught.exception))

    def test_enabled_uses_the_shared_boolean_parser(self):
        self.path.write_text("[host:a]\nssh = a@a\nenabled = YES\n[host:b]\nssh = b@b\nenabled = 0\n", encoding="utf-8")
        self.assertEqual([h["name"] for h in cli.Config(strict=True).hosts()], ["a"])
        for text, expected in (("1", True), ("True", True), ("no", False), ("OFF", None)):
            if expected is None:
                with self.assertRaises(host_config.InvalidField):
                    host_config.parse_bool(text)
            else:
                self.assertIs(host_config.parse_bool(text), expected)

    def test_non_strict_reload_failure_message_names_the_host_and_field(self):
        with self.assertRaises(cli.ConfigError) as caught:
            self.config(max_load_per_core="nan").host("box")
        self.assertIn("[host:box]", str(caught.exception))
        self.assertIn("max_load_per_core", str(caught.exception))


class HostIsolationTests(ConfigFixture):
    GOOD = "[host:good]\nssh = good@h\n"

    def write(self, text):
        self.path.write_text(self.GOOD + text, encoding="utf-8")
        return cli.Config()

    def test_disabled_legacy_host_with_bad_values_never_breaks_the_list(self):
        for extra in ("enabled = false\nmax_swap_io_mb_s =\n", "enabled = false\nmin_free_disk_gb = 10G\n",
                      "enabled = false\ntimeout = 0\n", "enabled = false\nmax_heavy = 0\n",
                      "enabled = off\ntimeout = 0\n", "enabled =\ntimeout = 0\n", "enabled = maybe\nmax_heavy = 0\n"):
            with self.subTest(extra=extra):
                config = self.write("[host:old]\nssh = old@h\n" + extra)
                self.assertEqual([h["name"] for h in config.hosts()], ["good"])
                self.assertEqual(config.host_errors(), {})

    def test_disabled_host_is_still_resolvable_by_name_without_validation(self):
        config = self.write("[host:old]\nssh = old@h\nenabled = off\ntimeout = 0\n")
        self.assertEqual(config.host("old")["name"], "old")  # 旧任务仍能解析所在的已停用主机

    def test_deprecated_swap_fields_are_ignored_whatever_they_hold(self):
        for line in ("max_swap_io_mb_s =", "min_swap_free_gb = abc", "swap_active_mb_s = nan"):
            with self.subTest(line=line):
                config = self.write("[host:legacy]\nssh = l@h\n" + line + "\n")
                self.assertEqual([h["name"] for h in config.hosts()], ["good", "legacy"])
                cp = host_config.parse("[host:legacy]\nssh = l@h\n" + line + "\n")
                host_config.validate(host_config.effective(cp, "legacy"))  # CRUD 同样忽略

    def test_invalid_enabled_host_is_excluded_and_reported_but_others_run(self):
        config = self.write("[host:bad]\nssh = b@h\nmax_load_per_core = nan\n")
        self.assertEqual([h["name"] for h in config.hosts()], ["good"])
        errors = config.host_errors()
        self.assertEqual(list(errors), ["bad"])
        self.assertIn("max_load_per_core", errors["bad"])

    def test_explicit_use_of_an_invalid_host_exits_4_with_field_and_advice(self):
        config = self.write("[host:bad]\nssh = b@h\nmax_heavy = 0\n")
        with self.assertRaises(SystemExit) as stop, patch.object(cli, "probe") as probe:
            cli.pick_hosts(config, "bad", [])
        self.assertEqual(stop.exception.code, 4)
        probe.assert_not_called()

    def test_disabled_host_named_explicitly_for_a_run_is_validated_before_probing(self):
        config = self.write("[host:old]\nssh = o@h\nenabled = off\nmax_load_per_core = nan\n")
        with self.assertRaises(SystemExit) as stop, patch.object(cli, "probe") as probe:
            cli.pick_hosts(config, "old", [])
        self.assertEqual(stop.exception.code, 4)
        probe.assert_not_called()

    def test_auto_and_tag_selection_skip_invalid_hosts(self):
        config = self.write("[host:bad]\nssh = b@h\ntags = gpu\ntimeout = 0\n")
        healthy = {"reachable": True, "load1": "0.1", "ncpu": "8", "memfree": "80", "disk": "100", "running": "0"}
        with patch.object(cli, "probe", return_value=healthy), patch("builtins.print"):
            self.assertEqual([h["name"] for h in cli.pick_hosts(config, "auto", [])], ["good"])
            with self.assertRaises(SystemExit):
                cli.pick_hosts(config, "auto", ["gpu"])

    def test_hosts_command_shows_the_config_error_and_keeps_probing_the_rest(self):
        config = self.write("[host:bad]\nssh = b@h\nmax_cpu_pct = 150\n")
        healthy = {"reachable": True, "load1": "0.1", "ncpu": "8", "memfree": "80", "disk": "100", "running": "0"}
        output = io.StringIO()
        with patch.object(cli, "probe", return_value=healthy), contextlib.redirect_stdout(output):
            cli.cmd_hosts(None, config)
        text = output.getvalue()
        self.assertIn("good", text)
        self.assertRegex(text, r"bad\s+配置无效.*max_cpu_pct")

    def test_unit_suffix_gets_a_remove_the_unit_hint(self):
        config = self.write("[host:units]\nssh = u@h\nmin_free_disk_gb = 10G\n")
        message = config.host_errors()["units"]
        self.assertIn("min_free_disk_gb", message)
        self.assertIn("去掉单位", message)
        with self.assertRaises(SystemExit):
            config.host("units")

    def test_invalid_values_are_never_used_as_valid(self):
        config = self.write("[host:nan]\nssh = n@h\nmax_load_per_core = nan\n")
        self.assertNotIn("nan", [h["name"] for h in config.hosts()])


class DependencyDirectionTests(unittest.TestCase):
    def test_defaults_and_cache_parser_live_below_cli_and_are_re_exported(self):
        self.assertIs(cli.DEFAULTS, rd_defaults.DEFAULTS)
        self.assertIs(cli.parse_cache_dirs, host_config.parse_cache_dirs)
        self.assertEqual(cli.parse_cache_dirs("A=cache/a", "/root"), [("A", "cache/a", False)])

    def test_host_config_module_does_not_import_cli(self):
        self.assertNotIn("import cli", Path(host_config.__file__).read_text(encoding="utf-8").replace("from . import rd_", ""))
        self.assertNotIn("cli.", Path(host_config.__file__).read_text(encoding="utf-8").replace("cli.ini", ""))

    def test_service_state_directory_is_an_explicit_parameter_defaulting_to_cli_state(self):
        parameter = inspect.signature(rd_serve.Service.__init__).parameters["state"]
        self.assertIsNone(parameter.default)
        self.assertTrue(parameter.kind is inspect.Parameter.KEYWORD_ONLY)


if __name__ == "__main__":
    unittest.main()
