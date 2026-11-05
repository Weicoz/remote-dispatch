"""herdr-core（hc）的唯一子进程边界：只读发现、状态与读屏；不启动 harness，不向终端写入任何内容。

数据源是 `hc events --json`：首帧为快照（含 status、cwd、cols/rows），之后是带 seq 的增量。
`hc ls --json` 只在每次（重）连时做发现，补上快照里缺的终端；它不含 status。
读接口只读本模块的内存缓存；断流后标记不可用，重连并以新快照整体对齐。
"""
import json
import os
import re
import select
import shutil
import subprocess
import threading
import time


# hc `status.state` 的取值来自 `hc schema`（idle、working、done、blocked、error）。
# 本机只实测到普通 shell 的 status 为 null；其余取值未在真实 agent 终端上实测。
# error 在契约集合里没有对应项，未确认其含义，一律按 unknown。
STATUS_MAP = {"working": "working", "blocked": "blocked", "done": "done", "idle": "idle"}
ADDRESS = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*")


def map_status(status):
    """hc 的 status 记录（或 null）映射到会话状态；未确认的取值一律 unknown。"""
    state = status.get("state") if isinstance(status, dict) else None
    return STATUS_MAP.get(state, "unknown") if isinstance(state, str) else "unknown"


class Hc:
    """App 会话后端（herdr-core）。surface=app，M1 只支持读屏。"""
    surface = "app"
    capabilities = ("screen",)
    IDLE_TIMEOUT = 45.0  # 含心跳在内的任何输出都没有超过该时长，视为 hc 挂住
    GRACE = 5.0

    def __init__(self, on_change=None, debounce=0.3, screen_ttl=2.0):
        self.on_change = on_change
        self.debounce, self.screen_ttl = debounce, screen_ttl
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.process = None
        self.terminals = {}
        self.versions = {}
        self.screens = {}
        self.seq = None
        self.live = False
        self.delay = 0.2
        self.lost_at = None
        self.grace = self.GRACE
        self.idle_timeout = self.IDLE_TIMEOUT
        self.reason = "hc_not_started"

    # ---- 子进程边界 ----
    @staticmethod
    def binary():
        """RD_HC_BIN 未设置时在 PATH 里找 hc；设为空串表示关闭该后端。"""
        configured = os.environ.get("RD_HC_BIN")
        if configured is not None:
            return configured or None
        return shutil.which("hc")

    def command(self, *args):
        return [self.binary() or "hc", *args]

    def run(self, *args, timeout=5):
        if not self.binary():
            raise OSError("hc_unavailable")
        try:
            process = subprocess.run(self.command(*args), capture_output=True, timeout=timeout,
                                     stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as error:
            raise OSError("hc_unavailable") from error
        if process.returncode:
            raise OSError("hc_unavailable")
        return process.stdout.decode("utf-8", "replace")

    # ---- 生命周期 ----
    def start(self):
        """惰性启动监听线程；hc 未安装时直接标不可用，不影响 TUI 后端。"""
        with self.lock:
            if self.thread is not None:
                return
            if not self.binary():
                self.reason = "hc_not_installed"
                return
            self.reason = "hc_connecting"
            self.thread = threading.Thread(target=self.watch, daemon=True)
            self.thread.start()

    def close(self):
        self.stopped.set()
        with self.lock:
            process = self.process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        thread = self.thread
        if thread is not None:
            thread.join(timeout=5)

    @property
    def available(self):
        with self.lock:
            return self.live

    def status(self):
        with self.lock:
            return dict(available=self.live, reason=None if self.live else self.reason)

    # ---- 监听 ----
    def watch(self):
        while not self.stopped.is_set():
            try:
                self.stream()
            except (OSError, ValueError):
                pass
            with self.lock:
                # 断流后保留最近快照一小段时间，重连成功即以新快照对齐；超时仍连不上才标不可用。
                if self.live and self.lost_at is None:
                    self.lost_at = time.monotonic()
                    self.reason = "hc_stream_lost"
                elif self.live and time.monotonic() - self.lost_at > self.grace:
                    self.live = False
                elif self.reason == "hc_connecting":
                    self.reason = "hc_core_unavailable"
            self.changed()
            self.stopped.wait(self.delay)
            self.delay = min(self.delay * 2, 10)

    def stream(self):
        try:
            found = self.discover()
        except (OSError, ValueError):
            found = None
        process = subprocess.Popen(self.command("events", "--json"), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        with self.lock:
            self.process = process
        try:
            if self.stopped.is_set():
                return
            descriptor, pending, last = process.stdout.fileno(), b"", time.monotonic()
            while not self.stopped.is_set():
                remaining = self.idle_timeout - (time.monotonic() - last)
                if remaining <= 0:
                    raise OSError("hc_idle_timeout")
                ready, _, _ = select.select([descriptor], [], [], min(remaining, 0.2))
                if not ready:
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    raise OSError("hc_stream_ended")
                last = time.monotonic()
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("hc_event_invalid")
                    self.handle(message, found)
                    found = None if self.live else found
        finally:
            with self.lock:
                self.process = None
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
            if process.stdout:
                process.stdout.close()

    def discover(self):
        """`hc ls --json`：只取终端地址，作为快照之外的发现补充。"""
        result = json.loads(self.run("ls", "--json"))
        rows = result.get("terminals") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise OSError("hc_unavailable")
        return [row["address"] for row in rows
                if isinstance(row, dict) and isinstance(row.get("address"), str) and row.get("state", "live") == "live"]

    def handle(self, message, found):
        message = message.get("result") if isinstance(message.get("result"), dict) else message
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "terminals" in message and isinstance(message["terminals"], list):
            self.replace(message["terminals"], message.get("seq"), found)
        elif method == "heartbeat":
            return
        elif method == "missed":
            if isinstance(params.get("terminals"), list):
                self.replace(params["terminals"], params.get("seq"), None)
            else:
                # 落后订阅者：丢弃增量，重连取新快照。
                raise OSError("hc_missed")
        elif method == "event":
            self.apply(params)

    def replace(self, terminals, seq, found):
        table = {row["address"]: row for row in terminals
                 if isinstance(row, dict) and isinstance(row.get("address"), str)}
        for address in found or ():
            if address not in table:
                try:
                    table[address] = self.fetch(address)
                except (OSError, ValueError):
                    pass
        with self.lock:
            for address in table.keys() | self.terminals.keys():
                if table.get(address) != self.terminals.get(address):
                    self.versions[address] = self.versions.get(address, 0) + 1
            self.terminals = table
            self.seq = seq if type(seq) is int else 0
            self.live, self.reason, self.delay, self.lost_at = True, None, 0.2, None
            for address in list(self.screens):
                if address not in table:
                    del self.screens[address]
        self.changed()

    def fetch(self, address):
        if not ADDRESS.fullmatch(address):
            raise OSError("hc_unavailable")
        row = json.loads(self.run("get", address, "--json"))
        if not isinstance(row, dict):
            raise OSError("hc_unavailable")
        return dict(row, address=address)

    def apply(self, params):
        seq = params.get("seq")
        with self.lock:
            if type(seq) is int and self.seq is not None and seq <= self.seq:
                return
            if type(seq) is int:
                self.seq = seq
        terminal = params.get("terminal")
        embedded = terminal if isinstance(terminal, dict) else None
        address = embedded.get("address") if embedded else terminal if isinstance(terminal, str) else params.get("address")
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise OSError("hc_event_unrecognized")
        kind = params.get("kind") or params.get("event") or params.get("type")
        if kind == "removed":
            row = None
        elif embedded and "state" in embedded:
            with self.lock:
                row = dict(self.terminals.get(address, {}), **embedded)
        else:
            # 增量正文形状未在本机观测到；不识别的变化一律对该终端 `hc get` 对齐。
            try:
                row = self.fetch(address)
            except (OSError, ValueError):
                row = None
        with self.lock:
            if row is None:
                self.terminals.pop(address, None)
                self.screens.pop(address, None)
            else:
                self.terminals[address] = row
            self.versions[address] = self.versions.get(address, 0) + 1
        self.changed()

    def changed(self):
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    # ---- 统一后端接口（只读缓存，不在请求路径上起子进程） ----
    def snapshot(self):
        """address -> 终端记录（含 cwd）。"""
        with self.lock:
            return dict(self.terminals) if self.live else {}

    def agents(self):
        """可用时返回存活终端的归一化行；不可用时返回空列表。"""
        with self.lock:
            if not self.live:
                return []
            rows = []
            for address, row in sorted(self.terminals.items()):
                if row.get("state", "live") != "live":
                    continue
                foreground = row.get("foreground") if isinstance(row.get("foreground"), dict) else {}
                status = row.get("status") if isinstance(row.get("status"), dict) else None
                names = [status.get("app") if status else None, foreground.get("name")]
                for argv in (foreground.get("argv"), row.get("argv")):
                    if isinstance(argv, list) and argv and isinstance(argv[0], str):
                        names.append(os.path.basename(argv[0]))
                        if len(argv) > 1 and isinstance(argv[1], str):
                            names.append(os.path.splitext(os.path.basename(argv[1]))[0])
                rows.append(dict(address=address, names=[n for n in names if isinstance(n, str)],
                                 cwd=foreground.get("cwd") or row.get("cwd") or None,
                                 status=map_status(status), raw_state=status.get("state") if status else None,
                                 stamp_ms=row.get("status_ms") or row.get("created_ms")))
            return rows

    def viewport(self, address, *, timeout=None):
        with self.lock:
            row = self.terminals.get(address) if self.live else None
        cols, rows = (row or {}).get("cols"), (row or {}).get("rows")
        if type(cols) is not int or type(rows) is not int or not 0 < cols <= 65535 or not 0 < rows <= 65535:
            raise OSError("hc_viewport_unavailable")
        return cols, rows

    def notify(self, title, body, sound):
        """hc 没有界面通知通道；保持统一接口，什么也不做。"""
        return False

    def screen(self, address, *, timeout=5):
        """`hc read`：终端有变化事件或缓存超过 screen_ttl 时才重读，且两次读取至少间隔 debounce。"""
        if not ADDRESS.fullmatch(address):
            raise OSError("hc_unavailable")
        width, height = self.viewport(address)
        now = time.monotonic()
        with self.lock:
            cache = self.screens.get(address)
            version = self.versions.get(address, 0)
            if cache and (now - cache["at"] < self.debounce
                          or (cache["version"] == version and now - cache["at"] < self.screen_ttl)):
                return dict(cache["value"])
        result = json.loads(self.run("read", address, "--json", timeout=timeout))
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise OSError("hc_screen_unavailable")
        value = dict(text=result["text"], width=width, height=height, cursor=None)
        with self.lock:
            self.screens[address] = dict(at=time.monotonic(), version=version, value=value)
        return dict(value)
