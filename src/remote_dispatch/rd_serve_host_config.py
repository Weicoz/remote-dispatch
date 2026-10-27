"""Host configuration routes with a deliberately separate, secret-free projection."""
import configparser
from copy import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid
from urllib.parse import unquote, urlsplit

from . import cli
from . import rd_messages as messages
from .rd_idempotency import fingerprint
from .rd_lock import JobBusy, JobLock


SESSION_ACCESS = "/v1/config/session-access"
COLLECTION = "/v1/config/hosts"
ROUTE = re.compile(r"/v1/config/hosts(?:/([A-Za-z0-9._-]+))?")
PUBLIC_FIELDS = set(cli.DEFAULTS) - {"env_file", "codex_auth_file", "dsh_auth_file", "agy_auth_file", "ssh_opts"}
PUBLIC_FIELDS.add("ssh")


class InvalidField(ValueError):
    def __init__(self, field, reason="invalid_host_field"):
        self.field, self.reason = field, reason


def validate(values):
    for field in ("ssh", "workdir"):
        if not values.get(field, "").strip():
            raise InvalidField(field, "host_field_required")
    for field, allowed in (("sync", ("origin", "direct")),
                           ("platform", ("posix", "macos", "linux", "windows")),
                           ("enabled", ("1", "0", "true", "false", "yes", "no")),
                           ("launcher", ("nohup", "tmux"))):
        value = values[field].lower() if field == "enabled" else values[field]
        if value not in allowed:
            raise InvalidField(field)
    if values["ssh"] == "local":
        if values["platform"] == "windows":
            raise InvalidField("platform")
        if not values["workdir"].startswith(("/", "~", "$HOME")):
            raise InvalidField("workdir")
    for field in cli.DEFAULTS:
        if field.startswith(("max_", "min_", "warn_", "swap_")) or field == "timeout":
            try:
                number = float(values[field])
                if field in ("max_heavy", "timeout"):
                    int(values[field])  # Match the launcher's integer-string parser.
            except (ValueError, OverflowError):
                raise InvalidField(field) from None
            if (not math.isfinite(number) or number < 0
                    or field.endswith("_pct") and number > 100
                    or field in ("max_heavy", "timeout") and number < 1):
                raise InvalidField(field)


def parse(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    return cp


def effective(cp, name):
    values = dict(cli.DEFAULTS)
    if cp.has_section("defaults"):
        values.update(cp["defaults"])
    values.update(cp["host:" + name])
    return values


def edit(text, name, fields, *, delete=False, section_name=None):
    """Follow ConfigParser's indentation rules, retaining untouched raw lines."""
    newline = "\r\n" if "\r\n" in text else "\n"
    target = section_name or "host:" + name
    section, option_indent = None, None
    found, dropping = False, False
    result, pending = [], dict(fields)

    def append_pending():
        if pending and result and not result[-1].endswith("\n"):
            result[-1] += newline
        result.extend(f"{key} = {value}{newline}" for key, value in pending.items() if value is not None)
        pending.clear()

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            result.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        # Continuations take precedence over both section and option syntax.
        if option_indent is not None and indent > option_indent:
            if not dropping:
                result.append(line)
            continue
        header = configparser.ConfigParser.SECTCRE.match(line.lstrip())
        if header:
            if section == target:
                append_pending()
            section, option_indent, dropping = header.group("header"), None, False
            if section == target:
                found = True
            if section != target or not delete:
                result.append(line)
            else:
                annotation = line.lstrip()[header.end():].strip()
                if annotation.startswith((";", "#")):
                    result.append(annotation + newline)
            continue
        option = configparser.ConfigParser.OPTCRE.match(line.lstrip())
        option_indent = indent if option else None
        field = option.group("option").rstrip().lower() if option else None
        dropping = section == target and (delete or field in pending)
        if dropping:
            if not delete:
                value = pending.pop(field)
                if value is not None:
                    prefix = line[:indent] + option.group("option") + option.group("vi")
                    result.append(prefix + value + newline)
        else:
            result.append(line)
    if not found:
        if result and not result[-1].endswith("\n"):
            result[-1] += newline
        result.append("[" + target + "]" + newline)
    if not found or section == target:
        append_pending()
    return "".join(result)


class HostConfigRoutes:
    def __init__(self, service):
        self.service = service
        self.path = Path(cli.CONFIG).absolute()

    def load(self):
        if any(path.is_symlink() for path in (self.path, *self.path.parents)):
            raise OSError("invalid_config_path")
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raw = b""
        return raw, parse(raw.decode("utf-8"))

    def project(self, cp, name):
        values = effective(cp, name)
        self.remember_secrets(cp)
        public = {key: self.service.redact(value) for key, value in values.items()
                  if key in PUBLIC_FIELDS and key != "ssh"}
        connection = values.get("ssh", "")
        public["ssh"] = self.service.redact(connection, sensitive=self.service.sensitive - {connection})
        public["ssh"] = re.sub(r"(://)[^/@\s]+@", r"\1[redacted]@", public["ssh"])
        return dict(name=name, values=public,
                    protected_fields=[dict(field=key, is_set=bool(value)) for key, value in values.items()
                                      if key not in PUBLIC_FIELDS])

    def remember_secrets(self, cp):
        secrets = {value for section in cp.sections() if section.startswith("host:")
                   for field, value in effective(cp, section[5:]).items()
                   if field not in PUBLIC_FIELDS and value}
        # Assign a new set: concurrent redactors may be iterating the old one.
        self.service.sensitive = self.service.sensitive | secrets

    def get(self, handler):
        path = unquote(urlsplit(handler.path).path)
        if path == SESSION_ACCESS:
            handler.json(200, {"enabled": self.service.sessions.enabled})
            return True
        if path != COLLECTION:
            return False
        try:
            _, cp = self.load()
            handler.json(200, {"hosts": [self.project(cp, section[5:]) for section in cp.sections()
                                         if section.startswith("host:")]})
        except (OSError, UnicodeError, configparser.Error):
            handler.json(503, {"error": {"reason": "host_config_unavailable"}})
        return True

    def handle(self, handler):
        path = unquote(urlsplit(handler.path).path)
        match = ROUTE.fullmatch(path)
        if not match and path != SESSION_ACCESS:
            return False
        try:
            key = handler.headers.get("Idempotency-Key", "")
            if not key.strip() or len(key) > 255 or len(handler.headers.get_all("Idempotency-Key", [])) != 1:
                handler.json(400, {"error": {"reason": "idempotency_key_required"}})
                return True
            length = int(handler.headers.get("Content-Length", "0"))
            bare_delete = handler.command == "DELETE" and length == 0
            if (handler.headers.get("Transfer-Encoding") or not 0 <= length <= 8 * 1024 * 1024
                    or not bare_delete and (not length or handler.headers.get_content_type() != "application/json")):
                raise ValueError("invalid_request")
            body = {} if bare_delete else json.loads(handler.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("invalid_request")
            handler.json(*self.write(handler.command, path, match.group(1) if match else None, body, key,
                                     getattr(handler, "device_id", None)))
        except InvalidField as error:
            detail = dict(reason=error.reason, field=error.field)
            if error.field == "max_heavy":
                detail["text"] = "并发上限必须是大于等于 1 的整数。"
            handler.json(400, {"error": detail})
        except (ValueError, TypeError, UnicodeError):
            handler.json(400, {"error": {"reason": "invalid_host_request"}})
        except (OSError, configparser.Error):
            handler.json(503, {"error": {"reason": "host_config_unavailable"}})
        except JobBusy:
            handler.json(409, {"error": {"reason": "host_config_busy"}})
        return True

    def write(self, method, path, name, body, key, device):
        service = self.service
        digest = hashlib.sha256(json.dumps([service.user_id, device, key]).encode()).hexdigest()
        saved = service.state / "serve-host-config-requests" / (digest + ".json")
        request_hash = fingerprint(method, path, body)
        lock = JobLock(service.state, "host-config")
        lock.path = self.path.with_name(self.path.name + ".lock")
        with service.writer, lock:
            previous = messages.read_json(saved)
            if previous is not None:
                if previous["fingerprint"] != request_hash:
                    return 422, {"error": {"reason": "idempotency_key_reused"}}
                return previous["status"], previous["body"]
            if service.stop.is_set():
                raise OSError("serve_stopping")
            raw, cp = self.load()
            setting = path == SESSION_ACCESS
            if setting:
                if method != "PUT":
                    return 405, {"error": {"reason": "method_not_allowed"}}
                if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
                    return 400, {"error": {"reason": "invalid_request"}}
                text = edit(raw.decode("utf-8"), "", {"session_access": str(body["enabled"]).lower()},
                            section_name="serve")
                updated = parse(text)
                create = delete = False
            else:
                create = method == "POST" and name is None
                delete = method == "DELETE" and name is not None
                if not create and not delete and not (method == "PUT" and name is not None):
                    return 405, {"error": {"reason": "method_not_allowed"}}
                name = body.get("name") if create else name
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name):
                    raise InvalidField("name")
                if create and cp.has_section("host:" + name):
                    return 409, {"error": {"reason": "host_exists", "field": "name"}}
                if not create and not cp.has_section("host:" + name):
                    return 404, {"error": {"reason": "not_found"}}
                if not create and "name" in body and body["name"] != name:
                    raise InvalidField("name")
                if delete:
                    if body:
                        raise ValueError("invalid_delete_body")
                    # Include every local registry record, not just the current
                    # user's display list. Unknown/offline execution is unsafe.
                    records = [rec for rec in cli.all_jobs() if rec.get("host") == name]
                    states = service.query().states(records)
                    running = sorted({rec["job"] for rec in records
                                      if states.get(rec["job"], {}).get("state") not in ("done", "dead", "missing")})
                    if running:
                        return 409, {"error": {"reason": "host_has_active_dispatches",
                                               "running_count": len(running), "running_job_ids": running[:20]}}
                fields = {key: value for key, value in body.items() if key != "name"}
                for field, value in fields.items():
                    if not re.fullmatch(r"[a-z][a-z0-9_]*", field):
                        raise ValueError("invalid_field")
                    if value is not None and (not isinstance(value, str) or any(c in value for c in "\n\r\x00")):
                        raise InvalidField(field)
                text = edit(raw.decode("utf-8"), name, fields, delete=delete)
                updated = parse(text)
                if not delete:
                    validate(effective(updated, name))
            pending = dict(status=503, body={"error": {"reason": "host_config_outcome_unknown"}}, fingerprint=request_hash)
            messages.atomic_json(saved, pending)
            self.remember_secrets(updated)
            self.replace(raw, text.encode("utf-8"))
            # In-flight readers retain their parser; do not change the parser
            # while Config.hosts() may be traversing its section names.
            refreshed = copy(service.config)
            refreshed.cp = updated
            service.config = refreshed
            service.wake.set()
            reply = (dict(enabled=body["enabled"]) if setting else
                     dict(deleted=name) if delete else dict(host=self.project(updated, name)))
            status = 201 if create else 200
            messages.atomic_json(saved, dict(status=status, body=reply, fingerprint=request_hash))
            return status, reply

    def replace(self, raw, updated):
        # Backup is durable before the config replacement. Never overwrite an
        # earlier backup; secrets in both files remain owner-readable only.
        backup = self.path.with_name(self.path.name + ".bak." + uuid.uuid4().hex)
        backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(backup_fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
