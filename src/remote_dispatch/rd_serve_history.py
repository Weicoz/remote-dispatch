"""Read retained dispatch metadata and receipts without materializing query state."""
import re
from datetime import datetime, timezone
from . import rd_messages as messages


def instant(value):
    if not value or not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Legacy dispatch timestamps are controller-local wall times.
        return stamp.astimezone(timezone.utc)
    except ValueError:
        return None


def records(state, current, user_id):
    rows = {}
    paths = [path for path in (state / "results").glob("*/r*.json") if re.fullmatch(r"r[1-9][0-9]*\.json", path.name)]
    for path in sorted(paths, key=lambda path: (path.parent.name, int(path.stem[1:]))):
        receipt = messages.read_json(path)
        if receipt and receipt.get("job") == path.parent.name:
            row = rows.setdefault(receipt["job"], {})
            row.update({key: receipt[key] for key in
                        ("job", "user_id", "project", "origin", "created", "host", "agent", "model") if key in receipt})
            row["rounds"] = max(row.get("rounds", 1), int(receipt["round"]))
    for path in sorted((state / "results").glob("*/dispatch.json")):
        manifest = messages.read_json(path)
        if manifest and manifest.get("job") == path.parent.name:
            row = rows.setdefault(manifest["job"], {})
            latest = {key: row[key] for key in ("host", "agent", "model") if row.get(key) is not None}
            row.update(manifest)
            row.update(latest)
    for row in rows.values():
        row["archived"] = True
    for rec in current:
        rows[rec["job"]] = dict(rec, archived=False)
    result = []
    for rec in rows.values():
        messages.job_id(rec["job"])
        registered = messages.read_json(state / "jobs" / (rec["job"] + ".json")) or {}
        owner = registered.get("user_id") or rec.get("user_id")
        if owner not in (None, "", user_id):
            continue
        rec.setdefault("rounds", 1)
        rec.setdefault("host", None)
        result.append(rec)
    return result
