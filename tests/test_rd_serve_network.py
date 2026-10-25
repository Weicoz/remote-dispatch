"""Network API behavior with isolated state, loopback TLS and a fake push sender."""
import contextlib
import http.client
import json
import os
import shutil
import ssl
import socket
import time
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_events as events
from remote_dispatch.rd_serve import Service, UnixHTTPConnection


class NetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rn-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.path = self.root / "s"
        self.config = self.root / "config.ini"
        self.config.write_text("")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(cli, "STATE", str(self.state)))
        stack.enter_context(patch.object(cli, "CONFIG", str(self.config)))
        stack.enter_context(patch.dict(os.environ, {"RD_USER_ID": "fixture"}))

    def start(self, **options):
        service = Service(cli.Config(), self.path, poll_interval=3600, **options).start()
        self.addCleanup(service.close)
        return service

    def request(self, path, method="GET", body=None, *, key="request", connection=None, headers=None):
        connection = connection or UnixHTTPConnection(self.path)
        self.addCleanup(connection.close)
        headers = dict(headers or {})
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        connection.request(method, path, body, headers)
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_pairing_requires_a_local_window_expires_after_five_minutes_and_is_one_use(self):
        self.start()
        self.assertEqual(self.request("/v1/pair", "POST", {"code": "unknown"})[0], 403)
        with patch("remote_dispatch.rd_serve_network.monotonic", return_value=1000):
            status, window = self.request("/v1/pairing-window", "POST", {}, key="open")
        self.assertEqual(status, 200)
        with patch("remote_dispatch.rd_serve_network.monotonic", return_value=1300):
            self.assertEqual(self.request("/v1/pair", "POST", {"code": window["code"]}, key="late")[0], 403)
        status, window = self.request("/v1/pairing-window", "POST", {}, key="open-again")
        self.assertEqual(window["expires_in"], 300)
        status, device = self.request("/v1/pair", "POST", {"code": window["code"], "name": "Phone"}, key="pair")
        self.assertEqual(status, 200)
        self.assertTrue(device["device_token"])
        self.assertEqual(self.request("/v1/pair", "POST", {"code": window["code"]}, key="pair"), (status, device))
        self.assertEqual(self.request("/v1/pair", "POST", {"code": window["code"]}, key="reuse")[0], 403)
        status, devices = self.request("/v1/devices")
        self.assertEqual(devices["devices"][0]["device_id"], device["device_id"])
        self.assertFalse(devices["devices"][0]["session_access_enabled"])
        self.assertNotIn("device_token", json.dumps(devices))
        self.assertEqual(self.request("/v1/pairing-window", "POST", {}, key=None)[0], 400)

    def configure_network(self):
        self.config.write_text('[serve]\nlisten_addresses=' + json.dumps([
            dict(address="127.0.0.1", port=0, enabled=True),
            dict(address="127.0.0.2", port=0, enabled=False)]) + "\n")

    def network_info(self):
        status, info = self.request("/v1/network")
        self.assertEqual(status, 200)
        return info

    def tls(self, info):
        connection = http.client.HTTPSConnection("127.0.0.1", info["candidates"][0]["port"],
            context=ssl._create_unverified_context(), timeout=3)
        self.addCleanup(connection.close)
        return connection

    def pair(self, info):
        status, window = self.request("/v1/pairing-window", "POST", {}, key="window")
        self.assertEqual(status, 200)
        self.assertEqual(window["spki_sha256"], info["spki_sha256"])
        headers = {"X-Rdispatch-SPKI": info["spki_sha256"]}
        status, device = self.request("/v1/pair", "POST", {"code": window["code"], "name": "Phone"},
            key="pair", connection=self.tls(info), headers=headers)
        self.assertEqual(status, 200)
        return device, dict(headers, Authorization="Bearer " + device["device_token"])

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS certificates")
    def test_https_uses_only_selected_addresses_persists_identity_and_requires_pin_and_token(self):
        self.configure_network()
        service = self.start()
        info = self.network_info()
        self.assertEqual([row["address"] for row in info["candidates"]], ["127.0.0.1"])
        self.assertGreater(info["candidates"][0]["port"], 0)
        self.assertEqual(len(info["spki_sha256"]), 64)
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info))[0], 403)
        pin = {"X-Rdispatch-SPKI": info["spki_sha256"]}
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info), headers=pin)[0], 401)
        device, headers = self.pair(info)
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info), headers=headers), (200, {"jobs": []}))
        bad_pin = dict(headers, **{"X-Rdispatch-SPKI": "0" * 64})
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info), headers=bad_pin)[0], 403)
        for path, method, body in (("/v1/pairing-window", "POST", {}), ("/v1/devices", "GET", None),
                ("/v1/devices/" + device["device_id"], "DELETE", {}), ("/_cli/v1", "POST", {})):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, method, body, connection=self.tls(info), headers=headers)[0], 403)
        self.assertEqual(self.request("/v1/sessions", connection=self.tls(info), headers=headers)[0], 403)
        service.close()
        self.start()
        after = self.network_info()
        self.assertEqual(after["spki_sha256"], info["spki_sha256"])
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(after), headers=headers)[0], 200)

    def test_wildcard_public_hostname_and_invalid_listeners_are_rejected_before_starting(self):
        for address in ("0.0.0.0", "::", "8.8.8.8", "localhost", "224.0.0.1", "::ffff:8.8.8.8"):
            with self.subTest(address=address):
                self.config.write_text('[serve]\nlisten_addresses=' + json.dumps([
                    dict(address=address, port=0, enabled=True)]) + "\n")
                with self.assertRaisesRegex(ValueError, "invalid_listen_address"):
                    self.start()
                self.assertFalse(self.path.exists())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS certificates")
    def test_revocation_disconnects_an_idle_event_stream_and_rejects_token_after_restart(self):
        self.configure_network()
        service = self.start()
        info = self.network_info()
        device, headers = self.pair(info)
        connection = self.tls(info)
        connection.request("GET", "/v1/events", headers=headers)
        stream = connection.getresponse()
        self.addCleanup(stream.close)
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.readline(), b": connected\n")
        self.assertEqual(stream.readline(), b"\n")
        path = "/v1/devices/" + device["device_id"]
        self.assertEqual(self.request(path, "DELETE", {}, key="revoke")[0], 200)
        stream.read()  # Must reach EOF within the connection timeout, even when no new events arrive.
        self.assertTrue(stream.isclosed())
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info), headers=headers)[0], 401)
        service.close()
        self.start()
        info = self.network_info()
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info), headers=headers)[0], 401)
        self.assertEqual(self.request(path, "DELETE", {}, key="revoke"), (200, {"device_id": device["device_id"], "revoked": True}))

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS certificates")
    def test_push_registration_is_device_scoped_idempotent_and_hidden_from_queries(self):
        self.configure_network()
        self.start()
        info = self.network_info()
        device, headers = self.pair(info)
        route = "/v1/push-tokens"
        self.assertEqual(self.request(route, "POST", {"token": "private-apns"}, key=None,
            connection=self.tls(info), headers=headers)[0], 400)
        first = self.request(route, "POST", {"token": "private-apns"}, key="push",
            connection=self.tls(info), headers=headers)
        self.assertEqual(first, (200, {"device_id": device["device_id"], "registered": True}))
        self.assertEqual(self.request(route, "POST", {"token": "different-apns"}, key="push",
            connection=self.tls(info), headers=headers), first)
        self.assertNotIn("private-apns", json.dumps(self.request("/v1/devices")))
        self.assertNotIn("private-apns", json.dumps(events.read_after(self.state, 0)))

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_background_push_uses_registered_token_and_ignores_revoked_devices(self):
        deliveries = []
        sent = threading.Event()
        class Sender:
            def send(self, token, payload, *, stop):
                deliveries.append((token, payload))
                sent.set()
        self.start(push_sender=Sender())
        window = self.request("/v1/pairing-window", "POST", {}, key="window")[1]
        device = self.request("/v1/pair", "POST", {"code": window["code"]}, key="pair")[1]
        self.assertEqual(self.request("/v1/push-tokens", "POST", {"device_id": device["device_id"], "token": "apns"})[0], 200)
        log = events.EventLog(self.state)
        log.append("receipt.ready", dict(job="one", user_id="fixture", round=1, outcome="completed"))
        self.assertTrue(sent.wait(3), "后台未交给假发送器")
        self.assertEqual(deliveries, [("apns", {"aps": {"content-available": 1}, "counts": {"receipt_ready": 1}})])
        self.assertEqual(self.request("/v1/devices/" + device["device_id"], "DELETE", {}, key="revoke")[0], 200)
        # A live second device provides a delivery barrier for the same event.
        window = self.request("/v1/pairing-window", "POST", {}, key="window2")[1]
        second = self.request("/v1/pair", "POST", {"code": window["code"]}, key="pair2")[1]
        self.request("/v1/push-tokens", "POST", {"device_id": second["device_id"], "token": "apns2"}, key="push2")
        sent.clear()
        log.append("receipt.ready", dict(job="two", user_id="fixture", round=1, outcome="blocked"))
        self.assertTrue(sent.wait(3))
        self.assertEqual(deliveries[-1], ("apns2", {"aps": {"content-available": 1}, "counts": {"blocked": 1}}))
        self.assertEqual(len(deliveries), 2)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS certificates")
    def test_candidates_are_sent_in_events_and_session_access_can_only_be_enabled_locally(self):
        self.configure_network()
        self.start()
        info = self.network_info()
        device, headers = self.pair(info)
        connection = self.tls(info)
        connection.request("GET", "/v1/events", headers=headers)
        response = connection.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        event = None
        while True:
            line = response.readline()
            if line.startswith(b"data: "):
                value = json.loads(line[6:])
                if value["type"] == "network.addresses_changed":
                    event = value
                    break
        self.assertEqual(event["data"]["candidates"], info["candidates"])
        response.close()
        route = "/v1/devices/" + device["device_id"] + "/session-access"
        self.assertEqual(self.request(route, "POST", {"enabled": True}, connection=self.tls(info), headers=headers)[0], 403)
        self.assertEqual(self.request(route, "POST", {"enabled": True}, key="access")[0], 200)
        self.assertTrue(self.request("/v1/devices")[1]["devices"][0]["session_access_enabled"])
        # Session routes belong to ticket 25; the base service returns 404 once the network gate opens.
        self.assertEqual(self.request("/v1/sessions", connection=self.tls(info), headers=headers)[0], 404)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS certificates")
    def test_a_blocked_push_sender_does_not_delay_revocation_or_service_shutdown(self):
        entered = threading.Event()
        class BlockingSender:
            def send(self, token, payload, *, stop):
                entered.set()
                stop.wait()
        self.configure_network()
        service = self.start(push_sender=BlockingSender())
        info = self.network_info()
        device, headers = self.pair(info)
        self.request("/v1/push-tokens", "POST", {"token": "apns"}, key="push", connection=self.tls(info), headers=headers)
        connection = self.tls(info)
        connection.request("GET", "/v1/events", headers=headers)
        stream = connection.getresponse()
        self.addCleanup(stream.close)
        self.assertEqual(stream.status, 200)
        events.EventLog(self.state).append("receipt.ready", dict(job="one", user_id="fixture", round=1))
        self.assertTrue(entered.wait(3))
        local = UnixHTTPConnection(self.path)
        local.timeout = 3
        self.assertEqual(self.request("/v1/devices/" + device["device_id"], "DELETE", {}, key="revoke", connection=local)[0], 200)
        stream.read()
        self.assertTrue(stream.isclosed())
        service.close()  # The sender must observe stop, so shutdown can drain it.
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for pairing")
    def test_event_write_failure_rolls_back_pairing_and_allows_retry_with_same_key(self):
        self.start()
        window = self.request("/v1/pairing-window", "POST", {}, key="window")[1]
        replace = os.replace
        def unavailable(source, target):
            if Path(target).name == "events.jsonl":
                raise OSError("fixture event disk failure")
            return replace(source, target)
        with patch("os.replace", side_effect=unavailable):
            result = self.request("/v1/pair", "POST", {"code": window["code"]}, key="pair")
        self.assertEqual(result, (503, {"error": {"reason": "network_unavailable"}}))
        self.assertEqual(self.request("/v1/devices")[1], {"devices": []})
        self.assertEqual(self.request("/v1/pair", "POST", {"code": window["code"]}, key="pair")[0], 200)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS certificates")
    def test_an_unfinished_tls_handshake_does_not_block_other_clients_or_shutdown(self):
        self.configure_network()
        service = self.start()
        info = self.network_info()
        pending = socket.create_connection(("127.0.0.1", info["candidates"][0]["port"]), timeout=3)
        self.addCleanup(pending.close)
        self.assertEqual(self.request("/v1/jobs", connection=self.tls(info))[0], 403)
        service.close()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", info["candidates"][0]["port"]), timeout=3)
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for TLS certificates")
    def test_network_contract_examples_match_local_and_https_responses(self):
        self.configure_network()
        self.start()
        info = self.network_info()
        substitutions = {"SPKI": info["spki_sha256"]}
        def substitute(value):
            if isinstance(value, str):
                for key, item in substitutions.items():
                    value = value.replace(key, item)
                return value
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            if isinstance(value, list):
                return [substitute(item) for item in value]
            return value
        def structure(actual, expected):
            self.assertIs(type(actual), type(expected))
            if isinstance(expected, dict):
                self.assertTrue(set(expected) <= set(actual))
                for key in expected:
                    structure(actual[key], expected[key])
            elif isinstance(expected, list):
                self.assertEqual(len(actual), len(expected))
                for row, example in zip(actual, expected):
                    structure(row, example)
        contracts = Path(__file__).resolve().parents[1] / "contracts" / "v1"
        for name in ("addresses", "pairing-window", "pair", "push-tokens", "revoke", "unauthorized"):
            with self.subTest(contract=name):
                example = json.loads((contracts / ("network-" + name + ".json")).read_text())
                request = substitute(example["request"])
                remote = name in ("pair", "push-tokens", "unauthorized")
                status, response = self.request(request["path"], request["method"], request.get("body"), key=None,
                    connection=self.tls(info) if remote else None, headers=request.get("headers"))
                self.assertEqual(status, example["response"]["status"])
                structure(response, example["response"]["body"])
                if name == "pairing-window":
                    substitutions["PAIR_CODE"] = response["code"]
                if name == "pair":
                    substitutions.update(DEVICE_ID=response["device_id"], DEVICE_TOKEN=response["device_token"])
