"""Local Unix socket HTTP adapter. Public responses contain display fields only."""
from contextlib import nullcontext
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

from . import cli
from .rd_lock import JobBusy, JobLock
from . import rd_events as events
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
from .rd_serve_history import instant
from .rd_host_metrics import host_metrics
from .rd_reports import Reports, read_reports
from .rd_serve_cache import LocalFiles


WRITE_COMMANDS = {"session-default", "run", "resume", "ack", "review", "stop", "project", "inbox", "keep", "clean"}
MAX_REQUEST_BYTES = 8 * 1024 * 1024


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
    def __init__(self, path):
        super().__init__("localhost")
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


def forward(args, argv):
    """Only absence/refusal before sending permits direct CLI fallback."""
    path = os.environ.get("RD_SERVE_SOCKET") or Path(cli.STATE) / "serve.sock"
    if not hasattr(socket, "AF_UNIX") or not Path(path).exists():
        return None
    connection = UnixHTTPConnection(path)
    try:
        try:
            connection.connect()
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        harness = cli.initiators.harness()
        request = dict(argv=argv, cwd=os.getcwd(), config=str(Path(cli.CONFIG).resolve()),
                       user_id=cli.require_user(), initiator_session=cli.initiator_session(args, harness),
                       origin=cli.initiators.origin(harness, ""),
                       stdin=sys.stdin.read() if getattr(args, "prompt_file", None) == "-" else "")
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("cli_request_too_large: forwarded request exceeds 8 MiB")
        connection.request("POST", "/_cli/v1", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        reply = json.loads(response.read())
        if response.status != 200:
            raise ValueError("cli_forward_rejected: serve rejected the command; no direct retry")
        sys.stdout.write(reply["stdout"])
        sys.stderr.write(reply["stderr"])
        return reply["exit_code"]
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
        raise ValueError("cli_forward_failed: command may have run; check state before retrying") from error
    finally:
        connection.close()


def execute_cli():
    """Child owned by serve: isolate cwd, stdio and initiator context per command."""
    request = json.load(sys.stdin)
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
            if self.path != "/_cli/v1":
                return self.server.service.writes.handle(self)
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise ValueError("invalid_request")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("invalid_request")
            request = json.loads(self.rfile.read(length))
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
    def __init__(self, config, socket_path=None, *, poll_interval=15, push_sender=None):
        self.config = config
        self.host_config = HostConfigRoutes(self)
        self.state = Path(cli.STATE).resolve()
        self.user_id = cli.require_user()
        self.path = Path(socket_path or os.environ.get("RD_SERVE_SOCKET") or self.state / "serve.sock").absolute()
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
        self.restore_snapshots()
        self.host_last_seen = self.restore_host_last_seen()
        self.hosts = [dict(name=host["name"], local=cli.is_local(host), reachable=False,
                           last_seen_at=self.host_last_seen.get(host["name"]),
                           full=False, probed=False, metrics=host_metrics(host, {}),
                           **{key: None for key in ("load1", "ncpu", "cpu_pct", "memfree", "disk", "disk_total", "running", "heavy")})
                      for host in self.config.hosts()]
        self.network = network.Network(self, push_sender=push_sender)
        self.server = None
        self.thread = None
        self.poller = None
        self.session_poller = None
        self.project_routes = ProjectRoutes(self)
        self.sensitive = set()
        self.sessions = Sessions(self)
        self.reports = Reports(self)
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
            self.poller = threading.Thread(target=self.poll, daemon=True)
            self.poller.start()
            self.reports.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stop.set()
        self.wake.set()
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

    def redact(self, value, *, sensitive=None):
        """Project nested display data and scrub configured secrets in free text."""
        if sensitive is None:
            sensitive = self.sensitive
        if isinstance(value, dict):
            return {key: self.redact(item, sensitive=sensitive) for key, item in value.items()
                    if not self.is_sensitive_field(key)}
        if isinstance(value, list):
            return [self.redact(item, sensitive=sensitive) for item in value]
        if isinstance(value, str):
            for secret in sorted(sensitive, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
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
            if (self.generations.get(rec["job"]) != self.generation(rec)
                    or self.snapshots.get(rec["job"], {}).get("state") not in ("done", "dead", "missing")):
                return False
        query = self.query()
        receipt = messages.read_json(query.result_path(rec, rec["rounds"]))
        return bool(receipt and receipt.get("execution_state") in ("done", "dead", "missing")
                    and receipt.get("round") == rec["rounds"] and receipt.get("host") == rec["host"]
                    and query.acknowledgement(rec, receipt))

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
        while not self.stop.is_set():
            self.wake.clear()
            hosts = []
            available, attempted = set(), set()
            enabled_hosts = self.config.hosts()
            enabled_names = {host["name"] for host in enabled_hosts}
            for host in enabled_hosts:
                if self.stop.is_set():
                    break
                if not self.host_poll_due(host):
                    hosts.extend(row for row in self.hosts if row["name"] == host["name"])
                    continue
                attempted.add(host["name"])
                try:
                    info = cli.probe(host)
                    if info.get("reachable"):
                        available.add(host["name"])
                        self.host_last_seen[host["name"]] = messages.utc_now()
                        try:
                            messages.atomic_json(self.state / "serve-host-last-seen.json", self.host_last_seen)
                        except (OSError, ValueError):
                            print("rdispatch: 无法保存目标机最后在线时间。", file=sys.stderr)
                    else:
                        self.host_poll_failed(host["name"])
                    full = bool(info.get("reachable") and cli.assess(host, info))
                    log.observe_host(host, info, full=full)
                    hosts.append(dict(name=host["name"], local=cli.is_local(host),
                                      reachable=bool(info.get("reachable")), full=full, probed=True,
                                      last_seen_at=self.host_last_seen.get(host["name"]),
                                      metrics=host_metrics(host, info),
                                      **{key: info.get(key) for key in ("load1", "ncpu", "cpu_pct", "memfree", "disk", "disk_total", "running", "heavy")}))
                except (ValueError, OSError, SystemExit, subprocess.SubprocessError):
                    self.host_poll_failed(host["name"])
                    hosts.append(dict(name=host["name"], local=cli.is_local(host), reachable=False, full=False, probed=True,
                                      last_seen_at=self.host_last_seen.get(host["name"]),
                                      metrics=host_metrics(host, {}),
                                      **{key: None for key in ("load1", "ncpu", "cpu_pct", "memfree", "disk", "disk_total", "running", "heavy")}))
                    print("rdispatch: poll_unavailable: cannot probe a host", file=sys.stderr)
            with self.cache_lock:
                self.hosts = hosts
            try:
                records = [rec for rec in self.records() if not self.job_refresh_complete(rec)]
            except (ValueError, OSError, SystemExit):
                records = []
                print("rdispatch: poll_unavailable: cannot read jobs", file=sys.stderr)
            # Remote collection is batched, outside per-job locks. Validate the
            # generation again under lock before publishing any returned data.
            states = {}
            by_host = {}
            for rec in records:
                by_host.setdefault(rec["host"], []).append(rec)
            for name in available - by_host.keys():
                self.host_poll_recovered(name)
            for name, group in by_host.items():
                if self.stop.is_set():
                    break
                if name in enabled_names and name not in available:
                    if name in attempted:
                        states.update({rec["job"]: {"state": "unreachable"} for rec in group})
                    continue
                try:
                    # Disabling new dispatches must not freeze existing jobs.
                    if (name not in enabled_names and self.config.cp.has_section("host:" + name)
                            and not self.host_poll_due(self.config.host(name))):
                        continue
                    batch = cli.job_states(self.config, group)
                    states.update({rec["job"]: batch.get(rec["job"], {"state": "unreachable"})
                                   for rec in group})
                    if any(batch.get(rec["job"], {}).get("state", "unreachable") == "unreachable" for rec in group):
                        self.host_poll_failed(name)
                    else:
                        self.host_poll_recovered(name)
                except (ValueError, OSError, SystemExit, subprocess.SubprocessError):
                    self.host_poll_failed(name)
                    states.update({rec["job"]: {"state": "unreachable"} for rec in group})
                    print("rdispatch: poll_unavailable: cannot collect host jobs", file=sys.stderr)
            for saved in records:
                if saved["job"] not in states:
                    continue
                if self.stop.is_set():
                    break
                try:
                    # Lock before reloading: a resume must not mix old and new round data.
                    with self.job_guard(saved["job"]), JobLock(self.state, saved["job"]):
                        rec = cli.load_job(saved["job"])
                        if self.generation(rec) != self.generation(saved):
                            continue
                        snapshot = states.get(rec["job"], {"state": "unreachable"})
                        number = int(snapshot.get("round") or 0) or rec["rounds"]
                        if number != rec["rounds"]:
                            # A delayed target may still expose the previous launch.
                            continue
                        snapshot = dict(snapshot, round=str(number), snapshot_at=messages.utc_now())
                        progress = cli.remote_report(cli.job_host(self.config, rec), rec, number, "progress") if rec.get("handoff_version") and snapshot.get("state") in ("running", "done") else None
                        log.observe_job(rec, snapshot)
                        history = ProgressHistory(self)
                        if progress is not None:
                            history.collect(rec, number, progress)
                        if rec.get("handoff_version") and snapshot.get("state") in ("running", "done"):
                            for prior in range(1, number):
                                saved_history = history.saved(rec, prior)
                                if saved_history is None or not saved_history.get("final"):
                                    source = dict(rec)
                                    if saved_history:
                                        _, source["host"], source["worker_root"] = saved_history["generation"]
                                        if source["worker_root"] is None:
                                            source.pop("worker_root")
                                        if source["host"] != rec["host"]:
                                            source.pop("worker_platform", None)
                                            source.pop("worker_python", None)
                                    history.collect(rec, prior, cli.remote_report(
                                        cli.job_host(self.config, source), source, prior, "progress"), final=True)
                        cli.collect_result(self.config, rec, snapshot)
                        # Persist only display data, so restart can serve the last
                        # observation while the first remote collection is pending.
                        saved_snapshot = {key: snapshot[key] for key in
                                          ("state", "round", "exit", "head", "dirty", "started_at", "snapshot_at")
                                          if key in snapshot}
                        saved_progress = {key: progress[key] for key in
                                          ("schema", "job", "round", "kind", "phase", "summary", "artifacts", "updated_at")
                                          if key in progress} if progress else None
                        messages.atomic_json(self.state / "serve-snapshots" / (rec["job"] + ".json"),
                            dict(user_id=self.user_id, generation=self.generation(rec),
                                 snapshot=self.redact(saved_snapshot), progress=self.redact(saved_progress)))
                        with self.cache_lock:
                            self.snapshots[rec["job"]] = snapshot
                            self.generations[rec["job"]] = self.generation(rec)
                            self.progress[(rec["job"], number)] = progress
                            self.snapshot_version += 1
                except JobBusy:
                    continue
                except (ValueError, OSError, SystemExit, subprocess.SubprocessError):
                    print("rdispatch: poll_unavailable: cannot collect a job", file=sys.stderr)
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
        if path == "/v1/reports":
            return 200, {"reports": read_reports(self.state, self.user_id, args.get("initiator_session", [None])[0])}
        extra = self.project_routes.read(path, args, device)
        if extra is not None:
            return extra
        query = self.query()
        if path == "/v1/hosts":
            with self.cache_lock:
                return 200, {"hosts": list(self.hosts)}
        records = self.records()
        if path == "/v1/jobs":
            return 200, {"jobs": [self.card(rec, query) for rec in records]}
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
            for event in events.read_after(self.state, 0):
                if (event["type"] == "progress.updated" and event["data"].get("job") == job
                        and self.visible(event)):
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
