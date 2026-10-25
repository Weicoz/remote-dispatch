"""Session routes kept separate from the shared serve routing module."""
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

from . import cli
from . import rd_messages as messages
from . import rd_events as events
from .rd_herdr import Herdr
from .rd_projects import project_id
from .rd_transcripts import convert_transcript
from .rd_lock import JobBusy, JobLock


HARNESSES = {"codex": "codex", "claude": "claude", "claude-code": "claude",
             "kimi": "kimi", "kimi-code": "kimi", "agy": "agy",
             "antigravity": "agy", "antigravity-cli": "agy"}
STATUS_TEXT = {"working": "工作中", "idle": "空闲", "blocked": "等待输入", "unknown": "状态未知"}
DISABLED = (403, {"error": {"reason": "session_access_disabled"}})


class Sessions:
    def __init__(self, service):
        self.service = service
        self.herdr = Herdr()
        self.enabled = service.config.cp.getboolean("serve", "session_access", fallback=True)
        self.observed = {}
        self.seen_messages = {}
        for event in events.read_after(service.state, 0):
            data = event["data"]
            if data.get("user_id") != service.user_id:
                continue
            if event["type"] == "session.changed":
                if data.get("removed"):
                    self.observed.pop(data["pane_id"], None)
                else:
                    self.observed[data["pane_id"]] = data
            elif event["type"] == "session.message":
                self.seen_messages.setdefault((data["pane_id"], data.get("session_id")), set()).add(data["message_id"])

    def collect(self):
        if not self.enabled:
            return
        try:
            items = self.discover()
            current, seen = {}, {pane: set(ids) for pane, ids in self.seen_messages.items()}
            with events.transaction():
                for item in items:
                    row = item["row"]
                    pane = row["pane_id"]
                    data = events.display_data("session.changed", dict(row, user_id=self.service.user_id, removed=False))
                    current[pane] = data
                    if self.observed.get(pane) != data:
                        events.emit(self.service.state, "session.changed", data)
                    for message in self.messages(item)["messages"]:
                        ids = seen.setdefault((pane, row["session_id"]), set())
                        if message["id"] not in ids:
                            events.emit(self.service.state, "session.message", dict(user_id=self.service.user_id,
                                pane_id=pane, session_id=row["session_id"], project_id=row["project_id"],
                                harness=row["harness"], message_id=message["id"], kind=message["kind"], message_time=message["time"]))
                            ids.add(message["id"])
                for pane in self.observed.keys() - current.keys():
                    events.emit(self.service.state, "session.changed", dict(self.observed[pane], removed=True))
            self.observed, self.seen_messages = current, seen
        except (OSError, ValueError, TypeError, KeyError):
            # A failed query is not evidence that panes have closed.
            return

    def allowed(self, device=None):
        """device is trusted authentication context, never request headers."""
        return self.enabled and (device is None or device.get("session_access") is True)

    def close(self):
        self.herdr.close()

    @staticmethod
    def is_session_path(path):
        return path == "/v1/sessions" or path.startswith("/v1/sessions/") or bool(
            re.fullmatch(r"/v1/jobs/[^/]+/session", path))

    def event_allowed(self, handler, event):
        return not event["type"].startswith("session.") or self.allowed(getattr(handler, "session_device", None))

    def list(self):
        return [item["row"] for item in self.discover()]

    def transcripts(self, harness):
        if harness == "codex":
            root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            paths = (root / "sessions").rglob("rollout-*.jsonl")
        elif harness == "claude":
            root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
            paths = (root / "projects").glob("*/*.jsonl")
        else:
            return []
        found = []
        for path in paths:
            try:
                if not path.resolve().is_relative_to(root.resolve()):
                    continue
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        record = json.loads(line)
                        meta = record.get("payload", {}) if record.get("type") == "session_meta" else record
                        identity = meta.get("id") if harness == "codex" else meta.get("sessionId")
                        if isinstance(identity, str) and identity and isinstance(meta.get("cwd"), str):
                            found.append(dict(path=path, id=identity, cwd=meta["cwd"], mtime=path.stat().st_mtime_ns))
                            break
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return found

    def locate(self, agent, saved, cwd, harness):
        reference = saved.get("agent_session") if saved.get("cwd") == (agent.get("cwd") or cwd) else None
        live = agent.get("agent_session")
        if (isinstance(live, dict) and HARNESSES.get(live.get("agent")) == harness
                and live.get("kind") in ("id", "path") and isinstance(live.get("value"), str) and live["value"]):
            # Saved snapshots are delayed. Do not route a replaced pane to its old conversation.
            reference = live
        paths = self.transcripts(harness)
        if isinstance(reference, dict) and HARNESSES.get(reference.get("agent")) == harness:
            kind, value = reference.get("kind"), reference.get("value")
            if isinstance(value, str) and value and kind in ("id", "path"):
                match = next((p for p in paths if (p["id"] == value if kind == "id"
                              else p["path"].resolve() == Path(value).resolve())), None)
                return value if kind == "id" else match["id"] if match else None, match, False
        candidates = [p for p in paths if Path(p["cwd"]).resolve() == Path(cwd).resolve()]
        match = max(candidates, key=lambda p: (p["mtime"], str(p["path"])), default=None)
        return (match["id"], match, True) if match else (None, None, False)

    def discover(self):
        rows = []
        latest = {}
        for event in events.read_after(self.service.state, 0):
            data = event["data"]
            if data.get("user_id") != self.service.user_id or event["type"] not in ("session.changed", "session.message"):
                continue
            if data.get("removed"):
                latest.pop(data["pane_id"], None)
            else:
                latest[data["pane_id"]] = event
        saved = self.herdr.snapshot()
        local_hosts = {host["name"]: host for host in self.service.config.hosts() if cli.is_local(host)}
        execution_roots = [Path(cli.local_path(host["workdir"], "")).resolve() for host in local_hosts.values()]
        execution_roots.extend(Path(cli.local_path(rec["worker_root"], "")).resolve()
                               for rec in self.service.records()
                               if rec.get("host") in local_hosts and rec.get("worker_root"))
        for agent in self.herdr.agents():
            harness = HARNESSES.get(agent.get("agent"))
            cwd = agent.get("foreground_cwd") or agent.get("cwd")
            if not harness or not cwd:
                continue
            if any(Path(cwd).resolve().is_relative_to(root) for root in execution_roots):
                continue
            result = cli.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], check=False)
            if result.returncode:
                continue
            project, origin = cli.project_name(result.stdout.strip())
            raw_status = agent.get("agent_status", "unknown")
            status = "idle" if raw_status == "done" else raw_status
            if status not in STATUS_TEXT:
                status = "unknown"
            identity, transcript, probable = self.locate(agent, saved.get(agent["pane_id"], {}), cwd, harness)
            row = dict(id=agent["pane_id"], pane_id=agent["pane_id"], harness=harness,
                             project=project, project_id=project_id(project, origin),
                             status=status, status_text=STATUS_TEXT[status], has_new_reply=raw_status == "done",
                             session_id=identity, probable=probable,
                             identity_text="可能是这个会话" if probable else None)
            event = latest.get(row["pane_id"])
            row["updated_at"] = event["time"] if event and all(
                event["data"].get(key) == row[key] for key in ("session_id", "harness", "project_id")) else None
            rows.append(dict(row=row, transcript=transcript, origin=origin))
        return rows

    def messages(self, item, after=None):
        path = item["transcript"]
        if path is None:
            return dict(supported=False, messages=[], reason="unsupported_format", text="不支持")
        try:
            with path["path"].open(encoding="utf-8") as stream:
                result = convert_transcript(item["row"]["harness"], stream)
        except (OSError, UnicodeError):
            return dict(supported=False, messages=[], reason="unsupported_format", text="不支持")
        if after and result["supported"]:
            ids = [message["id"] for message in result["messages"]]
            if after not in ids:
                raise ValueError("invalid_message_cursor")
            result["messages"] = result["messages"][ids.index(after) + 1:]
        return result

    def read(self, target, device=None):
        path = unquote(urlsplit(target).path)
        if self.is_session_path(path) and not self.allowed(device):
            return DISABLED
        if path == "/v1/sessions":
            return 200, {"sessions": self.list()}
        match = re.fullmatch(r"/v1/jobs/([^/]+)/session", path)
        if match:
            job = messages.job_id(match[1])
            rec = messages.read_json(self.service.state / "jobs" / (job + ".json"))
            if not rec or rec.get("user_id") not in (None, "", self.service.user_id):
                return 404, {"error": {"reason": "not_found"}}
            rows = sorted(self.list(), key=lambda row: row["probable"])
            identity = rec.get("initiator_session")
            item = next((row for row in rows if identity and row["session_id"] == identity), None)
            matched_by = "session_id" if item else None
            if item is None:
                pane = (rec.get("initiator_herdr") or {}).get("pane_id")
                item = next((row for row in rows if row["pane_id"] == pane), None)
                matched_by = "pane_id" if item else None
            if item is None:
                return 200, dict(session=None, matched_by=None, reason="session_closed", text="会话已关闭")
            return 200, dict(session=item, matched_by=matched_by)
        match = re.fullmatch(r"/v1/sessions/([^/]+)/(messages|screen)", path)
        if match:
            item = next((item for item in self.discover() if item["row"]["id"] == match[1]), None)
            if item is None:
                return 404, {"error": {"reason": "session_closed", "text": "会话已关闭"}}
            if match[2] == "screen":
                return 200, dict(pane_id=item["row"]["pane_id"], text=self.herdr.screen(match[1]))
            after = parse_qs(urlsplit(target).query).get("after", [None])[0]
            return 200, self.messages(item, after)
        return None

    def handle_get(self, handler):
        path = unquote(urlsplit(handler.path).path)
        if self.is_session_path(path) and not self.allowed(getattr(handler, "session_device", None)):
            handler.json(*DISABLED)
            return True
        match = re.fullmatch(r"/v1/sessions/([^/]+)/terminal", path)
        if match is None:
            return False
        item = next((item for item in self.list() if item["id"] == match[1]), None)
        if item is None:
            handler.json(404, {"error": {"reason": "session_closed", "text": "会话已关闭"}})
            return True
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        frames = self.herdr.frames(match[1], self.service.stop, handler.connection)
        try:
            for line in frames:
                kind = json.loads(line).get("type")
                if kind not in ("terminal.frame", "terminal.closed"):
                    continue
                handler.wfile.write(b"event: " + kind.encode() + b"\ndata: " + line + b"\n\n")
                handler.wfile.flush()
        except (OSError, ValueError, TypeError, AttributeError):
            # Headers were already sent: end the stream without a second response.
            return True
        finally:
            frames.close()
        return True

    def handle_post(self, handler):
        path = unquote(urlsplit(handler.path).path)
        if not self.is_session_path(path):
            return False
        if not self.allowed(getattr(handler, "session_device", None)):
            # Drain a bounded ordinary request before closing, so clients sending
            # headers and body separately can still receive the 403 response.
            try:
                length = int(handler.headers.get("Content-Length", "0"))
                if not handler.headers.get("Transfer-Encoding") and 0 < length <= 8 * 1024 * 1024:
                    handler.rfile.read(length)
            except ValueError:
                pass
            handler.json(*DISABLED)
            return True
        match = re.fullmatch(r"/v1/sessions/([^/]+)/input", path)
        if match is None:
            return False
        try:
            if handler.headers.get("Transfer-Encoding") or handler.headers.get_content_type() != "application/json":
                raise ValueError("invalid_session_input")
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 8 * 1024 * 1024:
                raise ValueError("invalid_session_input")
            body = json.loads(handler.rfile.read(length))
            key = handler.headers.get("Idempotency-Key", "")
            if not re.fullmatch(r"[!-~]{1,256}", key):
                raise ValueError("idempotency_key_required")
            status, response = self.write_input(match[1], key, body, getattr(handler, "session_device", None))
        except JobBusy:
            status, response = 409, {"error": {"reason": "session_input_busy"}}
        except (ValueError, TypeError, UnicodeError):
            status, response = 400, {"error": {"reason": "invalid_session_input"}}
        except OSError:
            status, response = 503, {"error": {"reason": "session_input_unavailable"}}
        handler.json(status, response)
        return True

    def write_input(self, pane, key, body, device):
        device_id = device["id"] if device is not None else "local"
        identity = hashlib.sha256(json.dumps([self.service.user_id, device_id, key]).encode()).hexdigest()
        path = self.service.state / "sessions" / "inputs" / (identity + ".json")
        lock = JobLock(self.service.state, "sessions")
        lock.path = self.service.state / "locks" / "sessions" / "input.lock"
        with self.service.writer, lock:
            previous = messages.read_json(path)
            if previous is not None:
                return previous["status"], previous["body"]
            if self.service.stop.is_set():
                raise OSError("serve_stopping")
            if not isinstance(body, dict) or set(body) not in ({"text"}, {"keys"}):
                raise ValueError("invalid_session_input")
            if "text" in body:
                if not isinstance(body["text"], str) or not body["text"] or "\0" in body["text"]:
                    raise ValueError("invalid_session_input")
            elif (not isinstance(body["keys"], list) or not body["keys"] or any(
                    not isinstance(key, str) or key not in {"enter", "esc", "up", "down", "tab", "ctrl+c"}
                    for key in body["keys"])):
                raise ValueError("invalid_session_input")
            target = next((row for row in self.list() if row["id"] == pane), None)
            if target is None:
                result = dict(status=404, body={"error": {"reason": "session_closed", "text": "会话已关闭"}})
            else:
                # Persist an uncertainty result before crossing the external write boundary.
                # A crash must never turn a retry into a second terminal input.
                messages.atomic_json(path, dict(status=409, body={"error": {"reason": "session_input_result_unknown"}}))
                try:
                    self.herdr.input(pane, body)
                    result = dict(status=200, body=dict(pane_id=pane, accepted=True))
                except OSError:
                    result = dict(status=503, body={"error": {"reason": "session_input_unavailable"}})
            with events.write_transaction([path]):
                messages.atomic_json(path, result)
                if result["status"] == 200:
                    events.emit(self.service.state, "session.changed", dict(target, user_id=self.service.user_id, removed=False))
            self.service.wake.set()
            return result["status"], result["body"]
