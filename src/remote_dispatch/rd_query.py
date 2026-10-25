"""Read-only job queries shared by CLI and service adapters.

Callbacks read execution snapshots, reports and receiver sessions from the target.
No method creates directories, takes write locks, caches receipts or edits records.
"""
import hashlib
import json
from pathlib import Path

from . import rd_messages as messages


def result_path(state, rec, number):
    return (Path(state).resolve() / "results" / messages.job_id(rec["job"])
            / ("r" + str(messages.round_id(number)) + ".json"))


class Query:
    def __init__(self, state, *, states, report, receiver):
        self.state = state
        self.states = states
        self.report = report
        self.receiver = receiver

    def jobs(self, records, snapshots=None):
        """Return job records overlaid with their execution snapshot fields."""
        if snapshots is None:
            snapshots = self.states(records)
        return [dict(rec, **snapshots.get(rec["job"], {})) for rec in records]

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
        receipt["receipt_id"] = hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        receipt["captured_at"] = messages.utc_now()
        return receipt

    def delivery(self, rec, receipt):
        ack = messages.read_json(result_path(self.state, rec, receipt["round"]).with_suffix(".ack.json"))
        return "received" if ack and ack.get("receipt_id") == receipt["receipt_id"] else "pending"

    def details(self, rec, snapshot=None):
        """Return {job, initiator_session, execution, progress, result, delivery_state}."""
        if snapshot is None:
            snapshot = self.states([rec]).get(rec["job"], {})
        number = int(snapshot.get("round") or rec["rounds"])
        progress = self.report(rec, number, "progress") if rec.get("handoff_version") and snapshot.get("state") in ("running", "done") else None
        receipt = self.result(rec, number, snapshot)
        return dict(job=rec["job"], initiator_session=rec.get("initiator_session"), execution=snapshot,
                    progress=progress, result=receipt,
                    delivery_state=self.delivery(rec, receipt) if receipt else "not_available")
