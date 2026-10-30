"""汇报提醒通道：配置解析与分派；每个通道是独立函数，结果写入 delivery.channels。

提醒文本只用 rdispatch 自己的字段（派单标题、目标机、轮次、状态文字），
不含执行方摘要或任何来自目标机的文字；任何通道都不写入会话输入框。
"""
import subprocess
import sys

CHANNELS = ("herdr", "macos", "app")
NOTIFY_ERROR = "[monitor] notify 取值无效：只能是 herdr、macos、app（逗号分隔）或 none。"


def parse(cp):
    """[monitor] notify → 启用的通道元组；默认三者全开，none 全关；非法取值抛 ValueError。"""
    raw = cp.get("monitor", "notify", fallback=None)
    if raw is None or not raw.strip():
        return CHANNELS
    names = [name.strip().lower() for name in raw.split(",")]
    if names == ["none"]:
        return ()
    if any(name not in CHANNELS for name in names):
        raise ValueError(NOTIFY_ERROR)
    return tuple(name for name in CHANNELS if name in names)


def digest_enabled(cp):
    """[monitor] notify_digest：默认关闭；取值无效按关闭处理。"""
    return cp.get("monitor", "notify_digest", fallback="false").strip().lower() in ("1", "true", "yes", "on")


def text(row):
    """(标题, 正文, 提示音)；只取 rdispatch 自有字段。"""
    entries = row["entries"]
    kind = "进度摘要" if row.get("kind") == "digest" else "派单汇报"
    if len(entries) == 1:
        title = "%s：%s" % (kind, entries[0]["title"])
    else:
        title = "%s：%d 个派单" % (kind, len(entries))
    body = "\n".join("%s · %s · 第 %s 轮 · %s" % (e["title"], e["host"], e["round"], e["state_text"])
                     for e in entries)
    failed = any(e["state"] == "failed" for e in entries)
    return title, body, "request" if failed else "done"


def herdr(service, row):
    """Herdr 界面通知；不可用时记 herdr_unavailable，不重试。"""
    title, body, sound = text(row)
    try:
        service.sessions.herdr.notify(title, body, sound)
    except (OSError, ValueError):
        return dict(channel="herdr", ok=False, reason="herdr_unavailable")
    return dict(channel="herdr", ok=True, reason=None)


MACOS_SCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv) "
                "sound name (item 3 of argv)", "end run")
MACOS_SOUNDS = {"request": "Basso", "done": "Glass"}


def is_macos():
    return sys.platform == "darwin"


def show_macos(title, body, sound):
    """调用 osascript；标题、正文、提示音作为独立 argv 传入脚本，不拼进脚本文本，无法注入。
    launchd 用户代理在登录的图形会话内，可直接调用。失败抛 OSError。"""
    cmd = ["osascript"]
    for line in MACOS_SCRIPT:
        cmd += ["-e", line]
    cmd += ["--", title, body, MACOS_SOUNDS.get(sound, "Glass")]
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except subprocess.SubprocessError as error:
        raise OSError(str(error)) from error
    if result.returncode != 0:
        raise OSError("osascript 退出码 %d" % result.returncode)


def macos(service, row):
    """macOS 系统通知；非 macOS 记 unsupported_platform，失败记 notify_failed，不重试。"""
    if not is_macos():
        return dict(channel="macos", ok=False, reason="unsupported_platform")
    title, body, sound = text(row)
    try:
        show_macos(title, body, sound)
    except (OSError, ValueError):
        return dict(channel="macos", ok=False, reason="notify_failed")
    return dict(channel="macos", ok=True, reason=None)


def app(service, row):
    """App 推送：无推送设备记 no_device；实际计数推送由 rd_push 按 report.delivered 事件触发（带检查点）。"""
    try:
        has_device = service.network.has_push_device()
    except (OSError, ValueError, AttributeError):
        return dict(channel="app", ok=False, reason="no_device")
    if not has_device:
        return dict(channel="app", ok=False, reason="no_device")
    return dict(channel="app", ok=True, reason=None)


SENDERS = {"herdr": herdr, "macos": macos, "app": app}


def dispatch(service, row):
    """按 [monitor] notify 分派；未实现的通道跳过并记原因。返回 row["delivery"]。"""
    try:
        enabled = parse(service.config.cp)
    except ValueError:
        enabled = ()
    if row.get("kind") == "digest" and not digest_enabled(service.config.cp):
        enabled = ()
    channels = []
    for name in enabled:
        sender = SENDERS.get(name)
        if sender is None:
            channels.append(dict(channel=name, ok=False, reason="not_implemented"))
        else:
            try:
                channels.append(sender(service, row))
            except Exception as error:
                # 通道异常不得中断其他通道；只记通道名与异常类型，不含汇报正文。
                print("rdispatch: 提醒通道 %s 失败（%s）。" % (name, type(error).__name__), file=sys.stderr)
                channels.append(dict(channel=name, ok=False, reason="error:" + type(error).__name__))
    status = "notified" if any(c["ok"] for c in channels) else "inbox_only"
    return dict(status=status, channels=channels)
