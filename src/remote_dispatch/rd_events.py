"""Controller event history shared by CLI writes and serve's future poller.

Only display fields cross this boundary. Reading never creates state or locks.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import re
import tempfile

from . import rd_messages as messages
from .rd_lock import JobLock


FIELDS = {
    "report.delivered": "user_id report_id initiator_session channels delivered_at",
    "report.created": "user_id report_id initiator_session job_count",
    "network.addresses_changed": "user_id spki_sha256 candidates",
    "device.changed": "user_id device_id revoked session_access_enabled",
    "job.created": "job user_id project_id project host round agent model initiator_session dispatch_state",
    "job.state_changed": "job user_id project_id project host round agent model dispatch_state execution_state executor_exit stopped_by stopped_at suppress_push",
    "progress.updated": "job user_id project_id host round phase summary updated_at",
    "receipt.ready": "job user_id project_id host round receipt_id execution_state executor_exit outcome checks suppress_push stopped_by stopped_at",
    "receipt.acked": "job user_id project_id host round receipt_id received_at acked_by via suppress_push",
    "review.updated": "job user_id project_id host round review_state reviewed_by reviewed_at",
    "reclaim.planned": "user_id host mode limit_gb used_gb estimated_gb blocked_count",
    "reclaim.result": "user_id host mode limit_gb used_gb after_gb slimmed_count deleted_count failed_count",
    "harness.stats_changed": "user_id harness total_round_count today_round_count recent_round_limit duration_sample_count average_duration_seconds consecutive_accepted consecutive_rejected last_result last_checks last_job last_round last_finished_at date_basis",
    "host.changed": "host reachable state load1 ncpu cpu_pct memfree disk disk_total running heavy full",
    "project.changed": "user_id project_id name first_dispatch_at last_dispatch_at dispatch_count hidden removed host accepted_round_count",
    "session.changed": "user_id pane_id session_id harness project project_id status status_text has_new_reply probable identity_text removed dispatch_defaults",
    "terminal.connect": "user_id pane_id device_id",
    "terminal.takeover": "user_id pane_id device_id",
    "terminal.resize": "user_id pane_id device_id cols rows",
    "terminal.disconnect": "user_id pane_id device_id reason input_bytes output_bytes dropped_frames restored",
    "session.message": "user_id pane_id session_id project_id harness message_id kind message_time",
}
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_pending = ContextVar("pending_events", default=None)


def physical(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("event log path must not be a symbolic link")
    return path


def tail(path):
    """Read only the last line and repair an interrupted append under EventLock."""
    try:
        with physical(path).open("r+b") as stream:
            stream.seek(0, 2)
            end = stream.tell()
            position, data = end, b""
            while position:
                start = max(0, position - 8192)
                stream.seek(start)
                data = stream.read(position - start) + data
                position = start
                if b"\n" in data:
                    break
            complete = data.rfind(b"\n") + 1
            if complete != len(data):
                end = position + complete
                stream.truncate(end)
                stream.flush()
                os.fsync(stream.fileno())
            data = data[:complete]
            while data.count(b"\n") < 2 and position:
                start = max(0, position - 8192)
                stream.seek(start)
                data = stream.read(position - start) + data
                position = start
            return (json.loads(data.splitlines()[-1])["seq"] if data else 0), end
    except FileNotFoundError:
        return 0, 0


def display_data(kind, data):
    if kind not in FIELDS:
        raise ValueError("unsupported event type")
    projected = {key: data[key] for key in FIELDS[kind].split() if key in data
                 and (data[key] is None or type(data[key]) in (str, int, float, bool))}
    if kind == "network.addresses_changed" and isinstance(data.get("candidates"), list):
        projected["candidates"] = [{"address": row["address"], "port": row["port"],
            **({"kind": row["kind"]} if row.get("kind") in ("lan", "tailscale", "easytier", "other") else {})}
            for row in data["candidates"] if isinstance(row, dict)
            and isinstance(row.get("address"), str) and type(row.get("port")) is int]
    if kind == "session.changed" and isinstance(data.get("dispatch_defaults"), dict):
        projected["dispatch_defaults"] = {key: data["dispatch_defaults"].get(key) for key in ("host", "harness")}
        if "dispatch_mode" in data["dispatch_defaults"]:
            projected["dispatch_defaults"]["dispatch_mode"] = data["dispatch_defaults"]["dispatch_mode"]
    if kind in ("reclaim.planned", "reclaim.result") and isinstance(data.get("actions"), list):
        projected["actions"] = [{key: row[key] for key in ("job", "action", "kb", "ok", "reason")
                                 if key in row and type(row[key]) in (str, int, bool)}
                                for row in data["actions"] if isinstance(row, dict)]
    if kind == "harness.stats_changed" and isinstance(data.get("host_stats"), list):
        projected["host_stats"] = [dict(host=row["host"], daily_round_counts=[
            dict(date=day["date"], round_count=day["round_count"])
            for day in row.get("daily_round_counts", []) if isinstance(day, dict)
            and isinstance(day.get("date"), str) and type(day.get("round_count")) is int])
            for row in data["host_stats"] if isinstance(row, dict) and isinstance(row.get("host"), str)]
    return projected


def job_data(rec, **fields):
    """Project a controller record before crossing the event boundary."""
    data = {key: rec[key] for key in ("job", "user_id", "project_id", "project", "host", "agent",
                                     "model", "initiator_session", "dispatch_state") if key in rec}
    data["round"] = rec.get("rounds", 1)
    data.update(fields)
    return data


def emit(state, kind, data):
    return EventLog(state).publish(kind, data)


@contextmanager
def transaction():
    """Publish only after a rollback-capable operation succeeds; no I/O lock held."""
    previous = _pending.get()
    pending = []
    token = _pending.set(pending)
    try:
        yield
    except BaseException:
        raise
    else:
        if previous is not None:
            previous.extend(pending)
        else:
            if pending:
                log = pending[0][0]
                if any(item[0].state != log.state for item in pending):
                    raise ValueError("event transaction must use one state directory")
                log.append_batch([(kind, data, changed) for _, kind, data, changed in pending])
    finally:
        _pending.reset(token)


@contextmanager
def write_transaction(paths):
    """Rollback local files when the operation or its atomic event batch fails."""
    # rd_review's query adapter imports projects, which also publishes events.
    from .rd_review import rollback_files
    with rollback_files(paths), transaction():
        yield


class EventLock(JobLock):
    """Independent, blocking, innermost lock; never take a domain lock inside it."""
    def __init__(self, state):
        super().__init__(state, "events")
        self.path = Path(state).resolve() / "locks" / "events" / "log.lock"

    def acquire(self):
        return super().acquire(blocking=True)


class EventLog:
    def __init__(self, state, *, max_bytes=None):
        self.state = Path(state).resolve()
        self.path = self.state / "events.jsonl"
        value = max_bytes if max_bytes is not None else os.environ.get("RD_EVENT_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)
        try:
            self.max_bytes = int(value)
        except (TypeError, ValueError):
            raise ValueError("RD_EVENT_LOG_MAX_BYTES must be a positive integer (bytes)") from None
        if self.max_bytes <= 0:
            raise ValueError("RD_EVENT_LOG_MAX_BYTES must be a positive integer (bytes)")

    def archives(self):
        return sorted(path for path in self.state.glob("events.*.jsonl")
                      if re.fullmatch(r"events\.[0-9]{20}\.jsonl", path.name))

    def publish(self, kind, data, *, only_if_changed=False):
        data = display_data(kind, data)
        pending = _pending.get()
        if pending is not None:
            pending.append((self, kind, data, only_if_changed))
            return None
        return self.append(kind, data, only_if_changed=only_if_changed)

    def append(self, kind, data, *, only_if_changed=False):
        rows = self.append_batch([(kind, data, only_if_changed)])
        return rows[0] if rows else None

    def latest(self, kind, data):
        identity = (("project_id", "user_id", "host") if kind == "project.changed" else
                    ("job", "user_id") if "job" in data else ("harness", "user_id")
                    if kind == "harness.stats_changed" else ("host",))
        for path in [self.path, *reversed(self.archives())]:
            try:
                lines = physical(path).read_bytes().splitlines(keepends=True)
            except FileNotFoundError:
                continue
            for line in reversed(lines):
                if line.endswith(b"\n"):
                    previous = json.loads(line)
                    if previous["type"] == kind and all(previous["data"].get(key) == data.get(key) for key in identity):
                        return previous["data"]
        return None

    def append_batch(self, items):
        """Publish a complete operation in one rename, or publish no new rows."""
        try:
            return self._append_batch(items)
        except OSError as error:
            raise ValueError("event_write_failed: 无法保存事件；请检查状态目录后重试。") from error

    def _append_batch(self, items):
        items = [(kind, display_data(kind, data), changed) for kind, data, changed in items]
        with EventLock(self.state):
            last_seq, size = tail(self.path)
            if not last_seq:
                archives = self.archives()
                if archives:
                    last_seq, _ = tail(archives[-1])
            rows, latest = [], {}
            for kind, data, changed in items:
                key = (kind, data.get("job"), data.get("user_id"), data.get("host") if "job" not in data else None, data.get("harness"), data.get("project_id") if kind == "project.changed" else None)
                if changed:
                    if key not in latest:
                        latest[key] = self.latest(kind, data)
                    if latest[key] == data:
                        continue
                latest[key] = data
                rows.append(dict(seq=last_seq + len(rows) + 1, time=messages.utc_now(), type=kind, data=data))
            if not rows:
                return []
            body = b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for row in rows)
            rotate = size and size + len(body) > self.max_bytes
            original = physical(self.path).read_bytes() if size and not rotate else b""
            fd, staging = tempfile.mkstemp(prefix=".events-", dir=self.state)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(original)
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                if rotate:
                    destination = physical(self.state / f"events.{last_seq:020d}.jsonl")
                    os.replace(self.path, destination)
                os.replace(staging, self.path)
            finally:
                # No failing I/O after publication: a cleanup error must not
                # turn a committed batch into a failed business operation.
                try:
                    os.unlink(staging)
                except OSError:
                    pass
            return rows

    def observe_job(self, rec, snapshot, progress=None):
        """Explicit poller write boundary. Never call from a query adapter."""
        number = int(snapshot.get("round") or 0) or rec.get("rounds", 1)
        code = snapshot.get("exit")
        code = int(code) if code not in (None, "") else None
        self.publish("job.state_changed", job_data(rec, round=number,
                    execution_state=snapshot.get("state", "unreachable"), executor_exit=code), only_if_changed=True)
        if progress is not None:
            data = job_data(rec, **{key: progress[key] for key in ("phase", "summary", "updated_at") if key in progress})
            data["round"] = number
            self.publish("progress.updated", data, only_if_changed=True)

    def observe_host(self, host, info, *, full):
        """Publish display-only probe changes (not probe timestamps or SSH config)."""
        self.publish("host.changed", dict(info, host=host["name"], full=full), only_if_changed=True)

    def read_after(self, seq):
        if type(seq) is not int or seq < 0:
            raise ValueError("event sequence must be a non-negative integer")
        # Capture the active segment first. A concurrent rename then appears in
        # archives as well; merge by seq to avoid both holes and duplicates.
        try:
            active = physical(self.path).read_bytes()
        except FileNotFoundError:
            active = b""
        rows = {}
        for path in self.archives():
            if int(path.name.split(".")[1]) <= seq:
                continue
            for line in physical(path).read_bytes().splitlines(keepends=True):
                row = json.loads(line)
                if row["seq"] > seq:
                    rows[row["seq"]] = row
        for line in active.splitlines(keepends=True):
            if line.endswith(b"\n"):
                row = json.loads(line)
                if row["seq"] > seq:
                    rows[row["seq"]] = row
        return [rows[number] for number in sorted(rows)]


def read_after(state, seq):
    """Read all retained events with seq strictly greater than the cursor."""
    return EventLog(state).read_after(seq)


class EventCursor:
    """Incremental reader across atomic append replacements and log rotation.

    A segment's first seq remains stable when the writer replaces its inode.
    Byte offsets skip parsed prefixes; seq merges the captured active segment
    with archives if a rotation happens during a read.
    """
    def __init__(self, state):
        self.log = EventLog(state)
        self.seq = 0
        self.segment = None
        self.offset = 0

    def read(self):
        rows = {}
        try:
            with physical(self.log.path).open("rb") as stream:
                first = stream.readline()
                segment = json.loads(first)["seq"] if first.endswith(b"\n") else None
                size = os.fstat(stream.fileno()).st_size
                offset = self.offset if segment == self.segment and size >= self.offset else 0
                stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break
                    row = json.loads(line)
                    if row["seq"] > self.seq:
                        rows[row["seq"]] = row
                    offset = stream.tell()
        except FileNotFoundError:
            segment, offset = None, 0
        for path in self.log.archives():
            if int(path.name.split(".")[1]) <= self.seq:
                continue
            with physical(path).open("rb") as stream:
                # The previous active segment may now be this archive.
                first = stream.readline()
                start = json.loads(first)["seq"]
                stream.seek(self.offset if start == self.segment else 0)
                for line in stream:
                    row = json.loads(line)
                    if row["seq"] > self.seq:
                        rows[row["seq"]] = row
        self.segment, self.offset = segment, offset
        if rows:
            self.seq = max(rows)
        return [rows[number] for number in sorted(rows)]
