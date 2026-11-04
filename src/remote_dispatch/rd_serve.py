"""Local Unix socket HTTP adapter. Public responses contain display fields only."""
from contextlib import nullcontext
import configparser
import http.client
import json
import math
import re
import subprocess
import sys
import os
from pathlib import Path
import socket
import socketserver
import signal
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from . import cli
from .rd_lock import JobBusy, JobLock
from . import rd_events as events
from . import rd_board as board
from . import rd_messages as messages
from . import rd_serve_network as network
from .rd_query import Query, result_path
from .rd_serve_writes import Writes
from .rd_serve_host_config import HostConfigRoutes
from .rd_serve_projects import ProjectRoutes
from . import rd_harness_stats
from .rd_serve_sessions import Sessions
from .rd_serve_logs import LogSnapshots
from .rd_serve_progress import ProgressHistory
from .rd_serve_poll import Collector
from .rd_serve_readonly import CachedReads
from .rd_serve_history import instant
from .rd_host_metrics import host_metrics
from .rd_reports import Reports, read_reports
from .rd_serve_cache import LocalFiles
from .rd_reclaim_schedule import ScheduledReclaim
from . import rd_reclaim_plan as reclaim_plan


# 只允许 serve 子进程执行、不让 CLI 自动转发给 serve 的命令（suggest 仍由 CLI 直接运行）。
FORWARD_ONLY = {"suggest"}
RECLAIM_JOIN_SECONDS = 5
# serve 在线且缓存可用时，直接由 serve 进程内生成输出的只读命令（见 rd_serve_readonly）。
CACHED_READS = {"inbox", "status"}
WRITE_COMMANDS = {"session-default", "run", "resume", "ack", "review", "stop", "project", "inbox", "keep", "clean", "plan"}
SOCKET_NAME = "serve.sock"
VERSION_ROUTE = "/v1/version"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
# /v1 合同版本：仅在接口不兼容变化或 App 依赖的新增字段出现时递增。
CONTRACT_VERSION = 1
# 配置检查间隔：默认每秒一次；测试或高频采集可用更小的 --poll-interval。
CONFIG_RELOAD_INTERVAL = 1.0
# 错误行可能很长；限制进入版本接口与 /v1/hosts 的文本长度。
CONFIG_RELOAD_ERROR_LIMIT = 1000


# 常见裸令牌（无 key=value 标签也要遮盖）：OpenAI/Anthropic 风格 sk-、GitHub ghp_/github_pat_、Bearer、Slack xox*-、AWS AKIA。
# 强前缀令牌宁可多遮：只排除前一字符为 ASCII 字母数字或下划线（URL 用户名、路径、扩展名、中文紧邻都遮）；
# 强前缀只排除前接字母数字（含 _ 不排除，KEY_sk-… 也遮）；sk-ant-/sk-proj- 另排除「字母数字+-」，避免命中派单 ID。通用 sk/pk/rk- 是弱形态：须含数字、≥20 位，前一字符还不得是 / . -。
TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}(?![0-9A-Z]))"
    r"|(?<![A-Za-z0-9])(?<![A-Za-z0-9]-)sk-(?:ant-|proj-)[A-Za-z0-9_\-]{16,}"
    r"|(?<![A-Za-z0-9_/.\-])(?:sk|pk|rk)-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"
    r"|(?i:(?<![A-Za-z0-9_])Bearer\s+)[A-Za-z0-9._~+/=\-]{8,}")

# 结构化标识键：值用于路由与关联（派单 ID、分支、主机、路径等），不做文本脱敏；列表元素继承该语义。
# 路径（cwd、repo、worker_root）不在此列：可能含配置密钥路径，令牌规则不会命中路径片段。
# 自由文本（last、summary、phase、text、reply、note、message、error、日志与终端帧等）不在此列。
IDENTIFIER_KEYS = frozenset({
    "job", "jobs", "last_job", "running_job_ids", "job_id", "job_ids", "branch", "source_branch", "shared_branch", "base", "head",
    "upstream", "project", "project_id", "projects", "host", "hosts", "initiator_host", "user_id", "round", "rounds",
    "last_round", "current_rounds", "receipt_id", "report_id", "message_id", "initiator_session", "receiver_session",
    "session", "session_id", "pane_id", "workspace_id", "device_id", "id", "seq", "type", "kind", "state", "execution_state",
    "review_state", "delivery_state", "status", "created", "created_at", "updated_at", "snapshot_at", "captured_at",
    "started_at", "stopped_at", "finished", "reviewed_at", "received_at", "delivered_at", "channels", "previous", "line", "name", "unit"})
MIN_SECRET_LENGTH = 8


def file_changes(receipt):
    """Project saved report statistics; a missing list is distinct from no changes."""
    report = receipt.get("report") or {}
    changes = report.get("changed_files", receipt.get("changed_files"))
    if not isinstance(changes, list):
        return [], False
    rows = []
    for item in changes:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]
                or any(key not in item or (item[key] is not None and
                       (type(item[key]) is not int or item[key] < 0)) for key in ("additions", "deletions"))):
            return [], False
        rows.append({key: item[key] for key in ("path", "additions", "deletions")})
    return rows, True


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


class ServeUnavailable(Exception):
    """Nothing was sent: the serve socket is absent, or the connection failed.

    `absent` is true when serve is plainly not running (no socket, refused);
    other connect errors leave serve's state unknown.
    """

    def __init__(self, message, absent=True):
        super().__init__(message)
        self.absent = absent


class ServeFailed(Exception):
    """The request was sent, but serve returned no complete response."""


def serve_socket_path():
    return os.environ.get("RD_SERVE_SOCKET") or Path(cli.STATE) / SOCKET_NAME


def serve_call(method, path, body=None, *, headers=None, timeout=None, socket_path=None):
    """One request to the local serve socket; returns (status, parsed JSON).

    ServeUnavailable means nothing was sent (callers may fall back); ServeFailed
    means the request may have been processed. `body` is bytes or a JSON value.
    """
    socket_path = socket_path or serve_socket_path()
    if not hasattr(socket, "AF_UNIX") or not Path(socket_path).exists():
        raise ServeUnavailable(f"serve socket 不可用（{socket_path}）")
    connection = UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        try:
            connection.connect()
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError):
            raise ServeUnavailable(f"serve socket 不可用（{socket_path}）") from None
        except OSError as error:
            raise ServeUnavailable(f"无法连接 serve socket（{socket_path}）：{error}", absent=False) from None
        headers = dict(headers or {})
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise ServeFailed(f"serve 请求失败（{path}）：{error}") from None
    finally:
        connection.close()


def admin_request(method, path, body=None, *, key=None, timeout=10):
    """Call serve's local admin API; device and pairing state never live in the CLI."""
    return serve_call(method, path, body, headers={"Idempotency-Key": key} if key else None, timeout=timeout)


def forward(args, argv):
    """Only absence/refusal before sending permits direct CLI fallback."""
    try:
        harness = cli.initiators.harness()
        request = dict(argv=argv, cwd=os.getcwd(), config=str(Path(cli.CONFIG).resolve()),
                       user_id=cli.require_user(), initiator_session=cli.initiator_session(args, harness),
                       origin=cli.initiators.origin(harness, ""),
                       stdin=sys.stdin.read() if getattr(args, "prompt_file", None) == "-" else "")
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("cli_request_too_large: forwarded request exceeds 8 MiB")
        status, reply = serve_call("POST", "/_cli/v1", body)
        if status != 200:
            raise ValueError("cli_forward_rejected: serve rejected the command; no direct retry")
        sys.stdout.write(reply["stdout"])
        sys.stderr.write(reply["stderr"])
        return reply["exit_code"]
    except ServeUnavailable as error:
        if error.absent:
            return None
        raise ValueError("cli_forward_failed: command may have run; check state before retrying") from error
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, ServeFailed) as error:
        raise ValueError("cli_forward_failed: command may have run; check state before retrying") from error


def cached_read_params(args):
    """CLI 已解析的参数里，缓存路径能原样复现的子集；带筛选、--details、--wait 等返回 None。"""
    if args.command == "inbox":
        if getattr(args, "wait", False) or getattr(args, "orphans", False):
            return None
        owner = cli.initiator_session(args)
        return dict(initiator_session=owner) if owner else None
    if args.command == "status":
        if (getattr(args, "unmerged", False) or getattr(args, "into", None) or getattr(args, "project", None)
                or cli.get_dir(args) or getattr(args, "details", False)):
            return None
        return dict(job=args.job, json=bool(args.json))
    return None


def cached_read(args):
    """serve 在线时由它用缓存生成只读命令输出；返回退出码，None 表示回退到原路径。

    只读且无副作用，所以任何失败（离线、请求失败、缓存不足）都回退，不报错。
    """
    params = cached_read_params(args)
    if params is None:
        return None
    try:
        status, reply = serve_call("POST", "/_cli/v1/read", dict(
            command=args.command, params=params, user_id=cli.require_user(),
            config=str(Path(cli.CONFIG).resolve())), timeout=5)
    except (ServeUnavailable, ServeFailed, OSError, ValueError):
        return None
    if status != 200 or not isinstance(reply, dict) or not reply.get("handled"):
        return None
    sys.stdout.write(reply["stdout"])
    sys.stderr.write(reply["stderr"])
    return reply["exit_code"]


def execute_cli():
    """Child owned by serve: isolate cwd, stdio and initiator context per command."""
    request = json.load(sys.stdin)
    os.environ["RD_LOCK_WAIT_SECONDS"] = "0"  # serve 写接口必须立即返回 409，不得阻塞写锁
    import io
    sys.stdin = io.StringIO(request.get("stdin", ""))
    cli.main(request["argv"], forwarded=request)


def run_service(config, socket_path, poll_interval):
    service = Service(config, socket_path, poll_interval=poll_interval)
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
    try:
        service.start()
        print(f"rdispatch: serving on {service.path}", flush=True)
        service.stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        signal.signal(signal.SIGTERM, previous)


class UnixServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    address_family = getattr(socket, "AF_UNIX", None)
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        self.list_slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def do_GET(self):
        try:
            if self.server.service.host_config.get(self):
                return
            if self.server.service.sessions.handle_get(self):
                return
            if urlsplit(self.path).path == "/v1/events":
                return self.events()
            # Bound completed list requests, not idle sockets or SSE lifetimes.
            slots = getattr(self.server, "list_slots", None)
            listing = urlsplit(self.path).path in ("/v1/projects", "/v1/jobs")
            with slots if listing and slots is not None else nullcontext():
                status, body = self.server.service.read(self.path, getattr(self, "session_device", None))
                self.json(status, self.server.service.redact(body))
        except (ValueError, TypeError):
            self.json(400, {"error": {"reason": "invalid_request"}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, subprocess.SubprocessError):
            self.json(503, {"error": {"reason": "read_unavailable"}})

    def do_PUT(self):
        if self.server.service.host_config.handle(self):
            return
        self.server.service.sessions.defaults.put(self)

    def do_DELETE(self):
        if not self.server.service.host_config.handle(self):
            self.json(404, {"error": {"reason": "not_found"}})

    def do_POST(self):
        try:
            if self.server.service.host_config.handle(self):
                return
            if (self.server.service.project_routes.post(self)
                    or self.server.service.sessions.handle_post(self)):
                return
            if self.path not in ("/_cli/v1", "/_cli/v1/read"):
                return self.server.service.writes.handle(self)
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise ValueError("invalid_request")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("invalid_request")
            request = json.loads(self.rfile.read(length))
            if self.path == "/_cli/v1/read":
                # 只读命令：用进程内缓存直接生成输出，不起子进程、不碰目标机。
                return self.json(200, self.server.service.cached_reads.handle(request))
            self.json(200, self.server.service.execute(request))
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.json(400, {"error": {"reason": "invalid_cli_request"}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, subprocess.SubprocessError):
            self.json(503, {"error": {"reason": "cli_unavailable"}})

    def events(self):
        service = self.server.service
        args = parse_qs(urlsplit(self.path).query)
        cursor = int(self.headers.get("Last-Event-ID", args.get("after", ["0"])[0]))
        if cursor < 0:
            raise ValueError("invalid_event_cursor")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": connected\n\n")
        oldest = events.EventLog(service.state).oldest_seq()
        if cursor and oldest > cursor + 1:
            # 游标早于仍保留的最早事件（历史已按上限清理）：注释行不是事件，旧客户端忽略；
            # 新客户端应据此重新拉取快照，再从 oldest_seq 继续。
            self.wfile.write(f": cursor_expired oldest_seq={oldest}\n\n".encode())
        self.wfile.flush()
        heartbeat = time.monotonic()
        reader = events.EventCursor(service.state)
        reader.seq = cursor
        try:
            while not service.stop.is_set() and network.stream_allowed(self):
                for row in reader.read():
                    cursor = row["seq"]
                    if not service.visible(row) or not service.sessions.event_allowed(self, row):
                        continue
                    body = service.redact(dict(row, data=events.display_data(row["type"], row["data"])))
                    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                    frame = f'id: {cursor}\nevent: {row["type"]}\ndata: {data}\n\n'
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                if time.monotonic() - heartbeat >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                service.stop.wait(0.1)
        except (OSError, ValueError):
            # A closed stream is resumed through its last delivered ID.
            return


class PollWake(threading.Event):
    """Wake both independent collectors for local writes."""
    def __init__(self):
        super().__init__()
        self.sessions = threading.Event()

    def set(self):
        self.sessions.set()
        super().set()


class Service:
    def __init__(self, config, socket_path=None, *, poll_interval=15, push_sender=None, state=None):
        self.config = config
        self.host_config = HostConfigRoutes(self)
        self.config_path = self.host_config.path
        self.config_stat_error = None
        try:
            self.config_stamp = self.config_file_stamp(self.config_path)
        except OSError:
            # Startup already read the config; a transient stat failure must not
            # turn into a traceback. The reload loop records the next failure.
            self.config_stamp = None
        self.state = Path(state or cli.STATE).resolve()  # 状态目录可显式传入，缺省仍取 cli.STATE
        self.user_id = cli.require_user()
        # 启动时已成功读完配置；后续自动重载在成功或失败后更新同一结构。
        self.config_reload = self.reload_status(config.loaded_at)
        self.path = Path(socket_path or os.environ.get("RD_SERVE_SOCKET")
                          or self.state / SOCKET_NAME).absolute()
        self.lock = JobLock(self.state, "serve")
        self.lock.path = self.state / "locks" / "serve" / "service.lock"
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("invalid_poll_interval: poll interval must be positive seconds")
        self.poll_interval = poll_interval
        self.host_failures = {}
        self.host_retry_at = {}
        self.host_connections = {}
        self.execute_timeout = network.positive_setting(config.cp, "execute_timeout", 120)
        self.stop = threading.Event()
        self.wake = PollWake()
        self.cache_lock = threading.Lock()
        self.local_files = LocalFiles(self.state)
        self.snapshot_version = 0
        self.writer = threading.Lock()
        self.job_guards = {}
        self.writes = Writes(self)
        self.snapshots = {}
        self.generations = {}
        self.progress = {}
        self.raw_rounds = {}
        self.restored_jobs = set()  # 来自磁盘、缺 last／network_sample 的快照，实采后移出
        self.complete_snapshots = set()  # 带完整采集字段（可供只读缓存逐字复现 CLI 输出）的快照
        self.restore_snapshots()
        self.host_last_seen = self.restore_host_last_seen()
        initial_hosts = self.config.hosts()
        self.hosts = [self.host_row(host) for host in initial_hosts]
        self.host_fingerprints = {host["name"]: self.host_fingerprint(host) for host in initial_hosts}
        self.network = network.Network(self, push_sender=push_sender)
        self.server = None
        self.thread = None
        self.poller = None
        self.session_poller = None
        self.config_poller = None
        self.reclaim = ScheduledReclaim(self)
        self.reclaim_poller = None
        self.project_routes = ProjectRoutes(self)
        self.sensitive = set()
        self.sessions = Sessions(self)
        self.reports = Reports(self)
        self.collector = Collector(self)
        self.cached_reads = CachedReads(self)
        self.log_snapshots = LogSnapshots(self)
        for host in self.config.hosts():
            for key, value in host.items():
                if value and (key in ("env_file", "ssh_opts", "ssh") or "auth_file" in key):
                    if value != "local":
                        self.sensitive.add(str(value))
        self.host_config.remember_secrets(config.cp)

    def start(self):
        try:
            self.lock.acquire()
        except JobBusy:
            raise ValueError("serve_already_running: a service already owns this state directory") from None
        try:
            if any(p.is_symlink() for p in (self.path, *self.path.parents)):
                raise ValueError("invalid_socket_path: socket path must not be a symbolic link")
            if UnixServer.address_family is None:
                raise ValueError("unix_socket_unavailable: Unix sockets are not supported")
            if self.path.exists():
                if not stat.S_ISSOCK(self.path.lstat().st_mode):
                    raise ValueError("socket_path_in_use: socket path already exists")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as check:
                    try:
                        check.connect(str(self.path))
                    except ConnectionRefusedError:
                        self.path.unlink()
                    else:
                        raise ValueError("socket_path_in_use: socket path already exists")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.server = UnixServer(str(self.path), network.handler_class(Handler), bind_and_activate=False)
            self.server.service = self
            self.server.server_bind()
            self.inode = self.path.stat().st_ino
            os.chmod(self.path, 0o600)
            self.server.server_activate()
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.network.start(network.handler_class(Handler))
            self.log_snapshots.start()
            self.session_poller = threading.Thread(target=self.poll_sessions, daemon=True)
            self.session_poller.start()
            self.config_poller = threading.Thread(target=self.poll_config, daemon=True)
            self.config_poller.start()
            self.poller = threading.Thread(target=self.poll, daemon=True)
            self.poller.start()
            self.reclaim_poller = threading.Thread(target=self.reclaim.loop, daemon=True)
            self.reclaim_poller.start()
            self.reports.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.config_poller is not None:
            self.config_poller.join()
            self.config_poller = None
        self.network.close()
        if self.session_poller is not None:
            self.session_poller.join()
            self.session_poller = None
        self.sessions.close()
        self.log_snapshots.close()
        if self.thread is not None:
            self.server.shutdown()
            self.thread.join()
            self.thread = None
        if self.server is not None:
            self.server.server_close()
            self.server = None
        if hasattr(self, "inode"):
            try:
                info = self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and info.st_ino == self.inode:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            del self.inode
        if self.poller is not None:
            self.poller.join()
            self.poller = None
        self.collector.close()
        if self.reclaim_poller is not None:
            # 回收线程可能卡在 ssh/du；它是守护线程，超时后放弃等待。
            self.reclaim_poller.join(timeout=RECLAIM_JOIN_SECONDS)
            self.reclaim_poller = None
        self.reports.close()
        # Drain an accepted CLI write before another service can own this state.
        with self.writes.lock, self.writer:
            self.lock.release()

    def job_guard(self, job):
        with self.cache_lock:
            return self.job_guards.setdefault(job, threading.RLock())

    def execute(self, request):
        if not isinstance(request, dict) or request.get("user_id") != self.user_id:
            raise ValueError("invalid_cli_user")
        if request.get("config") != str(Path(cli.CONFIG).resolve()):
            raise ValueError("cli_config_mismatch")
        if (not isinstance(request.get("argv"), list) or not request["argv"]
                or any(not isinstance(item, str) for item in request["argv"])):
            raise ValueError("invalid_cli_arguments")
        if not isinstance(request.get("cwd"), str) or not Path(request["cwd"]).is_absolute():
            raise ValueError("invalid_cli_directory")
        if not isinstance(request.get("stdin", ""), str) or not isinstance(request.get("origin"), dict):
            raise ValueError("invalid_cli_context")
        argv = request["argv"]
        job = next((argv[index + 1] for index, action in enumerate(argv[:-1])
                    if action in ("ack", "review", "stop", "resume", "keep")
                    and re.fullmatch(r"[A-Za-z0-9._-]+", argv[index + 1])), None)
        # Wait for this job's collector before occupying the global CLI writer.
        with self.job_guard(job) if job else nullcontext(), self.writer:
            if self.stop.is_set():
                raise ValueError("serve_stopping")
            env = dict(os.environ, RD_STATE=str(self.state), RD_CONFIG=str(Path(cli.CONFIG).resolve()),
                       RD_USER_ID=self.user_id,
                       PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            for name in ("RD_INITIATOR_SESSION", *filter(None, cli.initiators.SESSION_VARIABLES.values()),
                         "HERDR_SESSION", "HERDR_WORKSPACE_ID", "HERDR_PANE_ID"):
                env.pop(name, None)
            process = subprocess.run([sys.executable, "-c", "from remote_dispatch.rd_serve import execute_cli; execute_cli()"],
                                     input=json.dumps(request), cwd=request["cwd"], env=env,
                                     capture_output=True, text=True, timeout=self.execute_timeout)
            self.wake.set()
            return dict(exit_code=process.returncode, stdout=process.stdout, stderr=process.stderr)

    @staticmethod
    def is_sensitive_field(key):
        return bool(re.search(r"(?i)(?:env_?file|auth_?file|ssh|password|secret|token|private_?key|api_?key|credential|authorization)", key))

    @staticmethod
    def secret_values(sensitive):
        """Configured secrets long enough to replace without damaging ordinary text (shared by HTTP and terminal paths)."""
        return tuple(sorted((item for item in sensitive if len(item) >= MIN_SECRET_LENGTH), key=len, reverse=True))

    def redact(self, value, *, sensitive=None):
        """Project nested display data; scrub configured secrets and tokens in free text only."""
        if sensitive is None:
            sensitive = self.sensitive
        secrets = self.secret_values(sensitive)
        pattern = re.compile("|".join(map(re.escape, secrets))) if secrets else None
        return self.redact_value(value, pattern, False)

    def redact_value(self, value, pattern, identifier):
        if isinstance(value, dict):
            return {key: self.redact_value(item, pattern, key in IDENTIFIER_KEYS) for key, item in value.items()
                    if not self.is_sensitive_field(key)}
        if isinstance(value, list):
            return [self.redact_value(item, pattern, identifier) for item in value]
        if isinstance(value, str) and not identifier:
            if pattern is not None:
                value = pattern.sub("[redacted]", value)
            value = TOKEN_PATTERN.sub("[redacted]", value)
            value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                           "[redacted]", value, flags=re.S)
            value = re.sub(r"(?i)(?<![\w-])[\w-]*(?:auth_file|env_file|ssh(?:_opts)?|password|secret|token|api_key|private_?key)"
                           r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)", "[redacted]", value)
        return value

    def visible(self, event):
        data = event["data"]
        if data.get("user_id"):
            return data["user_id"] == self.user_id
        if data.get("job"):
            rec = messages.read_json(self.state / "jobs" / (messages.job_id(data["job"]) + ".json"))
            return rec is None or rec.get("user_id") in (None, "", self.user_id)
        return True

    def records(self):
        _, documents = self.local_files.snapshot("jobs")
        return [rec for path, rec in sorted(documents.items())
                if rec and rec.get("user_id") in (None, "", self.user_id)]

    @staticmethod
    def generation(rec):
        return rec["rounds"], rec["host"], rec.get("worker_root")

    def restore_snapshots(self):
        """A damaged derived cache must not prevent serving local records."""
        for path in (self.state / "serve-snapshots").glob("*.json"):
            try:
                saved = messages.read_json(path)
                if not isinstance(saved, dict) or saved.get("user_id") != self.user_id:
                    continue
                generation = saved["generation"]
                snapshot, progress = saved["snapshot"], saved.get("progress")
                if (not isinstance(generation, list) or len(generation) != 3
                        or type(generation[0]) is not int or generation[0] < 1
                        or not isinstance(generation[1], str)
                        or generation[2] is not None and not isinstance(generation[2], str)
                        or not isinstance(snapshot, dict) or not isinstance(snapshot.get("state"), str)
                        or int(snapshot["round"]) != generation[0]
                        or instant(snapshot.get("snapshot_at")) is None
                        or progress is not None and not isinstance(progress, dict)):
                    continue
                self.snapshots[path.stem] = snapshot
                self.generations[path.stem] = tuple(generation)
                if "raw_round" in saved:
                    self.raw_rounds[path.stem] = saved["raw_round"]
                    self.restored_jobs.add(path.stem)  # 落盘时去掉了 last 与 network_sample
                    if snapshot["state"] not in ("done", "dead", "missing"):
                        self.complete_snapshots.add(path.stem)
                    # 终态快照缺 last／network_sample：不记为完整，按旧格式补采一次（连不上时保留原快照）。
                if progress:
                    self.progress[(path.stem, generation[0])] = progress
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def restore_host_last_seen(self):
        try:
            saved = messages.read_json(self.state / "serve-host-last-seen.json")
        except (OSError, ValueError):
            return {}
        if not isinstance(saved, dict):
            return {}
        return {name: instant(value).isoformat() for name, value in saved.items()
                if instant(value) is not None}

    def query(self, documents=None):
        read = messages.read_json if documents is None else documents.get
        with self.cache_lock:
            snapshots, progress = dict(self.snapshots), dict(self.progress)
            generations = dict(self.generations)
        def states(records):
            rows = {}
            for rec in records:
                if generations.get(rec["job"]) == self.generation(rec):
                    rows[rec["job"]] = snapshots[rec["job"]]
                    continue
                receipt = read(self.state / "results" / rec["job"] / f'r{rec["rounds"]}.json') or {}
                rows[rec["job"]] = dict(
                    state=receipt.get("execution_state") or rec.get("execution_state") or
                          ("dead" if rec.get("dispatch_state") == "preparation_failed" else "starting"),
                    round=str(rec["rounds"]), snapshot_at=receipt.get("captured_at"))
            return rows
        return Query(self.state, states=states, read_json=read,
                     report=lambda rec, number, kind: progress.get((rec["job"], number))
                     if kind == "progress" and generations.get(rec["job"]) == self.generation(rec) else None,
                     receiver=lambda rec: rec.get("receiver_session"),
                     project_names=[section.split(":", 1)[1] for section in self.config.cp.sections()
                                    if section.startswith("project:")])

    def job_refresh_complete(self, rec):
        with self.cache_lock:
            # 老格式（升级前落盘）的快照没有完整采集字段：补采一次后才算完成，之后降频。
            if (self.generations.get(rec["job"]) != self.generation(rec)
                    or rec["job"] not in self.complete_snapshots
                    or self.snapshots.get(rec["job"], {}).get("state") not in ("done", "dead", "missing")):
                return False
        query = self.query()
        receipt = messages.read_json(query.result_path(rec, rec["rounds"]))
        return bool(receipt and receipt.get("execution_state") in ("done", "dead", "missing")
                    and receipt.get("round") == rec["rounds"] and receipt.get("host") == rec["host"]
                    and query.acknowledgement(rec, receipt))

    @staticmethod
    def config_file_stamp(path):
        """Cheap change detector; a missing file is a valid empty configuration."""
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino, info.st_mode)

    @staticmethod
    def reload_status(at, error=None):
        return dict(ok=error is None, at=at, error=error)

    @staticmethod
    def host_fingerprint(host):
        return tuple(sorted(host.items()))

    def host_row(self, host, *, reachable=False, full=False, probed=False, metrics=None, **values):
        return dict(name=host["name"], local=cli.is_local(host), reachable=reachable,
                    last_seen_at=self.host_last_seen.get(host["name"]),
                    full=full, probed=probed,
                    metrics=metrics if metrics is not None else host_metrics(host, {}),
                    **{key: values.get(key) for key in ("load1", "ncpu", "cpu_pct", "memfree",
                                                        "disk", "disk_total", "running", "heavy")})

    def record_reload(self, error):
        raw = str(error).strip() or error.__class__.__name__
        # Parser errors quote the offending line, so every reload error goes
        # through the same scrubber as other free text.
        text = self.redact(raw).strip()
        if len(text) > CONFIG_RELOAD_ERROR_LIMIT:
            text = text[:CONFIG_RELOAD_ERROR_LIMIT] + "…"
        self.config_reload = self.reload_status(messages.utc_now(), text)
        print("rdispatch: config_reload_failed: " + text, file=sys.stderr)

    def reload_config(self):
        try:
            return self._reload_config()
        except OSError as error:
            # A stat/read race at any point must not kill the watcher thread.
            self.record_reload(error)
            return False

    def _reload_config(self):
        path = self.config_path
        try:
            stamp = self.config_file_stamp(path)
        except OSError as error:
            signature = (error.errno, str(error))
            if signature != self.config_stat_error:
                self.config_stat_error = signature
                self.record_reload(error)
            return False
        self.config_stat_error = None
        if stamp == self.config_stamp:
            return False
        try:
            config = cli.Config(strict=True)
            hosts = config.hosts()
            # 启动时一台主机写坏不拖垮 serve；但运行中的主机在重载时被写坏，保留旧定义继续监控其在途任务，
            # 并报错（已知的无效主机不阻塞其他修改）。
            known = self.config.host_errors()
            fresh = [text for name, text in config.host_errors().items() if known.get(name) != text]
            if fresh:
                raise ValueError("; ".join(fresh))
            execute_timeout = network.positive_setting(config.cp, "execute_timeout", 120)
            config.cp.getboolean("serve", "session_access", fallback=True)
            config.notify_channels()
            report_window = config.cp.getfloat("monitor", "report_merge_window_seconds", fallback=5)
            if not math.isfinite(report_window) or report_window < 0:
                raise ValueError("汇报合并窗口必须为非负秒数。")
        except (cli.ConfigError, configparser.Error, UnicodeError, ValueError, OSError) as error:
            self.config_stamp = stamp
            self.record_reload(error)
            return False
        # An editor save or API write may race the read. Keep the old object and
        # try again on the next tick rather than publish a torn configuration.
        if self.config_file_stamp(path) != stamp:
            return False
        with self.writer:
            if self.stop.is_set() or self.config_file_stamp(path) != stamp:
                return False
            fingerprints = {host["name"]: self.host_fingerprint(host) for host in hosts}
            # Only the validation pass needs strict errors; the live object keeps
            # the CLI's existing behavior for callers that predate reload.
            config.strict = False
            self.config = config
            self.execute_timeout = execute_timeout
            self.reports.window = report_window
            self.host_config.remember_secrets(config.cp)
            with self.cache_lock:
                # Read the published rows under the lock so a concurrent poll cannot
                # hand us stale probe rows to carry over.
                previous = {row["name"]: row for row in self.hosts}
                previous_fingerprints = self.host_fingerprints
                self.hosts = [previous.get(host["name"])
                              if previous_fingerprints.get(host["name"]) == fingerprints[host["name"]]
                              else self.host_row(host)
                              for host in hosts]
                self.host_fingerprints = fingerprints
            self.config_stamp = stamp
            self.config_reload = self.reload_status(config.loaded_at)
            self.wake.set()
        return True

    def poll_config(self):
        while not self.stop.is_set():
            self.stop.wait(min(CONFIG_RELOAD_INTERVAL, self.poll_interval))
            if self.stop.is_set():
                return
            self.reload_config()

    def host_poll_due(self, host):
        # A connection edit is new evidence: do not retain the old circuit.
        connection = tuple(sorted(host.items()))
        name = host["name"]
        if self.host_connections.get(name) != connection:
            self.host_connections[name] = connection
            self.host_failures.pop(name, None)
            self.host_retry_at.pop(name, None)
        return time.monotonic() >= self.host_retry_at.get(name, 0)

    def host_poll_failed(self, name):
        failures = self.host_failures.get(name, 0) + 1
        self.host_failures[name] = failures
        # Policy in existing poll cycles, not a new guessed transport timeout.
        # After three failures the circuit opens; a due probe is half-open.
        self.host_retry_at[name] = time.monotonic() + self.poll_interval * 2 ** min(failures - 1, 4)

    def host_poll_recovered(self, name):
        self.host_failures.pop(name, None)
        self.host_retry_at.pop(name, None)

    def poll_sessions(self):
        while not self.stop.is_set():
            self.wake.sessions.clear()
            self.sessions.collect()
            self.wake.sessions.wait(self.poll_interval)

    def poll(self):
        log = events.EventLog(self.state)
        try:
            log.warm()
        except (OSError, ValueError):
            pass
        while not self.stop.is_set():
            self.wake.clear()
            try:
                log.warm()  # 索引在写失败或历史清理后被重置，这里按需重建
            except (OSError, ValueError):
                pass
            try:
                # 按目标机分工作单元并发采集；阶段错误聚合记录，见 rd_serve_poll。
                self.collector.cycle(log)
                self.collector.errors.ok("poll", "cycle")
            except Exception as error:  # 一轮的意外缺陷不得终止后台轮询
                self.collector.errors.failed("poll", "cycle", error)
            try:
                rd_harness_stats.observe(self)
                self.project_routes.observe()
                self.project_routes.prewarm()
            except (ValueError, OSError):
                print("rdispatch: poll_unavailable: cannot update harness statistics", file=sys.stderr)
            self.reports.ready.set()
            self.wake.wait(self.poll_interval)

    def card(self, rec, query):
        detail = query.details(rec, user_id=self.user_id)
        receipt = detail["result"] or {}
        report = receipt.get("report") or {}
        progress = detail["progress"] or {}
        execution = detail["execution"]
        project = query.jobs([rec], {}, user_id=self.user_id)[0]
        card = {key: rec.get(key) for key in ("job", "host", "model", "initiator_harness", "initiator_host", "initiator_session", "initiator_herdr")}
        card.update(project=project["project"], project_id=project["project_id"], harness=rec.get("agent"),
                    round=rec["rounds"], execution_state=execution.get("state", "starting"),
                    executor_exit=receipt.get("executor_exit"), outcome=report.get("outcome"), checks=report.get("checks"),
                    delivery_state=detail["delivery_state"], review_state=detail["review_state"],
                    acknowledgement=detail["acknowledgement"], stopped_by=receipt.get("stopped_by"),
                    snapshot_at=execution.get("snapshot_at"),
                    updated_at=report.get("updated_at") or progress.get("updated_at") or rec.get("created"))
        return card

    def receipt(self, rec, number, query):
        if number < rec["rounds"] and messages.read_json(result_path(self.state, rec, number)) is None:
            return None
        receipt = query.result(rec, number)
        if receipt is None:
            return None
        fields = "schema job round host initiator_session execution_state executor_exit receiver_session head dirty needs_initiator_review receipt_id captured_at stopped_by stopped_at suppress_push review_state reviewed_by reviewed_at review_note"
        row = {key: receipt.get(key) for key in fields.split()}
        report = receipt.get("report")
        row["changed_files"], row["changed_files_available"] = file_changes(receipt)
        row["report"] = {key: report[key] for key in ("schema", "job", "round", "kind", "phase", "summary", "outcome", "checks", "artifacts", "evidence", "updated_at", "started_at") if key in report} if report else None
        row.update(acknowledgement=query.acknowledgement(rec, receipt), delivery_state=query.delivery(rec, receipt))
        return row

    def logs(self, rec, number, stream, tail):
        host = cli.job_host(self.config, rec)
        name = f"{'stdout' if stream == 'out' else 'stderr'}.r{number}.log"
        if host["platform"] == "windows":
            return cli.native.request(host, "logs", {"job": rec["job"], "file": name, "lines": tail})
        directory = f'"$ROOT/jobs/{rec["job"]}"'
        path = f'"$ROOT/jobs/{rec["job"]}/{name}"'
        script = f'if [ -L {directory} ] || [ -L {path} ]; then exit 4; fi; if [ -f {path} ]; then tail -n {tail} {path}; fi'
        result = cli.ssh(host, script, check=False, timeout=30)
        if result is None or result.returncode:
            raise OSError("log_unavailable")
        return result.stdout

    def read(self, target, device=None):
        session_response = self.sessions.read(target, device)
        if session_response is not None:
            return session_response
        url = urlsplit(target)
        path = unquote(url.path)
        args = parse_qs(url.query)
        if path == VERSION_ROUTE:
            return 200, dict(package_version=__version__, contract_version=CONTRACT_VERSION,
                             config_reload=dict(self.config_reload))
        if path == "/v1/reports":
            return 200, {"reports": read_reports(self.state, self.user_id, args.get("initiator_session", [None])[0])}
        extra = self.project_routes.read(path, args, device)
        if extra is not None:
            return extra
        query = self.query()
        if path == "/v1/hosts":
            with self.cache_lock:
                return 200, {"hosts": list(self.hosts),
                             "config_errors": [dict(name=name, error=self.redact(text))
                                               for name, text in self.config.host_errors().items()],
                             "config_reload": dict(self.config_reload)}
        if path == "/v1/reclaim":
            # 定时回收状态：从未运行时 reclaim 为 null；只读状态文件，不连目标机。
            data = reclaim_plan.read(self.state, self.user_id)
            return 200, {"enabled": self.config.scheduled_reclaim(),
                         "reclaim": reclaim_plan.view(data, time.time())}
        records = self.records()
        if path == "/v1/jobs":
            return 200, {"jobs": [self.card(rec, query) for rec in records]}
        if path == "/v1/board":
            # 与 CLI board --json 同一结构；ADR 0004：本人派单不要求原发起会话。
            everything = args.get("all", [""])[0] in ("1", "true")
            owner = None if everything else args.get("initiator_session", [None])[0]
            if not everything and not owner:
                raise ValueError("invalid_board_scope")
            own = [rec for rec in records if rec.get("user_id") == self.user_id
                   and (everything or rec.get("initiator_session") == owner)]
            return 200, board.build(self.state, query, own, query.states(own) if own else {}, user_id=self.user_id,
                                    scope="all" if everything else "session", initiator_session=owner,
                                    stale_days=self.config.line_stale_days())
        match = re.fullmatch(r"/v1/jobs/([^/]+)(?:/rounds/([1-9][0-9]*)/(receipt|logs|progress))?", path)
        if match:
            job, number, kind = match.groups()
            messages.job_id(job)
            rec = next((rec for rec in records if rec["job"] == job), None)
            if rec is None:
                return 404, {"error": {"reason": "not_found"}}
            if kind:
                number = int(number)
                if number > rec["rounds"]:
                    return 404, {"error": {"reason": "not_found"}}
                if kind == "progress":
                    return 200, {"progress": ProgressHistory(self).read(rec, number)}
                if kind == "receipt":
                    receipt = self.receipt(rec, number, query)
                    return (200, {"receipt": receipt}) if receipt else (404, {"error": {"reason": "receipt_unavailable"}})
                stream = args.get("stream", ["out"])[0]
                tail = int(args.get("tail", ["40"])[0])
                if stream not in ("out", "err") or not 1 <= tail <= 10000:
                    raise ValueError("invalid_log_options")
                return self.log_snapshots.read(rec, number, stream, tail)
            rounds = []
            progress_events = {}
            for event in events.EventLog(self.state).read_job(job):
                if self.visible(event):
                    row = dict(event, data=events.display_data(event["type"], event["data"]))
                    progress_events.setdefault(row["data"].get("round"), []).append(row)
            for history in progress_events.values():
                history.sort(key=lambda row: (instant(row["data"].get("updated_at") or row["time"]).timestamp()
                                               if instant(row["data"].get("updated_at") or row["time"]) else 0,
                                               row["seq"]))
            for number in range(1, rec["rounds"] + 1):
                try:
                    receipt = self.receipt(rec, number, query)
                except ValueError:
                    receipt = None
                snapshot = query.states([rec]).get(job, {}) if number == rec["rounds"] else {}
                progress = query.report(rec, number, "progress")
                prompt = rec.get("round_prompts", {}).get(str(number))
                if number > 1:
                    previous_review = query.review(rec, number - 1)
                    if previous_review["review_state"] == "rejected":
                        prompt = previous_review.get("review_note")
                rounds.append(dict(round=number, receipt=receipt, review=query.review(rec, number),
                                   prompt=prompt,
                                   started_at=((receipt or {}).get("report") or {}).get("started_at") or snapshot.get("started_at") or None,
                                   progress_events=progress_events.get(number, []),
                                   execution_state=snapshot.get("state") or (receipt or {}).get("execution_state"),
                                   snapshot_at=snapshot.get("snapshot_at") or (receipt or {}).get("captured_at"),
                                   progress={key: progress[key] for key in ("schema", "job", "round", "kind", "phase", "summary", "artifacts", "updated_at")
                                             if key in progress} if progress else None))
            parameters = {key: rec.get(key) for key in ("branch", "base", "source_branch", "model", "effort", "permission_mode", "heavy", "sync")}
            return 200, dict(job=self.card(rec, query), parameters=parameters,
                             prompt=rec.get("round_prompts", {}).get("1"), rounds=rounds)
        return 404, {"error": {"reason": "not_found"}}
