"""Controller event history shared by CLI writes and serve's future poller.

Only display fields cross this boundary. Reading never creates state or locks.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import re
import threading

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
# 活动段之外最多保留的归档段数；总量约 (DEFAULT_MAX_ARCHIVES + 1) * 单段上限。
DEFAULT_MAX_ARCHIVES = 5
# 每个派单在内存索引里保留的 progress.updated 位置数上限（取最新）。
JOB_HISTORY_LIMIT = 2000
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
    if kind == "report.delivered" and isinstance(data.get("channels"), list):
        projected["channels"] = [name for name in data["channels"] if isinstance(name, str)]
    if kind == "report.delivered" and isinstance(data.get("rounds"), list):
        # 「派单:轮次」标识列表，推送按它去重；不含汇报正文。
        projected["rounds"] = [key for key in data["rounds"] if isinstance(key, str)]
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


def identity(kind, data):
    """去重身份：同类型事件里「同一对象」的最新值用它定位。"""
    keys = (("project_id", "user_id", "host") if kind == "project.changed" else
            ("job", "user_id") if "job" in data else ("harness", "user_id")
            if kind == "harness.stats_changed" else ("host",))
    return (kind, tuple(data.get(key) for key in keys))


def write_active(path, body):
    """单次追加写并 fsync；崩溃最多留下一条不完整尾行，下一次写入前由 tail() 修复。"""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class LogIndex:
    """进程内索引：每个对象的最新事件（去重）与每个派单的 progress.updated 位置。

    只在内存里，可随时由日志重建；活动段被他人替换或截短时整体重建，追加则增量追上。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.latest = {}
        self.jobs = {}
        self.ready = False
        self.fresh_active = False
        self.inode = None
        self.offset = 0
        self.segment = None

    def record(self, row, segment, position):
        data = row["data"]
        self.latest[identity(row["type"], data)] = data
        if row["type"] == "progress.updated" and isinstance(data.get("job"), str):
            places = self.jobs.setdefault(data["job"], [])
            places.append((segment, position))
            if len(places) > JOB_HISTORY_LIMIT:
                del places[:len(places) - JOB_HISTORY_LIMIT]

    def consume(self, path, segment, start):
        """从 start 起吸收完整行；返回 (inode, 新偏移, 段首 seq)。坏行跳过，不完整尾行不计入。"""
        with physical(path).open("rb") as stream:
            inode = os.fstat(stream.fileno()).st_ino
            stream.seek(start)
            position = start
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    break
                try:
                    row = json.loads(line)
                    if segment is None:
                        segment = row["seq"]
                    self.record(row, segment, position)
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
                position += len(line)
            return inode, position, segment

    def sync(self, log):
        """追上日志当前内容（调用方持有 self.lock）。"""
        try:
            info = physical(log.path).stat()
        except FileNotFoundError:
            info = None
        if self.ready and self.fresh_active:
            self.fresh_active = False
            if info is not None:
                self.inode, self.offset, self.segment = self.consume(log.path, None, 0)
            return
        if (not self.ready or (info is None) != (self.inode is None)
                or info is not None and (info.st_ino != self.inode or info.st_size < self.offset)):
            for attempt in range(3):
                try:
                    return self.rebuild(log)
                except FileNotFoundError:
                    if attempt == 2:
                        raise
            return
        if info is not None and self.offset and not self.boundary(log.path):
            return self.rebuild(log)  # 截断后被更长写入覆盖：偏移落在行中间
        if info is not None and info.st_size > self.offset:
            self.inode, self.offset, self.segment = self.consume(log.path, self.segment, self.offset)

    def boundary(self, path):
        try:
            with physical(path).open("rb") as stream:
                stream.seek(self.offset - 1)
                return stream.read(1) == b"\n"
        except OSError:
            return False

    def rebuild(self, log):
        self.reset()
        for path in log.archives():
            self.consume(path, None, 0)
        if physical(log.path).exists():
            self.inode, self.offset, self.segment = self.consume(log.path, None, 0)
        self.ready = True


_indexes = {}
_high_water = {}
_indexes_lock = threading.Lock()


class EventLock(JobLock):
    """Independent, blocking, innermost lock; never take a domain lock inside it."""
    def __init__(self, state):
        super().__init__(state, "events")
        self.path = Path(state).resolve() / "locks" / "events" / "log.lock"

    def acquire(self):
        return super().acquire(blocking=True)


class EventLog:
    def __init__(self, state, *, max_bytes=None, max_archives=None):
        self.state = Path(state).resolve()
        self.path = self.state / "events.jsonl"
        value = max_bytes if max_bytes is not None else os.environ.get("RD_EVENT_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)
        try:
            self.max_bytes = int(value)
        except (TypeError, ValueError):
            raise ValueError("RD_EVENT_LOG_MAX_BYTES must be a positive integer (bytes)") from None
        if self.max_bytes <= 0:
            raise ValueError("RD_EVENT_LOG_MAX_BYTES must be a positive integer (bytes)")
        value = max_archives if max_archives is not None else os.environ.get(
            "RD_EVENT_LOG_MAX_ARCHIVES", DEFAULT_MAX_ARCHIVES)
        try:
            self.max_archives = int(value)
        except (TypeError, ValueError):
            raise ValueError("RD_EVENT_LOG_MAX_ARCHIVES must be a positive integer (archive segments)") from None
        if self.max_archives <= 0:
            raise ValueError("RD_EVENT_LOG_MAX_ARCHIVES must be a positive integer (archive segments)")

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

    def index(self):
        with _indexes_lock:
            return _indexes.setdefault(str(self.state), LogIndex())

    def warm(self):
        """预热内存索引（serve 启动时调用），之后的去重与按派单读取都不再扫描全部历史。"""
        index = self.index()
        with index.lock:
            index.sync(self)

    def latest(self, kind, data):
        """同一对象最近一次同类型事件的数据。索引已预热（serve）就查索引；
        否则（一次性 CLI）从活动段尾部向前倒读，找到即停，不在写锁内解析全部历史。"""
        index = self.index()
        with index.lock:
            if index.ready:
                index.sync(self)
                return index.latest.get(identity(kind, data))
        wanted = identity(kind, data)
        # 行是紧凑 JSON：先用子串粗筛（类型、派单/目标机标识），只解析可能命中的行。
        needles = [('"type":"%s"' % kind).encode()]
        needles += [json.dumps(value, ensure_ascii=False).encode() for value in wanted[1] if isinstance(value, str)]
        for path in [self.path, *reversed(self.archives())]:
            try:
                lines = physical(path).read_bytes().splitlines(keepends=True)
            except FileNotFoundError:
                continue
            for line in reversed(lines):
                if line.endswith(b"\n") and all(needle in line for needle in needles):
                    previous = json.loads(line)
                    if identity(previous["type"], previous["data"]) == wanted:
                        return previous["data"]
        return None

    def append_batch(self, items):
        """Publish a complete operation with one fsynced append, or publish no new rows."""
        try:
            return self._append_batch(items)
        except OSError as error:
            raise ValueError("event_write_failed: 无法保存事件；请检查状态目录后重试。") from error

    def _append_batch(self, items):
        items = [(kind, display_data(kind, data), changed) for kind, data, changed in items]
        index = self.index()
        with EventLock(self.state), index.lock:
            last_seq, size = tail(self.path)
            if not last_seq:
                archives = self.archives()
                if archives:
                    last_seq, _ = tail(archives[-1])
            last_seq = max(last_seq, self.high_water())
            rows, latest = [], {}
            if index.ready and any(changed for _, _, changed in items):
                index.sync(self)
            for kind, data, changed in items:
                key = identity(kind, data)
                if changed:
                    if key not in latest:
                        latest[key] = index.latest.get(key) if index.ready else self.latest(kind, data)
                    if latest[key] == data:
                        continue
                latest[key] = data
                rows.append(dict(seq=last_seq + len(rows) + 1, time=messages.utc_now(), type=kind, data=data))
            if not rows:
                return []
            body = b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for row in rows)
            rotate = bool(size and size + len(body) > self.max_bytes)
            physical(self.path)
            if rotate:
                if index.ready:
                    index.sync(self)
                os.replace(self.path, physical(self.state / f"events.{last_seq:020d}.jsonl"))
                size = 0
                index.fresh_active = index.ready
            try:
                write_active(self.path, body)
            except BaseException:
                # 读者可能已在 fsync 前读到这些行：它们的 seq 不得复用。先截回写前长度（磁盘满时
                # 这一步才释放空间），再记高水位；进程内高水位不依赖文件写入是否成功。
                index.reset()
                try:
                    with self.path.open("r+b") as stream:
                        stream.truncate(size)
                        os.fsync(stream.fileno())
                except OSError:
                    pass
                self.remember_high_water(rows[-1]["seq"])
                raise
            if not size:
                sync_directory(self.state)
            if rotate:
                self.prune()
            if index.ready:
                index.sync(self)
            return rows

    def high_water(self):
        try:
            saved = int(physical(self.state / "events.hwm").read_text())
        except (FileNotFoundError, ValueError, OSError):
            saved = 0
        return max(saved, _high_water.get(str(self.state), 0))

    def remember_high_water(self, seq):
        """写失败后被撤回的 seq 上界；之后的批次从它之后编号。进程内必记，文件尽力写入（供其他进程）。"""
        _high_water[str(self.state)] = max(seq, _high_water.get(str(self.state), 0))
        try:
            staging = self.state / ".events.hwm.tmp"
            staging.write_text(str(seq))
            os.replace(staging, self.state / "events.hwm")
        except OSError:
            pass

    def prune(self):
        """归档段超过上限时删除最旧的；删除失败不影响已提交的事件。"""
        removed = False
        for path in self.archives()[:-self.max_archives]:
            try:
                path.unlink()
                removed = True
            except OSError:
                pass
        if removed:
            # 内存索引里属于已清理历史段的位置和最新值一并丢弃：重置后下次使用时按保留的日志重建。
            self.index().reset()

    def segment_files(self):
        """[(段首 seq, 路径)]，按时间从旧到新；空文件和读不到的段跳过。"""
        found = []
        for path in [*self.archives(), self.path]:
            try:
                with physical(path).open("rb") as stream:
                    first = stream.readline()
                if first.endswith(b"\n"):
                    found.append((json.loads(first)["seq"], path))
            except (FileNotFoundError, ValueError, KeyError):
                continue
        return found

    def oldest_seq(self):
        """仍保留的最早事件 seq；日志为空返回 0。"""
        files = self.segment_files()
        return files[0][0] if files else 0

    def read_job(self, job, kind="progress.updated"):
        """按派单读取 progress.updated 历史（按 seq 升序），不扫描全部日志。"""
        if kind != "progress.updated":
            raise ValueError("unsupported event type")
        index = self.index()
        with index.lock:
            index.sync(self)
            places = list(index.jobs.get(job, ()))
        if not places:
            return []
        paths = {segment: path for segment, path in self.segment_files()}
        rows = {}
        for segment in sorted({segment for segment, _ in places}):
            path = paths.get(segment)
            if path is None:
                continue  # 该段已被清理或轮转中，历史保留上限之外
            try:
                with physical(path).open("rb") as stream:
                    for _, position in (place for place in places if place[0] == segment):
                        stream.seek(position)
                        line = stream.readline()
                        if not line.endswith(b"\n"):
                            continue
                        row = json.loads(line)
                        if row["type"] == kind and row["data"].get("job") == job:
                            rows[row["seq"]] = row
            except (FileNotFoundError, ValueError, KeyError, AttributeError):
                continue
        return [rows[number] for number in sorted(rows)]

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
            try:
                lines = physical(path).read_bytes().splitlines(keepends=True)
            except FileNotFoundError:
                continue  # 归档在读取期间被清理
            for line in lines:
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

    def read_active(self, rows):
        """读活动段新增行。写失败被截断后又被更长写入覆盖时，旧偏移可能落在行中间：
        遇到坏行就从段首重扫一次（seq 过滤保证不重复）；重扫仍遇坏行则按未写完的尾部处理。"""
        rescan = False
        while True:
            try:
                with physical(self.log.path).open("rb") as stream:
                    first = stream.readline()
                    segment = json.loads(first)["seq"] if first.endswith(b"\n") else None
                    size = os.fstat(stream.fileno()).st_size
                    offset = self.offset if segment == self.segment and size >= self.offset and not rescan else 0
                    stream.seek(offset)
                    while line := stream.readline():
                        if not line.endswith(b"\n"):
                            break
                        row = json.loads(line)
                        if row["seq"] > self.seq:
                            rows[row["seq"]] = row
                        offset = stream.tell()
                    return segment, offset
            except FileNotFoundError:
                return None, 0
            except (ValueError, KeyError):
                if rescan:
                    return self.segment, self.offset
                rescan = True

    def read(self):
        rows = {}
        segment, offset = self.read_active(rows)
        for path in self.log.archives():
            if int(path.name.split(".")[1]) <= self.seq:
                continue
            try:
                stream = physical(path).open("rb")
            except FileNotFoundError:
                continue  # 归档在读取期间被清理
            with stream:
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
