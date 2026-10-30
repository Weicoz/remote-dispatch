"""控制端汇报：消费状态事件、持久去重与按发起会话合并，存入 inbox 并发布 report.created。

汇报不写入任何会话输入框；提醒由 notify 单一入口记录结果。
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures

from . import rd_digest as digest
from . import rd_events as events
from . import rd_messages as messages
from . import rd_notify as notifier
from .rd_query import result_path
from .rd_serve_history import instant


STATE_TEXT = {"completed": "已完成", "blocked": "卡住", "offline": "离线", "failed": "失败"}


def store_path(state, user_id):
    return Path(state).resolve() / "reports" / (messages.job_id(user_id) + ".json")


def read_reports(state, user_id, owner=None):
    saved = messages.read_json(events.physical(store_path(state, user_id)), limit=None) or {}
    rows = [row for row in saved.get("reports", []) if owner is None or row["initiator_session"] == owner]
    for row in rows:
        row.setdefault("delivery", dict(status="queued", reason=None, pane_id=None, delivered_at=None))
    return rows


class Reports:
    def __init__(self, service):
        self.service = service
        self.path = store_path(service.state, service.user_id)
        self.ready = threading.Event()
        self.thread = None
        self.reader = events.EventCursor(service.state)
        self.batch = None
        self.lock = threading.RLock()
        self.pool = None
        self.futures = []
        self.started_at = messages.utc_now()
        try:
            self.window = service.config.cp.getfloat("monitor", "report_merge_window_seconds", fallback=5)
        except ValueError:
            raise ValueError("汇报合并窗口必须为非负秒数。") from None
        if not math.isfinite(self.window) or self.window < 0:
            raise ValueError("汇报合并窗口必须为非负秒数。")

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        if self.pool is not None:
            # 有界等待：慢通道不得拖住 serve 关闭。
            wait_futures(self.futures, timeout=10)
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None
            self.futures = []

    def run(self):
        while not self.service.stop.is_set():
            if self.ready.is_set():
                try:
                    self.tick(background=True)
                except (OSError, ValueError, KeyError, TypeError):
                    print("rdispatch: 无法生成汇报；请检查控制端状态目录。", file=sys.stderr)
            self.service.stop.wait(0.25)

    def tick(self, *, now=None, background=False):
        with self.lock:
            self.step(now, background)

    def step(self, now, background):
        now = time.time() if now is None else now
        saved = messages.read_json(events.physical(self.path), limit=None)
        records = {rec["job"]: rec for rec in self.service.records()}
        if saved is None:
            self.initialize(records, now)
            return
        before = copy.deepcopy(saved)
        if self.batch is None:
            self.reader.seq = saved["cursor"]
            self.batch = self.reader.read()
        for event in self.batch:
            data = event["data"]
            rec = records.get(data.get("job"))
            if (event["type"] in ("receipt.ready", "job.state_changed", "progress.updated") and rec is not None
                    and data.get("user_id") in (None, "", self.service.user_id)):
                number = data.get("round", rec["rounds"])
                receipt = messages.read_json(result_path(self.service.state, rec, number)) or {}
                key = rec["job"] + ":" + str(number)
                if event["type"] == "progress.updated":
                    saved["progress"][key] = self.service.redact(data.get("summary", ""))
                else:
                    snapshot = dict(state=data.get("execution_state", "done"), exit=data.get("executor_exit"))
                    self.observe(saved, dict(rec, host=data.get("host") or rec["host"]), number,
                                 snapshot, receipt, now, observed_at=event["time"])
            saved["cursor"] = event["seq"]
        if now >= saved.get("scan_due", 0):
            query = self.service.query()
            for rec in records.values():
                snapshot = query.states([rec]).get(rec["job"], {})
                number = int(snapshot.get("round") or rec["rounds"])
                receipt = messages.read_json(result_path(self.service.state, rec, number)) or {}
                stamp = snapshot.get("snapshot_at")
                key = rec["job"] + ":" + str(number)
                # A stale cache must not undo a newer state event during collection.
                previous = saved["observed_at"].get(key)
                if previous and (not stamp or instant(stamp) is None or instant(stamp) < instant(previous)):
                    continue
                if snapshot.get("state") in ("starting", None):
                    continue
                progress = query.report(rec, number, "progress")
                if progress:
                    saved["progress"][key] = self.service.redact(progress.get("summary", ""))
                self.observe(saved, rec, number, snapshot, receipt, now, observed_at=stamp)
            saved["scan_due"] = now + 60
        known = len(saved["reports"])
        created = digest.step(self.service, saved, now)
        for owner, group in list(saved["pending"].items()):
            if now < group["due"]:
                continue
            content = [owner, sorted("{job}:{round}:{state}".format(**entry) for entry in group["entries"]),
                       group["due"]]
            report_id = hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            row = dict(id=report_id, initiator_session=owner, created_at=messages.utc_now(), entries=group["entries"])
            saved["reports"].append(row)
            created.append(dict(report_id=report_id, user_id=self.service.user_id,
                                initiator_session=owner, job_count=len(row["entries"])))
            del saved["pending"][owner]
        fresh = []
        for row in saved["reports"][known:]:
            # 先持久化「排队中」的 delivery，再通知；事件写入失败后的重试不会对同一汇报重复通知。
            row.setdefault("delivery", dict(status="queued", channels=[]))
            fresh.append(copy.deepcopy(row))
        if saved != before:
            with events.write_transaction([self.path]):
                messages.atomic_json(self.path, saved, limit=None)
                for data in created:
                    events.emit(self.service.state, "report.created", data)
        # Retain the batch on failure, so an in-process retry cannot lose events
        # already consumed by the incremental cursor.
        self.batch = None
        for row in fresh:
            if background:
                if self.pool is None:
                    self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rd-notify")
                self.futures = [f for f in self.futures if not f.done()]
                self.futures.append(self.pool.submit(self.deliver, row))
            else:
                self.deliver(row)

    def deliver(self, row):
        """调用提醒通道（可能较慢，不持锁），再把结果回写 delivery 并发布 report.delivered。"""
        try:
            result = self.notify(None, row)
        except Exception:
            result = dict(status="inbox_only", channels=[])
        with self.lock:
            saved = messages.read_json(events.physical(self.path), limit=None)
            if not saved:
                return
            target = next((r for r in saved["reports"] if r["id"] == row["id"]), None)
            if target is None:
                return
            target["delivery"] = result
            try:
                with events.write_transaction([self.path]):
                    messages.atomic_json(self.path, saved, limit=None)
                    if result["status"] == "notified":
                        events.emit(self.service.state, "report.delivered", dict(
                            user_id=self.service.user_id, report_id=row["id"],
                            initiator_session=row["initiator_session"],
                            rounds=["%s:%s" % (e["job"], e["round"]) for e in row["entries"]],
                            channels=[c["channel"] for c in result["channels"] if c["ok"]],
                            delivered_at=messages.utc_now()))
            except (OSError, ValueError):
                print("rdispatch: 无法记录汇报送达结果。", file=sys.stderr)

    def initialize(self, records, now):
        """首次启用只记录当前状态；历史事件和终态不变成新汇报。"""
        log = self.reader.log
        with events.EventLock(self.service.state):
            cursor, _ = events.tail(log.path)
            if not cursor:
                archives = log.archives()
                if archives:
                    cursor, _ = events.tail(archives[-1])
        saved = dict(cursor=cursor, states={}, observed_at={}, progress={}, pending={}, reports=[],
                     baseline_pending=[], baseline_started_at=self.started_at, scan_due=now + 60,
                     digest=dict(due=now + digest.interval(self.service.config), fingerprints={}))
        query = self.service.query()
        for rec in records.values():
            snapshot = query.states([rec]).get(rec["job"], {})
            number = int(snapshot.get("round") or rec["rounds"])
            receipt = messages.read_json(result_path(self.service.state, rec, number)) or {}
            stamp = instant(snapshot.get("snapshot_at"))
            fresh = stamp is not None and stamp >= instant(self.started_at)
            execution = snapshot.get("state")
            if (execution in ("starting", None) or execution == "done" and not receipt
                    or execution != "done" and not fresh):
                saved["baseline_pending"].append(rec["job"] + ":" + str(number))
                continue
            self.observe(saved, rec, number, snapshot, receipt, now, observed_at=snapshot.get("snapshot_at"))
        saved["pending"] = {}
        messages.atomic_json(self.path, saved, limit=None)

    def notify(self, saved, row):
        """生成汇报后的提醒步骤，返回要写入 row["delivery"] 的结果。

        这是唯一入口：按 [monitor] notify 分派通道，各通道结果追加到 channels。
        没有成功通道时为 inbox_only；绝不写入会话输入框。
        """
        return notifier.dispatch(self.service, row)

    def observe(self, saved, rec, number, snapshot, receipt, now, *, observed_at=None):
        report = receipt.get("report") or {}
        execution = snapshot.get("state")
        if execution == "done" and not receipt:
            # receipt.ready follows collection; do not briefly report a blocked round as completed.
            return
        state = ("offline" if execution == "unreachable" else "failed" if execution in ("dead", "missing")
                 else "blocked" if execution == "done" and report.get("outcome") == "blocked"
                 else "failed" if execution == "done" and (report.get("outcome") == "failed" or
                      receipt.get("executor_exit") not in (None, 0, "0"))
                 else "completed" if execution == "done" else "running")
        key = rec["job"] + ":" + str(number)
        if state == "offline" and saved["states"].get(key) in ("completed", "failed", "blocked"):
            # 已报终态的轮次不再因连接不可达改状态或生成「离线」汇报。
            return
        if key in saved.get("baseline_pending", []):
            stamp = instant(observed_at)
            if stamp is None or stamp < instant(saved["baseline_started_at"]):
                return
        if observed_at:
            saved["observed_at"][key] = observed_at
        if key in saved.get("baseline_pending", []):
            # First target collection may finish after the reports thread starts.
            # Its first trustworthy observation belongs to the upgrade baseline.
            saved["baseline_pending"].remove(key)
            saved["states"][key] = state
            return
        if saved["states"].get(key) == state:
            return
        saved["states"][key] = state
        if state == "running":
            return
        owner = rec.get("initiator_session")
        if not owner or owner.startswith("app:"):
            return
        summary = report.get("summary") if execution == "done" else saved["progress"].get(key)
        if state == "failed" and (execution != "done" or not summary):
            code = receipt.get("executor_exit") if execution == "done" else snapshot.get("exit")
            summary = "执行进程已退出（退出码 %s）。" % (code if code is not None else "未知")
        summary = summary or {"offline": "目标机离线，状态无法确认。", "failed": "本轮执行失败。",
                              "blocked": "本轮卡住，等待发起会话处理。", "completed": "本轮已完成。"}[state]
        entry = self.service.redact(dict(job=rec["job"], title=rec.get("title") or rec["job"],
            host=rec["host"], round=number, state=state, state_text=STATE_TEXT[state], summary=summary))
        group = saved["pending"].setdefault(owner, dict(due=now + self.window, entries=[]))
        group["entries"].append(entry)
