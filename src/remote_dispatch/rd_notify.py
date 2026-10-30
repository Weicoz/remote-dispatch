"""汇报提醒通道：配置解析与分派；每个通道是独立函数，结果写入 delivery.channels。

提醒文本只用 rdispatch 自己的字段（派单标题、目标机、轮次、状态文字），
不含执行方摘要或任何来自目标机的文字；任何通道都不写入会话输入框。
"""
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


def app(service, row):
    """App 推送：无推送设备记 no_device；实际计数推送由 rd_push 按 report.delivered 事件触发（带检查点）。"""
    try:
        has_device = service.network.has_push_device()
    except (OSError, ValueError, AttributeError):
        return dict(channel="app", ok=False, reason="no_device")
    if not has_device:
        return dict(channel="app", ok=False, reason="no_device")
    return dict(channel="app", ok=True, reason=None)


SENDERS = {"herdr": herdr, "app": app}


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
            channels.append(sender(service, row))
    status = "notified" if any(c["ok"] for c in channels) else "inbox_only"
    return dict(status=status, channels=channels)
