"""Raw harness statistics from retained rounds; queries never publish events."""
from datetime import datetime, timedelta

from . import rd_events as events
from . import rd_messages as messages
from .rd_query import result_path
from .rd_serve_history import instant, records as history_records


def finished_at(receipt):
    report = receipt.get("report") or {}
    return report.get("finished_at") or report.get("updated_at") or receipt.get("captured_at")


def host_statistics(service):
    """Ended rounds by controller-local calendar date, including today's partial day."""
    now = datetime.now().astimezone()
    dates = [(now.date() - timedelta(days=offset)).isoformat() for offset in range(13, -1, -1)]
    counts = {host["name"]: dict.fromkeys(dates, 0) for host in service.config.hosts()}
    for rec in history_records(service.state, service.records(), service.user_id):
        if rec.get("host"):
            counts.setdefault(rec["host"], dict.fromkeys(dates, 0))
        for number in range(1, rec["rounds"] + 1):
            receipt = messages.read_json(result_path(service.state, rec, number))
            if not receipt:
                continue
            host = receipt.get("host") or rec.get("host")
            if not host:
                continue
            days = counts.setdefault(host, dict.fromkeys(dates, 0))
            end = instant(finished_at(receipt))
            date = end.astimezone().date().isoformat() if end else None
            if date in days and end <= now:
                days[date] += 1
    return [dict(host=host, daily_round_counts=[dict(date=date, round_count=count)
                for date, count in days.items()]) for host, days in sorted(counts.items())]


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
            end_text = finished_at(receipt)
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
        rows.append(dict(harness=harness, total_round_count=len(finished), today_round_count=sum(1 for row in finished if row["end"] and today <= row["end"] <= now),
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
    hosts = host_statistics(service)
    rows = statistics(service)
    # Commands also contribute to machine history when there is no named harness.
    if not rows and hosts:
        rows = [dict(harness=None)]
    for row in rows:
        log.publish("harness.stats_changed", dict(row, user_id=service.user_id,
                    date_basis="controller_local", host_stats=hosts), only_if_changed=True)
