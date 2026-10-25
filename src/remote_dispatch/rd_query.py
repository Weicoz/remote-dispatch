"""Read-only job queries shared by CLI and service adapters.

Callbacks read execution snapshots, reports and receiver sessions from the target.
No method creates directories, takes write locks, caches receipts or edits records.
"""
import hashlib
import json
from pathlib import Path

from . import rd_messages as messages
from .rd_projects import ProjectIndex, project_fields


def result_path(state, rec, number):
    return (Path(state).resolve() / "results" / messages.job_id(rec["job"])
            / ("r" + str(messages.round_id(number)) + ".json"))


def receipt_id(receipt):
    content = {k: v for k, v in receipt.items() if k not in ("receipt_id", "captured_at")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Query:
    def __init__(self, state, *, states, report, receiver, project_names=()):
        self.state = state
        self.states = states
        self.report = report
        self.receiver = receiver
        self.project_names = project_names

    def projects(self, user_id):
        """Return persistent project history, including hidden projects."""
        return sorted(ProjectIndex(self.state, user_id).read().values(), key=lambda row: (row["name"], row["id"]))

    def jobs(self, records, snapshots=None, *, user_id=None):
        """Return job records overlaid with their execution snapshot fields."""
        if snapshots is None:
            snapshots = self.states(records)
        known = set(self.project_names) | {rec["project"] for rec in records if rec.get("project")}
        names_by_user = {}
        rows = []
        for rec in records:
            uid = rec.get("user_id") or user_id
            if uid not in names_by_user:
                names = set(known)
                if uid:
                    names.update(row["name"] for row in self.projects(uid))
                    for path in (Path(self.state) / "jobs").glob("*.json"):
                        saved = messages.read_json(path)
                        if saved and saved.get("project") and saved.get("user_id") in (None, "", uid):
                            names.add(saved["project"])
                names_by_user[uid] = names
            row = dict(rec, **project_fields(rec, uid, names_by_user[uid]))
            row.update(snapshots.get(rec["job"], {}))
            rows.append(row)
        return rows

    def result(self, rec, number=None, snapshot=None):
        """Return a cached or in-memory receipt, or None while unavailable.

        An uncached historical round cannot be reconstructed from current state.
        Existing receipt IDs remain unchanged. New IDs cover receipt content,
        excluding query time so a later explicit ack can materialize the receipt.
        """
        number = messages.round_id(number if number is not None else rec["rounds"])
        cached = messages.read_json(result_path(self.state, rec, number))
        if cached is not None:
            return cached
        if snapshot is None:
            snapshot = self.states([rec]).get(rec["job"], {})
        current = int(snapshot.get("round") or rec["rounds"])
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
        receipt["receipt_id"] = receipt_id(receipt)
        receipt["captured_at"] = messages.utc_now()
        return receipt

    def acknowledgement(self, rec, receipt):
        """Read the matching acknowledgement, including legacy records."""
        ack = messages.read_json(result_path(self.state, rec, receipt["round"]).with_suffix(".ack.json"))
        return ack if ack and ack.get("receipt_id") == receipt["receipt_id"] else None

    def delivery(self, rec, receipt):
        return "received" if self.acknowledgement(rec, receipt) else "pending"

    def details(self, rec, snapshot=None, *, user_id=None):
        """Return execution, project, reports, matching acknowledgement and delivery state."""
        if snapshot is None:
            snapshot = self.states([rec]).get(rec["job"], {})
        number = int(snapshot.get("round") or rec["rounds"])
        progress = self.report(rec, number, "progress") if rec.get("handoff_version") and snapshot.get("state") in ("running", "done") else None
        receipt = self.result(rec, number, snapshot)
        project = self.jobs([rec], {}, user_id=user_id)[0]
        return dict(job=rec["job"], project=project["project"], project_id=project["project_id"],
                    initiator_session=rec.get("initiator_session"), execution=snapshot,
                    progress=progress, result=receipt,
                    acknowledgement=self.acknowledgement(rec, receipt) if receipt else None,
                    delivery_state=self.delivery(rec, receipt) if receipt else "not_available")
