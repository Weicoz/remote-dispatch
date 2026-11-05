"""发起来源：父进程链决定 harness，环境变量只提供其会话与派单时的 Herdr 上下文。"""
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess


SESSION_VARIABLES = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
                     "dsh": "DSH_SESSION_ID", "agy": "ANTIGRAVITY_CONVERSATION_ID", "kimi": None}


def read_process(pid):
    """进程信息的系统接缝；进程退出、无权限或平台不支持时返回 None。"""
    try:
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "ppid=", "-o", "command="],
                                capture_output=True, text=True)
        if result.returncode:
            return None
        parent, command = result.stdout.strip().split(None, 1)
        return int(parent), command
    except (OSError, ValueError):
        return None


def harness():
    """就近识别父进程中的 harness；不扫描命令参数中的任意名称。"""
    pid, seen = os.getppid(), set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        process = read_process(pid)
        if process is None:
            break
        pid, command = process
        try:
            # ps 不保证对提示词参数做 shell 转义；只解析识别入口所需的前缀。
            tokens = shlex.shlex(command, posix=True)
            tokens.whitespace_split, tokens.commenters = True, ""
            argv = []
            for _ in range(3):
                token = next(tokens, None)
                if token is None:
                    break
                argv.append(token)
                name = command_harness(argv)
                if name:
                    return name
        except ValueError:
            continue
    return "unknown"


def command_harness(argv):
    if not argv:
        return None
    executable = Path(argv[0]).name
    if executable in SESSION_VARIABLES:
        return executable
    if executable in ("node", "nodejs") and len(argv) > 1:
        script = Path(argv[1])
        if script.stem in SESSION_VARIABLES and script.suffix in ("", ".js", ".mjs", ".cjs"):
            return script.stem
        # npm 的入口有时统一叫 cli.js / index.js，只认已知包的入口。
        for package, entry, name in (("@anthropic-ai/claude-code/cli.js", "cli.js", "claude"),
                                     ("@deepseek-ai/dsh/lib/index.js", "index.js", "dsh")):
            if script.name == entry and script.as_posix().endswith("/node_modules/" + package):
                return name
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", executable) and len(argv) > 1:
        if argv[1:3] == ["-m", "kimi_cli"]:
            return "kimi"
        script = Path(argv[1]).name
        if script in SESSION_VARIABLES:
            return script
    return None


def session(harness_name):
    variable = SESSION_VARIABLES.get(harness_name)
    return os.environ.get(variable) if variable else None


def hc_context(created):
    """herdr-core 终端地址：原样记 HC_TERMINAL_ID；`机器/终端` 形式取第一个 `/` 前的机器名，本机裸地址为 null。"""
    terminal = (os.environ.get("HC_TERMINAL_ID") or "").strip()
    if not terminal:
        return None
    machine = terminal.split("/", 1)[0] if "/" in terminal else None
    return dict(terminal=terminal, machine=machine or None, captured_at=created)


TOOL_TIMEOUT = 2
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]*")


def tool_json(argv):
    """只读外部命令的系统接缝：短超时，任何失败返回 None；非零退出但输出 JSON（如 pane_not_found）照常解析。"""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT,
                                stdin=subprocess.DEVNULL)
        value = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return value if isinstance(value, dict) else None


def ancestors():
    """当前进程的祖先 pid 链（由近到远）；ps 不可用时为空。"""
    chain, pid, seen = [], os.getppid(), set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        process = read_process(pid)
        chain.append(pid)
        if process is None:
            break
        pid = process[0]
    return chain


def hc_root_pid(terminal):
    binary = os.environ.get("RD_HC_BIN", "hc")  # 与 serve 的 hc 后端同一开关；空串表示不使用 hc
    value = tool_json([binary, "get", terminal, "--json"]) if binary and TOKEN.fullmatch(terminal) else None
    pid = (value or {}).get("pid")
    return pid if type(pid) is int and pid > 1 else None


def pane_root(pane):
    """返回 (shell pid 或 None, 窗格是否确认不存在)。"""
    value = tool_json(["herdr", "pane", "process-info", "--pane", pane]) if TOKEN.fullmatch(pane) else None
    if value is None:
        return None, False
    if (value.get("error") or {}).get("code") == "pane_not_found":
        return None, True
    info = (value.get("result") or {}).get("process_info") or {}
    pid = info.get("shell_pid")
    return (pid if type(pid) is int and pid > 1 else None), False


def classify():
    """判定发起会话的内外层。返回 (primary, herdr_stale)。

    两个 ID 都在时，herdr-core 可能是从某个 Herdr 窗格启动的，App 终端因此继承了陈旧的 HERDR_PANE_ID。
    按两者根进程在当前祖先链里的先后判内层；窗格已不存在则 herdr 视为继承的陈旧值。
    无法判定时 primary 为 None（serve 沿用原规则：先 herdr 后 hc）。"""
    pane = (os.environ.get("HERDR_PANE_ID") or "").strip()
    terminal = (os.environ.get("HC_TERMINAL_ID") or "").strip()
    if not terminal:
        return ("herdr" if pane else None), False
    if not pane:
        return "hc", False
    if os.name == "nt":
        return None, False
    shell_pid, missing = pane_root(pane)
    if missing:
        return "hc", True
    chain = ancestors()
    hc_pid = hc_root_pid(terminal)
    positions = {name: chain.index(pid) for name, pid in (("hc", hc_pid), ("herdr", shell_pid)) if pid in chain}
    if positions:
        return min(positions, key=positions.get), False
    return None, False


def origin(harness_name, created):
    primary, stale = classify()
    return dict(initiator_harness=harness_name, initiator_host=socket.gethostname(),
                initiator_herdr=dict(session=None if stale else os.environ.get("HERDR_SESSION") or None,
                                     workspace_id=None if stale else os.environ.get("HERDR_WORKSPACE_ID") or None,
                                     pane_id=None if stale else os.environ.get("HERDR_PANE_ID") or None,
                                     captured_at=created),
                initiator_hc=hc_context(created), initiator_primary=primary)
