"""任务进度与结果回执；只记录本轮工作，不调度下一轮。"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

MAX_BYTES = 65536
OUTCOMES = ("completed", "partial", "blocked", "failed")
CHECKS = ("passed", "failed", "partial", "not_run", "not_reported")


def artifact_index(root, paths):
    """Hash declared regular artifacts only; never follow links or read outside the task."""
    root = Path(root).absolute()
    result = []
    for name in paths:
        identifier(name, "产物路径")
        path = Path(name)
        path = path if path.is_absolute() else root / path
        row = {"path": name, "state": "unverified"}
        try:
            path.relative_to(root)
            if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("nonphysical artifact")
            if not path.exists():
                row["state"] = "missing"
            elif path.is_file():
                before = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError("artifact changed")
                row.update(state="present", sha256=digest.hexdigest(), bytes=after.st_size)
            elif path.is_dir():
                row.update(state="present", kind="directory")
        except (OSError, ValueError):
            pass
        result.append(row)
    return result


def validate_evidence(value):
    if not isinstance(value, dict) or set(value) - {"source_fingerprint", "checks", "artifacts", "cleanup_confirmed"}:
        raise ValueError("证据索引只接受来源指纹、检查、产物与回收状态。")
    if "source_fingerprint" in value and not re.fullmatch(r"[0-9a-f]{64}", str(value["source_fingerprint"])):
        raise ValueError("来源指纹必须是SHA256。")
    if "cleanup_confirmed" in value and type(value["cleanup_confirmed"]) is not bool:
        raise ValueError("回收状态必须是布尔值。")
    if any(not isinstance(value.get(key, []), list) for key in ("checks", "artifacts")):
        raise ValueError("检查和产物必须是列表。")
    for row in value.get("checks", []):
        if not isinstance(row, dict) or set(row) - {"name", "status", "artifact"} or row.get("status") not in CHECKS:
            raise ValueError("证据检查格式无效。")
        identifier(row.get("name"), "检查名称")
        if "artifact" in row:
            identifier(row["artifact"], "检查原件路径")
    for row in value.get("artifacts", []):
        if (not isinstance(row, dict) or set(row) - {"path", "state", "sha256", "bytes", "kind"}
                or row.get("state") not in ("present", "missing", "unverified")):
            raise ValueError("证据产物格式无效。")
        identifier(row.get("path"), "产物路径")
        if "sha256" in row and not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"])):
            raise ValueError("产物散列必须是SHA256。")
        if "bytes" in row and (type(row["bytes"]) is not int or row["bytes"] < 0):
            raise ValueError("产物大小无效。")
    return value


def runtime_failure(directory, number):
    """Classify Codex startup failures, without storing raw error bodies or calling an API."""
    texts, complete, observed, unknown_item = [], True, False, False
    for kind in ("stdout", "stderr"):
        path = Path(directory) / (kind + ".r" + str(number) + ".log")
        if not path.is_file() or path.is_symlink():
            complete = False
            continue
        size = path.stat().st_size
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES)
            if size > MAX_BYTES:
                complete = False
                stream.seek(-MAX_BYTES // 2, 2)
                raw = raw[:MAX_BYTES // 2] + b"\n" + stream.read(MAX_BYTES // 2)
        text = raw.decode("utf-8", errors="replace")
        if kind == "stderr":
            texts.append(text.lower())
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            item = row.get("item", {})
            if isinstance(item, dict) and item.get("type") in ("command_execution", "tool_call", "mcp_tool_call", "file_change"):
                observed = True
            elif isinstance(item, dict) and item.get("type") not in (None, "reasoning", "agent_message", "todo_list"):
                unknown_item = True
            if row.get("type") == "error":
                texts.append(str(row.get("message", "")).lower())
            if row.get("type") == "turn.failed":
                error = row.get("error")
                if isinstance(error, dict):
                    texts.append(str(error.get("message", "")).lower())
    message = "\n".join(texts)
    patterns = (("model_route", ("workspace routing discovery failed",)),
                ("authentication", ("401 unauthorized", "invalid_api_key", "authentication token")),
                ("access_denied", ("403 forbidden",)),
                ("rate_limit", ("429", "rate limit")),
                ("transport", ("ssl", "tls", "connection reset", "connection timed out", "error sending request")),
                ("provider", ("503", "502", "500 internal server", "service unavailable")),
                ("context", ("context window", "context length", "context_length_exceeded")),
                ("compaction", ("remote compact task", "failed to compact")))
    for kind, needles in patterns:
        if any(needle in message for needle in needles):
            return {"kind": kind, "scan_complete": complete,
                    "work_observed": True if observed else (False if complete and not unknown_item else None)}
    return None


def result_summary(receipt):
    result = {k: receipt[k] for k in ("job", "round", "receipt_id", "head", "execution_state",
                                    "executor_exit", "receiver_session", "needs_initiator_review", "delivery_state") if k in receipt}
    report = receipt.get("report") or {}
    result.update({k: report[k] for k in ("outcome", "checks", "phase") if k in report})
    if isinstance(report.get("runtime_failure"), dict):
        result["runtime_failure"] = {k: report["runtime_failure"].get(k) for k in ("kind", "work_observed", "scan_complete")}
    result["summary"] = report.get("summary", "")[:600]
    evidence = report.get("evidence") or {}
    result["evidence"] = {"source_fingerprint": evidence.get("source_fingerprint"),
                          "artifact_count": len(evidence.get("artifacts", [])),
                          "missing_artifacts": sum(x.get("state") == "missing" for x in evidence.get("artifacts", [])),
                          "unverified_artifacts": sum(x.get("state") == "unverified" for x in evidence.get("artifacts", [])),
                          "check_counts": {s: sum(x.get("status") == s for x in evidence.get("checks", [])) for s in CHECKS},
                          "cleanup_reported": evidence.get("cleanup_confirmed"),
                          "provenance": "executor_report; initiator acceptance remains separate"}
    return result


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def identifier(value, label):
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(label + " 必须是非空、无控制字符的标识。")
    return value


def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or value in (".", ".."):
        raise ValueError("无效的任务 ID。")
    return value


def round_id(value):
    number = int(value)
    if number <= 0:
        raise ValueError("轮次必须是正整数。")
    return number


def atomic_json(path, data, limit=MAX_BYTES):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("回执路径不得是符号链接。")
    body = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if limit is not None and len(body) > limit:
        raise ValueError("回执超过64KiB；正文保存在私有产物中并引用路径。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, staging = tempfile.mkstemp(prefix=".receipt-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_size > MAX_BYTES:
        raise ValueError("回执路径或大小无效。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("回执不是有效的UTF-8 JSON。") from None


def validate_report(data, job, number, kind):
    if (not isinstance(data, dict) or type(data.get("schema")) is not int or data.get("schema") != 1
        or type(data.get("round")) is not int or data.get("job") != job or data.get("round") != number or data.get("kind") != kind):
        raise ValueError("回执任务、轮次或格式不匹配。")
    identifier(data.get("updated_at"), "回执时间")
    for field in ("phase", "summary"):
        if not isinstance(data.get(field, ""), str) or len(data.get(field, "")) > 8000:
            raise ValueError("回执阶段或摘要无效。")
    if kind == "result" and (data.get("outcome") not in OUTCOMES or data.get("checks") not in CHECKS):
        raise ValueError("结果或检查状态无效。")
    if not isinstance(data.get("artifacts", []), list) or any(not isinstance(x, str) for x in data.get("artifacts", [])):
        raise ValueError("产物必须是路径列表。")
    if "evidence" in data:
        validate_evidence(data["evidence"])
    return data


def report(directory, job, number, kind, phase="", summary="", outcome="completed", checks="not_reported", artifacts=(), evidence=None):
    job, number = job_id(job), round_id(number)
    if kind not in ("progress", "result"):
        raise ValueError("只支持 progress 或 result。")
    data = dict(schema=1, job=job, round=number, kind=kind, phase=phase, summary=summary,
                artifacts=list(artifacts), updated_at=utc_now())
    if kind == "result":
        data.update(outcome=outcome, checks=checks)
    if evidence is not None:
        data["evidence"] = validate_evidence(evidence)
    validate_report(data, job, number, kind)
    atomic_json(Path(directory).resolve() / "reports" / (kind + ".r" + str(number) + ".json"), data)
    return data


def finish(directory, job, number, code, agent=None):
    directory, job, number = Path(directory).resolve(), job_id(job), round_id(number)
    path = directory / "reports" / ("result.r" + str(number) + ".json")
    data = read_json(path)
    if data is None:
        reply = directory / ("last.r" + str(number) + ".txt")
        summary = ""
        if reply.is_file() and not reply.is_symlink():
            with reply.open("rb") as stream:
                summary = stream.read(MAX_BYTES).decode("utf-8", errors="replace")[:8000]
        data = report(directory, job, number, "result", summary=summary, outcome="completed" if code == 0 else "failed")
    else:
        validate_report(data, job, number, "result")
    data.update(executor_exit=int(code), finished_at=utc_now(),
                reply_file="last.r" + str(number) + ".txt", stdout_file="stdout.r" + str(number) + ".log")
    data.pop("runtime_failure", None)
    if code != 0:
        data["outcome"] = "failed"
        if (agent or os.environ.get("RD_AGENT")) == "codex":
            failure = runtime_failure(directory, number)
            if failure:
                data["runtime_failure"] = failure
    atomic_json(path, data)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("progress", "result", "finish"))
    parser.add_argument("--phase", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--outcome", choices=OUTCOMES, default="completed")
    parser.add_argument("--checks", choices=CHECKS, default="not_reported")
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--evidence-file", type=Path, help="小型结构化证据JSON；原件保留在任务目录")
    parser.add_argument("--source-fingerprint", help="当前绑定输入的SHA256，不冒充编译或验收版本")
    parser.add_argument("--exit", type=int)
    args = parser.parse_args()
    try:
        directory = os.environ["RD_JOB_DIR"]
        job, number = job_id(os.environ["RD_JOB"]), round_id(os.environ["RD_ROUND"])
        if args.action == "finish":
            if args.exit is None:
                raise ValueError("finish 必须指定 --exit。")
            finish(directory, job, number, args.exit)
        else:
            evidence = read_json(args.evidence_file.absolute()) if args.evidence_file else {}
            if evidence is None:
                raise ValueError("证据索引不存在。")
            validate_evidence(evidence)
            if args.source_fingerprint:
                evidence["source_fingerprint"] = args.source_fingerprint
            if args.artifact:
                evidence["artifacts"] = artifact_index(os.environ.get("RD_WT", directory), args.artifact)
            report(directory, job, number, args.action, args.phase, args.summary, args.outcome, args.checks, args.artifact, evidence or None)
        print("本轮回执已保存；下一步由发起方判断。")
    except (KeyError, ValueError, OSError) as error:
        parser.exit(4, "rd-report: " + ("请在派单任务环境中执行。" if isinstance(error, KeyError) else str(error)) + "\n")


if __name__ == "__main__":
    main()
