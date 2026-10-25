"""Per-user project history, independent of the removable job registry."""
import hashlib
import json
from pathlib import Path
import re

from . import rd_messages as messages
from . import rd_profiles as profiles
from .rd_lock import JobBusy, JobLock


class InvalidIndex(ValueError):
    def __init__(self):
        super().__init__("invalid project index; run rdispatch project rebuild")


class ProjectLock(JobLock):
    """Use a separate lock namespace, never the job's lock inode."""
    def __init__(self, state, user_id):
        super().__init__(state, profiles.user_id(user_id))
        self.path = Path(state).resolve() / "locks" / "projects" / (self.job + ".lock")

    def acquire(self):
        try:
            return super().acquire()
        except JobBusy:
            raise ValueError("project index busy; retry after the current operation finishes") from None


def project_id(name, origin):
    body = json.dumps([name, origin], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]


def project_fields(rec, user_id, known_names):
    """Infer legacy membership without mutating the record or its source file."""
    name = rec.get("project")
    if not name:
        job = rec["job"]
        prefix = (rec.get("user_id") or user_id or "") + "-"
        if job.startswith(prefix):
            job = job[len(prefix):]
        matches = [name for name in known_names if job.startswith(name + "-")]
        name = max(matches, key=len) if matches else "未归属"
    origin = rec.get("origin") or rec.get("origin_url") or ""
    return dict(project=name, origin=origin, project_id=project_id(name, origin))


class ProjectIndex:
    def __init__(self, state, user_id):
        self.state = Path(state).resolve()
        self.user_id = profiles.user_id(user_id)
        self.path = self.state / "projects" / (self.user_id + ".json")

    def read(self):
        """Read without creating directories, acquiring locks or writing caches."""
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("project index path must not be a symbolic link")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (UnicodeError, ValueError):
            raise InvalidIndex() from None
        if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("projects"), dict):
            raise InvalidIndex()
        for identity, row in value["projects"].items():
            if (not isinstance(row, dict) or row.get("id") != identity
                or not isinstance(row.get("name"), str) or not row["name"]
                or not isinstance(row.get("origin"), str)
                or identity != project_id(row["name"], row["origin"])
                or type(row.get("dispatch_count")) is not int or row["dispatch_count"] < 0
                or type(row.get("hidden")) is not bool
                or any(key not in row or (row[key] is not None and not isinstance(row[key], str))
                       for key in ("first_dispatch_at", "last_dispatch_at"))):
                raise InvalidIndex()
        return value["projects"]

    def write(self, projects):
        messages.atomic_json(self.path, dict(schema=1, projects=projects), limit=None)

    def set_hidden(self, identity, hidden):
        with ProjectLock(self.state, self.user_id):
            projects = self.read()
            if identity not in projects:
                raise ValueError(f"unknown project '{identity}'; use a project ID from status --json")
            projects[identity]["hidden"] = hidden
            self.write(projects)
            return projects[identity]

    def register(self, rec):
        """Register a newly created dispatch exactly once, before target setup."""
        with ProjectLock(self.state, self.user_id):
            projects = self.read()
            job = messages.job_id(rec["job"])
            # A small dispatch manifest survives even when clean discards target
            # reports or the dispatch never reached its first receipt.
            manifest = self.state / "results" / job / "dispatch.json"
            if manifest.exists():
                return
            messages.atomic_json(self.state / "jobs" / (job + ".json"), rec, limit=None)
            messages.atomic_json(manifest, {key: rec.get(key) for key in
                                 ("job", "user_id", "project", "origin", "created")})
            identity = project_id(rec["project"], rec.get("origin") or "")
            row = projects.setdefault(identity, dict(id=identity, name=rec["project"],
                                      origin=rec.get("origin") or "", first_dispatch_at=rec["created"],
                                      last_dispatch_at=rec["created"], dispatch_count=0, hidden=False))
            row["dispatch_count"] += 1
            row["first_dispatch_at"] = min(row["first_dispatch_at"] or rec["created"], rec["created"])
            row["last_dispatch_at"] = max(row["last_dispatch_at"] or rec["created"], rec["created"])
            row["hidden"] = False
            self.write(projects)

    def rebuild(self, known_names=()):
        """Recount each dispatch once from jobs and retained receipt history."""
        with ProjectLock(self.state, self.user_id):
            try:
                previous = self.read()
            except InvalidIndex:
                # Hidden flags cannot be recovered from an unreadable index.
                previous = {}
            records = {}
            # More authoritative dispatch metadata overrides receipt fields.
            paths = (sorted((self.state / "results").glob("*/r[0-9]*.json"))
                     + sorted((self.state / "results").glob("*/dispatch.json"))
                     + sorted((self.state / "jobs").glob("*.json")))
            for path in paths:
                if path.name.endswith((".ack.json", ".review.json")):
                    continue
                rec = messages.read_json(path)
                if rec and rec.get("job"):
                    records.setdefault(rec["job"], {}).update(rec)
            records = [rec for rec in records.values() if not rec.get("user_id") or rec["user_id"] == self.user_id]
            names = set(known_names) | {row["name"] for row in previous.values()}
            names.update(rec["project"] for rec in records if rec.get("project"))
            rebuilt = {}
            for rec in records:
                fields = project_fields(rec, self.user_id, names)
                name, origin, identity = fields["project"], fields["origin"], fields["project_id"]
                created = rec.get("created")
                if not created:
                    stamp = re.search(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-\d+)?$", rec["job"])
                    if stamp:
                        created = "{}-{}-{} {}:{}:{}".format(*stamp.groups())
                row = rebuilt.setdefault(identity, dict(id=identity, name=name, origin=origin,
                                         first_dispatch_at=None, last_dispatch_at=None, dispatch_count=0,
                                         hidden=previous.get(identity, {}).get("hidden", False)))
                row["dispatch_count"] += 1
                if created:
                    row["first_dispatch_at"] = min(row["first_dispatch_at"] or created, created)
                    row["last_dispatch_at"] = max(row["last_dispatch_at"] or created, created)
            self.write(rebuilt)
            return sorted(rebuilt.values(), key=lambda row: (row["name"], row["id"]))
