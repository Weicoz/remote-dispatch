"""Count-only push decisions. The injected sender owns APNs credentials/transport."""
import json
from datetime import datetime
from pathlib import Path
import threading
from typing import Protocol

from . import rd_events as events
from . import rd_messages as messages


class PushCancelled(Exception):
    """Shutdown interrupted delivery; leave its checkpoint pending."""


class PushSender(Protocol):
    def send(self, token: str, payload: dict, *, stop: threading.Event) -> None:
        """Send or raise; return promptly when stop is set so shutdown can drain."""


class PushDispatcher:
    def __init__(self, state, user_id, sender: PushSender, *, stop=None, is_active=None, hold=30.0):
        self.path = Path(state).resolve() / "network" / "push.json"
        self.user_id = user_id
        self.sender = sender
        self.stop = stop if stop is not None else threading.Event()
        self.is_active = is_active
        # 回执推送先延后 hold 秒，等同轮汇报（report.delivered）合并；超时仍推，不丢。
        self.hold = hold

    def dispatch(self, rows, devices, *, now):
        """Consume ordered events and tick persisted incident timers, without APNs I/O of our own.

        Transport failures raise; the caller may retry. Delivery is at least once
        across a crash between a successful send and its local checkpoint.
        """
        try:
            checkpoint = json.loads(events.physical(self.path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            checkpoint = dict(cursor=0, incidents={})
        incidents = checkpoint["incidents"]
        held = checkpoint.setdefault("held", {})
        pushed = checkpoint.setdefault("pushed", [])
        changed = False
        for row in rows:
            if row["seq"] <= checkpoint["cursor"]:
                continue
            changed = True
            data = row["data"]
            if data.get("user_id") in (None, "", self.user_id):
                job_key = "job:" + data["job"] if data.get("job") else None
                if row["type"] == "receipt.ready":
                    incidents.pop(job_key, None)
                    round_key = "%s:%s" % (data.get("job"), data.get("round"))
                    if not data.get("suppress_push") and round_key not in pushed:
                        if data.get("outcome") == "blocked":
                            # 卡住需要立刻处理：即时推送；同轮后到的汇报因已推送而不再重复。
                            self.deliver("blocked", devices)
                            self.remember(pushed, [round_key])
                        else:
                            held[round_key] = dict(since=datetime.fromisoformat(row["time"]).timestamp())
                elif row["type"] == "report.delivered":
                    # 汇报的 app 通道：只发计数推送，负载不含汇报正文与执行方文字。
                    if "app" in (data.get("channels") or []):
                        rounds = [key for key in (data.get("rounds") or []) if isinstance(key, str)]
                        for key in rounds:
                            held.pop(key, None)
                        # 汇报覆盖的轮次都已单独推过时不再重复；无轮次信息的旧事件照常推送。
                        if not rounds or any(key not in pushed for key in rounds):
                            self.deliver("report_ready", devices)
                        self.remember(pushed, rounds)
                elif row["type"] in ("job.state_changed", "host.changed"):
                    key = job_key or "host:" + str(data.get("host"))
                    state = data.get("execution_state") if job_key else (
                        "unreachable" if data.get("reachable") is False else "running")
                    if state not in ("dead", "unreachable") or data.get("suppress_push"):
                        incidents.pop(key, None)
                    else:
                        prior = incidents.get(key)
                        if prior is None or prior["round"] != data.get("round"):
                            prior = dict(since=datetime.fromisoformat(row["time"]).timestamp(),
                                         round=data.get("round"), sent=False)
                            incidents[key] = prior
                        prior["state"] = state
            checkpoint["cursor"] = row["seq"]
            messages.atomic_json(self.path, checkpoint, limit=None)
        for key, item in list(held.items()):
            if now - item["since"] >= self.hold:
                self.deliver("receipt_ready", devices)
                held.pop(key)
                self.remember(pushed, [key])
                changed = True
        for incident in incidents.values():
            if not incident["sent"] and now - incident["since"] > 300:
                self.deliver(incident["state"], devices)
                incident["sent"] = True
                changed = True
        if changed:
            messages.atomic_json(self.path, checkpoint, limit=None)
        return checkpoint["cursor"]

    @staticmethod
    def remember(pushed, keys):
        pushed.extend(key for key in keys if key not in pushed)
        del pushed[:-500]

    def deliver(self, reason, devices):
        if self.stop.is_set():
            raise PushCancelled()
        for device in devices:
            if self.stop.is_set():
                raise PushCancelled()
            if (device.get("push_token") and not device.get("revoked")
                    and (self.is_active is None or self.is_active(device))):
                self.sender.send(device["push_token"], {"aps": {"content-available": 1}, "counts": {reason: 1}}, stop=self.stop)
                if self.stop.is_set():
                    raise PushCancelled()
