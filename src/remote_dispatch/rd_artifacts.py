"""非 Git 产物取回与环境指纹核对：纯函数部分（路径校验、体量统计、差异文本）。

传输与派单流程在 cli.py：这里不连任何机器。
"""
import difflib
import hashlib
import os
import posixpath
import re

UNSAFE = set("*?[]{}$`\"'\\;|&<>!#")
DEFAULT_FINGERPRINT_TIMEOUT = 120
FINGERPRINT_TEXT_LIMIT = 4096


def parse_artifacts(values):
    """规范化 --artifact：仓库内相对路径，拒绝绝对路径、.. 逃逸、仓库根、.git、通配符和控制字符。

    返回去重后的 posix 相对路径列表；任一项不合法抛 ValueError。"""
    items = []
    for raw in values or []:
        if not raw or not raw.strip():
            raise ValueError("--artifact 不得为空")
        if not raw.isprintable():
            raise ValueError(f"--artifact 路径含控制字符：{raw!r}")
        if UNSAFE & set(raw):
            raise ValueError(f"--artifact 必须是具体路径，不得含通配符或 shell 字符：{raw}")
        path = raw.replace("\\", "/")
        if path.startswith(("/", "~")) or re.match(r"[A-Za-z]:", path):
            raise ValueError(f"--artifact 必须是仓库内相对路径，不得是绝对路径：{raw}")
        if ".." in path.split("/"):
            raise ValueError(f"--artifact 不得用 .. 越出仓库：{raw}")
        norm = posixpath.normpath(path)
        if norm in (".", ""):
            raise ValueError(f"--artifact 不得指向仓库根目录：{raw}")
        if ".git" in norm.split("/"):
            raise ValueError(f"--artifact 不得含 .git：{raw}")
        if norm not in items:
            items.append(norm)
    return items


def local_size(path):
    """本地文件或目录的 (文件数, 字节数)；不存在返回 (0, 0)。不跟随符号链接。"""
    if os.path.islink(path) or os.path.isfile(path):
        try:
            return 1, os.lstat(path).st_size
        except OSError:
            return 0, 0
    files = total = 0
    for folder, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
                files += 1
            except OSError:
                continue
    return files, total


def human_size(count):
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024


def artifact_lines(results):
    """把取回结果列成输出与回执共用的中文行。"""
    names = {"fetched": "已拉回", "missing": "缺失", "error": "失败", "skipped": "已跳过"}
    lines = []
    for item in results:
        text = f"  {names.get(item['status'], item['status'])}  {item['path']}"
        if item["status"] == "fetched":
            text += f"  {item.get('files', 0)} 个文件，{human_size(item.get('bytes', 0))}"
        elif item.get("error"):
            text += f"  {item['error']}"
        lines.append(text)
    return lines


def artifacts_pending(rec):
    """声明了产物但还有路径没成功处理（未尝试或失败）；远端确认缺失、本地保护跳过的是终态，不算待拉。"""
    if not rec.get("artifact_paths"):
        return False
    state = rec.get("artifacts_state") or {}
    done = {item["path"] for item in state.get("results", []) if item["status"] in ("fetched", "missing", "skipped")}
    return any(path not in done for path in rec["artifact_paths"])


def tracked_conflicts(tracked, path):
    """artifact 路径与已跟踪文件冲突的清单。tracked 是仓库已跟踪文件（posix 相对路径）可迭代对象。
    冲突：路径本身是已跟踪文件、目录内含已跟踪文件、或某个上级目录位置上是已跟踪文件。"""
    hits = []
    prefix = path + "/"
    parents = {path.rsplit("/", i)[0] for i in range(1, path.count("/") + 1)}
    for name in tracked:
        if name == path or name.startswith(prefix) or name in parents:
            hits.append(name)
    return hits


def safe_member(name):
    """worker 回传的 tar 成员名必须是相对、无 .. 的路径。"""
    path = name.replace("\\", "/")
    return bool(name) and not path.startswith("/") and not re.match(r"[A-Za-z]:", path) and ".." not in path.split("/")


def trim(text):
    return text if len(text) <= FINGERPRINT_TEXT_LIMIT else text[:FINGERPRINT_TEXT_LIMIT] + "\n…（已截断）"


def fingerprint_diff(local, remote, host):
    """本机与目标机指纹的 unified diff 文本；一致时为空串。"""
    return "\n".join(difflib.unified_diff(local.splitlines(), remote.splitlines(),
                                          fromfile="local", tofile=f"remote:{host}", lineterm=""))


def fingerprint_digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
