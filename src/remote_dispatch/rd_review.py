"""Immutable per-round acceptance records. Callers hold the outer job lock."""
from contextlib import contextmanager
import os
from pathlib import Path
from . import rd_messages as messages
from .rd_query import result_path


def review_path(state, rec, number):
    return result_path(state, rec, number).with_suffix(".review.json")


def read(state, rec, number):
    return messages.read_json(review_path(state, rec, number)) or dict(review_state="pending")


def decide(state, rec, number, receipt, action, reviewer, note):
    previous = read(state, rec, number)
    if previous["review_state"] != "pending":
        raise ValueError("already_reviewed: 已由 {reviewed_by} 于 {reviewed_at} 验收为 {review_state}".format(**previous))
    outcome = (receipt.get("report") or {}).get("outcome")
    if action == "accepted" and (receipt.get("execution_state") != "done"
                                 or receipt.get("executor_exit") not in (None, 0)
                                 or outcome not in ("completed", "partial")):
        raise ValueError("review_not_allowed: 此回执不可通过；可打回或作废。")
    return dict(review_state=action, reviewed_by=reviewer,
                reviewed_at=messages.utc_now(), review_note=note,
                checks_at_review=(receipt.get("report") or {}).get("checks", "not_reported"))


@contextmanager
def rollback_files(paths):
    """Restore local receipt, acknowledgement and job files if an operation fails."""
    saved = {}
    for value in paths:
        path = Path(value)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("回执路径不得是符号链接。")
        saved[path] = (path.read_bytes(), path.stat()) if path.exists() else None
    try:
        yield
    except (Exception, SystemExit, KeyboardInterrupt):
        for path, previous in saved.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                if not path.exists() or path.read_bytes() != previous[0]:
                    path.write_bytes(previous[0])
                os.utime(path, ns=(previous[1].st_atime_ns, previous[1].st_mtime_ns))
        raise
