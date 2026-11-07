"""Public HTTP writes reuse CLI commands, locks and events in isolated children."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import threading
from urllib.parse import unquote, urlsplit

from . import cli
from . import rd_line as workline
from . import rd_messages as messages
from .rd_idempotency import fingerprint


ROUTE = re.compile(r"/v1/jobs/([A-Za-z0-9._-]+)/(?:rounds/([1-9][0-9]*)/(ack|review)|(stop|resume))")
CONFLICTS = {"already_reviewed", "round_in_progress", "review_not_allowed"}
UNAVAILABLE = {"receipt_host_unavailable"}  # 目标机暂时连不上，可重试


def error(status, reason, **fields):
    return status, {"error": dict(reason=reason, **fields)}


def text(body, name, *, required=False):
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or "\x00" in value:
        raise ValueError("invalid_request")
    return value


class Writes:
    def __init__(self, service):
        self.service = service
        # Serialize lookup, execution and durable result publication, including
        # requests that arrive while the first invocation is still running.
        self.lock = threading.Lock()

    def handle(self, handler):
        from .rd_serve import MAX_REQUEST_BYTES
        route = ROUTE.fullmatch(unquote(urlsplit(handler.path).path))
        if not route and handler.path != "/v1/runs":
            return handler.json(*error(404, "not_found"))
        try:
            key = handler.headers.get("Idempotency-Key", "")
            if (len(handler.headers.get_all("Idempotency-Key", [])) > 1
                    or handler.headers.get("Transfer-Encoding")
                    or handler.headers.get_content_type() != "application/json"):
                raise ValueError("invalid_request")
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("invalid_request")
            body = json.loads(handler.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("invalid_request")
            if not key.strip():
                return handler.json(*error(400, "idempotency_key_required"))
            status, reply = self.write(key, route, body, device_id=getattr(handler, "device_id", None),
                                       method=handler.command)
            handler.json(status, reply)
        except (ValueError, TypeError, KeyError, UnicodeError):
            handler.json(*error(400, "invalid_request"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, subprocess.SubprocessError):
            handler.json(*error(503, "write_unavailable"))

    def write(self, key, route, body, *, device_id=None, method="POST"):
        service = self.service
        scope = [service.user_id, device_id, key] if device_id else [service.user_id, key]
        digest = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
        path = service.state / "serve-writes" / (digest + ".json")
        request_hash = fingerprint(method, route.group(0) if route else "/v1/runs", body)
        # A slow collector for one job must not occupy the shared write queue.
        guard = service.job_guard(route.group(1)) if route else nullcontext()
        with guard, self.lock:
            saved = messages.read_json(path)
            if saved is not None:
                if saved.get("fingerprint") != request_hash:
                    return error(422, "idempotency_key_reused")
                return saved["status"], saved["body"]
            if service.stop.is_set():
                return error(503, "write_unavailable")
            # Publish an indeterminate result BEFORE invoking the CLI. If the
            # process dies between side effects and response publication, never
            # start the command again merely because a client retried its key.
            pending = error(503, "write_outcome_unknown")
            messages.atomic_json(path, dict(status=pending[0], body=pending[1], fingerprint=request_hash), limit=None)
            try:
                result = self.command(route, body)
            except (ValueError, TypeError, KeyError):
                result = error(400, "invalid_request")
            except (OSError, subprocess.SubprocessError):
                result = pending
            status, reply = result
            token = reply["suggestion"].get("token") if isinstance(reply.get("suggestion"), dict) else None
            reply = service.redact(reply)
            if token and "suggestion" in reply:
                # 令牌是单次、限时、绑定发起会话的接受凭据，App 必须能拿到；其余字段照常脱敏。
                reply["suggestion"]["token"] = token
            try:
                messages.atomic_json(path, dict(status=status, body=reply, fingerprint=request_hash), limit=None)
            except OSError:
                return pending
            return status, reply

    @staticmethod
    def line_arguments(body):
        """解析 /v1/runs 的 plan_item、after、suggestion；返回 (plan 参数, 接受/拒绝参数或 None)。"""
        item = text(body, "plan_item")
        after = body.get("after", [])
        decision = body.get("suggestion")
        if not isinstance(after, list) or any(not isinstance(value, str) or not value or "\x00" in value for value in after):
            raise ValueError("invalid_request")
        if item is None:
            if after or decision is not None:
                raise ValueError("invalid_request")
            return [], None
        args = ["--plan-item=" + item] + ["--after=" + value for value in after]
        if decision is None:
            return args, None
        if not isinstance(decision, dict):
            raise ValueError("invalid_request")
        action = text(decision, "action", required=True)
        if action == "decline":
            return args, ["--no-line"]
        if action != "accept":
            raise ValueError("invalid_request")
        line, token = text(decision, "line", required=True), text(decision, "token", required=True)
        flags = ["--line=" + line, "--accept=" + token]
        chain = decision.get("accept_chain", False)
        if not isinstance(chain, bool):
            raise ValueError("invalid_request")
        return args, flags + (["--accept-chain"] if chain else [])

    def suggest(self, run_argv, cwd, stdin, initiator, origin):
        """用与 run 相同的参数执行只读的 suggest --json；有建议返回其结构，否则返回 None（照常派单）。"""
        argv = ["suggest", "--json"] + run_argv[1:]
        request = dict(argv=argv, cwd=cwd, config=str(Path(cli.CONFIG).resolve()), user_id=self.service.user_id,
                       initiator_session=initiator, origin=origin, stdin=stdin)
        result = self.service.execute(request)
        if result["exit_code"]:
            return None
        try:
            suggestion = json.loads(result["stdout"])
        except ValueError:
            return None
        if not isinstance(suggestion, dict) or not suggestion.get("suggest"):
            return None
        return suggestion

    def command(self, route, body):
        service = self.service
        job, number, action, simple = route.groups() if route else (None, None, None, None)
        action = action or simple
        rec = None
        if job:
            messages.job_id(job)
            rec = next((saved for saved in service.records() if saved["job"] == job), None)
            if rec is None or rec.get("user_id") not in (None, "", service.user_id):
                return error(404, "not_found")
        argv = [action, job] if job else []
        if number:
            argv += ["--round", number]
        stdin = ""
        cwd = str(Path(cli.CONFIG).resolve().parent)
        initiator = rec.get("initiator_session") if rec else None
        origin = {}
        if action == "ack":
            argv += ["--receipt-id=" + text(body, "receipt_id", required=True),
                     "--acked-by=" + text(body, "acked_by", required=True)]
        elif action == "review":
            reviewer = text(body, "reviewed_by", required=True)
            state = text(body, "review_state", required=True)
            argv += ["--reviewed-by=" + reviewer]
            if state == "accepted":
                argv += ["--accept"]
            elif state == "voided":
                argv += ["--void"]
            elif state == "rejected":
                note = text(body, "review_note") or ""
                argv += ["--reject=" + note]
            else:
                raise ValueError("invalid_request")
        elif action == "stop":
            argv += ["--stopped-by=" + text(body, "stopped_by", required=True)]
        elif action == "resume":
            stdin = text(body, "prompt", required=True)
            argv += ["--prompt-file=-"]
        elif route is None:
            project = text(body, "project", required=True)
            index = cli.projects.ProjectIndex(service.state, service.user_id).read()
            names = {row["name"] for row in index.values()}
            names.update(section.split(":", 1)[1] for section in service.config.cp.sections()
                         if section.startswith("project:"))
            # The dispatch manifest survives clean, unlike the job registry.
            sources = {}
            for path in sorted((service.state / "results").glob("*/dispatch.json")):
                saved = messages.read_json(path)
                if saved and saved.get("user_id") in (None, "", service.user_id):
                    sources[saved["job"]] = saved
            for saved in service.records():
                sources.setdefault(saved["job"], {}).update(saved)
            names.update(saved["project"] for saved in sources.values() if saved.get("project"))
            candidates = []
            for saved in sources.values():
                fields = cli.projects.project_fields(saved, service.user_id, names)
                if project in (fields["project"], fields["project_id"]):
                    candidates.append(dict(saved, **fields))
            if not candidates:
                return error(422, "project_unavailable")
            identities = {saved["project_id"] for saved in candidates}
            if len(identities) != 1:
                return error(422, "project_ambiguous")
            previous = max(candidates, key=lambda saved: (saved.get("created") or "", saved["job"]))
            if not previous.get("repo") or not Path(previous["repo"]).is_dir():
                return error(422, "project_unavailable")
            cwd = previous["repo"]
            _, current_origin = cli.project_name(cwd)
            if cli.projects.project_id(previous["project"], current_origin) != previous["project_id"]:
                return error(422, "project_unavailable")
            harness = text(body, "harness") or previous.get("agent") or "codex"
            stdin = text(body, "prompt", required=True)
            initiator = text(body, "initiator_session") or "app"
            argv = ["run", "--project=" + previous["project"],
                    "--agent=" + harness, "--prompt-file=-"]
            for name in ("host", "model"):
                value = text(body, name)
                if value is not None:
                    argv += ["--" + name + "=" + value]
            origin = dict(initiator_harness="app", initiator_host=socket.gethostname(),
                          initiator_herdr=dict(session=None, workspace_id=None, pane_id=None),
                          initiator_hc=None, initiator_primary=None)
            plan_args, decision = self.line_arguments(body)
            argv += plan_args
            if plan_args and decision is None:
                # 第一步：先问建议；有建议就返回给 App 确认，不派单。
                suggestion = self.suggest(argv, cwd, stdin, initiator, origin)
                if suggestion is not None:
                    return 200, dict(suggestion=suggestion)
            elif decision:
                argv += decision
        else:
            raise ValueError("invalid_request")
        if action in ("review", "resume"):
            for name in ("host", "model", "harness"):
                value = text(body, name)
                if value is not None:
                    argv += ["--" + name + "=" + value]
        request = dict(argv=argv, cwd=cwd,
                       config=str(Path(cli.CONFIG).resolve()), user_id=service.user_id,
                       initiator_session=initiator, origin=origin, stdin=stdin)
        result = service.execute(request)
        if result["exit_code"]:
            message = re.sub(r"^rdispatch: ", "", result["stderr"].strip())
            # Match the existing 600-character display-summary convention; raw
            # CLI errors can include arbitrarily long user-supplied arguments.
            truncated = len(message) > 600
            message = message[:600]
            match = re.match(r"([a-z][a-z_]+):", message)
            reason = match[1] if match else "command_failed"
            status = 409 if reason in CONFLICTS else 503 if reason in UNAVAILABLE else 422
            fields = dict(message=message, exit_code=result["exit_code"])
            if truncated:
                fields["message_truncated"] = True
            if reason == "already_reviewed":
                review = service.query().review(rec, int(number))
                fields.update({key: review[key] for key in ("reviewed_by", "reviewed_at", "review_state")})
            return error(status, reason, **fields)
        if route is None:
            match = re.search(r"started\. job id: ([A-Za-z0-9._-]+)", result["stdout"])
            if match is None:
                # 接受建议后接线：新工作项作为前序派单的新轮次，不创建新 job。
                wired = workline.parse_wired_marker(result["stdout"])
                current = wired and next((saved for saved in service.records() if saved["job"] == wired[0]), None)
                if not current:
                    return error(503, "write_outcome_unknown")
                return 201, dict(job=current["job"], round=current["rounds"], line=wired[1])
            return 201, dict(job=match[1], round=1)
        if action == "review":
            return 200, dict(job=job, round=int(number), review=service.query().review(rec, int(number)))
        if action in ("stop", "resume"):
            current = next(saved for saved in service.records() if saved["job"] == job)
            return 200, dict(job=job, round=current["rounds"])
        return 200, dict(job=job, round=int(number),
                         acknowledgement=service.query().acknowledgement(rec, service.query().result(rec, int(number))))
