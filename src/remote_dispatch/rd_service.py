"""macOS LaunchAgent that keeps the resident serve running.

`rdispatch service` 只支持 macOS。路径与环境由调用方给出，测试才能在临时 HOME
和 PATH 里的假 `launchctl` 下断言 plist 内容与调用序列，不碰真实 launchd。
"""
import http.client
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import tempfile

from . import __version__
from .rd_serve import SOCKET_NAME, VERSION_ROUTE, UnixHTTPConnection


LABEL = "io.github.weicoz.remote-dispatch.serve"
LOG_NAME = "serve.log"
ERROR_LOG_NAME = "serve.err.log"
PLIST_DIRECTORY = Path("Library") / "LaunchAgents"


def plist_path(home=None):
    return Path(home or Path.home()) / PLIST_DIRECTORY / (LABEL + ".plist")


def entry():
    """当前运行的这个 rdispatch 入口：launchd 没有可用的 PATH，只能写绝对路径或解释器加模块。"""
    candidate = sys.argv[0] if sys.argv else ""
    if candidate and os.sep in candidate and Path(candidate).name != "__main__.py":
        path = Path(candidate).absolute()
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path)]
    if sys.executable:
        return [sys.executable, "-m", "remote_dispatch"]
    raise ValueError("service_entry_not_found: 无法确定 rdispatch 入口；"
                     "请在安装 rdispatch 的终端里重试。")


def document(arguments, environment):
    state = Path(environment["RD_STATE"])
    return {
        "Label": LABEL,
        "ProgramArguments": [*arguments, "serve"],
        "EnvironmentVariables": dict(environment),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(state / LOG_NAME),
        "StandardErrorPath": str(state / ERROR_LOG_NAME),
    }


def prepare(state):
    """launchd 不建目录；日志路径所在的状态目录必须先生成。"""
    Path(state).mkdir(parents=True, exist_ok=True, mode=0o700)


def write(path, content):
    """与配置写入一致：先写同目录临时文件并落盘，再原子替换。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(plistlib.dumps(content))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def launchctl(*arguments, check=True):
    try:
        result = subprocess.run(["launchctl", *arguments], text=True, capture_output=True)
    except FileNotFoundError:
        raise ValueError("service_launchctl_missing: 找不到 launchctl；"
                         "rdispatch service 只支持 macOS。") from None
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"service_launch_failed: launchctl {arguments[0]} 失败"
                         f"（{result.returncode}）：{detail}")
    return result


def bootout():
    """Best effort: 未加载时 launchctl 报找不到服务，不算失败。"""
    return launchctl("bootout", f"gui/{os.getuid()}/{LABEL}", check=False)


def bootstrap(path):
    return launchctl("bootstrap", f"gui/{os.getuid()}", str(path))


def reload(path, wait=5.0):
    """重新加载 plist：先卸下可能存在的旧任务，等 launchd 不再列出它，再按文件加载。

    bootout 是异步的；立刻 bootstrap 可能撞上尚未卸完的旧任务而报 I/O 错误。
    """
    bootout()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            if LABEL not in jobs():
                break
        except ValueError:
            break
        time.sleep(0.1)
    return bootstrap(path)


def unload():
    """卸载后确认 launchd 不再列出该任务；无法确认或仍在列出时保留 plist，避免留下孤儿 serve。"""
    bootout()
    try:
        still_loaded = LABEL in jobs()
    except ValueError as error:
        raise ValueError(f"service_unload_failed: 无法确认 launchd 已卸载该任务（{error}）；"
                         "未删除 plist。") from None
    if still_loaded:
        raise ValueError("service_unload_failed: serve 仍被 launchd 列出，未删除 plist；"
                         "稍后重试，或检查 launchctl 后重试。")


def jobs():
    """launchctl list 的三列输出：PID、上次退出码、Label；PID 为 - 表示未运行。"""
    result = launchctl("list")
    running = {}
    for line in (result.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            fields = line.split()
        if len(fields) != 3:
            continue
        pid, _, label = fields
        running[label] = int(pid) if pid.isdigit() else None
    return running


def version(socket_path, timeout=5):
    """读 serve 的 /v1/version；不在线时返回 None，不抛错。"""
    connection = UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        connection.connect()
        connection.request("GET", VERSION_ROUTE)
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = json.loads(response.read())
        return body if isinstance(body, dict) else None
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        connection.close()


def status(path):
    """已安装的 LaunchAgent 快照；serve 不在线时仍报告 plist 与 launchd 状态。"""
    path = Path(path)
    installed = path.exists()
    document = {}
    plist_error = None
    if installed:
        try:
            document = plistlib.loads(path.read_bytes())
        except OSError as error:
            plist_error = f"plist_unreadable: {error}"
        except plistlib.InvalidFileException as error:
            plist_error = f"plist_invalid: {error}"
        if not isinstance(document, dict):
            document = {}
    environment = document.get("EnvironmentVariables") or {}
    state = environment.get("RD_STATE")
    # plist 不可读或没写 RD_STATE 时不猜 socket，避免把别的 serve 当成这个服务。
    socket = environment.get("RD_SERVE_SOCKET") or (str(Path(state) / SOCKET_NAME) if state else None)
    loaded = None
    pid = None
    launchctl_error = None
    if installed:
        try:
            running_jobs = jobs()
            loaded = LABEL in running_jobs
            pid = running_jobs.get(LABEL)
        except ValueError as error:
            launchctl_error = str(error)
    running = None if loaded is None else bool(pid and alive(pid))
    body = version(socket) if socket else None
    reply = body or {}
    config_reload = reply.get("config_reload")
    if not isinstance(config_reload, dict):
        config_reload = None
    return {
        "installed": installed,
        "label": LABEL,
        "plist": str(path),
        "executable": (document.get("ProgramArguments") or [None])[0],
        "state_dir": state,
        "config": environment.get("RD_CONFIG"),
        "socket": socket,
        "log": document.get("StandardOutPath"),
        "error_log": document.get("StandardErrorPath"),
        "loaded": loaded,
        "pid": pid,
        "running": running,
        "serve_online": body is not None,
        "package_version": reply.get("package_version"),
        "contract_version": reply.get("contract_version"),
        "config_reload": config_reload,
        "cli_version": __version__,
        "plist_error": plist_error,
        "launchctl_error": launchctl_error,
    }


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
