"""真实 Unix HTTP 验证票 45 指标；仅替换资源探针的系统边界。"""
import json
import shlex
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from tests import test_rd_serve as base


class HostMetricsTests(unittest.TestCase):
    setUp = base.ServeTests.setUp
    start = base.ServeTests.start
    request = base.ServeTests.request
    eventually = base.ServeTests.eventually
    assert_structure = base.ServeTests.assert_structure
    open_events = base.ServeTests.open_events
    next_event = base.ServeTests.next_event

    def host_row(self, info=None, settings="", windows=False):
        self.config.write_text("[host:target]\n" + (
            "ssh=target.invalid\nplatform=windows\n" if windows else
            f"ssh=local\nworkdir={self.root / 'worker'}\n") + settings)
        info = dict(dict(os="Darwin", ncpu="8", load1="1", memfree="80", disk="100",
                         disk_total="500", running="0", heavy="0"), **(info or {}))
        script = "echo " + shlex.quote(" ".join(f"{key}={value}" for key, value in info.items()))
        boundary = (patch.object(cli.native, "request", return_value=dict(info, os="Windows"))
                    if windows else patch.object(cli, "PROBE", script))
        with boundary, patch.object(cli.messages, "utc_now", return_value="2026-10-26T01:00:00+00:00"):
            service = self.start(poll_interval=3600)
            row = self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"][0],
                                  lambda row: row["probed"])
            service.close()
        return row

    def test_hosts_returns_three_metrics_with_values_lines_levels_and_disk_total(self):
        row = self.host_row()
        self.assertFalse(row["full"])
        self.assertEqual(row["disk_total"], "500")
        self.assertEqual(row["metrics"], {
            "cpu": dict(value=0.125, unit="load_per_core", warning=1.0, pressure=1.5, level="normal"),
            "memory": dict(value=80.0, unit="free_pct", warning=20.0, pressure=10.0, level="normal"),
            "disk": dict(value=100.0, total=500.0, unit="free_gb", warning=20.0,
                         pressure=10.0, level="normal"),
        })

    def test_warning_lines_are_inclusive_and_do_not_make_the_target_full(self):
        row = self.host_row(dict(load1="8", memfree="20", disk="20"))
        self.assertFalse(row["full"])
        self.assertEqual([row["metrics"][key]["level"] for key in ("cpu", "memory", "disk")],
                         ["warning", "warning", "warning"])

    def test_new_host_contract_examples_match_live_http_values_and_types(self):
        for name, values, windows in (
            ("hosts-populated", {}, False),
            ("hosts-warning", dict(load1="8", memfree="20", disk="20"), False),
            ("hosts-pressure", dict(load1="12.08", memfree="9", disk="9"), False),
            ("hosts-unknown", dict(load1="", memfree="", disk="", disk_total=""), False),
            ("hosts-windows", dict(load1="", cpu_pct="70"), True),
        ):
            with self.subTest(contract=name):
                row = self.host_row(values, windows=windows)
                example = json.loads((base.CONTRACTS / (name + ".json")).read_text())
                self.assert_structure({"hosts": [row]}, {"hosts": example["response"]["body"]["hosts"]})
                self.assertEqual(row, example["response"]["body"]["hosts"][0])

    def test_new_fields_cannot_carry_secrets_from_probe_or_configuration(self):
        row = self.host_row(dict(cpu_pct="token=private-marker", disk_total="private-marker",
                                 load1="env_file=/private/fixture-env", memfree="private-marker",
                                 disk="private-marker", token="private-marker", last_seen_at="private-marker"),
                            "env_file=/private/fixture-env\nssh_opts=private-marker\n"
                            "agent_auth_file=private-marker\n")
        self.assertEqual(set(row["metrics"]), {"cpu", "memory", "disk"})
        self.assertEqual(row["last_seen_at"], "2026-10-26T01:00:00+00:00")
        for key, metric in row["metrics"].items():
            self.assertEqual(set(metric), {"value", "unit", "warning", "pressure", "level"}
                             | ({"total"} if key == "disk" else set()))
        text = json.dumps(row)
        for secret in ("private-marker", "env_file", "agent_auth_file", "ssh_opts", "token"):
            self.assertNotIn(secret, text)
        # Replay the same malicious probe event through SSE after restarting.
        with patch.object(cli, "PROBE", "exit 1"):
            service = self.start(poll_interval=3600)
            stream = self.open_events()
            _, kind, event = self.next_event(stream)
            self.assertEqual(kind, "host.changed")
            self.assertTrue({"cpu_pct", "disk_total"} <= set(event["data"]))
            for secret in ("private-marker", "env_file", "agent_auth_file", "ssh_opts", "token"):
                self.assertNotIn(secret, json.dumps(event))
            stream.close()
            service.close()

    def test_pressure_lines_match_assess_including_equality_and_other_refusal_reasons(self):
        cases = [
            (dict(load1="12", memfree="10", disk="10"), False, ["warning"] * 3),
            (dict(load1="12.08"), True, ["pressure", "normal", "normal"]),
            (dict(memfree="9.9"), True, ["normal", "pressure", "normal"]),
            (dict(disk="9"), True, ["normal", "normal", "pressure"]),
            (dict(swapio="20480"), False, ["normal"] * 3),
            (dict(memfree=""), True, ["normal", "unknown", "normal"]),
        ]
        for values, full, levels in cases:
            with self.subTest(values=values):
                row = self.host_row(values)
                self.assertEqual(row["full"], full)
                info = dict(dict(reachable=True, os="Darwin", ncpu="8", load1="1",
                                 memfree="80", disk="100"), **values)
                self.assertEqual(row["full"], bool(cli.assess(dict(cli.DEFAULTS), info)))
                self.assertEqual([row["metrics"][key]["level"] for key in ("cpu", "memory", "disk")], levels)

    def test_host_specific_warning_and_pressure_values_drive_the_levels(self):
        row = self.host_row(dict(load1="6.4", memfree="30", disk="50"),
                            "warn_load_per_core=0.8\nwarn_free_mem_pct=30\nwarn_free_disk_gb=50\n"
                            "max_load_per_core=1.2\nmin_free_mem_pct=15\nmin_free_disk_gb=25\n")
        self.assertFalse(row["full"])
        self.assertEqual([(metric["warning"], metric["pressure"], metric["level"])
                          for metric in row["metrics"].values()],
                         [(0.8, 1.2, "warning"), (30.0, 15.0, "warning"), (50.0, 25.0, "warning")])

    def test_missing_or_invalid_metrics_are_unknown_and_legacy_disk_total_is_nullable(self):
        for value in ("", "?", "NaN", "inf"):
            with self.subTest(value=value):
                row = self.host_row(dict(load1=value, memfree=value, disk=value, disk_total=value))
                for metric in row["metrics"].values():
                    self.assertEqual((metric["value"], metric["level"]), (None, "unknown"))
                self.assertIsNone(row["metrics"]["disk"]["total"])
        row = self.host_row(dict(ncpu="0", disk_total=""))
        self.assertEqual(row["metrics"]["cpu"]["level"], "unknown")
        self.assertEqual(row["metrics"]["disk"]["level"], "normal")
        self.assertIsNone(row["metrics"]["disk"]["total"])

    def test_windows_cpu_uses_percentage_and_existing_cpu_refusal_boundary(self):
        for value, level, full in (("69", "normal", False), ("70", "warning", False),
                                   ("85", "warning", False), ("85.1", "pressure", True),
                                   ("", "unknown", True)):
            with self.subTest(cpu=value):
                row = self.host_row(dict(cpu_pct=value, load1=""), windows=True)
                self.assertEqual(row["metrics"]["cpu"], dict(
                    value=float(value) if value else None, unit="cpu_pct",
                    warning=70.0, pressure=85.0, level=level))
                self.assertEqual(row["full"], full)
        row = self.host_row(dict(cpu_pct="60"), "warn_cpu_pct=60\nmax_cpu_pct=80\n", windows=True)
        self.assertEqual((row["metrics"]["cpu"]["warning"], row["metrics"]["cpu"]["pressure"],
                          row["metrics"]["cpu"]["level"]), (60.0, 80.0, "warning"))

    def test_windows_cpu_only_changes_notify_clients_even_before_pressure(self):
        self.config.write_text("[host:target]\nssh=target.invalid\nplatform=windows\n")
        info = dict(os="Windows", ncpu="8", cpu_pct="20", memfree="80", disk="100", disk_total="500")
        with patch.object(cli.native, "request", side_effect=lambda *a, **kw: dict(info)):
            service = self.start(poll_interval=0.05)
            self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"][0], lambda row: row["probed"])
            stream = self.open_events()
            _, kind, _ = self.next_event(stream)
            self.assertEqual(kind, "host.changed")
            info["cpu_pct"] = "70"
            row = self.eventually(lambda: self.request("/v1/hosts")[1]["hosts"][0],
                                  lambda row: row["metrics"]["cpu"]["level"] == "warning")
            self.assertFalse(row["full"])
            _, kind, event = self.next_event(stream)
            while kind != "host.changed":
                _, kind, event = self.next_event(stream)
            self.assertEqual(kind, "host.changed")
            self.assertEqual(event["data"]["cpu_pct"], "70")
            self.assertEqual(event["data"]["disk_total"], "500")
            stream.close()
            service.close()
