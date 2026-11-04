"""汇报提醒通道：配置解析与分派；每个通道是独立函数，结果写入 delivery.channels。

提醒文本只用 rdispatch 自己的字段（派单标题、目标机、轮次、状态文字），
不含执行方摘要或任何来自目标机的文字；任何通道都不写入会话输入框。
"""
import collections
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout

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
# 高负载下 osascript 启动可能超过数秒；通道在线程池里调用，放宽超时不阻塞汇报线程。
MACOS_TIMEOUT = 15


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
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=MACOS_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise OSError("osascript 超过 %d 秒未返回" % MACOS_TIMEOUT) from error
    except subprocess.SubprocessError as error:
        raise OSError("osascript 调用失败：%s" % type(error).__name__) from error
    if result.returncode != 0:
        raise OSError("osascript 退出码 %d" % result.returncode)


def macos(service, row):
    """macOS 系统通知；非 macOS 记 unsupported_platform，失败记 notify_failed，不重试。"""
    if not is_macos():
        return dict(channel="macos", ok=False, reason="unsupported_platform")
    title, body, sound = text(row)
    try:
        show_macos(title, body, sound)
    except (OSError, ValueError) as error:
        # 只记原因（超时、退出码），不含标题与正文。
        print("rdispatch: macos notify_failed: %s" % error, file=sys.stderr, flush=True)
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


# 通道隔离：每个通道一个工作线程和一条有上限的待发队列；慢通道只拖慢自己。
QUEUE_LIMIT = 50          # 每通道待发上限；超限丢弃最旧并计数
BREAKER_THRESHOLD = 3     # 连续失败几次后熔断
BREAKER_COOLDOWN = 300.0  # 熔断持续秒数；到期后放行一次探测
WAIT_SECONDS = MACOS_TIMEOUT + 10  # 单个通道结果的最长等待，超过记 timeout
LOG_INTERVAL = 60.0       # 丢弃计数日志的最短间隔
# 配置性结果（未配置设备、平台不支持）不是通道故障，不计入熔断。
NEUTRAL_REASONS = ("unsupported_platform", "no_device")
# 通道没有真正尝试发送的原因：汇报不能据此记 inbox_only。
REFUSED = ("circuit_open", "channel_closed", "queue_overflow", "queue_timeout")


def say(message):
    print("rdispatch: " + message, file=sys.stderr, flush=True)


class Channel:
    """单个通道的执行器：串行工作线程 + 有界队列 + 连续失败熔断。"""

    def __init__(self, name, *, queue_limit=QUEUE_LIMIT, threshold=BREAKER_THRESHOLD,
                 cooldown=BREAKER_COOLDOWN, clock=time.monotonic):
        self.name, self.queue_limit, self.threshold, self.cooldown = name, queue_limit, threshold, cooldown
        self.clock = clock
        self.cv = threading.Condition()
        self.queue = collections.deque()
        self.thread = None
        self.closed = False
        self.failures = 0
        self.open_until = 0.0
        self.skipped = 0
        self.dropped = 0
        self.logged_drop_at = None

    @staticmethod
    def refusal(name, reason):
        future = Future()
        future.set_result(dict(channel=name, ok=False, reason=reason))
        return future

    def submit(self, sender, service, row):
        with self.cv:
            if self.closed:
                return self.refusal(self.name, "channel_closed")
            now = self.clock()
            if self.open_until and now < self.open_until:
                self.skipped += 1
                return self.refusal(self.name, "circuit_open")
            future = Future()
            self.queue.append((sender, service, row, future))
            while len(self.queue) > self.queue_limit:
                _, _, _, oldest = self.queue.popleft()
                self.settle_queued(oldest, dict(channel=self.name, ok=False, reason="queue_overflow"))
                self.dropped += 1
                if self.logged_drop_at is None or now - self.logged_drop_at >= LOG_INTERVAL:
                    self.logged_drop_at = now
                    say("提醒通道 %s 待发队列已满（上限 %d），累计丢弃最旧 %d 条。"
                        % (self.name, self.queue_limit, self.dropped))
            if self.thread is None:
                self.thread = threading.Thread(target=self.work, name="rd-notify-" + self.name, daemon=True)
                self.thread.start()
            self.cv.notify()
            return future

    def work(self):
        while True:
            with self.cv:
                while not self.queue and not self.closed:
                    self.cv.wait()
                if self.closed:
                    while self.queue:
                        self.settle_queued(self.queue.popleft()[3], dict(channel=self.name, ok=False, reason="channel_closed"))
                    return
                sender, service, row, future = self.queue.popleft()
            if not future.set_running_or_notify_cancel():
                continue  # 调用方等待超时已撤销：不发送，也不计入熔断
            try:
                result = sender(service, row)
            except Exception as error:
                # 通道异常不得中断其他通道；只记通道名与异常类型，不含汇报正文。
                say("提醒通道 %s 失败（%s）。" % (self.name, type(error).__name__))
                result = dict(channel=self.name, ok=False, reason="error:" + type(error).__name__)
            self.settle(result)
            future.set_result(result)

    @staticmethod
    def settle_queued(future, result):
        if future.set_running_or_notify_cancel():
            future.set_result(result)

    def settle(self, result):
        """按结果更新熔断：连续失败达阈值后在冷却期内直接拒绝，到期放行一次探测。"""
        with self.cv:
            if result["ok"] or result.get("reason") in NEUTRAL_REASONS:
                if self.failures >= self.threshold:
                    say("提醒通道 %s 已恢复（熔断期间跳过 %d 条）。" % (self.name, self.skipped))
                self.failures, self.open_until, self.skipped = 0, 0.0, 0
                return
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = self.clock() + self.cooldown
                if self.failures == self.threshold:
                    say("提醒通道 %s 连续失败 %d 次，熔断 %d 秒。" % (self.name, self.failures, self.cooldown))

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


_fallback_channels = {}
_fallback_lock = threading.Lock()


def channels_of(service):
    """通道执行器挂在服务对象上；无法挂载（测试替身）时退回进程级共享。"""
    holder = getattr(service, "notify_channels_state", None)
    if isinstance(holder, dict):
        return holder
    try:
        service.notify_channels_state = holder = {}
        return holder
    except AttributeError:
        return _fallback_channels


def close(service):
    for channel in list(channels_of(service).values()):
        channel.close()


def dispatch(service, row):
    """按 [monitor] notify 分派；各通道并行、互不阻塞。返回 row["delivery"]。"""
    try:
        enabled = parse(service.config.cp)
    except ValueError:
        enabled = ()
    if row.get("kind") == "digest" and not digest_enabled(service.config.cp):
        enabled = ()
    # 重投时只发还没有结果的通道：已有结果（成功、失败、或超时的在途发送——可能已送达）的通道沿用，不重发。
    prior = {c["channel"]: c for c in (row.get("delivery") or {}).get("channels", [])
             if isinstance(c, dict) and c.get("reason") not in REFUSED}
    pending = []
    with _fallback_lock:
        registry = channels_of(service)
        runners = {name: registry.setdefault(name, Channel(name)) for name in enabled}
    for name in enabled:
        sender = SENDERS.get(name)
        if name in prior:
            pending.append((name, None, prior[name]))
        elif sender is None:
            pending.append((name, None, dict(channel=name, ok=False, reason="not_implemented")))
        else:
            pending.append((name, runners[name], runners[name].submit(sender, service, row)))
    channels = []
    deadline = time.monotonic() + WAIT_SECONDS
    for name, runner, outcome in pending:
        if runner is None:
            channels.append(outcome)
            continue
        try:
            channels.append(outcome.result(timeout=max(0.0, deadline - time.monotonic())))
        except FutureTimeout:
            # 还在排队就撤销（不发送、不计熔断）；已在发送的由工作线程自己记结果。
            if outcome.cancel():
                channels.append(dict(channel=name, ok=False, reason="queue_timeout"))  # 没发出去，可重投
            elif outcome.done():
                channels.append(outcome.result())
            else:
                channels.append(dict(channel=name, ok=False, reason="timeout"))  # 在途发送，可能已送达，不重投
    if not any(c["ok"] for c in channels) and any(c.get("reason") in REFUSED for c in channels):
        # 有通道没有真正尝试（熔断、关闭、队列满、排队超时）且没有任何成功：保持 queued，稍后重投。
        # 已有结果的通道随 queued 一起保存，重投只针对没尝试过的通道。
        return dict(status="queued", channels=[c for c in channels if c.get("reason") not in REFUSED])
    status = "notified" if any(c["ok"] for c in channels) else "inbox_only"
    return dict(status=status, channels=channels)
