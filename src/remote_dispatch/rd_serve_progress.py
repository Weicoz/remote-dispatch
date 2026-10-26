"""Persist complete reporter histories independently of bounded latest snapshots."""
from . import rd_events as events
from . import rd_messages as messages
from .rd_serve_history import instant


class ProgressHistory:
    def __init__(self, service):
        self.service = service

    def path(self, rec, number):
        return self.service.state / "serve-progress" / messages.job_id(rec["job"]) / ("r" + str(number) + ".json")

    def saved(self, rec, number):
        row = messages.read_json(self.path(rec, number), limit=None)
        if not isinstance(row, dict) or row.get("user_id") != self.service.user_id:
            return None
        if number == rec["rounds"] and row.get("generation") != list(self.service.generation(rec)):
            return None
        return row

    def read(self, rec, number):
        saved = self.saved(rec, number)
        rows = saved["rows"] if saved else []
        return sorted(rows, key=lambda row: instant(row.get("updated_at")).timestamp()
                      if instant(row.get("updated_at")) else 0)

    def collect(self, rec, number, progress, *, final=False):
        rows = (progress.get("history") if "history" in progress else
                [{key: progress.get(key, "") for key in ("updated_at", "phase", "summary")}]) if progress else []
        rows = self.service.redact([{key: row.get(key, "") for key in ("updated_at", "phase", "summary")}
                                    for row in rows])
        previous = self.saved(rec, number)
        old = previous["rows"] if previous else []
        if progress is None and previous:
            rows = old
        if previous is not None and rows == old and (not final or previous.get("final")):
            return
        # Append histories have a stable prefix. Legacy latest-only files replace it.
        start = len(old) if rows[:len(old)] == old else 0
        path = self.path(rec, number)
        with events.write_transaction([path]):
            messages.atomic_json(path, dict(user_id=self.service.user_id,
                generation=(number, rec["host"], rec.get("worker_root")), rows=rows, final=final), limit=None)
            log = events.EventLog(self.service.state)
            for row in rows[start:]:
                log.publish("progress.updated", events.job_data(rec, round=number, **row))
