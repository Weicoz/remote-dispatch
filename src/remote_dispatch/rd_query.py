"""Read-only job queries shared by CLI and service adapters.

Callbacks read execution snapshots, reports and receiver sessions from the target.
No method creates directories, takes write locks, caches receipts or edits records.
"""
import hashlib
import json
from pathlib import Path

from . import rd_messages as messages
from . import rd_usage as usage
from .rd_projects import ProjectIndex, project_fields


def result_path(state, rec, number):
    return (Path(state).resolve() / "results" / messages.job_id(rec["job"])
            / ("r" + str(messages.round_id(number)) + ".json"))


def receipt_id(receipt):
    content = {k: v for k, v in receipt.items() if k not in ("receipt_id", "captured_at")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Query:
    def __init__(self, state, *, states, report, receiver, project_names=(), read_json=None, output=None):
        self.state = Path(state).resolve()
        self.read_json = read_json or messages.read_json
        self.states = states
        self.report = report
        self.receiver = receiver
        # 读本轮 harness 输出日志文本的回调；缺省或读不到时模型与用量记为「未记录」。
        self.output = output
        self.project_names = project_names
        # A Query is a single read view. Resolve legacy project names once,
        # even when the adapter renders its records one card at a time.
        self.names_by_user = {}

    def projects(self, user_id):
        """Return persistent project history, including hidden projects."""
        return sorted(ProjectIndex(self.state, user_id).read().values(), key=lambda row: (row["name"], row["id"]))

    def jobs(self, records, snapshots=None, *, user_id=None):
        """Return job records overlaid with their execution snapshot fields."""
        if snapshots is None:
            snapshots = self.states(records)
        known = set(self.project_names) | {rec["project"] for rec in records if rec.get("project")}
        names_by_user = self.names_by_user
        rows = []
        for rec in records:
            uid = rec.get("user_id") or user_id
            if uid not in names_by_user:
                names = set(known)
                if uid:
                    names.update(row["name"] for row in self.projects(uid))
                    for path in (Path(self.state) / "jobs").glob("*.json"):
                        saved = self.read_json(path)
                        if saved and saved.get("project") and saved.get("user_id") in (None, "", uid):
                            names.add(saved["project"])
                names_by_user[uid] = names
            names_by_user[uid].update(known)
            row = dict(rec, **project_fields(rec, uid, names_by_user[uid]))
            snapshot = snapshots.get(rec["job"], {})
            row.update(snapshot)
            number = int(snapshot.get("round") or 0) or rec["rounds"]
            row.update(self.review(rec, number))
            row.update(self.usage_fields(self.read_json(self.result_path(rec, number)), rec))
            rows.append(row)
        return rows

    def result(self, rec, number=None, snapshot=None):
        """Return a cached or in-memory receipt, or None while unavailable.

        An uncached historical round cannot be reconstructed from current state.
        Existing receipt IDs remain unchanged. New IDs cover receipt content,
        excluding query time so a later explicit ack can materialize the receipt.
        """
        number = messages.round_id(number if number is not None else rec["rounds"])
        cached = self.read_json(self.result_path(rec, number))
        if cached is not None:
            return dict(cached, **self.usage_fields(cached, rec), **self.review(rec, number))
        if snapshot is None:
            snapshot = self.states([rec]).get(rec["job"], {})
        current = (int(snapshot.get("round") or 0) or rec["rounds"])
        if number != current:
            raise ValueError("该历史轮次尚无本地回执；先在原轮次收取结果。")
        if snapshot.get("state") not in ("done", "dead", "missing"):
            return None
        report = self.report(rec, number, "result") if snapshot["state"] == "done" and rec.get("handoff_version") else None
        code = snapshot.get("exit")
        receiver = rec.get("receiver_session")
        if rec.get("agent") and snapshot["state"] == "done":
            receiver = self.receiver(rec) or receiver
        receipt = dict(schema=1, job=rec["job"], round=number, host=rec["host"],
                       initiator_session=rec.get("initiator_session"), execution_state=snapshot["state"],
                       executor_exit=int(code) if code not in (None, "") else None,
                       receiver_session=receiver, report=report,
                       head=snapshot.get("head"), dirty=snapshot.get("dirty"),
                       needs_initiator_review=True)
        receipt.update({key: rec[key] for key in ("user_id", "project", "origin", "created", "agent", "model") if key in rec})
        receipt.update(self.measured(rec, number))
        receipt["receipt_id"] = receipt_id(receipt)
        receipt["captured_at"] = messages.utc_now()
        return dict(receipt, **self.review(rec, number))

    def measured(self, rec, number):
        """从本轮 harness 输出取实际模型与用量；任何读取失败都记「未记录」。"""
        text = None
        if self.output and rec.get("agent"):
            try:
                text = self.output(rec, number)
            except Exception:
                text = None
        return usage.describe(rec.get("agent"), text, rec.get("model"))

    @staticmethod
    def usage_fields(receipt, rec):
        """老记录缺字段时显示「未记录」，不迁移数据。"""
        receipt = receipt or {}
        return dict(model=receipt.get("model") or rec.get("model") or usage.UNRECORDED,
                    usage=receipt.get("usage") or usage.UNRECORDED)

    def review(self, rec, number):
        return self.read_json(self.result_path(rec, number).with_suffix(".review.json")) or dict(review_state="pending")

    def result_path(self, rec, number):
        return self.state / "results" / messages.job_id(rec["job"]) / f"r{messages.round_id(number)}.json"

    def acknowledgement(self, rec, receipt):
        """Read the matching acknowledgement, including legacy records."""
        ack = self.read_json(self.result_path(rec, receipt["round"]).with_suffix(".ack.json"))
        return ack if ack and ack.get("receipt_id") == receipt["receipt_id"] else None

    def delivery(self, rec, receipt):
        return "received" if self.acknowledgement(rec, receipt) else "pending"

    def details(self, rec, snapshot=None, *, user_id=None):
        """Return execution, project, reports, matching acknowledgement and delivery state."""
        if snapshot is None:
            snapshot = self.states([rec]).get(rec["job"], {})
        number = (int(snapshot.get("round") or 0) or rec["rounds"])
        progress = self.report(rec, number, "progress") if rec.get("handoff_version") and snapshot.get("state") in ("running", "done") else None
        receipt = self.result(rec, number, snapshot)
        project = self.jobs([rec], {}, user_id=user_id)[0]
        return dict(job=rec["job"], project=project["project"], project_id=project["project_id"],
                    initiator_session=rec.get("initiator_session"), execution=snapshot,
                    progress=progress, result=receipt,
                    acknowledgement=self.acknowledgement(rec, receipt) if receipt else None,
                    delivery_state=self.delivery(rec, receipt) if receipt else "not_available",
                    **self.review(rec, number))
