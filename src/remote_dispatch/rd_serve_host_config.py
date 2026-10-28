"""Host configuration routes with a deliberately separate, secret-free projection."""
import configparser
from copy import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from . import cli
from . import rd_messages as messages
from .rd_host_config import (
    COLLECTION, PUBLIC_FIELDS, ROUTE, SESSION_ACCESS, InvalidField, active_job_ids,
    config_digest, effective, host_candidate, load as load_config, replace as replace_config,
    session_access_candidate, validate, validate_field, validate_name,
)
from .rd_idempotency import fingerprint
from .rd_lock import JobBusy, JobLock


class HostConfigRoutes:
    def __init__(self, service):
        self.service = service
        self.path = Path(cli.CONFIG).absolute()

    def same_config(self, handler):
        requested = handler.headers.get("X-Rdispatch-Config-SHA256")
        return requested is None or requested == config_digest(self.path)

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
        if path not in (SESSION_ACCESS, COLLECTION):
            return False
        if not self.same_config(handler):
            handler.json(400, {"error": {"reason": "cli_config_mismatch"}})
            return True
        if path == SESSION_ACCESS:
            handler.json(200, {"enabled": self.service.sessions.enabled})
            return True
        try:
            _, cp = load_config(self.path)
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
        if not self.same_config(handler):
            handler.json(400, {"error": {"reason": "cli_config_mismatch"}})
            return True
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
            raw, cp = load_config(self.path)
            setting = path == SESSION_ACCESS
            if setting:
                if method != "PUT":
                    return 405, {"error": {"reason": "method_not_allowed"}}
                if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
                    return 400, {"error": {"reason": "invalid_request"}}
                updated, encoded = session_access_candidate(raw, body["enabled"])
                create = delete = False
            else:
                create = method == "POST" and name is None
                delete = method == "DELETE" and name is not None
                if not create and not delete and not (method == "PUT" and name is not None):
                    return 405, {"error": {"reason": "method_not_allowed"}}
                name = body.get("name") if create else name
                validate_name(name)
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
                    running = active_job_ids(records, service.query().states(records))
                    if running:
                        return 409, {"error": {"reason": "host_has_active_dispatches",
                                               "running_count": len(running), "running_job_ids": running[:20]}}
                fields = {key: value for key, value in body.items() if key != "name"}
                for field, value in fields.items():
                    validate_field(field, value)
                updated, encoded = host_candidate(raw, name, fields, delete=delete)
                if not delete:
                    validate(effective(updated, name))
            pending = dict(status=503, body={"error": {"reason": "host_config_outcome_unknown"}}, fingerprint=request_hash)
            messages.atomic_json(saved, pending)
            self.remember_secrets(updated)
            replace_config(self.path, raw, encoded)
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
