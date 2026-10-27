"""Authenticated terminal control leases and bounded WebSocket bridges."""
import base64
import json
import queue
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import re

from . import rd_events as events
from .rd_herdr import TerminalControl
from .rd_terminal_redact import TerminalRedactor
from .rd_websocket import Reader, WebSocketError, frame, handshake
from .rd_serve_network import positive_setting, stream_allowed


MAX_INPUT = 1024 * 1024
QUEUE_BYTES = 8 * 1024 * 1024
QUEUE_FRAMES = 64


def command(value):
    if not isinstance(value, dict):
        raise WebSocketError(1008)
    kind = value.get("type")
    fields = {
        "terminal.input": {"type", "bytes"},
        "terminal.resize": {"type", "cols", "rows"},
        "terminal.scroll": {"type", "direction", "lines"},
        "terminal.mouse": {"type", "action", "button", "column", "row"},
        "terminal.release": {"type"},
    }
    if not isinstance(kind, str) or kind not in fields or set(value) != fields[kind]:
        raise WebSocketError(1008)
    if kind == "terminal.input":
        try:
            data = base64.b64decode(value["bytes"], validate=True)
        except (ValueError, TypeError):
            raise WebSocketError(1008) from None
        if not 0 < len(data) <= MAX_INPUT:
            raise WebSocketError(1009)
    elif kind == "terminal.resize":
        if any(type(value[key]) is not int or not 0 < value[key] <= 65535 for key in ("cols", "rows")):
            raise WebSocketError(1008)
    elif kind == "terminal.scroll":
        if value["direction"] not in ("up", "down") or type(value["lines"]) is not int or not 0 < value["lines"] <= 65535:
            raise WebSocketError(1008)
    elif kind == "terminal.mouse":
        if (value["action"] not in ("down", "up", "move", "drag")
                or value["button"] not in ("left", "middle", "right", "none")
                or any(type(value[key]) is not int or not 0 <= value[key] <= 65535 for key in ("column", "row"))):
            raise WebSocketError(1008)
    return value


class TerminalSockets:
    def __init__(self, sessions):
        self.sessions = sessions
        self.lock = threading.RLock()
        self.leases = {}
        self.closed = False
        self.timeout = positive_setting(sessions.service.config.cp, "request_timeout", 15)

    def close(self):
        with self.lock:
            self.closed = True
            for bridge in list(self.leases.values()):
                bridge.finish("serve_stopping", 1001)
                bridge.cleaned.wait()

    def handle_get(self, handler):
        path = unquote(urlsplit(handler.path).path)
        match = re.fullmatch(r"/v1/sessions/([^/]+)/terminal/ws", path)
        if match is None:
            return False
        pane = match[1]
        bridge = None
        handler.close_connection = True
        try:
            accept = handshake(handler)
            query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
            if (set(query) - {"takeover", "cols", "rows"}
                    or any(len(values) != 1 for values in query.values())
                    or query.get("takeover", ["false"])[0] not in ("true", "false")
                    or (("cols" in query) != ("rows" in query))):
                raise WebSocketError(1008)
            takeover = query.get("takeover") == ["true"]
            size = None
            if "cols" in query:
                try:
                    size = tuple(int(query[key][0]) for key in ("cols", "rows"))
                except ValueError:
                    raise WebSocketError(1008) from None
                if any(not 0 < n <= 65535 for n in size):
                    raise WebSocketError(1008)
            if not any(row["id"] == pane for row in self.sessions.list()):
                handler.json(404, {"error": {"reason": "session_closed"}})
                return True
            with self.lock:
                if self.closed or self.sessions.service.stop.is_set():
                    handler.json(503, {"error": {"reason": "serve_stopping"}})
                    return True
                old = self.leases.get(pane)
                if old and old.done.is_set():
                    old.cleaned.wait()
                    old = None
                if old:
                    if not takeover:
                        handler.json(409, {"error": {"reason": "terminal_busy"}})
                        return True
                    old.finish("taken_over", 1000)
                    old.cleaned.wait()
                cols, rows = size or self.sessions.herdr.viewport(pane, timeout=self.timeout)
                bridge = Bridge(self, pane, handler)
                # Publish before starting: close/takeover must wait for cleanup.
                self.leases[pane] = bridge
                bridge.control = TerminalControl(self.sessions.herdr, pane, cols, rows, takeover, self.timeout)
                first = bridge.control.receive(bridge.done)
                if first and first.get("type") == "terminal.closed":
                    reason = "terminal_busy" if "already has an attached client" in str(first.get("reason")) else "terminal_unavailable"
                    handler.json(409 if reason == "terminal_busy" else 503, {"error": {"reason": reason}})
                    return True
                if not first or first.get("type") != "terminal.frame" or first.get("full") is not True:
                    raise ValueError("terminal_unavailable")
                first = bridge.normalize(first)
                handler.connection.settimeout(self.timeout)
                handler.send_response(101)
                handler.send_header("Upgrade", "websocket")
                handler.send_header("Connection", "Upgrade")
                handler.send_header("Sec-WebSocket-Accept", accept)
                handler.end_headers()
                bridge.upgraded = True
                bridge.audit("connect")
                if takeover:
                    bridge.audit("takeover")
            bridge.run(first)
        except WebSocketError:
            if bridge is None or not bridge.upgraded:
                handler.json(400, {"error": {"reason": "invalid_websocket_request"}})
        except (OSError, EOFError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            if bridge is None or not bridge.upgraded:
                handler.json(503, {"error": {"reason": "terminal_unavailable"}})
        finally:
            if bridge is not None:
                bridge.cleanup()
                with self.lock:
                    if self.leases.get(pane) is bridge:
                        self.leases.pop(pane)
        return True


class Bridge:
    def __init__(self, owner, pane, handler):
        self.owner, self.pane, self.handler = owner, pane, handler
        self.control = None
        self.done = threading.Event()
        self.cleaned = threading.Event()
        self.released = threading.Event()
        self.control_reaped = threading.Event()
        self.reason, self.code = "released", 1000
        self.close_payload = None
        self.upgraded = False
        self.outbox = queue.Queue(QUEUE_FRAMES)
        self.queue_lock = threading.Lock()
        self.queued_bytes = self.queued_frames = 0
        self.input_bytes = self.output_bytes = self.dropped = 0
        self.redactor = TerminalRedactor(owner.sessions.service)
        self.sequence = 0
        self.cursor = dict(x=0, y=0, visible=False)
        self.last_pong = time.monotonic()
        self.ping = None
        self.workers = []

    def audit(self, action, **data):
        # Fixed allowlist only: never pass client JSON or Herdr error messages.
        events.emit(self.owner.sessions.service.state, "terminal." + action, dict(
            user_id=self.owner.sessions.service.user_id, pane_id=self.pane,
            device_id=getattr(self.handler, "device_id", None), **data))

    def finish(self, reason, code=1000):
        with self.queue_lock:
            if not self.done.is_set():
                self.reason, self.code = reason, code
                self.done.set()
        # The writer sends terminal.closed and closes the socket. During startup
        # there is no reader to wake, and cleanup is owned by handle_get.

    def enqueue(self, opcode, payload):
        packet = frame(opcode, payload)
        with self.queue_lock:
            if self.done.is_set():
                return
            if self.queued_bytes + len(packet) > QUEUE_BYTES or self.queued_frames >= QUEUE_FRAMES:
                self.dropped += 1
                self.reason, self.code = "backpressure", 1013
                self.done.set()
                return
            self.queued_bytes += len(packet)
            self.queued_frames += 1
            self.outbox.put_nowait(packet)

    def normalize(self, message):
        if message.get("encoding", "ansi") != "ansi":
            raise ValueError("terminal_unavailable")
        if (any(type(message.get(key)) is not int or not 0 < message[key] <= 65535 for key in ("width", "height"))
                or type(message.get("full")) is not bool):
            raise ValueError("terminal_unavailable")
        raw = base64.b64decode(message.get("bytes", message.get("data")), validate=True)
        self.output_bytes += len(raw)
        data = self.redactor.feed(raw)
        self.cursor = self.owner.sessions.herdr.cursor(data.decode("utf-8"), self.cursor)
        self.sequence += 1
        return dict(type="terminal.frame", seq=self.sequence, encoding="ansi", data=base64.b64encode(data).decode(),
                    width=message["width"], height=message["height"], full=message["full"], cursor=dict(self.cursor))

    def output_loop(self):
        try:
            while not self.done.is_set():
                message = self.control.receive(self.done)
                if message is None:
                    continue
                if message.get("type") == "terminal.closed":
                    self.finish("herdr_closed")
                    return
                if message.get("type") == "terminal.frame":
                    self.enqueue(1, json.dumps(self.normalize(message)).encode())
        except (OSError, EOFError, ValueError, TypeError, KeyError):
            self.finish("terminal_unavailable", 1011)
        finally:
            self.control.drain(self.control_reaped)

    def access_allowed(self):
        return stream_allowed(self.handler) and self.owner.sessions.allowed(getattr(self.handler, "session_device", None))

    def write_loop(self):
        try:
            next_ping = time.monotonic() + self.owner.timeout / 2
            while not self.done.is_set():
                now = time.monotonic()
                if not self.access_allowed():
                    self.finish("session_access_disabled", 1008)
                    break
                if self.ping is not None and now - self.last_pong >= self.owner.timeout:
                    self.finish("heartbeat_timeout", 1001)
                    break
                if now >= next_ping:
                    self.ping = struct.pack("!d", now)
                    self.handler.connection.sendall(frame(9, self.ping))
                    next_ping = now + self.owner.timeout / 2
                try:
                    packet = self.outbox.get(timeout=self.owner.timeout / 4)
                except queue.Empty:
                    continue
                self.handler.connection.sendall(packet)
                with self.queue_lock:
                    self.queued_bytes -= len(packet)
                    self.queued_frames -= 1
            self.released.wait()
            with self.queue_lock:
                self.dropped += self.outbox.qsize()
            # Drop queued viewport deltas: any reconnect starts with a full paint.
            self.handler.connection.sendall(frame(1, json.dumps(dict(type="terminal.closed", reason=self.reason,
                                                                    reconnect_full=True)).encode()))
            self.handler.connection.sendall(frame(8, self.close_payload if self.close_payload is not None else struct.pack("!H", self.code)))
        except OSError:
            self.finish("connection_closed", 1001)
        finally:
            self.done.set()
            try:
                self.handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self, first):
        self.enqueue(1, json.dumps(first).encode())
        for target in (self.write_loop, self.output_loop, self.read_loop):
            worker = threading.Thread(target=target, daemon=True)
            self.workers.append(worker)
            worker.start()
        self.done.wait()

    def read_loop(self):
        reader = Reader(self.handler.rfile)
        try:
            while not self.done.is_set():
                opcode, payload = reader.receive()
                if opcode == 8:
                    self.close_payload = payload
                    self.finish("connection_closed")
                    break
                if opcode == 9:
                    self.enqueue(10, payload)
                elif opcode == 10:
                    if self.ping is not None and payload == self.ping:
                        self.last_pong = time.monotonic()
                        self.ping = None
                else:
                    if not self.access_allowed():
                        self.finish("session_access_disabled", 1008)
                        break
                    value = command(json.loads(payload))
                    if value["type"] == "terminal.release":
                        self.finish("released")
                        break
                    self.control.send(value, self.done)
                    if value["type"] == "terminal.input":
                        self.input_bytes += len(base64.b64decode(value["bytes"]))
                    elif value["type"] == "terminal.resize":
                        self.audit("resize", cols=value["cols"], rows=value["rows"])
        except WebSocketError as exc:
            self.finish("message_too_large" if exc.code == 1009 else "invalid_terminal_message", exc.code)
        except (ValueError, TypeError):
            self.finish("invalid_terminal_message", 1008)
        except socket.timeout:
            self.finish("heartbeat_timeout", 1001)
        except (OSError, EOFError):
            self.finish("connection_closed", 1001)

    def cleanup(self):
        if self.cleaned.is_set():
            return
        self.finish("connection_closed", 1001)
        try:
            # Startup failures also need stdout draining while restoring size.
            if self.control is not None and not self.workers:
                drainer = threading.Thread(target=self.control.drain, args=(self.control_reaped,), daemon=True)
                self.workers.append(drainer)
                drainer.start()
            # Complete process cleanup before another owner can acquire this pane.
            restored = self.control.close() if self.control is not None else False
            self.control_reaped.set()
            self.released.set()
            for worker in self.workers:
                worker.join()
            if self.upgraded:
                self.audit("disconnect", reason=self.reason, input_bytes=self.input_bytes,
                           output_bytes=self.output_bytes, dropped_frames=self.dropped, restored=restored)
        finally:
            self.control_reaped.set()
            self.released.set()
            self.cleaned.set()
