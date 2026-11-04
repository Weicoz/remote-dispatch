"""任务进度与结果回执；只记录本轮工作，不调度下一轮。"""
import argparse
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

MAX_BYTES = 65536
# 发起方回执封装远端报告（上限 MAX_BYTES）及任务元数据，需单独留出余量。
RECEIPT_BYTES = 2 * MAX_BYTES
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
            result = row.get("result") if row.get("event") == "result" else None
            if isinstance(result, dict):
                # AGY 终态：失败原因在 result.error；轮数大于零说明模型已开始工作。
                texts.append(str(result.get("error", "")).lower())
                if isinstance(result.get("num_turns"), int) and result["num_turns"] > 0:
                    observed = True
    message = "\n".join(texts)
    patterns = (("model_route", ("workspace routing discovery failed",)),
                ("authentication", ("401 unauthorized", "invalid_api_key", "authentication token",
                                    "authentication required", "authentication failed")),
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


# 认证失败的判定集中在这里：按 harness 只看各自输出里的错误字段；识别不了就不标记。
AUTH_NEEDLES = {
    "codex": ("401 unauthorized", "missing bearer or basic authentication", "invalid_api_key", "incorrect api key"),
    "agy": ("authentication failed", "not logged in", "login required", "unauthenticated", "401 unauthorized"),
    "dsh": ("missing_credential", "no api key for provider", "not logged in", "401 unauthorized"),
}


def error_texts(agent, text):
    """harness 输出里的错误文本（小写）。DSH 输出格式未公开固定：整段按原文；其他只取 JSON 错误字段。"""
    if agent == "dsh":
        return [text.lower()]
    errors = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "error":
            errors.append(str(row.get("message", "")).lower())
        if row.get("type") == "turn.failed" and isinstance(row.get("error"), dict):
            errors.append(str(row["error"].get("message", "")).lower())
        if row.get("event") == "result" and isinstance(row.get("result"), dict):
            errors.append(str(row["result"].get("error", "")).lower())
    return errors


def credential_suspect(agent, text, exit_code):
    """本轮失败输出里有明确认证失败返回 True。只在执行方非零退出时判定；不读取凭据内容，不调用 API。"""
    needles = AUTH_NEEDLES.get(agent)
    if not needles or not text or exit_code in (None, "", 0, "0"):
        return False
    return any(needle in error for error in error_texts(agent, text) for needle in needles)


# 网络类失败的已知模式（小写子串）；识别不了不标记。
NETWORK_NEEDLES = ("workspace routing discovery failed", "tls handshake", "ssl handshake", "handshake failed",
                   "error sending request", "connection reset", "connection timed out", "connection refused",
                   "network is unreachable", "dns error", "failed to lookup address", "could not resolve host")


PROGRESS_EVENTS = ("item.completed", "turn.completed")


def final_failure(agent, text, skip=None):
    """本轮输出里最后一条失败事件的小写文本（最后一条 turn.failed 或 error）；没有时返回 None。
    skip(文本) 为真的事件（瞬时重连）不算；失败之后又出现 item.completed／turn.completed 等进展事件，
    说明该错误已被重试掉，同样不算。纯文本输出不用于判定（宁可不标）：只有 DSH（输出格式未公开固定）
    取末尾 20 行里最后一条以 error／fatal 开头的行。"""
    if agent == "dsh":
        for line in reversed(text.splitlines()[-20:]):
            value = line.strip().lower()
            if value.startswith(("error", "[error", "fatal")) and not (skip and skip(line)):
                return value
        return None
    last = None
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        message = None
        if row.get("type") == "error":
            message = str(row.get("message", ""))
        elif row.get("type") == "turn.failed" and isinstance(row.get("error"), dict):
            message = str(row["error"].get("message", ""))
        elif row.get("event") == "result" and isinstance(row.get("result"), dict) and row["result"].get("error"):
            message = str(row["result"]["error"])
        elif row.get("type") in PROGRESS_EVENTS:
            last = None
        if message is not None and not (skip and skip(message)):
            last = message.lower()
    return last


def failure_hint(agent, text, exit_code, skip=None):
    """失败输出的已知原因提示：返回 (kind, 中文说明) 或 None。只在非零退出时判定，且只看最后一条失败事件，
    先前被重试掉的瞬时错误不算。认证失败优先（auth_failed），其次网络类（network_unreachable）。"""
    if not agent or not text or exit_code in (None, "", 0, "0"):
        return None
    final = final_failure(agent, text, skip)
    if not final:
        return None
    if any(needle in final for needle in AUTH_NEEDLES.get(agent, ())):
        return "auth_failed", "疑似认证失效：目标机上的登录凭据被服务端拒绝；见 credential_hint 的续跑命令。"
    for needle in NETWORK_NEEDLES:
        if needle in final:
            return "network_unreachable", ("疑似网络不可达：目标机访问模型服务失败（" + needle + "）；"
                                          "先用 rdispatch hosts 看该机网络探针，恢复后再续跑，或换主机重派。")
    return None


def refresh_auth_hint(job):
    """认证失败时给发起方的提示：显式重新复制目标机认证文件的续跑命令。不自动执行。"""
    return ("疑似认证失败：先在目标机更新登录凭据，再用 rdispatch resume " + str(job)
            + " --refresh-auth --prompt-file <提示文件> 重新复制凭据并续跑；rdispatch 不会自动重复制或续跑")


def result_summary(receipt):
    result = {k: receipt[k] for k in ("job", "round", "receipt_id", "head", "execution_state",
                                    "executor_exit", "receiver_session", "needs_initiator_review", "delivery_state",
                                    "acknowledgement", "stopped_by", "stopped_at", "suppress_push", "review_state",
                                    "reviewed_by", "reviewed_at", "review_note") if k in receipt}
    report = receipt.get("report") or {}
    result.update({k: report[k] for k in ("outcome", "checks", "phase") if k in report})
    if receipt.get("credential_suspect"):
        result.update({k: receipt[k] for k in ("credential_suspect", "credential_hint") if k in receipt})
    if receipt.get("failure_hint"):
        result.update({k: receipt[k] for k in ("failure_hint", "failure_note") if k in receipt})
    if isinstance(report.get("runtime_failure"), dict):
        result["runtime_failure"] = {k: report["runtime_failure"].get(k) for k in ("kind", "work_observed", "scan_complete")}
    if isinstance(report.get("denied_actions"), list):
        result["denied_actions"] = report["denied_actions"]
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


def read_json(path, limit=RECEIPT_BYTES):
    path = Path(path)
    if not path.exists():
        return None
    if any(p.is_symlink() for p in (path, *path.parents)) or limit is not None and path.stat().st_size > limit:
        raise ValueError("回执路径或大小无效。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("回执不是有效的UTF-8 JSON。") from None


def progress_rows(directory, job, number):
    """Read complete append records; old workers expose only their latest update."""
    job, number = job_id(job), round_id(number)
    path = Path(directory) / "reports" / ("progress.r" + str(number) + ".jsonl")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("进度路径不得是符号链接。")
    try:
        rows = []
        with path.open("rb") as stream:
            while True:
                line = stream.readline(MAX_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_BYTES:
                    raise ValueError("单条进度超过64KiB。")
                if not line.endswith(b"\n"):
                    break  # A concurrent writer has not finished this record yet.
                row = json.loads(line)
                if not isinstance(row, dict) or not {"updated_at", "phase", "summary"} <= set(row):
                    raise ValueError("进度记录格式无效。")
                validate_report(dict(row, schema=1, job=job, round=number, kind="progress"), job, number, "progress")
                rows.append({key: row[key] for key in ("updated_at", "phase", "summary")})
        return rows
    except FileNotFoundError:
        latest = read_json(path.with_suffix(".json"))
        if latest is None:
            return []
        validate_report(latest, job, number, "progress")
        return [{key: latest.get(key, "") for key in ("updated_at", "phase", "summary")}]


@contextmanager
def progress_writer(path):
    """The reporter is uploaded standalone, so its lock must work without the package."""
    lock = path.with_suffix(".lock")
    if any(p.is_symlink() for p in (lock, *lock.parents)):
        raise ValueError("进度锁路径不得是符号链接。")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if os.name == "nt":
            import msvcrt
            # _locking supports bytes past EOF; avoid a racy initialization write.
            stream.seek(0)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def append_progress(path, row):
    body = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as stream:
        # Recover only an unfinished final record, while holding progress_writer.
        end = stream.seek(0, 2)
        start = max(0, end - MAX_BYTES)
        stream.seek(start)
        tail = stream.read()
        if tail and not tail.endswith(b"\n"):
            if start and b"\n" not in tail:
                raise ValueError("进度尾行超过64KiB。")
            stream.truncate(start + tail.rfind(b"\n") + 1)
        stream.write(body)
        stream.flush()
        os.fsync(fd)


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
    for field in ("decisions", "unfinished"):
        rows = data.get(field, [])
        if kind != "result" and field in data:
            raise ValueError("决策和未完成项只用于结果回执。")
        if (not isinstance(rows, list) or len(rows) > 50
                or any(not isinstance(x, str) or not x.strip() or len(x) > 2000 for x in rows)):
            raise ValueError("决策和未完成项必须是最多 50 条、每条不超过 2000 字的非空文本列表。")
    if "evidence" in data:
        validate_evidence(data["evidence"])
    return data


def report(directory, job, number, kind, phase="", summary="", outcome="completed", checks="not_reported", artifacts=(), evidence=None, decisions=(), unfinished=()):
    job, number = job_id(job), round_id(number)
    if kind not in ("progress", "result"):
        raise ValueError("只支持 progress 或 result。")
    data = dict(schema=1, job=job, round=number, kind=kind, phase=phase, summary=summary,
                artifacts=list(artifacts), updated_at=utc_now())
    if kind == "result":
        data.update(outcome=outcome, checks=checks)
        if decisions:
            data["decisions"] = list(decisions)
        if unfinished:
            data["unfinished"] = list(unfinished)
    elif decisions or unfinished:
        raise ValueError("--decision 和 --unfinished 只用于 result。")
    if evidence is not None:
        data["evidence"] = validate_evidence(evidence)
    validate_report(data, job, number, kind)
    path = Path(directory).resolve() / "reports" / (kind + ".r" + str(number) + ".json")
    history = path.with_suffix(".jsonl")
    if kind == "progress" and any(p.is_symlink() for p in (history, *history.parents)):
        raise ValueError("进度路径不得是符号链接。")
    if kind == "progress":
        with progress_writer(history):
            atomic_json(path, data)
            append_progress(history, {key: data[key] for key in ("updated_at", "phase", "summary")})
    else:
        atomic_json(path, data)
    return data


def agy_result(directory, number):
    """读取 AGY stream-json 的终态事件；只看日志尾部，不保留整段输出。"""
    path = Path(directory) / ("stdout.r" + str(number) + ".log")
    if not path.is_file() or path.is_symlink():
        return None
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 4 * MAX_BYTES))
        lines = stream.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == "result" and isinstance(row.get("result"), dict):
            return row["result"]
    return None


def phase_timings(directory, number):
    """读取 run 脚本写的 timing.rN（key=value 行）；缺失或损坏的字段不写，查询时视为未记录。"""
    path = Path(directory) / ("timing.r" + str(number))
    result = {}
    if not path.is_file() or path.is_symlink():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return result
    for line in lines:
        key, _, value = line.partition("=")
        if key in ("setup_seconds", "body_seconds"):
            try:
                seconds = float(value)
            except ValueError:
                continue
            if 0 <= seconds < 10 ** 8:
                result[key] = int(seconds) if seconds == int(seconds) else round(seconds, 1)
        elif key == "setup_note" and value == "已跳过":
            result[key] = value
        elif key == "path_size_kb":
            number, tab, rel = value.partition("\t")
            try:
                kb = int(number)
            except ValueError:
                continue
            if tab and rel and 0 <= kb < 10 ** 12:
                result.setdefault("regenerable_kb", {})[rel] = kb
    return result


def rollout_calls(directory):
    """汇总 Codex rollout 里逐次调用的用量；任何读取或解析失败都返回「未记录」，不影响派单。"""
    try:
        total = dict(count=0, input_tokens=0, cached_input_tokens=0, output_tokens=0)
        for path in sorted((Path(directory) / "codex" / "sessions").rglob("rollout-*.jsonl")):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                for raw in stream:
                    try:
                        row = json.loads(raw)
                        payload = row["payload"]
                        if row["type"] != "event_msg" or payload["type"] != "token_count":
                            continue
                        usage = payload["info"]["last_token_usage"]
                        values = [int(usage.get(key) or 0) for key in ("input_tokens", "cached_input_tokens", "output_tokens")]
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    total["count"] += 1
                    total["input_tokens"] += values[0]
                    total["cached_input_tokens"] += values[1]
                    total["output_tokens"] += values[2]
        return total if total["count"] else "未记录"
    except (OSError, ValueError):
        return "未记录"


def finish(directory, job, number, code, agent=None, test_command=None):
    directory, job, number = Path(directory).resolve(), job_id(job), round_id(number)
    agent = agent or os.environ.get("RD_AGENT")
    path = directory / "reports" / ("result.r" + str(number) + ".json")
    final = agy_result(directory, number) if agent == "agy" else None
    reply = directory / ("last.r" + str(number) + ".txt")
    if final and isinstance(final.get("response"), str) and not reply.exists():
        # 与 Codex 的 -o 一致：最后回复落到 last.rN.txt，rdispatch logs JOB last 可读。
        reply.write_text(final["response"][:MAX_BYTES], encoding="utf-8")
    data = read_json(path)
    if data is None:
        summary = ""
        if reply.is_file() and not reply.is_symlink():
            with reply.open("rb") as stream:
                summary = stream.read(MAX_BYTES).decode("utf-8", errors="replace")[:8000]
        # 显式测试派单（run --test）：退出码即检查结论，并记录被执行的命令；代理任务不适用。
        is_test = bool(test_command) and not agent
        data = report(directory, job, number, "result", summary=summary, outcome="completed" if code == 0 else "failed",
                      checks=("passed" if code == 0 else "failed") if is_test else "not_reported")
        if is_test:
            data["test_command"] = test_command[:1000]
    else:
        validate_report(data, job, number, "result")
    started = directory / ("started.r" + str(number))
    if started.is_file() and not started.is_symlink():
        data["started_at"] = started.read_text(encoding="utf-8").strip()
    data.update(executor_exit=int(code), finished_at=utc_now(),
                reply_file="last.r" + str(number) + ".txt", stdout_file="stdout.r" + str(number) + ".log")
    for key in ("setup_seconds", "body_seconds", "setup_note", "rollout_calls", "regenerable_kb"):
        data.pop(key, None)
    data.update(phase_timings(directory, number))
    if agent == "codex" and os.environ.get("RD_ROLLOUT_STATS") == "1":
        data["rollout_calls"] = rollout_calls(directory)
    data.pop("runtime_failure", None)
    data.pop("denied_actions", None)
    denied = final.get("denied_actions") if final else None
    if isinstance(denied, list) and denied:
        # AGY 无人值守时自动拒绝权限请求却仍报 SUCCESS；被拒操作意味着本轮可能没做完。
        data["denied_actions"] = [str(item.get("action", "")) if isinstance(item, dict) else str(item) for item in denied][:20]
        if data["outcome"] == "completed":
            data["outcome"] = "partial"
    if code != 0:
        data["outcome"] = "failed"
        if agent in ("codex", "agy"):
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
    parser.add_argument("--decision", action="append", default=[], help="本轮做出的决策（可重复，仅 result）")
    parser.add_argument("--unfinished", action="append", default=[], help="未完成项（可重复，仅 result）")
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
            finish(directory, job, number, args.exit, test_command=os.environ.get("RD_TEST_COMMAND") or None)
        else:
            evidence = read_json(args.evidence_file.absolute()) if args.evidence_file else {}
            if evidence is None:
                raise ValueError("证据索引不存在。")
            validate_evidence(evidence)
            if args.source_fingerprint:
                evidence["source_fingerprint"] = args.source_fingerprint
            if args.artifact:
                evidence["artifacts"] = artifact_index(os.environ.get("RD_WT", directory), args.artifact)
            report(directory, job, number, args.action, args.phase, args.summary, args.outcome, args.checks, args.artifact, evidence or None,
                   args.decision, args.unfinished)
        print("本轮回执已保存；下一步由发起方判断。")
    except (KeyError, ValueError, OSError) as error:
        parser.exit(4, "rd-report: " + ("请在派单任务环境中执行。" if isinstance(error, KeyError) else str(error)) + "\n")


if __name__ == "__main__":
    main()
