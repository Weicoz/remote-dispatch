"""Project summaries and project HTTP routes, separate from the serve transport."""
import hashlib
import json
import re
import threading
import uuid
from urllib.parse import unquote, urlsplit

from datetime import datetime, timedelta, timezone

from . import rd_events as events
from . import rd_messages as messages
from .rd_projects import ProjectIndex, ProjectLock
from .rd_serve_history import instant, records as history_records


def urgency(card):
    if card["execution_state"] in ("starting", "running"):
        return "running", 5
    if card.get("stopped_by") and card["review_state"] == "pending":
        return "pending_review", 4
    if card["review_state"] in ("accepted", "voided"):
        return "idle", 6
    if card["outcome"] == "blocked" and card["delivery_state"] == "pending":
        return "needs_reply", 0
    if card["execution_state"] in ("dead", "missing", "unreachable"):
        return "interrupted", 1
    if card["delivery_state"] == "pending":
        return ("failed_pending", 2) if card["outcome"] == "failed" or card.get("executor_exit") not in (None, 0) else ("pending_receipt", 3)
    if card["delivery_state"] == "received" and card["review_state"] == "pending":
        return "pending_review", 4
    return "idle", 6


class ProjectRoutes:
    def __init__(self, service):
        self.service = service
        self.pages = {}
        self.page_lock = threading.Lock()

    def records(self):
        return history_records(self.service.state, self.service.records(), self.service.user_id)

    def card(self, rec, query):
        card = self.service.card(rec, query)
        receipt = messages.read_json(self.service.state / "results" / rec["job"] / f'r{rec["rounds"]}.json')
        if receipt:
            card["execution_state"] = receipt.get("execution_state", card["execution_state"])
        elif rec.get("archived"):
            card["execution_state"] = "done"
        return card

    @staticmethod
    def summary(row, session_only=False):
        return dict(row, session_only=session_only, sessions=[], needs_attention_count=0,
                    in_progress_count=0, hosts={}, week_dispatch_count=0,
                    last_accepted_at=None, last_harness=None, recent_event=None)

    def accepted_counts(self, rec, query):
        counts = {}
        for number in range(1, rec["rounds"] + 1):
            if query.review(rec, number)["review_state"] != "accepted":
                continue
            receipt = messages.read_json(self.service.state / "results" / rec["job"] / f"r{number}.json")
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

    def projects(self, device=None):
        service = self.service
        query = service.query()
        sessions_allowed = service.sessions.allowed(device)
        rows = {row["id"]: self.summary(row)
                for row in query.projects(service.user_id)}
        now = datetime.now(timezone.utc).astimezone()
        week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        visible_events = [event for event in events.read_after(service.state, 0) if service.visible(event)
                          and (sessions_allowed or not event["type"].startswith("session."))]
        job_updates = {}
        for event in visible_events:
            if event["data"].get("job"):
                job_updates[event["data"]["job"]] = event["time"]
        ranked, last_dispatch, job_projects, accepted_counts = {}, {}, {}, {}
        for rec in self.records():
            card = self.card(rec, query)
            identity = card["project_id"]
            job_projects[rec["job"]] = identity
            row = rows.setdefault(identity, self.summary(dict(id=identity, name=card["project"], hidden=False,
                      origin=rec.get("origin") or "", dispatch_count=0, first_dispatch_at=None,
                      last_dispatch_at=None)))
            state, priority = urgency(card)
            row["needs_attention_count"] += int(priority < 5)
            row["in_progress_count"] += int(priority == 5)
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
                row["hosts"][card["host"]] = dict(state=state, job=card["job"], updated_at=updated)
        if sessions_allowed:
            try:
                sessions = service.sessions.discover()
            except OSError:
                sessions = []
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
                row["hosts"].setdefault(host["name"], dict(state="idle", job=None, updated_at=None))
            for identity, host in accepted_counts:
                if identity == row["id"]:
                    row["hosts"].setdefault(host, dict(state="idle", job=None, updated_at=None))
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
            return 200, {"projects": self.projects(device)}
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
                query = service.query()
                records = self.records() if state == "history" else service.records()
                rows = []
                for rec in records:
                    card = self.card(rec, query)
                    if project and card["project_id"] != project:
                        continue
                    historical = bool(rec.get("archived")) or urgency(card)[1] == 6
                    if state == "history" and not historical or state == "active" and historical:
                        continue
                    rows.append(dict(card, created_at=rec.get("created")))
                if state != "history":
                    return {"jobs": rows}
                rows.sort(key=lambda row: (instant(row["created_at"]) or datetime.min.replace(tzinfo=timezone.utc), row["job"]), reverse=True)
                identity, offset = uuid.uuid4().hex, 0
                if len(rows) > limit:
                    self.pages[identity] = (project, rows, limit)
            end = offset + limit
            return dict(jobs=rows[offset:end], next_cursor=f"{identity}.{end}" if end < len(rows) else None)

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
                previous = messages.read_json(saved)
                if previous is None:
                    with ProjectLock(service.state, service.user_id):
                        if identity not in index.read():
                            previous = dict(status=404, body={"error": {"reason": "not_found"}})
                            messages.atomic_json(saved, previous)
                        else:
                            with events.write_transaction([index.path, saved]):
                                row = index.set_hidden_locked(identity, action == "hide")
                                row = dict(row, origin=re.sub(r"(://)[^/@\s]+@", r"\1", row["origin"]))
                                previous = dict(status=200, body={"project": row})
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
