"""控制端汇报：消费状态事件、持久去重与按发起会话合并，不写入会话。"""
import copy
import hashlib
import math
from pathlib import Path
import sys
import threading
import time

from . import rd_events as events
from . import rd_messages as messages
from .rd_query import result_path
from .rd_serve_history import instant


STATE_TEXT = {"completed": "已完成", "blocked": "卡住", "offline": "离线", "failed": "失败"}


def store_path(state, user_id):
    return Path(state).resolve() / "reports" / (messages.job_id(user_id) + ".json")


def read_reports(state, user_id, owner=None):
    saved = messages.read_json(events.physical(store_path(state, user_id)), limit=None) or {}
    return [row for row in saved.get("reports", []) if owner is None or row["initiator_session"] == owner]


class Reports:
    def __init__(self, service):
        self.service = service
        self.path = store_path(service.state, service.user_id)
        self.ready = threading.Event()
        self.thread = None
        self.reader = events.EventCursor(service.state)
        self.batch = None
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

    def run(self):
        while not self.service.stop.is_set():
            if self.ready.is_set():
                try:
                    self.tick()
                except (OSError, ValueError, KeyError, TypeError):
                    print("rdispatch: 无法生成汇报；请检查控制端状态目录。", file=sys.stderr)
            self.service.stop.wait(0.25)

    def tick(self, *, now=None):
        now = time.time() if now is None else now
        saved = messages.read_json(events.physical(self.path), limit=None) or dict(cursor=0, states={}, observed_at={}, progress={}, pending={}, reports=[])
        before = copy.deepcopy(saved)
        records = {rec["job"]: rec for rec in self.service.records()}
        sessions = self.service.sessions.list()
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
                                 snapshot, receipt, sessions, now, observed_at=event["time"])
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
                self.observe(saved, rec, number, snapshot, receipt, sessions, now, observed_at=stamp)
            saved["scan_due"] = now + 60
        created = []
        for owner, group in list(saved["pending"].items()):
            if not any(row.get("session_id") == owner for row in sessions):
                del saved["pending"][owner]
                continue
            if now < group["due"]:
                continue
            report_id = hashlib.sha256(str(group).encode()).hexdigest()
            row = dict(id=report_id, initiator_session=owner, created_at=messages.utc_now(), entries=group["entries"])
            saved["reports"].append(row)
            created.append(dict(report_id=report_id, user_id=self.service.user_id,
                                initiator_session=owner, job_count=len(row["entries"])))
            del saved["pending"][owner]
        if saved != before:
            with events.write_transaction([self.path]):
                messages.atomic_json(self.path, saved, limit=None)
                for data in created:
                    events.emit(self.service.state, "report.created", data)
        # Retain the batch on failure, so an in-process retry cannot lose events
        # already consumed by the incremental cursor.
        self.batch = None

    def observe(self, saved, rec, number, snapshot, receipt, sessions, now, *, observed_at=None):
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
        if observed_at:
            saved["observed_at"][key] = observed_at
        if saved["states"].get(key) == state:
            return
        saved["states"][key] = state
        if state == "running":
            return
        owner = rec.get("initiator_session")
        if not owner or owner.startswith("app:") or not any(row.get("session_id") == owner for row in sessions):
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
