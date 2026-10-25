"""Local Unix socket HTTP adapter. Public responses contain display fields only."""
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
from .rd_query import Query, result_path
from .rd_serve_projects import ProjectRoutes
from . import rd_harness_stats


WRITE_COMMANDS = {"run", "resume", "ack", "review", "stop", "project", "inbox", "keep", "clean"}
MAX_REQUEST_BYTES = 8 * 1024 * 1024


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
            if urlsplit(self.path).path == "/v1/events":
                return self.events()
            status, body = self.server.service.read(self.path)
            self.json(status, self.server.service.redact(body))
        except (ValueError, TypeError):
            self.json(400, {"error": {"reason": "invalid_request"}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, subprocess.SubprocessError):
            self.json(503, {"error": {"reason": "read_unavailable"}})

    def do_POST(self):
        try:
            if self.server.service.project_routes.post(self):
                return
            if self.path != "/_cli/v1":
                return self.json(404, {"error": {"reason": "not_found"}})
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
        try:
            while not service.stop.is_set():
                for row in events.read_after(service.state, cursor):
                    cursor = row["seq"]
                    if not service.visible(row):
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


class Service:
    def __init__(self, config, socket_path=None, *, poll_interval=15):
        self.config = config
        self.state = Path(cli.STATE).resolve()
        self.user_id = cli.require_user()
        self.path = Path(socket_path or os.environ.get("RD_SERVE_SOCKET") or self.state / "serve.sock").absolute()
        self.lock = JobLock(self.state, "serve")
        self.lock.path = self.state / "locks" / "serve" / "service.lock"
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("invalid_poll_interval: poll interval must be positive seconds")
        self.poll_interval = poll_interval
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.cache_lock = threading.Lock()
        self.writer = threading.Lock()
        self.snapshots = {}
        self.generations = {}
        self.progress = {}
        self.hosts = []
        self.server = None
        self.thread = None
        self.poller = None
        self.project_routes = ProjectRoutes(self)
        self.sensitive = set()
        for host in self.config.hosts():
            for key, value in host.items():
                if value and (key in ("env_file", "ssh_opts", "ssh") or "auth_file" in key):
                    if value != "local":
                        self.sensitive.add(str(value))

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
            self.server = UnixServer(str(self.path), Handler, bind_and_activate=False)
            self.server.service = self
            self.server.server_bind()
            self.inode = self.path.stat().st_ino
            os.chmod(self.path, 0o600)
            self.server.server_activate()
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.poller = threading.Thread(target=self.poll, daemon=True)
            self.poller.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stop.set()
        self.wake.set()
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
        # Drain an accepted CLI write before another service can own this state.
        with self.writer:
            self.lock.release()

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
        with self.writer:
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
                                     capture_output=True, text=True)
            self.wake.set()
            return dict(exit_code=process.returncode, stdout=process.stdout, stderr=process.stderr)

    def redact(self, value):
        """Project nested display data and scrub configured secrets in free text."""
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()
                    if not re.search(r"(?i)(?:env_?file|auth_?file|ssh|password|secret|token|private_?key|api_?key|credential|authorization)", key)}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self.sensitive, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                           "[redacted]", value, flags=re.S)
            value = re.sub(r"(?i)(?:[\w-]*auth_file|env_file|ssh(?:_opts)?|password|secret|token|api_key)"
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
        return [rec for rec in cli.all_jobs() if rec.get("user_id") in (None, "", self.user_id)]

    @staticmethod
    def generation(rec):
        return rec["rounds"], rec["host"], rec.get("worker_root")

    def query(self):
        with self.cache_lock:
            snapshots, progress = dict(self.snapshots), dict(self.progress)
            generations = dict(self.generations)
        def states(records):
            return {rec["job"]: snapshots.get(rec["job"], {})
                    if generations.get(rec["job"]) == self.generation(rec)
                    else {"state": "starting", "round": str(rec["rounds"])} for rec in records}
        return Query(self.state, states=states,
                     report=lambda rec, number, kind: progress.get((rec["job"], number)) if kind == "progress" else None,
                     receiver=lambda rec: rec.get("receiver_session"),
                     project_names=[section.split(":", 1)[1] for section in self.config.cp.sections()
                                    if section.startswith("project:")])

    def poll(self):
        log = events.EventLog(self.state)
        while not self.stop.is_set():
            self.wake.clear()
            try:
                records = self.records()
            except (ValueError, OSError, SystemExit):
                records = []
                print("rdispatch: poll_unavailable: cannot read jobs", file=sys.stderr)
            for saved in records:
                if self.stop.is_set():
                    break
                try:
                    # Lock before reloading: a resume must not mix old and new round data.
                    with self.writer, JobLock(self.state, saved["job"]):
                        rec = cli.load_job(saved["job"])
                        snapshot = cli.job_states(self.config, [rec]).get(rec["job"], {"state": "unreachable"})
                        number = int(snapshot.get("round") or 0) or rec["rounds"]
                        progress = cli.remote_report(cli.job_host(self.config, rec), rec, number, "progress") if rec.get("handoff_version") and snapshot.get("state") in ("running", "done") else None
                        log.observe_job(rec, snapshot, progress)
                        cli.collect_result(self.config, rec, snapshot)
                        with self.cache_lock:
                            self.snapshots[rec["job"]] = snapshot
                            self.generations[rec["job"]] = self.generation(rec)
                            self.progress[(rec["job"], number)] = progress
                except JobBusy:
                    continue
                except (ValueError, OSError, SystemExit, subprocess.SubprocessError):
                    print("rdispatch: poll_unavailable: cannot collect a job", file=sys.stderr)
            try:
                rd_harness_stats.observe(self)
            except (ValueError, OSError):
                print("rdispatch: poll_unavailable: cannot update harness statistics", file=sys.stderr)
            hosts = []
            for host in self.config.hosts():
                if self.stop.is_set():
                    break
                try:
                    info = cli.probe(host)
                    full = bool(info.get("reachable") and cli.assess(host, info))
                    log.observe_host(host, info, full=full)
                    hosts.append(dict(name=host["name"], local=cli.is_local(host),
                                      reachable=bool(info.get("reachable")), full=full,
                                      **{key: info.get(key) for key in ("load1", "ncpu", "memfree", "disk", "running", "heavy")}))
                except (ValueError, OSError, SystemExit, subprocess.SubprocessError):
                    hosts.append(dict(name=host["name"], local=cli.is_local(host), reachable=False, full=False,
                                      **{key: None for key in ("load1", "ncpu", "memfree", "disk", "running", "heavy")}))
                    print("rdispatch: poll_unavailable: cannot probe a host", file=sys.stderr)
            with self.cache_lock:
                self.hosts = hosts
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
        row["report"] = {key: report[key] for key in ("schema", "job", "round", "kind", "phase", "summary", "outcome", "checks", "artifacts", "evidence", "updated_at") if key in report} if report else None
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

    def read(self, target):
        url = urlsplit(target)
        path = unquote(url.path)
        args = parse_qs(url.query)
        extra = self.project_routes.read(path, args)
        if extra is not None:
            return extra
        query = self.query()
        if path == "/v1/hosts":
            with self.cache_lock:
                return 200, {"hosts": list(self.hosts)}
        records = self.records()
        if path == "/v1/jobs":
            return 200, {"jobs": [self.card(rec, query) for rec in records]}
        match = re.fullmatch(r"/v1/jobs/([^/]+)(?:/rounds/([1-9][0-9]*)/(receipt|logs))?", path)
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
                if kind == "receipt":
                    receipt = self.receipt(rec, number, query)
                    return (200, {"receipt": receipt}) if receipt else (404, {"error": {"reason": "receipt_unavailable"}})
                stream = args.get("stream", ["out"])[0]
                tail = int(args.get("tail", ["40"])[0])
                if stream not in ("out", "err") or not 1 <= tail <= 10000:
                    raise ValueError("invalid_log_options")
                return 200, dict(job=job, round=number, stream=stream, text=self.logs(rec, number, stream, tail))
            rounds = []
            for number in range(1, rec["rounds"] + 1):
                try:
                    receipt = self.receipt(rec, number, query)
                except ValueError:
                    receipt = None
                snapshot = query.states([rec]).get(job, {}) if number == rec["rounds"] else {}
                progress = query.report(rec, number, "progress")
                rounds.append(dict(round=number, receipt=receipt, review=query.review(rec, number),
                                   execution_state=snapshot.get("state") or (receipt or {}).get("execution_state"),
                                   progress={key: progress[key] for key in ("schema", "job", "round", "kind", "phase", "summary", "artifacts", "updated_at")
                                             if key in progress} if progress else None))
            parameters = {key: rec.get(key) for key in ("branch", "base", "source_branch", "model", "effort", "permission_mode", "heavy", "sync")}
            return 200, dict(job=self.card(rec, query), parameters=parameters, rounds=rounds)
        return 404, {"error": {"reason": "not_found"}}
