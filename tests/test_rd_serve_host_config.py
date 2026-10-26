"""目标机配置 HTTP 契约；实际文件与 Unix socket，探针替换在 OS 边界。"""
import json
from unittest.mock import patch
import unittest

from remote_dispatch import cli
from tests import test_rd_serve as serve_tests
from tests import test_rd_serve_network as network_tests


class HostConfigTests(unittest.TestCase):
    setUp = serve_tests.ServeTests.setUp
    start = serve_tests.ServeTests.start
    request = serve_tests.ServeTests.request
    seed = serve_tests.ServeTests.seed
    eventually = serve_tests.ServeTests.eventually
    assert_structure = serve_tests.ServeTests.assert_structure

    def assert_contract(self, name, reply):
        example = json.loads((serve_tests.CONTRACTS / ("host-config-" + name + ".json")).read_text())
        self.assertEqual(reply[0], example["response"]["status"])
        self.assert_structure(reply[1], example["response"]["body"])

    def configured(self):
        self.config.write_text("; keep header\n[defaults]\nsync=direct\nmax_heavy=2\n"
                               "[host:target]\n; keep host comment\nssh=local\n"
                               "enabled=false\ncodex_auth_file=/private/fixture-secret\n"
                               "[project:demo]\n; keep project\ntest=python3 -m unittest\n")
        probe = patch.object(cli, "probe", return_value={"reachable": False})
        probe.start()
        self.addCleanup(probe.stop)
        return self.start(poll_interval=3600)

    def write(self, path, body=None, method="POST", key="host-write"):
        return self.request(path, method, json.dumps(body or {}),
                            {"Content-Type": "application/json", "Idempotency-Key": key})

    def test_lists_disabled_hosts_effective_fields_and_only_credential_presence(self):
        self.configured()
        status, body = self.request("/v1/config/hosts")
        self.assertEqual(status, 200, body)
        self.assert_contract("list", (status, body))
        host = body["hosts"][0]
        self.assertEqual(host["name"], "target")
        self.assertEqual({key: host["values"][key] for key in ("ssh", "sync", "max_heavy", "enabled", "platform")},
                         dict(ssh="local", sync="direct", max_heavy="2", enabled="false", platform="posix"))
        self.assertIn(dict(field="codex_auth_file", is_set=True), host["protected_fields"])
        self.assertNotIn("fixture-secret", json.dumps(body))

    def test_create_is_backed_up_atomic_preserves_comments_and_replays(self):
        service = self.configured()
        original = self.config.read_bytes()
        body = dict(name="second", ssh="worker@example.invalid", workdir="~/worker", sync="origin",
                    codex_auth_file="/private/new-credential")
        reply = self.write("/v1/config/hosts", body)
        self.assertEqual(reply[0], 201, reply)
        self.assert_contract("create", reply)
        self.assertEqual(reply[1]["host"]["values"]["ssh"], "worker@example.invalid")
        self.assertNotIn("new-credential", json.dumps(reply))
        self.assertEqual(self.write("/v1/config/hosts", body), reply)
        backups = list(self.root.glob("config.ini.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.config.read_bytes().startswith(original))
        self.assertEqual([h["name"] for h in self.request("/v1/config/hosts")[1]["hosts"]], ["target", "second"])
        self.assertEqual(service.config.host("second")["sync"], "origin")
        self.assertEqual(self.write("/v1/config/hosts", dict(body, sync="direct"))[0], 422)

    def test_validation_rejects_duplicate_required_sync_and_ini_injection_without_writing(self):
        self.configured()
        before = self.config.read_bytes()
        cases = [({"name": "target", "ssh": "local"}, 409, "host_exists", "name"),
                 ({"name": "missing"}, 400, "host_field_required", "ssh"),
                 ({"name": "bad", "ssh": "local", "sync": "other"}, 400, "invalid_host_field", "sync"),
                 ({"name": "bad", "ssh": "local", "platform": "windows"}, 400, "invalid_host_field", "platform"),
                 ({"name": "bad", "ssh": "local", "max_heavy": "NaN"}, 400, "invalid_host_field", "max_heavy"),
                 ({"name": "bad", "ssh": "local", "workdir": "relative"}, 400, "invalid_host_field", "workdir"),
                 ({"name": "bad", "ssh": "local", "warn_cpu_pct": "101"}, 400, "invalid_host_field", "warn_cpu_pct"),
                 ({"name": "bad", "ssh": "local\n[host:injected]"}, 400, "invalid_host_field", "ssh"),
                 ({"name": "bad/name", "ssh": "local"}, 400, "invalid_host_field", "name")]
        for index, (body, status, reason, field) in enumerate(cases):
            with self.subTest(field=field, body=body):
                reply = self.write("/v1/config/hosts", body, key=f"invalid-{index}")
                self.assertEqual(reply, (status, {"error": {"reason": reason, "field": field}}))
                if field == "sync":
                    self.assert_contract("invalid", reply)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.root.glob("config.ini.bak.*")), [])

    def test_update_keeps_comments_other_sections_and_credentials_and_refreshes_reads(self):
        service = self.configured()
        before = self.config.read_bytes()
        reply = self.write("/v1/config/hosts/target", dict(max_heavy="3", workdir="~/changed"), method="PUT")
        self.assertEqual(reply[0], 200, reply)
        self.assertEqual(reply[1]["host"]["values"]["max_heavy"], "3")
        self.assert_contract("update", reply)
        text = self.config.read_text()
        self.assertIn("; keep host comment\nssh=local\nenabled=false\ncodex_auth_file=/private/fixture-secret\n", text)
        self.assertIn("[project:demo]\n; keep project\ntest=python3 -m unittest\n", text)
        self.assertEqual(next(self.root.glob("config.ini.bak.*")).read_bytes(), before)
        self.assertEqual(service.config.host("target")["workdir"], "~/changed")
        self.assertEqual(self.request("/v1/config/hosts")[1]["hosts"][0]["values"]["max_heavy"], "3")
        cleared = self.write("/v1/config/hosts/target", dict(max_heavy=None, codex_auth_file=""), method="PUT", key="clear")
        self.assertEqual(cleared[0], 200, cleared)
        self.assertEqual(cleared[1]["host"]["values"]["max_heavy"], "2")
        self.assertIn(dict(field="codex_auth_file", is_set=False), cleared[1]["host"]["protected_fields"])

    def test_delete_rejects_in_progress_dispatch_then_succeeds_after_completion(self):
        self.seed(running=True, poll_interval=0.05)
        self.eventually(lambda: self.request("/v1/jobs"),
                        lambda reply: any(row["execution_state"] == "running" for row in reply[1]["jobs"]))
        before = self.config.read_bytes()
        reply = self.write("/v1/config/hosts/target", method="DELETE", key="delete-running")
        self.assertEqual(reply, (409, {"error": {"reason": "host_has_active_dispatches"}}))
        self.assert_contract("active", reply)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.root.glob("config.ini.bak.*")), [])
        (self.worker / "exit_code").write_text("0")
        cli.save_job(dict(cli.load_job("other-job"), execution_state="done"))
        self.eventually(lambda: self.request("/v1/jobs"),
                        lambda reply: reply[1]["jobs"][0]["execution_state"] == "done")
        reply = self.write("/v1/config/hosts/target", method="DELETE", key="delete-done")
        self.assertEqual(reply, (200, {"deleted": "target"}))
        self.assert_contract("delete", reply)
        self.assertEqual(self.request("/v1/config/hosts"), (200, {"hosts": []}))
        self.assertEqual(self.write("/v1/config/hosts/target", method="DELETE", key="delete-done"), reply)

    def test_delete_protects_other_user_unknown_dispatch_and_preserves_unrelated_content(self):
        self.configured()
        before = self.config.read_bytes()
        cli.save_job(dict(job="another-user-job", user_id="other", host="target", rounds=1))
        reply = self.write("/v1/config/hosts/target", method="DELETE")
        self.assertEqual(reply[0], 409, reply)
        self.assertNotIn("another-user-job", json.dumps(reply))
        self.assertEqual(self.config.read_bytes(), before)
        (self.state / "jobs" / "another-user-job.json").unlink()
        reply = self.write("/v1/config/hosts/target", method="DELETE", key="delete-safe")
        self.assertEqual(reply, (200, {"deleted": "target"}))
        self.assert_contract("delete", reply)
        self.assertIn("; keep host comment\n", self.config.read_text())
        self.assertIn("[project:demo]\n; keep project\ntest=python3 -m unittest\n", self.config.read_text())

    def test_new_credentials_are_redacted_from_other_public_responses(self):
        self.seed(poll_interval=3600)
        credential = "/private/changed-credential"
        reply = self.write("/v1/config/hosts/target", dict(codex_auth_file=credential), method="PUT")
        self.assertEqual(reply[0], 200, reply)
        cli.save_job(dict(self.rec, model=credential))
        self.assertNotIn(credential, json.dumps(self.request("/v1/jobs")))
        self.assertNotIn(credential, json.dumps(self.request("/v1/config/hosts")))

    def test_replace_failure_preserves_original_and_backup_without_partial_config(self):
        self.configured()
        before = self.config.read_bytes()
        import os
        replace = os.replace
        def failing_replace(source, target):
            if str(target) == str(self.config):
                raise OSError("fixture replace failure")
            return replace(source, target)
        with patch.object(os, "replace", side_effect=failing_replace):
            reply = self.write("/v1/config/hosts/target", dict(sync="origin"), method="PUT")
        self.assertEqual(reply[0], 503, reply)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(next(self.root.glob("config.ini.bak.*")).read_bytes(), before)
        self.assertEqual(self.request("/v1/config/hosts")[1]["hosts"][0]["values"]["sync"], "direct")
        self.assertEqual(list(self.root.glob(".config.ini.*")), [])

    def test_backup_failure_blocks_write_and_bare_delete_is_supported(self):
        self.configured()
        before = self.config.read_bytes()
        import os
        open_file = os.open
        def failing_backup(path, flags, *args, **kwargs):
            if "config.ini.bak." in str(path):
                raise OSError("fixture backup failure")
            return open_file(path, flags, *args, **kwargs)
        with patch.object(os, "open", side_effect=failing_backup):
            reply = self.write("/v1/config/hosts/target", dict(sync="origin"), method="PUT")
        self.assertEqual(reply[0], 503, reply)
        self.assertEqual(self.config.read_bytes(), before)
        reply = self.request("/v1/config/hosts/target", "DELETE", headers={"Idempotency-Key": "bare-delete"})
        self.assertEqual(reply, (200, {"deleted": "target"}))
        self.assert_contract("delete", reply)

    def test_update_preserves_crlf_and_comments_in_multiline_options(self):
        self.configured()
        self.config.write_bytes(b"[host:target]\r\nssh = local\r\n; host note\r\ncodex_auth_file = old\r\n"
                                b"  continuation\r\n  ; credential note\r\n[project:demo]\r\n# project note\r\ntest = echo okay\r\n")
        reply = self.write("/v1/config/hosts/target", dict(codex_auth_file="new"), method="PUT")
        self.assertEqual(reply[0], 200, reply)
        saved = self.config.read_bytes()
        self.assertIn(b"; host note\r\n", saved)
        self.assertIn(b"  ; credential note\r\n", saved)
        self.assertNotIn(b"continuation", saved)
        self.assertTrue(saved.endswith(b"[project:demo]\r\n# project note\r\ntest = echo okay\r\n"))
        self.assertNotIn(b"\n", saved.replace(b"\r\n", b""))


    def test_integer_fields_reject_float_and_exponent_strings_used_by_launch_int_parser(self):
        self.configured()
        before = self.config.read_bytes()
        for field, value in (("max_heavy", "1.0"), ("timeout", "1e3"), ("timeout", "1" + "0" * 400)):
            reply = self.write("/v1/config/hosts/target", {field: value}, method="PUT", key=field + value[:4])
            self.assertEqual(reply, (400, {"error": {"reason": "invalid_host_field", "field": field}}))
        self.assertEqual(self.config.read_bytes(), before)

    def test_edit_does_not_treat_unchanged_multiline_credential_content_as_options(self):
        self.configured()
        self.config.write_text("[host:target]\nssh=local\ncodex_auth_file=first\n"
                               "  sync = credential-continuation\n  [host:fake]\n; keep note\nsync=origin\n")
        reply = self.write("/v1/config/hosts/target", dict(sync="direct"), method="PUT")
        self.assertEqual(reply[0], 200, reply)
        self.assertEqual(reply[1]["host"]["values"]["sync"], "direct")
        self.assertIn("codex_auth_file=first\n  sync = credential-continuation\n  [host:fake]\n; keep note\n", self.config.read_text())


class HostConfigNetworkTests(unittest.TestCase):
    setUp = network_tests.NetworkTests.setUp
    start = network_tests.NetworkTests.start
    request = network_tests.NetworkTests.request
    configure_network = network_tests.NetworkTests.configure_network
    network_info = network_tests.NetworkTests.network_info
    tls = network_tests.NetworkTests.tls
    pair = network_tests.NetworkTests.pair

    def test_paired_https_client_can_crud_and_unpaired_client_cannot_read(self):
        self.configure_network()
        self.start()
        info = self.network_info()
        pin = {"X-Rdispatch-SPKI": info["spki_sha256"]}
        self.assertEqual(self.request("/v1/config/hosts", connection=self.tls(info), headers=pin)[0], 401)
        _, headers = self.pair(info)
        created = self.request("/v1/config/hosts", "POST",
            dict(name="new", ssh="local", enabled="false"), key="create", connection=self.tls(info), headers=headers)
        self.assertEqual(created[0], 201, created)
        updated = self.request("/v1/config/hosts/new", "PUT", dict(sync="direct"),
            key="update", connection=self.tls(info), headers=headers)
        self.assertEqual(updated[0], 200, updated)
        read = self.request("/v1/config/hosts", connection=self.tls(info), headers=headers)
        self.assertEqual(read[1]["hosts"][0]["values"]["sync"], "direct")
        deleted = self.request("/v1/config/hosts/new", "DELETE", {}, key="delete",
            connection=self.tls(info), headers=headers)
        self.assertEqual(deleted, (200, {"deleted": "new"}))
