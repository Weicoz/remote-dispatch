"""Project summaries and project HTTP routes, separate from the serve transport."""
import copy
import hashlib
import json
import re
import threading
import uuid
from urllib.parse import unquote, urlsplit

from datetime import datetime, timedelta, timezone

from . import rd_events as events
from . import rd_messages as messages
from .rd_idempotency import fingerprint
from .rd_projects import ProjectIndex, ProjectLock
from .rd_serve_history import instant, records as history_records


def urgency(card):
    execution = card["execution_state"]
    if execution in ("starting", "running"):
        return "running", 3
    if execution == "unreachable":
        state, priority = "offline", 2
    elif execution in ("dead", "missing") or card["outcome"] == "failed" or card.get("executor_exit") not in (None, 0):
        state, priority = "failed", 1
    elif card["outcome"] == "blocked":
        state, priority = "blocked", 0
    elif execution == "done":
        state, priority = "completed", 4
    else:
        state, priority = "none", 5
    if card.get("stopped_by") or card["review_state"] in ("accepted", "voided"):
        priority = 4
    return state, priority


class ProjectRoutes:
    def __init__(self, service):
        self.service = service
        self.pages = {}
        self.page_lock = threading.Lock()
        self.list_lock = threading.Lock()
        self.list_keys = {}
        self.list_payloads = {}
        self.event_reader = events.EventCursor(service.state)
        self.latest_events = {}
        self.state_events = {}

    def local_view(self, include_history):
        service = self.service
        jobs_version, job_documents = service.local_files.snapshot("jobs")
        current = [rec for path, rec in sorted(job_documents.items())
                   if rec and rec.get("user_id") in (None, "", service.user_id)]
        results_version, documents = service.local_files.snapshot("results", nested=True,
            job_rounds=None if include_history else {rec["job"]: rec["rounds"] for rec in current})
        documents = dict(documents)
        documents.update(job_documents)
        return (jobs_version, results_version), current, documents

    def records(self):
        service = self.service
        _, current, documents = self.local_view(True)
        return history_records(service.state, current, service.user_id, documents=documents)

    def prewarm(self):
        self.list_view(False)
        self.list_view()

    def cached_view(self):
        """已发布的完整视图；从不重建，未就绪返回 None。供读路径使用。"""
        return self.list_payloads.get(True)

    def list_view(self, include_history=True):
        """Single-flight rebuild. No TTL: every read checks all local generations."""
        service = self.service
        with self.list_lock:
            versions, current, documents = self.local_view(include_history)
            projects_version, _ = service.local_files.snapshot("projects", filename=service.user_id + ".json")
            for event in self.event_reader.read():
                data = event["data"]
                identity = (data.get("user_id"), data.get("job"), data.get("project_id"),
                            event["type"].startswith("session."))
                self.latest_events[identity] = event
                if event["type"] == "job.state_changed" and data.get("execution_state") and service.visible(event):
                    self.state_events[(data.get("job"), data.get("round"), data.get("host"))] = event
            with service.cache_lock:
                snapshot_version = service.snapshot_version
            with service.sessions.cache_lock:
                session_version = service.sessions.version
            key = (versions, projects_version, tuple(rec["job"] for rec in current),
                   self.event_reader.seq, snapshot_version, session_version,
                   service.sessions.enabled,
                   tuple((section, tuple(service.config.cp.items(section))) for section in service.config.cp.sections()),
                   datetime.now(timezone.utc).astimezone().date())
            if key != self.list_keys.get(include_history):
                query = service.query(documents)
                retained = (history_records(service.state, current, service.user_id, documents=documents)
                            if include_history else current)
                cards = {rec["job"]: self.card(rec, query) for rec in retained}
                visible = sorted((event for event in self.latest_events.values() if service.visible(event)),
                                 key=lambda event: event["seq"])
                payload = dict(current=current, retained=retained, cards=cards)
                if include_history:
                    for allowed in (False, True):
                        payload[allowed] = self.projects(
                            None if allowed else {"session_access": False}, query=query,
                            records=retained, cards=cards, visible_events=visible)
                # Publish together only after a successful complete rebuild.
                self.list_keys[include_history] = key
                self.list_payloads[include_history] = payload
            return self.list_payloads[include_history]

    def card(self, rec, query):
        card = self.service.card(rec, query)
        receipt = query.read_json(query.result_path(rec, rec["rounds"]))
        if receipt:
            card["execution_state"] = receipt.get("execution_state", card["execution_state"])
        elif rec.get("archived"):
            card["execution_state"] = "done"
        event = self.state_events.get((rec["job"], rec["rounds"], rec.get("host")))
        if event:
            stamp = instant(event["time"])
            known_stamps = [instant(value) for value in (card.get("snapshot_at"),
                            (receipt or {}).get("captured_at")) if instant(value)]
            # Retained receipts may lack captured_at. A newer file or a replaced
            # record (including same-round migration) supersedes older events.
            paths = [self.service.state / "jobs" / (rec["job"] + ".json")]
            if receipt:
                paths.append(query.result_path(rec, rec["rounds"]))
            for path in paths:
                try:
                    known_stamps.append(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
                except FileNotFoundError:
                    pass
            known = max(known_stamps, default=None)
            state = event["data"].get("execution_state")
            # An unreachable target does not invalidate an already retained terminal receipt.
            if (state and stamp and (known is None or stamp > known)
                    and not (receipt and state == "unreachable")):
                card["execution_state"] = state
                card["snapshot_at"] = event["time"]
                if event["data"].get("executor_exit") is not None:
                    card["executor_exit"] = event["data"]["executor_exit"]
        return card

    @staticmethod
    def summary(row, session_only=False):
        return dict(row, session_only=session_only, sessions=[], needs_attention_count=0,
                    blocked_count=0, offline_count=0, failed_count=0,
                    in_progress_count=0, review_pending_count=0, hosts={}, week_dispatch_count=0,
                    last_accepted_at=None, last_harness=None, recent_event=None)

    @staticmethod
    def summary_totals(rows):
        """顶层汇总：前三项为未隐藏项目对应计数之和；sessions_working 为所有会话（TUI 与 App）中工作中的数量。"""
        shown = [row for row in rows if not row.get("hidden")]
        return dict(needs_attention=sum(row["needs_attention_count"] for row in shown),
                    in_progress=sum(row["in_progress_count"] for row in shown),
                    review_pending=sum(row["review_pending_count"] for row in shown),
                    sessions_working=sum(session["status"] == "working"
                                         for row in rows for session in row["sessions"]))

    def accepted_counts(self, rec, query):
        counts = {}
        for number in range(1, rec["rounds"] + 1):
            if query.review(rec, number)["review_state"] != "accepted":
                continue
            receipt = query.read_json(query.result_path(rec, number))
            host = (receipt or {}).get("host") or rec.get("host")
            if host:
                counts[host] = counts.get(host, 0) + 1
        return counts

    def observe(self):
        service = self.service
        query = service.query()
        projects = {row["id"]: row for row in query.projects(service.user_id)}
        counts = {}
        for rec in query.jobs(self.records(), snapshots={}, user_id=service.user_id):
            identity = rec["project_id"]
            projects.setdefault(identity, dict(name=rec["project"], hidden=False))
            for host, count in self.accepted_counts(rec, query).items():
                key = (identity, host)
                counts[key] = counts.get(key, 0) + count
        log = events.EventLog(service.state)
        for (identity, host), count in sorted(counts.items()):
            log.publish("project.changed", dict(projects[identity], user_id=service.user_id,
                        project_id=identity, host=host, accepted_round_count=count), only_if_changed=True)

    def projects(self, device=None, *, query=None, records=None, cards=None, visible_events=None):
        service = self.service
        if query is None:
            return copy.deepcopy(self.list_view()[service.sessions.allowed(device)])
        sessions_allowed = service.sessions.allowed(device)
        rows = {row["id"]: self.summary(row)
                for row in query.projects(service.user_id)}
        now = datetime.now(timezone.utc).astimezone()
        week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        visible_events = [event for event in visible_events
                          if sessions_allowed or not event["type"].startswith("session.")]
        job_updates = {}
        for event in visible_events:
            if event["data"].get("job"):
                job_updates[event["data"]["job"]] = event["time"]
        ranked, last_dispatch, job_projects, accepted_counts = {}, {}, {}, {}
        for rec in records:
            card = cards[rec["job"]]
            identity = card["project_id"]
            job_projects[rec["job"]] = identity
            row = rows.setdefault(identity, self.summary(dict(id=identity, name=card["project"], hidden=False,
                      origin=rec.get("origin") or "", dispatch_count=0, first_dispatch_at=None,
                      last_dispatch_at=None)))
            state, priority = urgency(card)
            if priority < 3:
                row[state + "_count"] += 1
            row["needs_attention_count"] = row["blocked_count"] + row["offline_count"] + row["failed_count"]
            row["in_progress_count"] += int(state == "running")
            row["review_pending_count"] += int(card["outcome"] in ("completed", "partial")
                                               and card["review_state"] == "pending")
            created = instant(rec.get("created"))
            if created and week <= created <= now:
                row["week_dispatch_count"] += 1
            key = (created or datetime.min.replace(tzinfo=timezone.utc), rec["job"])
            if identity not in last_dispatch or key > last_dispatch[identity]:
                last_dispatch[identity] = key
                row["last_harness"] = card["harness"]
            for host, count in self.accepted_counts(rec, query).items():
                key = (identity, host)
                accepted_counts[key] = accepted_counts.get(key, 0) + count
            for number in range(1, rec["rounds"] + 1):
                review = query.review(rec, number)
                accepted = review.get("reviewed_at") if review["review_state"] == "accepted" else None
                if accepted and (instant(accepted) or datetime.min.replace(tzinfo=timezone.utc)) > (instant(row["last_accepted_at"]) or datetime.min.replace(tzinfo=timezone.utc)):
                    row["last_accepted_at"] = accepted
            review = query.review(rec, rec["rounds"])
            acknowledgement = card.get("acknowledgement") or {}
            candidates = [card.get("updated_at"), review.get("reviewed_at"), acknowledgement.get("received_at"), job_updates.get(rec["job"])]
            updated = max((stamp for stamp in candidates if instant(stamp)), key=instant, default=None)
            rank = (priority, -(instant(updated).timestamp() if instant(updated) else 0), rec["job"])
            host_key = (identity, card["host"])
            if host_key not in ranked or rank < ranked[host_key]:
                ranked[host_key] = rank
                row["hosts"][card["host"]] = dict(state=state, execution_state=card["execution_state"],
                                                     job=card["job"], updated_at=updated)
        if sessions_allowed:
            try:
                sessions = service.sessions.cached()
            except OSError:
                sessions = []
            service.sessions.count_attention([item["row"] for item in sessions], records, cards)
            for item in sessions:
                session = item["row"]
                identity = session["project_id"]
                row = rows.setdefault(identity, self.summary(dict(id=identity, name=session["project"],
                    origin=item["origin"], hidden=False, dispatch_count=0,
                    first_dispatch_at=None, last_dispatch_at=None), session_only=True))
                row["sessions"].append(session)
        for event in visible_events:
            identity = event["data"].get("project_id") or job_projects.get(event["data"].get("job"))
            if identity in rows:
                rows[identity]["recent_event"] = dict(event, data=events.display_data(event["type"], event["data"]))
        for row in rows.values():
            row["origin"] = re.sub(r"(://)[^/@\s]+@", r"\1", row["origin"])
            for host in service.config.hosts():
                row["hosts"].setdefault(host["name"], dict(state="none", execution_state=None, job=None, updated_at=None))
            for identity, host in accepted_counts:
                if identity == row["id"]:
                    row["hosts"].setdefault(host, dict(state="none", execution_state=None, job=None, updated_at=None))
            for host, cell in row["hosts"].items():
                cell["accepted_round_count"] = accepted_counts.get((row["id"], host), 0)
        return sorted(rows.values(), key=lambda row: (row["name"] == "未归属", row["name"], row["id"]))

    def read(self, path, args, device=None):
        if path == "/v1/harness-stats":
            from .rd_harness_stats import host_statistics, statistics
            recent = int(args.get("recent", ["20"])[0])
            if not 1 <= recent <= 200 or any(len(values) != 1 for values in args.values()):
                raise ValueError("invalid_stats_options")
            return 200, {"harness_stats": statistics(self.service, recent),
                         "host_stats": host_statistics(self.service), "date_basis": "controller_local"}
        if path == "/v1/projects":
            rows = self.projects(device)
            return 200, {"projects": rows, "summary": self.summary_totals(rows)}
        if path == "/v1/jobs":
            return 200, self.jobs(args)
        return None

    def jobs(self, args):
        service = self.service
        if any(len(values) != 1 for values in args.values()):
            raise ValueError("invalid_job_options")
        project = args.get("project", [None])[0]
        state = args.get("state", [None])[0]
        limit = int(args.get("limit", ["50"])[0])
        if not 1 <= limit <= 200 or state not in (None, "active", "history"):
            raise ValueError("invalid_job_options")
        cursor = args.get("cursor", [None])[0]
        with self.page_lock:
            if cursor:
                try:
                    identity, offset = cursor.split(".")
                    saved_project, rows, saved_limit = self.pages[identity]
                    offset = int(offset)
                    if state != "history" or saved_project != project or saved_limit != limit or not 0 < offset < len(rows):
                        raise ValueError("invalid_history_cursor")
                except (KeyError, ValueError):
                    raise ValueError("invalid_history_cursor") from None
            else:
                view = self.list_view(state == "history")
                records = view["retained"] if state == "history" else view["current"]
                rows = []
                for rec in records:
                    card = view["cards"][rec["job"]]
                    if project and card["project_id"] != project:
                        continue
                    historical = bool(rec.get("archived")) or (
                        card["execution_state"] not in ("starting", "running")
                        and card["review_state"] in ("accepted", "voided"))
                    if state == "history" and not historical or state == "active" and historical:
                        continue
                    rows.append(dict(card, created_at=rec.get("created")))
                if state != "history":
                    return {"jobs": copy.deepcopy(rows)}
                rows.sort(key=lambda row: (instant(row["created_at"]) or datetime.min.replace(tzinfo=timezone.utc), row["job"]), reverse=True)
                identity, offset = uuid.uuid4().hex, 0
                if len(rows) > limit:
                    self.pages[identity] = (project, rows, limit)
            end = offset + limit
            return dict(jobs=copy.deepcopy(rows[offset:end]), next_cursor=f"{identity}.{end}" if end < len(rows) else None)

    def post(self, handler):
        match = re.fullmatch(r"/v1/projects/([^/]+)/(hide|show)", unquote(urlsplit(handler.path).path))
        if not match:
            return False
        service = self.service
        key = handler.headers.get("Idempotency-Key", "").strip()
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if (handler.headers.get("Transfer-Encoding") or not 0 <= length <= 8 * 1024 * 1024
                    or (length and handler.headers.get_content_type() != "application/json")):
                raise ValueError("invalid_request")
            if length and not isinstance(json.loads(handler.rfile.read(length)), dict):
                raise ValueError("invalid_request")
            if not key or len(key) > 255:
                handler.json(400, {"error": {"reason": "idempotency_key_required"}})
                return True
            digest = hashlib.sha256(json.dumps([service.user_id, key]).encode()).hexdigest()
            saved = service.state / "serve-project-requests" / (digest + ".json")
            identity, action = match.groups()
            index = ProjectIndex(service.state, service.user_id)
            with service.writer:
                if service.stop.is_set():
                    raise OSError("serve_stopping")
                request_hash = fingerprint("POST", "/v1/projects/%s/%s" % (identity, action), {})
                previous = messages.read_json(saved)
                if previous is not None and previous.get("fingerprint", request_hash) != request_hash:
                    handler.json(422, {"error": {"reason": "idempotency_key_reused"}})
                    return True
                if previous is None:
                    with ProjectLock(service.state, service.user_id):
                        if identity not in index.read():
                            previous = dict(status=404, body={"error": {"reason": "not_found"}}, fingerprint=request_hash)
                            messages.atomic_json(saved, previous)
                        else:
                            with events.write_transaction([index.path, saved]):
                                row = index.set_hidden_locked(identity, action == "hide")
                                row = dict(row, origin=re.sub(r"(://)[^/@\s]+@", r"\1", row["origin"]))
                                previous = dict(status=200, body={"project": row}, fingerprint=request_hash)
                                messages.atomic_json(saved, previous)
            handler.json(previous["status"], service.redact(previous["body"]))
        except (ValueError, TypeError, UnicodeError) as error:
            if str(error).startswith("event_write_failed:"):
                handler.json(503, {"error": {"reason": "project_write_failed"}})
            elif str(error).startswith("project index busy"):
                handler.json(409, {"error": {"reason": "project_busy"}})
            else:
                handler.json(400, {"error": {"reason": "invalid_project_request"}})
        except OSError:
            handler.json(503, {"error": {"reason": "project_write_failed"}})
        return True
