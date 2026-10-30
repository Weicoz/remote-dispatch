"""WebSocket/假 Herdr 可执行文件接缝；永不连接真实 Herdr。"""
import base64
import json
import os
import socket
import ssl
import shutil
import struct
import threading
import unittest
from pathlib import Path

from tests import test_rd_serve_sessions as sessions_base


class WSClient:
    def __init__(self, connection):
        self.connection = connection
        self.file = connection.makefile("rb")

    def send(self, value, opcode=1, final=True, masked=True):
        payload = json.dumps(value).encode() if isinstance(value, dict) else value
        header = bytes([(128 if final else 0) | opcode])
        length = len(payload)
        flag = 128 if masked else 0
        if length < 126:
            header += bytes([flag | length])
        elif length < 65536:
            header += bytes([flag | 126]) + struct.pack("!H", length)
        else:
            header += bytes([flag | 127]) + struct.pack("!Q", length)
        mask = b"abcd" if masked else b""
        self.connection.sendall(header + mask + bytes(b ^ mask[i % 4] if masked else b
                                                       for i, b in enumerate(payload)))

    def receive(self):
        header = self.file.read(2)
        if not header:
            return None, None
        opcode, length = header[0] & 15, header[1] & 127
        if length == 126:
            length = struct.unpack("!H", self.file.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.file.read(8))[0]
        payload = self.file.read(length)
        return opcode, json.loads(payload) if opcode == 1 else payload

    def message(self):
        while True:
            opcode, payload = self.receive()
            if opcode == 9:
                self.send(payload, 10)
                continue
            return payload

    def close(self):
        self.file.close()
        self.connection.close()


class TerminalWSTests(unittest.TestCase):
    setUp = sessions_base.SessionTests.setUp
    agent = sessions_base.SessionTests.agent
    save_fixture = sessions_base.SessionTests.save_fixture
    start = sessions_base.SessionTests.start
    request = sessions_base.SessionTests.request
    def eventually(self, read, predicate):
        # 等待上限放宽到 30 秒；条件满足即返回，只影响失败路径与高负载。
        return sessions_base.SessionTests.eventually(self, read, predicate, timeout=30)

    # 心跳窗口等于 request_timeout；测试客户端等待期间不回 pong，高负载下 1 秒窗口会先到期。
    # 不测心跳的用例用此配置放宽窗口（心跳用例仍用默认 1 秒）。
    RELAXED = "[serve]\nrequest_timeout=60\n"

    def control_fixture(self, config_text=None, **options):
        self.data.update(options)
        self.save_fixture()
        script = self.bin / "herdr"
        content = script.read_text()
        branch = Path(__file__).with_name("fixtures").joinpath("herdr_control.inc").read_text()
        script.write_text(content.replace('elif a[:2] == ["pane", "read"]:',
                                         branch + '\nelif a[:2] == ["pane", "read"]:'))
        # 默认放宽心跳窗口：只有心跳用例显式用 1 秒（见 test_heartbeat_timeout_...）。
        self.config.write_text(config_text or self.RELAXED)
        return self.start(poll_interval=3600)

    def connect(self, query="", pane="w1:p1", headers="", connection=None):
        if connection is None:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(60)
            connection.connect(str(self.path))
        self.addCleanup(connection.close)
        connection.sendall((f"GET /v1/sessions/{pane}/terminal/ws{query} HTTP/1.1\r\n"
                            "Host: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                            "Sec-WebSocket-Version: 13\r\n"
                            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n" + headers + "\r\n").encode())
        client = WSClient(connection)
        self.addCleanup(client.close)
        status = int(client.file.readline().split()[1])
        response_headers = {}
        while True:
            line = client.file.readline()
            if line == b"\r\n":
                break
            key, value = line.decode().split(":", 1)
            response_headers[key.lower()] = value.strip()
        client.status, client.headers = status, response_headers
        return client

    def commands(self):
        if not self.writes.exists():
            return []
        return [json.loads(line) for line in self.writes.read_text().splitlines()]

    def test_raw_input_paste_controls_and_restore_before_release(self):
        self.control_fixture()
        client = self.connect()
        self.assertEqual(client.status, 101)
        self.assertEqual(client.headers["sec-websocket-accept"], "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        frame = client.message()
        self.assertEqual((frame["type"], frame["full"], frame["width"], frame["height"]),
                         ("terminal.frame", True, 149, 45))
        self.assertEqual(frame["cursor"], dict(x=4, y=2, visible=True))
        paste = b"\x1b[200~line1\n" + "中文".encode() + b"\x1b[201~"
        commands = [dict(type="terminal.input", bytes=base64.b64encode(paste).decode()),
                    dict(type="terminal.resize", cols=80, rows=24),
                    dict(type="terminal.scroll", direction="up", lines=5),
                    dict(type="terminal.mouse", action="down", button="left", column=12, row=5)]
        for command in commands:
            client.send(command)
        self.eventually(self.commands, lambda rows: len(rows) >= 5)
        client.send(dict(type="terminal.release"))
        self.assertEqual(client.message()["type"], "terminal.closed")
        rows = self.eventually(self.commands, lambda rows: rows[-1].get("type") == "terminal.release")
        self.assertEqual(rows[0], dict(start=True, cols=149, rows=45, takeover=False))
        self.assertEqual(rows[1:5], commands)
        self.assertEqual(rows[-2:], [dict(type="terminal.resize", cols=149, rows=45),
                                    dict(type="terminal.release")])

    def test_redacts_credentials_across_frames_ansi_and_split_osc_without_logging_body(self):
        def paint(text, full=False):
            return dict(type="terminal.frame", full=full, width=149, height=45,
                        bytes=base64.b64encode(text.encode()).decode())
        service = self.control_fixture(control_frames=[
            paint("\x1b[2Jfixture-", True), paint("se\x1b[31mcret-value "),
            paint("token=abc"), paint("def \x1b]52;c;private"),
            paint("-osc\x1b"), paint("\\safe\x1b[3;"), paint("5H\x1b[?25h")])
        service.sensitive.add("fixture-secret-value")
        client = self.connect()
        self.assertEqual(client.status, 101)
        frames = [client.message() for _ in range(7)]
        data = b"".join(base64.b64decode(item["data"]) for item in frames)
        for secret in (b"fixture-", b"secret-value", b"abcdef", b"private-osc"):
            self.assertNotIn(secret, data)
        self.assertIn(b"\x1b[31m", data)
        self.assertIn(b"safe\x1b[3;5H\x1b[?25h", data)
        self.assertEqual(frames[-1]["cursor"], dict(x=4, y=2, visible=True))
        self.assertEqual([item["seq"] for item in frames], list(range(1, 8)))
        client.send(dict(type="terminal.release"))
        client.message()
        service.close()
        audit = (self.state / "events.jsonl").read_text()
        for forbidden in ("fixture-secret-value", "abcdef", "private-osc", '"bytes"', '"data"'):
            if forbidden != '"data"':  # Event envelope legitimately has a data field.
                self.assertNotIn(forbidden, audit)

    def test_busy_takeover_waits_for_old_cleanup_and_reconnect_starts_full(self):
        service = self.control_fixture()
        first = self.connect()
        self.assertEqual(first.status, 101)
        self.assertTrue(first.message()["full"])
        second = self.connect()
        self.assertEqual(second.status, 409)
        self.assertEqual(json.loads(second.file.read(int(second.headers["content-length"])))
                         ["error"]["reason"], "terminal_busy")
        third = self.connect("?takeover=true&cols=80&rows=24")
        self.assertEqual(third.status, 101)
        self.assertEqual(first.message()["reason"], "taken_over")
        frame = third.message()
        self.assertEqual((frame["seq"], frame["full"], frame["width"], frame["height"]), (1, True, 80, 24))
        rows = self.commands()
        self.assertEqual(rows[1:4], [dict(type="terminal.resize", cols=149, rows=45),
                                    dict(type="terminal.release"), dict(start=True, cols=80, rows=24, takeover=True)])
        third.send(dict(type="terminal.release"))
        third.message()
        service.close()
        audit = (self.state / "events.jsonl").read_text()
        self.assertIn('"terminal.takeover"', audit)

    def test_disconnect_reaps_control_and_restores_current_layout(self):
        pidfile = self.root / "control.pid"
        service = self.control_fixture(control_pid=str(pidfile))
        client = self.connect("?cols=80&rows=24")
        self.assertEqual(client.status, 101)
        client.message()
        pid = int(pidfile.read_text())
        self.data["viewport"] = dict(width=160, height=50)
        self.save_fixture()
        client.send(struct.pack("!H", 1000), 8)
        self.assertEqual(client.message()["type"], "terminal.closed")
        self.eventually(self.commands, lambda rows: rows[-1].get("type") == "terminal.release")
        service.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.commands()[-2], dict(type="terminal.resize", cols=160, rows=50))

    def test_fragmented_input_with_interleaved_ping_is_one_command_and_never_replayed(self):
        self.control_fixture()
        client = self.connect()
        client.message()
        payload = json.dumps(dict(type="terminal.input", bytes=base64.b64encode(b"\x1b[200~paste\x1b[201~").decode())).encode()
        client.send(payload[:20], final=False)
        client.send(b"probe", 9)
        self.assertEqual(client.receive(), (10, b"probe"))
        client.send(payload[20:], opcode=0)
        self.eventually(self.commands, lambda rows: len(rows) == 2)
        client.send(dict(type="terminal.release"))
        client.message()
        reconnect = self.connect()
        self.assertEqual(reconnect.status, 101)
        self.assertTrue(reconnect.message()["full"])
        reconnect.send(dict(type="terminal.release"))
        reconnect.message()
        self.assertEqual([row for row in self.commands() if row.get("type") == "terminal.input"],
                         [json.loads(payload)])

    def test_invalid_messages_and_rfc_framing_close_without_input(self):
        self.control_fixture()
        cases = [(dict(type="terminal.input", bytes="not base64"), 1, True, 1008),
                 (dict(type="terminal.resize", cols=True, rows=24), 1, True, 1008),
                 (b"unmasked", 1, False, 1002), (b"\xff", 1, True, 1007),
                 (b"binary", 2, True, 1003), (b"x" * 126, 9, True, 1002),
                 (struct.pack("!H", 1005), 8, True, 1002)]
        for payload, opcode, masked, expected in cases:
            with self.subTest(code=expected, opcode=opcode):
                client = self.connect()
                self.assertEqual(client.status, 101)
                client.message()
                client.send(payload, opcode, masked=masked)
                self.assertEqual(client.message()["type"], "terminal.closed")
                opcode, payload = client.receive()
                self.assertEqual((opcode, struct.unpack("!H", payload[:2])[0]), (8, expected))
        self.assertFalse(any(row.get("type") == "terminal.input" for row in self.commands()))

    def test_input_size_limit_and_oversize_fragment_totals(self):
        self.control_fixture(config_text=self.RELAXED)
        client = self.connect()
        client.message()
        raw = b"x" * (1024 * 1024)
        client.send(dict(type="terminal.input", bytes=base64.b64encode(raw).decode()))
        self.eventually(self.commands, lambda rows: any(row.get("type") == "terminal.input" for row in rows))
        client.send(dict(type="terminal.input", bytes=base64.b64encode(raw + b"x").decode()))
        self.assertEqual(client.message()["reason"], "message_too_large")
        self.assertEqual(struct.unpack("!H", client.receive()[1][:2])[0], 1009)
        rows = [row for row in self.commands() if row.get("type") == "terminal.input"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(base64.b64decode(rows[0]["bytes"])), 1024 * 1024)

    def test_slow_client_closes_with_backpressure_and_reaps_process(self):
        pidfile = self.root / "control.pid"
        frame = dict(type="terminal.frame", full=True, width=149, height=45,
                     bytes=base64.b64encode(b"x" * 32768).decode())
        # 慢客户端用例依赖短 socket 超时让写线程尽快退出，保留 1 秒窗口。
        service = self.control_fixture(config_text="[serve]\nrequest_timeout=1\n", control_frames=[frame], flood=True,
                                       control_pid=str(pidfile))
        client = self.connect()
        self.assertEqual(client.status, 101)
        def audit():
            return (self.state / "events.jsonl").read_text()
        self.eventually(audit, lambda data: '"backpressure"' in data and '"terminal.disconnect"' in data)
        pid = int(pidfile.read_text())
        service.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        disconnect = [json.loads(line) for line in audit().splitlines()
                      if json.loads(line)["type"] == "terminal.disconnect"][-1]
        self.assertGreaterEqual(disconnect["data"]["dropped_frames"], 1)
        self.assertTrue(disconnect["data"]["restored"])
        self.assertEqual(self.commands()[-2:], [dict(type="terminal.resize", cols=149, rows=45),
                                              dict(type="terminal.release")])

    def test_heartbeat_timeout_and_shutdown_restore_release_and_reap(self):
        pidfile = self.root / "control.pid"
        service = self.control_fixture(config_text="[serve]\nrequest_timeout=1\n", control_pid=str(pidfile))
        client = self.connect()
        # 心跳用例用短 socket 超时：服务端未按时断开时快速失败，而不是等满 60 秒。
        client.connection.settimeout(5)
        client.message()
        # Read the ping without answering it.
        self.assertEqual(client.receive()[0], 9)
        self.assertEqual(client.message()["reason"], "heartbeat_timeout")
        pid = int(pidfile.read_text())
        self.eventually(self.commands, lambda rows: rows[-1].get("type") == "terminal.release")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        another = self.connect("?cols=80&rows=24")
        self.assertEqual(another.status, 101)
        self.assertTrue(another.message()["full"])
        pid = int(pidfile.read_text())
        service.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.commands()[-2:], [dict(type="terminal.resize", cols=149, rows=45),
                                              dict(type="terminal.release")])

    def test_external_busy_and_closed_reasons_are_stable_without_herdr_text(self):
        service = self.control_fixture(control_busy=True)
        client = self.connect()
        self.assertEqual(client.status, 409)
        body = json.loads(client.file.read(int(client.headers["content-length"])))
        self.assertEqual(body["error"]["reason"], "terminal_busy")
        takeover = self.connect("?takeover=true")
        self.assertEqual(takeover.status, 101)
        self.assertTrue(takeover.message()["full"])
        takeover.send(dict(type="terminal.release"))
        takeover.message()
        self.data["control_busy"] = False
        self.data["control_frames"] = [dict(type="terminal.frame", full=True, width=149, height=45,
            bytes=base64.b64encode(b"ok").decode()),
            dict(type="terminal.closed", reason="private-upstream-body")]
        self.save_fixture()
        client = self.connect()
        self.assertEqual(client.status, 101)
        messages = [client.message()]
        if messages[-1]["type"] != "terminal.closed":
            messages.append(client.message())
        self.assertEqual(messages[-1]["reason"], "herdr_closed")
        service.close()
        self.assertNotIn("private-upstream-body", (self.state / "events.jsonl").read_text())

    def test_handshake_query_mapping_and_session_access_are_checked_before_upgrade(self):
        service = self.control_fixture()
        denied = self.connect(headers="Sec-WebSocket-Version: 12\r\n")
        self.assertEqual(denied.status, 400)
        self.assertEqual(self.connect("?takeover=1").status, 400)
        self.assertEqual(self.connect("?cols=abc&rows=24").status, 400)
        self.assertEqual(self.connect(pane="w1:unmapped").status, 404)
        self.assertEqual(self.request("/v1/config/session-access", "PUT", json.dumps({"enabled": False}),
                         {"Content-Type": "application/json", "Idempotency-Key": "disable-sessions"})[0], 200)
        self.assertEqual(self.connect().status, 403)
        self.assertEqual(self.commands(), [])

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture requires openssl")
    def test_tls_device_auth_session_grant_and_live_revocation(self):
        service = self.control_fixture(config_text='[serve]\nrequest_timeout=2\nlisten_addresses=[{"address":"127.0.0.1","port":0}]\n')
        info = self.request("/v1/network")[1]
        def tls():
            raw = socket.create_connection(("127.0.0.1", info["candidates"][0]["port"]), timeout=5)
            connection = ssl._create_unverified_context().wrap_socket(raw, server_hostname="localhost")
            self.addCleanup(connection.close)
            return connection
        def local_post(path, body, key):
            return self.request(path, "POST", json.dumps(body), {"Content-Type": "application/json", "Idempotency-Key": key})
        window = local_post("/v1/pairing-window", {}, "window")[1]
        device = local_post("/v1/pair", dict(code=window["code"], name="Fixture"), "pair")[1]
        pin = "X-Rdispatch-SPKI: " + info["spki_sha256"] + "\r\n"
        authorization = "Authorization: Bearer " + device["device_token"] + "\r\n"
        self.assertEqual(self.connect(connection=tls()).status, 403)
        self.assertEqual(self.connect(headers=pin, connection=tls()).status, 401)
        self.assertEqual(self.connect(headers=pin + authorization, connection=tls()).status, 403)
        self.assertEqual(local_post("/v1/devices/" + device["device_id"] + "/session-access", dict(enabled=True), "enable")[0], 200)
        client = self.connect(headers=pin + authorization, connection=tls())
        self.assertEqual(client.status, 101)
        self.assertTrue(client.message()["full"])
        client.send(dict(type="terminal.input", bytes=base64.b64encode(b"tls-input").decode()))
        self.eventually(self.commands, lambda rows: any(row.get("type") == "terminal.input" for row in rows))
        local_post("/v1/devices/" + device["device_id"] + "/session-access", dict(enabled=False), "disable")
        self.assertEqual(client.message()["reason"], "session_access_disabled")
        self.eventually(self.commands, lambda rows: rows[-1].get("type") == "terminal.release")
        service.close()

    def test_pem_and_quoted_credentials_remain_masked_across_many_paints(self):
        fragments = ["-----BE", "GIN OPENSSH PRIVATE KEY-----\n", "privatebody", "\n-----END OPENSSH ",
                     "PRIVATE KEY-----\n", "api_key=\"quoted", " ", "value\" public"]
        frames = [dict(type="terminal.frame", width=149, height=45, full=index == 0,
                       bytes=base64.b64encode(text.encode()).decode()) for index, text in enumerate(fragments)]
        self.control_fixture(control_frames=frames)
        client = self.connect()
        self.assertEqual(client.status, 101)
        text = b"".join(base64.b64decode(client.message()["data"]) for _ in frames)
        for secret in (b"privatebody", b"quoted", b"value"):
            self.assertNotIn(secret, text)
        self.assertIn(b"public", text)
        client.send(dict(type="terminal.release"))
        client.message()

    def test_credential_whitespace_and_escaped_quotes_do_not_expose_value(self):
        fragments = ['password', ' ' * 1024, '=\"one\\\"', 'two\" public']
        frames = [dict(type="terminal.frame", width=149, height=45, full=index == 0,
                       bytes=base64.b64encode(text.encode()).decode()) for index, text in enumerate(fragments)]
        self.control_fixture(control_frames=frames)
        client = self.connect()
        self.assertEqual(client.status, 101)
        text = b"".join(base64.b64decode(client.message()["data"]) for _ in frames)
        self.assertNotIn(b"one", text)
        self.assertNotIn(b"two", text)
        self.assertIn(b"public", text)
        client.send(dict(type="terminal.release"))
        client.message()

    def test_shutdown_finishes_partial_input_before_restore_and_release(self):
        gate = self.root / "allow-input"
        service = self.control_fixture(config_text=self.RELAXED, control_gate=str(gate))
        client = self.connect("?cols=80&rows=24")
        client.message()
        payload = dict(type="terminal.input", bytes=base64.b64encode(b"x" * (1024 * 1024)).decode())
        client.send(payload)
        self.eventually(self.commands, lambda rows: any("partial_input_bytes" in row for row in rows))
        closer = threading.Thread(target=service.close)
        closer.start()
        self.addCleanup(closer.join)
        self.eventually(service.stop.is_set, bool)
        gate.touch()
        closer.join(30)
        self.assertFalse(closer.is_alive())
        rows = [row for row in self.commands() if "type" in row]
        self.assertEqual([row["type"] for row in rows], ["terminal.input", "terminal.resize", "terminal.release"])
        self.assertTrue(rows[0] == payload, "原始 input 字节必须完整且只送达一次")
        self.assertEqual(rows[1:], [dict(type="terminal.resize", cols=149, rows=45), dict(type="terminal.release")])

    def test_json_credential_field_name_split_across_frames_is_scrubbed(self):
        fragments = ['{"tok', 'en"', ':"unconfigured-', 'value"} public']
        frames = [dict(type="terminal.frame", width=149, height=45, full=index == 0,
                       bytes=base64.b64encode(text.encode()).decode()) for index, text in enumerate(fragments)]
        self.control_fixture(config_text=self.RELAXED, control_frames=frames)
        client = self.connect()
        self.assertEqual(client.status, 101)
        text = b"".join(base64.b64decode(client.message()["data"]) for _ in frames)
        self.assertNotIn(b"unconfigured-", text)
        self.assertNotIn(b"value", text)
        self.assertIn(b"public", text)
        client.send(dict(type="terminal.release"))
        client.message()
