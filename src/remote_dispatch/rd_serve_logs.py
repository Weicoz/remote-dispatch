"""On-demand background log tails; HTTP handlers never contact a target."""
import subprocess
import threading

from . import cli
from . import rd_messages as messages


class LogSnapshots:
    def __init__(self, service):
        self.service = service
        self.condition = threading.Condition()
        self.pending = {}
        self.snapshots = {}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.collect, name="serve-log-collector", daemon=True)
        self.thread.start()

    def close(self):
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
            self.thread = None

    def read(self, rec, number, stream, tail):
        key = rec["job"], self.service.generation(rec), number, stream
        with self.condition:
            if not self.service.stop.is_set():
                self.pending.setdefault(key, dict(rec))
                self.condition.notify()
            status, snapshot = self.snapshots.get(key, (200, dict(text="", snapshot_at=None)))
            if status != 200:
                return status, snapshot
            return 200, dict(job=rec["job"], round=number, stream=stream,
                             text="".join(snapshot["text"].splitlines(keepends=True)[-tail:]),
                             snapshot_at=snapshot["snapshot_at"])

    def collect(self):
        while not self.service.stop.is_set():
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.service.stop.is_set())
                if self.service.stop.is_set():
                    return
                key = next(iter(self.pending))
                rec = self.pending[key]
            job, generation, number, stream = key
            try:
                current = cli.load_job(job)
                if (self.service.generation(current) != generation
                        or current.get("user_id") not in (None, "", self.service.user_id)):
                    raise ValueError("job_changed")
                text = self.service.logs(current, number, stream, 10000)
                result = 200, dict(text=self.service.redact(text), snapshot_at=messages.utc_now())
            except (ValueError, OSError, SystemExit, subprocess.SubprocessError):
                result = 503, {"error": {"reason": "read_unavailable"}}
            with self.condition:
                self.snapshots[key] = result
                self.pending.pop(key, None)
