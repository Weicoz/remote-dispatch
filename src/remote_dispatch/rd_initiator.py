"""发起来源：父进程链决定 harness，环境变量只提供其会话与派单时的 Herdr 上下文。"""
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


def origin(harness_name, created):
    return dict(initiator_harness=harness_name, initiator_host=socket.gethostname(),
                initiator_herdr=dict(session=os.environ.get("HERDR_SESSION") or None,
                                     workspace_id=os.environ.get("HERDR_WORKSPACE_ID") or None,
                                     pane_id=os.environ.get("HERDR_PANE_ID") or None,
                                     captured_at=created))
