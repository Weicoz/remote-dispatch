"""The single subprocess boundary for Herdr; never launches a harness."""
import base64
import json
import os
import re
from pathlib import Path
import select
import socket
import subprocess
import threading
import time


class InputNotSubmitted(OSError):
    """文本已写入窗格，但回车未成功；调用方不得重发文本。"""


class Herdr:
    def __init__(self):
        self.observers = set()
        self.observers_lock = threading.Lock()
        self.closed = False

    @staticmethod
    def reap(process):
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()

    def close(self):
        with self.observers_lock:
            self.closed = True
            observers = list(self.observers)
        for process, connection in observers:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.reap(process)

    def snapshot(self):
        config = Path(os.environ.get("HERDR_CONFIG_PATH", Path.home() / ".config/herdr/config.toml"))
        directory = config.parent
        session = os.environ.get("HERDR_SESSION")
        if session:
            # Session selection is process configuration, never an HTTP argument.
            if Path(session).name != session or session in (".", ".."):
                return {}
            directory = directory / "sessions" / session
        try:
            saved = json.loads((directory / "session.json").read_text(encoding="utf-8"))
            if saved.get("version") != 3:
                return {}
            panes = {}
            for workspace in saved["workspaces"]:
                for tab in workspace["tabs"]:
                    for raw, pane in tab["panes"].items():
                        if not isinstance(pane, dict):
                            continue
                        number = workspace["public_pane_numbers"].get(raw)
                        if type(number) is not int or number < 1:
                            continue
                        encoded = ""
                        while number:
                            number, digit = divmod(number - 1, 32)
                            encoded = "123456789ABCDEFGHJKMNPQRSTVWXYZ0"[digit] + encoded
                        panes[workspace["id"] + ":p" + encoded] = pane
            return panes
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def command(self, *args):
        return ["herdr", *args]

    def run(self, *args):
        process = subprocess.run(self.command(*args), capture_output=True,
                                 env=dict(os.environ, HERDR_ENV="1"))
        if process.returncode:
            raise OSError("herdr_unavailable")
        return process.stdout.decode("utf-8")

    def agents(self):
        try:
            result = json.loads(self.run("agent", "list"))
            if isinstance(result, dict):
                result = result.get("result", result)
            rows = result["agents"] if isinstance(result, dict) else result
        except (ValueError, KeyError, TypeError) as error:
            raise OSError("herdr_unavailable") from error
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise OSError("herdr_unavailable")
        return rows

    def viewport(self, pane):
        try:
            result = json.loads(self.run("pane", "layout", "--pane", pane))
            if not isinstance(result, dict):
                raise ValueError("invalid_layout")
            layout = result.get("result", result)["layout"]
            rect = next(row["rect"] for row in layout["panes"] if row["pane_id"] == pane)
            size = rect["width"], rect["height"]
            if any(type(n) is not int or not 0 < n <= 65535 for n in size):
                raise ValueError("invalid_viewport")
            return size
        except (ValueError, KeyError, TypeError, StopIteration) as error:
            raise OSError("herdr_viewport_unavailable") from error

    @staticmethod
    def cursor(text, previous):
        # Herdr's blitter always ends a paint with absolute CUP and ?25h/l.
        # Ignore OSC bodies, which may themselves contain cursor-like text.
        cursor = dict(previous)
        for match in re.finditer(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[([0-?]*)([ -/]*)([@-~])", text):
            params, intermediate, command = match.groups()
            if params is None or intermediate:
                continue
            if command in ("H", "f") and re.fullmatch(r"[0-9]*(?:;[0-9]*)?", params):
                parts = params.split(";")
                cursor.update(x=max(1, int(parts[1] or 1) if len(parts) > 1 else 1) - 1,
                              y=max(1, int(parts[0] or 1)) - 1)
            elif params == "?25" and command in ("h", "l"):
                cursor["visible"] = command == "h"
        return cursor

    def input(self, pane, body):
        if "text" in body:
            self.run("pane", "send-text", pane, "--", body["text"])
            if not body.get("submit", True):
                return
            try:
                self.run("pane", "send-keys", pane, "enter")
            except (OSError, ValueError) as error:
                raise InputNotSubmitted("文本已送达，但回车未成功。") from error
        else:
            self.run("pane", "send-keys", pane, *body["keys"])

    def frames(self, pane, stop, connection, poll_interval):
        width, height = self.viewport(pane)
        sequence = 0
        while not stop.is_set():
            cursor = dict(x=0, y=0, visible=False)
            with self.observers_lock:
                if self.closed or stop.is_set():
                    return
                process = subprocess.Popen(self.command("terminal", "session", "observe", pane,
                                                        "--cols", str(width), "--rows", str(height)),
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=dict(os.environ, HERDR_ENV="1"), bufsize=0)
                self.observers.add((process, connection))
            try:
                pending = b""
                next_layout = time.monotonic() + poll_interval
                while not stop.is_set():
                    ready, _, _ = select.select([process.stdout, connection], [], [], 0.1)
                    if connection in ready and not connection.recv(1, socket.MSG_PEEK):
                        return
                    if time.monotonic() >= next_layout:
                        size = self.viewport(pane)
                        if size != (width, height):
                            width, height = size
                            # A fresh read-only observer supplies a full repaint.
                            # Never relabel the old dimensions on existing ANSI.
                            break
                        next_layout = time.monotonic() + poll_interval
                    if process.stdout not in ready:
                        continue
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        return
                    pending += data
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        frame = json.loads(line)
                        if frame.get("type") == "terminal.frame":
                            encoded = frame.pop("bytes", frame.get("data"))
                            text = base64.b64decode(encoded, validate=True).decode("utf-8", "surrogateescape")
                            if (frame.get("width", width), frame.get("height", height)) != (width, height):
                                raise OSError("herdr_frame_size_mismatch")
                            cursor = self.cursor(text, cursor)
                            sequence = max(sequence + 1, frame.get("seq", 0))
                            frame.update(data=encoded, encoding="ansi", width=width, height=height,
                                         cursor=cursor, seq=sequence)
                        yield json.dumps(frame, ensure_ascii=True).encode("utf-8")
            finally:
                self.reap(process)
                process.stdout.close()
                with self.observers_lock:
                    self.observers.discard((process, connection))
