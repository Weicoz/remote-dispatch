"""Session routes kept separate from the shared serve routing module."""
import base64
import copy
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from . import cli
from . import rd_messages as messages
from . import rd_events as events
from .rd_herdr import Herdr
from .rd_hc import Hc
from .rd_terminal_ws import TerminalSockets
from .rd_session_defaults import DefaultRoutes, SessionDefaults
from .rd_projects import project_id
from .rd_transcripts import convert_transcript
from .rd_lock import JobBusy, JobLock


HARNESSES = {"codex": "codex", "claude": "claude", "claude-code": "claude",
             "kimi": "kimi", "kimi-code": "kimi", "agy": "agy",
             "antigravity": "agy", "antigravity-cli": "agy"}
STATUS_TEXT = {"working": "工作中", "idle": "空闲", "blocked": "等待输入", "unknown": "状态未知", "done": "已完成"}
# 会话 id：TUI 为 pane_id（w1:p1）；App 为 "hc:" + hc 地址，地址可含一个 "/"（机器/终端）。
SESSION_ID = r"(hc:[^?]+?|[^/]+)"
UNSUPPORTED_SURFACE = (409, {"error": "unsupported_surface", "surface": "app"})
TERMINAL_KEYS = {"backspace", "left", "right", "ctrl+d", "shift+tab", "ctrl+a", "ctrl+u",
                 "space", "enter", "esc", "up", "down", "tab", "ctrl+c"}
DISABLED = (403, {"error": {"reason": "session_access_disabled"}})


class Sessions:
    def __init__(self, service):
        self.service = service
        self.herdr = Herdr()
        self.hc = Hc(on_change=lambda: service.wake.sessions.set())
        self.terminals = TerminalSockets(self)
        self.defaults = DefaultRoutes(self)
        self.observed = {}
        self.seen_messages = {}
        self.cache_lock = threading.RLock()
        self.event_reader = events.EventCursor(service.state)
        self.latest = {}
        self.transcript_cache = {}
        self.items = []
        self.version = 0
        self.collection = dict(stale=False, consecutive_failures=0,
                               last_attempt_succeeded=None, reason=None, last_success_at=None)
        for event in self.event_reader.read():
            data = event["data"]
            if data.get("user_id") != service.user_id:
                continue
            self.remember_event(event)
            if event["type"] == "session.changed" and "dispatch_defaults" not in data:
                if data.get("removed"):
                    self.observed.pop(data["pane_id"], None)
                else:
                    self.observed[data["pane_id"]] = data
            elif event["type"] == "session.message":
                self.seen_messages.setdefault((data["pane_id"], data.get("session_id")), set()).add(data["message_id"])

    @property
    def enabled(self):
        return self.service.config.cp.getboolean("serve", "session_access", fallback=True)

    def collect(self):
        try:
            if not self.enabled:
                SessionDefaults(self.service.state, self.service.user_id).observe(None)
                return
            items = self.discover()
            # App 会话没有 pane_id：不进派单默认值与 session.* 事件，只出现在列表缓存里。
            tui = [item for item in items if item["row"].get("surface", "tui") == "tui"]
            SessionDefaults(self.service.state, self.service.user_id).observe([item["row"] for item in tui])
            current, seen = {}, {pane: set(ids) for pane, ids in self.seen_messages.items()}
            with events.transaction():
                for item in tui:
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
            with self.cache_lock:
                if self.items != items:
                    self.version += 1
                self.items = items
                self.collection = dict(stale=False, consecutive_failures=0,
                    last_attempt_succeeded=True, reason=None, last_success_at=messages.utc_now())
            self.warm_projects()
        except (OSError, ValueError, TypeError, KeyError) as error:
            # Exception text can contain terminal content or credentials. Log only
            # the exception category and failure count, never its message/traceback.
            with self.cache_lock:
                failures = self.collection["consecutive_failures"] + 1
                self.collection = dict(self.collection, stale=failures >= 3,
                    consecutive_failures=failures, last_attempt_succeeded=False,
                    reason=type(error).__name__)
            print(f"rdispatch: session_collect_failed reason={type(error).__name__} "
                  f"consecutive_failures={failures}; retaining previous list", file=sys.stderr)

    def warm_projects(self):
        """后台预热项目视图，让读路径的会话级 needs_attention_count 有缓存可读。"""
        try:
            self.service.project_routes.list_view()
        except Exception as error:
            print(f"rdispatch: session_warm_failed reason={type(error).__name__}", file=sys.stderr)

    def allowed(self, device=None):
        """device is trusted authentication context, never request headers."""
        return self.enabled and (device is None or device.get("session_access") is True)

    def close(self):
        self.terminals.close()
        self.herdr.close()
        self.hc.close()

    @staticmethod
    def is_session_path(path):
        return path == "/v1/sessions" or path.startswith("/v1/sessions/") or bool(
            re.fullmatch(r"/v1/jobs/[^/]+/session", path))

    def event_allowed(self, handler, event):
        return not event["type"].startswith(("session.", "terminal.")) or self.allowed(getattr(handler, "session_device", None))

    def list(self):
        return [item["row"] for item in self.cached()]

    @staticmethod
    def count_attention(rows, records, cards):
        """把「卡住 / 离线 / 失败」的派单挂到发起会话上：先按 initiator_herdr.pane_id 匹配 TUI 会话，
        匹配不到再按 initiator_hc.terminal 匹配 App 会话；probable 会话恒为 0。口径同项目级 needs_attention_count。"""
        from .rd_serve_projects import urgency
        by_pane = {row["pane_id"]: row for row in rows if row.get("surface") == "tui" and row.get("pane_id")}
        by_address = {row["address"]: row for row in rows if row.get("surface") == "app"}
        for row in rows:
            if "needs_attention_count" in row:
                row["needs_attention_count"] = 0
        for rec in records:
            card = cards.get(rec["job"])
            if card is None or urgency(card)[1] >= 3:
                continue
            pane = (rec.get("initiator_herdr") or {}).get("pane_id")
            target = by_pane.get(pane) if pane else None
            if target is None:
                terminal = (rec.get("initiator_hc") or {}).get("terminal")
                target = by_address.get(terminal) if terminal else None
            if target is not None and "needs_attention_count" in target and not target["probable"]:
                target["needs_attention_count"] += 1
        return rows

    def counted(self, rows):
        """请求路径上只读已有的项目缓存，绝不触发重建；缓存未就绪时计数为 0，由后台采集预热。"""
        view = self.service.project_routes.cached_view()
        if view is None:
            return rows
        return self.count_attention(rows, view["retained"], view["cards"])

    def cached(self):
        """HTTP reads use the last successful collection, including during outages."""
        with self.cache_lock:
            for event in self.event_reader.read():
                self.remember_event(event)
            items = copy.deepcopy(self.items)
            for item in items:
                row = item["row"]
                if row.get("surface", "tui") != "tui":
                    continue
                event = self.latest.get(row["pane_id"])
                row["updated_at"] = event["time"] if event and all(
                    event["data"].get(key) == row[key] for key in ("session_id", "harness", "project_id")) else None
            return items

    def backend(self, surface):
        return self.hc if surface == "app" else self.herdr

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

    def locate(self, agent, saved, cwd, harness, paths=None):
        reference = saved.get("agent_session") if saved.get("cwd") == (agent.get("cwd") or cwd) else None
        live = agent.get("agent_session")
        if (isinstance(live, dict) and HARNESSES.get(live.get("agent")) == harness
                and live.get("kind") in ("id", "path") and isinstance(live.get("value"), str) and live["value"]):
            # Saved snapshots are delayed. Do not route a replaced pane to its old conversation.
            reference = live
        if paths is None:
            paths = self.transcripts(harness)
        unmatched_id = None
        if isinstance(reference, dict) and HARNESSES.get(reference.get("agent")) == harness:
            kind, value = reference.get("kind"), reference.get("value")
            if isinstance(value, str) and value and kind in ("id", "path"):
                match = next((p for p in paths if (p["id"] == value if kind == "id"
                              else p["path"].resolve() == Path(value).resolve())), None)
                if match is not None or kind == "path":
                    return value if kind == "id" else match["id"] if match else None, match, False
                unmatched_id = value
        candidates = [p for p in paths if Path(p["cwd"]).resolve() == Path(cwd).resolve()]
        match = max(candidates, key=lambda p: (p["mtime"], str(p["path"])), default=None)
        return (match["id"], match, True) if match else (unmatched_id, None, False)

    def remember_event(self, event):
        data = event["data"]
        if (data.get("user_id") != self.service.user_id or "dispatch_defaults" in data
                or event["type"] not in ("session.changed", "session.message")):
            return
        if data.get("removed"):
            self.latest.pop(data["pane_id"], None)
        else:
            self.latest[data["pane_id"]] = event

    def discover(self):
        rows, transcripts = [], {}
        with self.cache_lock:
            for event in self.event_reader.read():
                self.remember_event(event)
            latest = dict(self.latest)
        saved = self.herdr.snapshot()
        local_hosts = {host["name"]: host for host in self.service.config.hosts() if cli.is_local(host)}
        execution_roots = [Path(cli.local_path(host["workdir"], "")).resolve() for host in local_hosts.values()]
        execution_roots.extend(Path(cli.local_path(rec["worker_root"], "")).resolve()
                               for rec in self.service.records()
                               if rec.get("host") in local_hosts and rec.get("worker_root"))
        candidates = []
        for agent in self.herdr.agents():
            pane = agent["pane_id"]
            live_cwd = agent.get("foreground_cwd") or agent.get("cwd") or None
            raw_status = agent.get("agent_status", "unknown")
            status = "idle" if raw_status == "done" else raw_status
            candidates.append(dict(
                surface="tui", id=pane, address=pane, pane_id=pane, harness=HARNESSES.get(agent.get("agent")),
                live_cwd=live_cwd, cwd=live_cwd or saved.get(pane, {}).get("cwd"), agent=agent,
                saved=saved.get(pane, {}), status=status, new_reply=raw_status == "done"))
        self.hc.start()
        for agent in self.hc.agents():
            address = agent["address"]
            candidates.append(dict(
                surface="app", id="hc:" + address, address=address, pane_id=None,
                harness=next((HARNESSES[name] for name in agent["names"] if name in HARNESSES), None),
                live_cwd=agent["cwd"], cwd=agent["cwd"], agent={}, saved={}, status=agent["status"],
                new_reply=agent["status"] == "done", stamp_ms=agent["stamp_ms"]))
        for found in candidates:
            harness, cwd, agent, surface = found["harness"], found["cwd"], found["agent"], found["surface"]
            if not harness or not cwd:
                continue
            if any(Path(cwd).resolve().is_relative_to(root) for root in execution_roots):
                continue
            result = cli.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], check=False)
            if result.returncode:
                continue
            project, origin = cli.project_name(result.stdout.strip())
            status = found["status"]
            if status not in STATUS_TEXT:
                status = "unknown"
            if harness not in transcripts:
                transcripts[harness] = self.transcripts(harness)
            identity, transcript, probable = self.locate(
                agent, found["saved"], cwd, harness, transcripts[harness])
            backend = self.backend(surface)
            row = dict(id=found["id"], pane_id=found["pane_id"], harness=harness,
                             project=project, project_id=project_id(project, origin), cwd=found["live_cwd"],
                             status=status, status_text=STATUS_TEXT[status], has_new_reply=found["new_reply"],
                             session_id=identity, probable=probable,
                             identity_text="可能是这个会话" if probable else None,
                             surface=surface, address=found["address"],
                             capabilities=list(backend.capabilities), needs_attention_count=0)
            if surface == "tui":
                event = latest.get(row["pane_id"])
                row["updated_at"] = event["time"] if event and all(
                    event["data"].get(key) == row[key] for key in ("session_id", "harness", "project_id")) else None
            else:
                stamp = found.get("stamp_ms")
                row["updated_at"] = (datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
                                     if type(stamp) in (int, float) else None)
            rows.append(dict(row=row, transcript=transcript, origin=origin))
        return rows

    def messages(self, item, after=None):
        path = item["transcript"]
        if path is None:
            return dict(supported=False, messages=[], reason="unsupported_format", text="不支持")
        key = None
        try:
            with self.cache_lock, path["path"].open("rb") as stream:
                stat = os.fstat(stream.fileno())
                key = (str(path["path"]), item["row"]["harness"])
                cache = self.transcript_cache.get(key)
                identity = stat.st_dev, stat.st_ino
                unchanged_prefix = True
                if cache is not None and cache["offset"]:
                    stream.seek(max(0, cache["offset"] - 128))
                    unchanged_prefix = stream.read(min(128, cache["offset"])) == cache["anchor"]
                if (cache is None or cache["identity"] != identity or stat.st_size < cache["offset"]
                        or not unchanged_prefix
                        or (stat.st_size == cache["offset"] and stat.st_mtime_ns != cache["mtime"])):
                    cache = dict(identity=identity, offset=0, lines=0, tools={}, mtime=None,
                                 result=dict(supported=True, messages=[]))
                stream.seek(cache["offset"])
                lines = []
                while line := stream.readline():
                    # An incomplete append is retried after the next newline.
                    if not line.endswith(b"\n"):
                        break
                    lines.append(line.decode("utf-8"))
                    cache["offset"] = stream.tell()
                if lines and cache["result"]["supported"]:
                    added = convert_transcript(key[1], lines, tools=cache["tools"], line_offset=cache["lines"])
                    if added["supported"]:
                        cache["result"]["messages"].extend(added["messages"])
                    else:
                        cache["result"] = added
                cache["lines"] += len(lines)
                cache["mtime"] = stat.st_mtime_ns
                stream.seek(max(0, cache["offset"] - 128))
                cache["anchor"] = stream.read(min(128, cache["offset"]))
                self.transcript_cache[key] = cache
                result = copy.deepcopy(cache["result"])
        except (OSError, UnicodeError, KeyError, TypeError):
            # A failed conversion may have advanced offsets or mutated tools.
            # Retry the whole file next time, never reuse a partially mutated cache.
            with self.cache_lock:
                self.transcript_cache.pop(key, None)
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
        defaults = self.defaults.read(path)
        if defaults is not None:
            return defaults
        if path == "/v1/sessions":
            rows = self.counted(self.list())
            with self.cache_lock:
                return 200, {"sessions": rows, "collection": dict(self.collection)}
        match = re.fullmatch(r"/v1/jobs/([^/]+)/session", path)
        if match:
            job = messages.job_id(match[1])
            rec = messages.read_json(self.service.state / "jobs" / (job + ".json"))
            if not rec or rec.get("user_id") not in (None, "", self.service.user_id):
                return 404, {"error": {"reason": "not_found"}}
            rows = sorted(self.counted(self.list()), key=lambda row: row["probable"])
            identity = rec.get("initiator_session")
            item = next((row for row in rows if identity and row["session_id"] == identity), None)
            matched_by = "session_id" if item else None
            if item is None:
                pane = (rec.get("initiator_herdr") or {}).get("pane_id")
                item = next((row for row in rows if row["pane_id"] == pane), None)
                matched_by = "pane_id" if item else None
            if item is None:
                terminal = (rec.get("initiator_hc") or {}).get("terminal")
                item = next((row for row in rows if terminal and row.get("surface") == "app"
                             and row["address"] == terminal), None)
                matched_by = "terminal" if item else None
            if item is None:
                return 200, dict(session=None, matched_by=None, reason="session_closed", text="会话已关闭")
            return 200, dict(session=item, matched_by=matched_by)
        match = re.fullmatch(r"/v1/sessions/" + SESSION_ID + r"/messages", path)
        if match and match[1].startswith("hc:"):
            if any(row["id"] == match[1] for row in self.list()):
                return UNSUPPORTED_SURFACE
            return 404, {"error": {"reason": "session_closed", "text": "会话已关闭"}}
        if match:
            item = next((item for item in self.cached() if item["row"]["id"] == match[1]), None)
            if item is None:
                return 404, {"error": {"reason": "session_closed", "text": "会话已关闭"}}
            after = parse_qs(urlsplit(target).query).get("after", [None])[0]
            return 200, self.messages(item, after)
        return None

    def handle_get(self, handler):
        path = unquote(urlsplit(handler.path).path)
        if self.is_session_path(path) and not self.allowed(getattr(handler, "session_device", None)):
            handler.json(*DISABLED)
            return True
        app = re.fullmatch(r"/v1/sessions/(hc:[^?]+?)/(terminal(?:/ws)?|screen)", path)
        if app is not None:
            item = next((item for item in self.list() if item["id"] == app[1]), None)
            if item is None:
                handler.json(404, {"error": {"reason": "session_closed", "text": "会话已关闭"}})
            elif app[2] == "screen":
                self.app_screen(handler, item)
            else:
                handler.json(*UNSUPPORTED_SURFACE)
            return True
        if self.terminals.handle_get(handler):
            return True
        match = re.fullmatch(r"/v1/sessions/([^/]+)/(terminal|screen)", path)
        if match is None:
            return False
        item = next((item for item in self.list() if item["id"] == match[1]), None)
        if item is None:
            handler.json(404, {"error": {"reason": "session_closed", "text": "会话已关闭"}})
            return True
        if match[2] == "screen":
            frames = self.herdr.frames(match[1], self.service.stop, handler.connection, self.service.poll_interval)
            try:
                for line in frames:
                    if not self.allowed(getattr(handler, "session_device", None)):
                        handler.json(*DISABLED)
                        return True
                    frame = json.loads(line)
                    if frame.get("type") == "terminal.frame" and frame.get("full") is True:
                        text = base64.b64decode(frame["data"], validate=True).decode("utf-8", "surrogateescape")
                        handler.json(200, self.service.redact(dict(pane_id=match[1],
                            text=redact_terminal(self.service, text), width=frame["width"],
                            height=frame["height"], cursor=frame["cursor"])))
                        return True
                    if frame.get("type") == "terminal.closed":
                        break
                raise OSError("herdr_screen_unavailable")
            finally:
                frames.close()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        frames = self.herdr.frames(match[1], self.service.stop, handler.connection, self.service.poll_interval)
        try:
            for line in frames:
                if not self.allowed(getattr(handler, "session_device", None)):
                    break
                frame = json.loads(line)
                kind = frame.get("type")
                if kind not in ("terminal.frame", "terminal.closed"):
                    continue
                original = dict(frame)
                if kind == "terminal.frame":
                    decoded = base64.b64decode(frame["data"], validate=True).decode("utf-8", "surrogateescape")
                    frame["data"] = base64.b64encode(redact_terminal(self.service, decoded).encode(
                        "utf-8", "surrogateescape")).decode("ascii")
                encoded = frame.pop("data", None)
                frame = self.service.redact(frame)
                if encoded is not None:
                    frame["data"] = encoded
                if frame != original:
                    line = json.dumps(frame, ensure_ascii=True).encode("utf-8")
                handler.wfile.write(b"event: " + kind.encode() + b"\ndata: " + line + b"\n\n")
                handler.wfile.flush()
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            # Headers were already sent: end the stream without a second response.
            return True
        finally:
            frames.close()
        return True

    def app_screen(self, handler, row):
        """App 会话读屏：`hc read`，只读，不改变终端尺寸；hc 不可用时 503。"""
        try:
            screen = self.hc.screen(row["address"], timeout=self.service.config.cp.getfloat(
                "serve", "request_timeout", fallback=15))
        except (OSError, ValueError):
            handler.json(503, {"error": {"reason": "hc_screen_unavailable"}})
            return
        handler.json(200, self.service.redact(dict(
            id=row["id"], pane_id=None, address=row["address"], surface="app",
            text=redact_terminal(self.service, screen["text"]), width=screen["width"],
            height=screen["height"], cursor=screen["cursor"])))

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
        app = re.fullmatch(r"/v1/sessions/(hc:[^?]+?)/input", path)
        if app is not None:
            try:
                length = int(handler.headers.get("Content-Length", "0"))
                if not handler.headers.get("Transfer-Encoding") and 0 < length <= 8 * 1024 * 1024:
                    handler.rfile.read(length)
            except ValueError:
                pass
            if any(item["id"] == app[1] for item in self.list()):
                handler.json(*UNSUPPORTED_SURFACE)
            else:
                handler.json(404, {"error": {"reason": "session_closed", "text": "会话已关闭"}})
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
            if not isinstance(body, dict) or set(body) not in ({"text"}, {"text", "submit"}, {"keys"}):
                raise ValueError("invalid_session_input")
            if "text" in body:
                if (not isinstance(body["text"], str) or not body["text"] or "\0" in body["text"]
                        or type(body.get("submit", True)) is not bool):
                    raise ValueError("invalid_session_input")
            elif (not isinstance(body["keys"], list) or not body["keys"] or any(
                    not isinstance(key, str) or key not in TERMINAL_KEYS
                    for key in body["keys"])):
                raise ValueError("invalid_session_input")
            target = next((item["row"] for item in self.discover() if item["row"]["id"] == pane), None)
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


# Preserve CSI/ESC bytes and OSC delimiters while scrubbing OSC text as well.
# Match secrets in visible text even when style codes split them.
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[@-Z\\-_])")


def redact_terminal(service, text):
    escapes = {}
    plain, offset, end = [], 0, 0
    for match in ANSI.finditer(text):
        chunk = text[end:match.start()]
        plain.append(chunk)
        offset += len(chunk)
        code = match.group()
        if code.startswith("\x1b]"):
            ending = 1 if code.endswith("\x07") else 2
            code = code[:2] + service.redact(code[2:-ending]) + code[-ending:]
        escapes.setdefault(offset, []).append(code)
        end = match.end()
    plain.append(text[end:])
    visible = "".join(plain)
    cleaned = service.redact(visible)
    if not escapes:
        return cleaned
    if cleaned == visible:
        codes = iter(code for group in escapes.values() for code in group)
        return ANSI.sub(lambda _: next(codes), text)
    # Map escape positions through text edits. Codes inside a replacement move
    # to its end, so a style change never splits the redaction marker itself.
    edits = difflib.SequenceMatcher(None, visible, cleaned).get_opcodes()
    markers = [match.span() for match in re.finditer(re.escape("[redacted]"), cleaned)]
    mapped = {}
    edit = 0
    for position, codes in escapes.items():
        while edit < len(edits) - 1 and edits[edit][2] <= position:
            edit += 1
        kind, start, finish, a, b = edits[edit]
        target = a + position - start if kind == "equal" else a if position == start else b
        for left, right in markers:
            if left < target < right:
                target = right
                break
        mapped.setdefault(target, []).extend(codes)
    output = []
    for position, char in enumerate(cleaned):
        output.extend(mapped.get(position, []))
        output.append(char)
    output.extend(mapped.get(len(cleaned), []))
    return "".join(output)
