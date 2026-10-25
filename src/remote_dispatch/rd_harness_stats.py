"""Raw harness statistics from retained rounds; queries never publish events."""
from datetime import datetime

from . import rd_events as events
from . import rd_messages as messages
from .rd_query import result_path
from .rd_serve_history import instant, records as history_records


def statistics(service, recent=20):
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    query = service.query()
    groups = {}
    for rec in history_records(service.state, service.records(), service.user_id):
        harness = rec.get("agent")
        if not harness:
            continue
        group = groups.setdefault(harness, dict(finished=[], current=[]))
        current_receipt = None
        for number in range(1, rec["rounds"] + 1):
            receipt = messages.read_json(result_path(service.state, rec, number))
            if not receipt:
                continue
            if number == rec["rounds"]:
                current_receipt = receipt
            report = receipt.get("report") or {}
            end_text = report.get("finished_at") or report.get("updated_at") or receipt.get("captured_at")
            end = instant(end_text)
            start = instant(report.get("started_at"))
            duration = (end - start).total_seconds() if start and end and end >= start else None
            group["finished"].append(dict(job=rec["job"], round=number, end=end, end_text=end_text,
                duration=duration, outcome=report.get("outcome"), checks=report.get("checks"),
                review_state=query.review(rec, number)["review_state"]))
        if not rec.get("archived") and not current_receipt:
            snapshot = query.states([rec]).get(rec["job"], {})
            if snapshot.get("state") in ("starting", "running"):
                start = instant(snapshot.get("started_at"))
                elapsed = max(0, (now - start).total_seconds()) if start else None
                group["current"].append(dict(job=rec["job"], round=rec["rounds"], elapsed_seconds=elapsed))
    rows = []
    for harness, group in sorted(groups.items()):
        finished = sorted(group["finished"], key=lambda row: (row["end"].timestamp() if row["end"] else 0, row["job"], row["round"]))
        durations = [row["duration"] for row in finished[-recent:] if row["duration"] is not None]
        last = finished[-1] if finished else {}
        accepted, rejected = 0, 0
        for row in reversed(finished):
            if row["review_state"] == "accepted" and not rejected:
                accepted += 1
            elif row["review_state"] == "rejected" and not accepted:
                rejected += 1
            else:
                break
        rows.append(dict(harness=harness, today_round_count=sum(1 for row in finished if row["end"] and today <= row["end"] <= now),
            recent_round_limit=recent, duration_sample_count=len(durations),
            average_duration_seconds=sum(durations) / len(durations) if durations else None,
            current_rounds=sorted(group["current"], key=lambda row: (row["job"], row["round"])),
            consecutive_accepted=accepted, consecutive_rejected=rejected,
            last_result=last.get("outcome"), last_checks=last.get("checks"), last_job=last.get("job"),
            last_round=last.get("round"), last_finished_at=last.get("end_text")))
    return rows


def observe(service):
    """Explicit background write boundary, independent of current elapsed time."""
    log = events.EventLog(service.state)
    for row in statistics(service):
        if row["last_job"] is not None:
            log.publish("harness.stats_changed", dict(row, user_id=service.user_id), only_if_changed=True)
