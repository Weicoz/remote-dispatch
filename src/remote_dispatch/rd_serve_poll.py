"""serve 后台采集：按目标机分工作单元并发执行，阶段错误互不阻断，错误日志聚合限频。

一个工作单元是一台目标机：探测、批量取状态，再逐个 job 加锁提交。单元之间并发（有上限），
一台慢机最多拖住自己；单机内仍逐 job 串行，与 CLI 写入共用同一把 job 锁。
终态回执先收取落盘，进度与历史补采失败只单独记录，不得阻断回执。
"""
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import cli
from . import rd_messages as messages
from .rd_lock import JobBusy, JobLock
from .rd_host_metrics import host_metrics
from .rd_query import result_path
from .rd_serve_progress import ProgressHistory

DEFAULT_CONCURRENCY = 4
# 同一 (对象, 阶段, 异常类型) 重复出错时，汇总行的最短间隔（秒）。
ERROR_LOG_INTERVAL = 600
# 终态且回执已落盘的 job 降频探测：每 SETTLED_FACTOR 个轮询周期一次，至少 SETTLED_MIN_SECONDS。
SETTLED_FACTOR = 8
SETTLED_MIN_SECONDS = 60
TERMINAL = ("done", "dead", "missing")
RECOVERABLE = (ValueError, OSError, SystemExit, subprocess.SubprocessError)
REASON_LIMIT = 120


def concurrency_setting(cp):
    """[serve] poll_concurrency：每轮并发采集的目标机数，默认 4；无效值按默认处理。"""
    try:
        value = int(cp.get("serve", "poll_concurrency", fallback=DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY
    return value if value > 0 else DEFAULT_CONCURRENCY


def stage_of(error, stage):
    return "session" if isinstance(error, cli.SessionLookupError) else stage


class PollErrors:
    """按 (对象, 阶段, 异常类型) 聚合错误日志。

    首次出错立即记一行；重复出错只计数，每 `interval` 秒汇总一行；同一 (对象, 阶段) 恢复时记一行。
    原因只含异常类名和简短文字，不含回执正文、命令行或凭据。
    """

    def __init__(self, redact=None, *, interval=ERROR_LOG_INTERVAL, clock=time.monotonic):
        self.redact = redact or (lambda text: text)
        self.interval, self.clock = interval, clock
        self.lock = threading.Lock()
        self.active = {}

    def reason(self, error):
        if isinstance(error, subprocess.TimeoutExpired):
            return "timeout after %ss" % (int(error.timeout) if error.timeout == int(error.timeout) else error.timeout)
        if isinstance(error, SystemExit):
            return "exit code %s" % (error.code,)
        if isinstance(error, subprocess.SubprocessError):
            return "subprocess error"
        if isinstance(error, OSError) and not isinstance(error, ValueError):
            text = error.strerror or ""
        else:
            text = str(error)
        text = self.redact(" ".join(text.split()))
        return text[:REASON_LIMIT]

    @staticmethod
    def say(message):
        print("rdispatch: " + message, file=sys.stderr, flush=True)

    def failed(self, subject, stage, error):
        key = (subject, stage, type(error).__name__)
        now = self.clock()
        with self.lock:
            entry = self.active.get(key)
            if entry is None:
                self.active[key] = dict(count=1, logged=1, since=now, last=now)
                line = "poll_error: %s stage=%s error=%s reason=%s" % (
                    subject, stage, key[2], self.reason(error))
            else:
                entry["count"] += 1
                if now - entry["last"] < self.interval:
                    return
                line = "poll_error_repeated: %s stage=%s error=%s count=%d over=%ds" % (
                    subject, stage, key[2], entry["count"] - entry["logged"], int(now - entry["last"]))
                entry["last"], entry["logged"] = now, entry["count"]
        self.say(line)

    def ok(self, subject, stage):
        with self.lock:
            keys = [key for key in self.active if key[0] == subject and key[1] == stage]
            entries = [self.active.pop(key) for key in keys]
        if entries:
            self.say("poll_recovered: %s stage=%s after=%d failures" % (
                subject, stage, sum(entry["count"] for entry in entries)))


def subject_of(job):
    return "job=" + job


class Collector:
    def __init__(self, service, *, clock=time.monotonic):
        self.service = service
        self.clock = clock
        self.errors = PollErrors(service.redact)
        self.concurrency = concurrency_setting(service.config.cp)
        self.pool = None
        self.settled = {}
        self.last_cycle_seconds = 0.0
        self.seen_lock = threading.Lock()

    def close(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    # 降频探测 -------------------------------------------------------------
    def settle_seconds(self):
        return max(self.service.poll_interval * SETTLED_FACTOR, SETTLED_MIN_SECONDS)

    def is_settled(self, rec):
        """终态且回执已落盘、探测频率已降低的 job；只读查询可直接信任其缓存快照。"""
        entry = self.settled.get(rec["job"])
        if entry and entry[0] == self.service.generation(rec):
            return True
        # 重启后：已签收的终态 job 不再轮询，靠落盘的完整快照继续可信。
        return self.service.job_refresh_complete(rec)

    def settled_recently(self, rec):
        entry = self.settled.get(rec["job"])
        return bool(entry and entry[0] == self.service.generation(rec)
                    and self.clock() - entry[1] < self.settle_seconds())

    def mark(self, rec, snapshot):
        job = rec["job"]
        if (snapshot.get("state") in TERMINAL
                and messages.read_json(result_path(self.service.state, rec, rec["rounds"])) is not None):
            self.settled[job] = (self.service.generation(rec), self.clock())
        else:
            self.settled.pop(job, None)

    # 一轮采集 -------------------------------------------------------------
    def cycle(self, log):
        started = self.clock()
        try:
            self.run_cycle(log)
        finally:
            self.last_cycle_seconds = self.clock() - started

    def run_cycle(self, log):
        service = self.service
        config = service.config  # 一轮一个配置代：重载不得混入半轮
        enabled_hosts = config.hosts()
        enabled = {host["name"]: host for host in enabled_hosts}
        try:
            records = [rec for rec in service.records()
                       if not service.job_refresh_complete(rec) and not self.settled_recently(rec)]
        except RECOVERABLE as error:
            records = []
            self.errors.failed("jobs", "read", error)
        else:
            self.errors.ok("jobs", "read")
        by_host = {}
        for rec in records:
            by_host.setdefault(rec["host"], []).append(rec)
        names = [host["name"] for host in enabled_hosts] + [name for name in by_host if name not in enabled]
        rows = {}
        pending = {}
        if self.pool is None:
            self.pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="rd-poll")
        for name in names:
            if service.stop.is_set():
                break
            future = self.pool.submit(self.unit, config, log, name, enabled_hosts, enabled, by_host.get(name, []))
            pending[future] = name
        while pending:
            done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                name = pending.pop(future)
                try:
                    row = future.result()
                except Exception as error:  # 单元内部缺陷不得拖垮轮询线程
                    self.errors.failed("host=" + name, "unit", error)
                    row = None
                if row is not None:
                    rows[name] = row
                    self.publish_host(config, enabled_hosts, rows)
            if service.stop.is_set():
                for future in pending:
                    future.cancel()
        if service.stop.is_set():
            return
        with service.cache_lock:
            if service.config is config:
                service.hosts = [rows[host["name"]] for host in enabled_hosts if host["name"] in rows]

    def publish_host(self, config, enabled_hosts, rows):
        service = self.service
        with service.cache_lock:
            if service.config is not config:
                return
            current = {row["name"]: row for row in service.hosts}
            current.update(rows)
            service.hosts = [current[host["name"]] for host in enabled_hosts if host["name"] in current]

    # 一台目标机 -------------------------------------------------------------
    def unit(self, config, log, name, enabled_hosts, enabled, group):
        service = self.service
        host = enabled.get(name)
        available = attempted = False
        row = None
        if host is not None:
            if service.host_poll_due(host):
                attempted = True
                row, available = self.probe(log, host)
                # 主机行先于该机 job 采集发布：慢 job 不得拖住资源数据。
                self.publish_host(config, enabled_hosts, {name: row})
            else:
                row = next((r for r in service.hosts if r["name"] == name), None)
        if not group:
            if available:
                service.host_poll_recovered(name)
            return row
        states = self.job_states(config, name, host, group, available, attempted)
        for saved in group:
            if saved["job"] not in states:
                continue
            if service.stop.is_set():
                break
            self.collect_job(config, log, saved, states)
        return row

    def probe(self, log, host):
        service, name = self.service, host["name"]
        try:
            info = cli.probe(host)
            available = bool(info.get("reachable"))
            if available:
                with self.seen_lock:
                    service.host_last_seen[name] = messages.utc_now()
                    try:
                        messages.atomic_json(service.state / "serve-host-last-seen.json", service.host_last_seen)
                    except (ValueError, OSError):
                        print("rdispatch: 无法保存目标机最后在线时间。", file=sys.stderr)
            else:
                service.host_poll_failed(name)
            full = bool(available and cli.assess(host, info))
            log.observe_host(host, info, full=full)
            self.errors.ok("host=" + name, "probe")
            return service.host_row(host, reachable=available, full=full, probed=True,
                                    metrics=host_metrics(host, info),
                                    **{key: info.get(key) for key in
                                       ("load1", "ncpu", "cpu_pct", "memfree", "disk",
                                        "disk_total", "running", "heavy")}), available
        except RECOVERABLE as error:
            service.host_poll_failed(name)
            self.errors.failed("host=" + name, "probe", error)
            return service.host_row(host, probed=True), False

    def job_states(self, config, name, host, group, available, attempted):
        service = self.service
        if host is not None and not available:
            if attempted:
                return {rec["job"]: {"state": "unreachable"} for rec in group}
            return {}
        try:
            # Disabling new dispatches must not freeze existing jobs.
            if (host is None and config.cp.has_section("host:" + name)
                    and not service.host_poll_due(config.host(name))):
                return {}
            batch = cli.job_states(config, group)
            states = {rec["job"]: batch.get(rec["job"], {"state": "unreachable"}) for rec in group}
            if any(state.get("state", "unreachable") == "unreachable" for state in states.values()):
                service.host_poll_failed(name)
            else:
                service.host_poll_recovered(name)
                self.errors.ok("host=" + name, "state")
            return states
        except RECOVERABLE as error:
            service.host_poll_failed(name)
            self.errors.failed("host=" + name, "state", error)
            return {rec["job"]: {"state": "unreachable"} for rec in group}

    # 一个 job：先回执，再进度 -------------------------------------------------
    def collect_job(self, config, log, saved, states):
        service = self.service
        job = saved["job"]
        subject = subject_of(job)
        try:
            # Lock before reloading: a resume must not mix old and new round data.
            with service.job_guard(job), JobLock(service.state, job):
                rec = cli.load_job(job)
                if service.generation(rec) != service.generation(saved):
                    return
                snapshot = states.get(job, {"state": "unreachable"})
                number = int(snapshot.get("round") or 0) or rec["rounds"]
                if number != rec["rounds"]:
                    # A delayed target may still expose the previous launch.
                    return
                raw_round = snapshot.get("round") if "round" in snapshot else None
                snapshot = dict(snapshot, round=str(number), snapshot_at=messages.utc_now())
                log.observe_job(rec, snapshot)
                # 1. 终态回执优先落盘；失败时保留旧快照，下一轮重试。
                try:
                    cli.collect_result(config, rec, snapshot)
                except RECOVERABLE as error:
                    self.errors.failed(subject, stage_of(error, "result"), error)
                    return
                self.errors.ok(subject, "result")
                self.errors.ok(subject, "session")
                # 2. 进度与历史补采：失败单独记录，不影响回执和快照发布。
                progress, progress_ok = self.collect_progress(config, rec, number, snapshot)
                self.publish(rec, number, snapshot, progress, progress_ok, raw_round)
                self.mark(rec, snapshot)
        except JobBusy:
            return
        except RECOVERABLE as error:
            self.errors.failed(subject, "commit", error)

    def collect_progress(self, config, rec, number, snapshot):
        job, subject = rec["job"], subject_of(rec["job"])
        live = rec.get("handoff_version") and snapshot.get("state") in ("running", "done")
        if not live:
            return None, True
        progress, history = None, ProgressHistory(self.service)
        try:
            progress = cli.remote_report(cli.job_host(config, rec), rec, number, "progress")
            if progress is not None:
                history.collect(rec, number, progress)
        except RECOVERABLE as error:
            self.errors.failed(subject, "progress", error)
            return None, False
        self.errors.ok(subject, "progress")
        try:
            for prior in range(1, number):
                saved_history = history.saved(rec, prior)
                if saved_history is None or not saved_history.get("final"):
                    source = dict(rec)
                    if saved_history:
                        _, source["host"], source["worker_root"] = saved_history["generation"]
                        if source["worker_root"] is None:
                            source.pop("worker_root")
                        if source["host"] != rec["host"]:
                            source.pop("worker_platform", None)
                            source.pop("worker_python", None)
                    history.collect(rec, prior, cli.remote_report(
                        cli.job_host(config, source), source, prior, "progress"), final=True)
        except RECOVERABLE as error:
            self.errors.failed(subject, "history", error)
            return progress, False
        self.errors.ok(subject, "history")
        return progress, True

    def publish(self, rec, number, snapshot, progress, progress_ok, raw_round):
        service = self.service
        # Persist only display data, so restart can serve the last
        # observation while the first remote collection is pending.
        # 完整快照落盘（经脱敏），重启后只读缓存才能逐字复现 CLI 输出；老格式文件没有 raw_round，不作缓存依据。
        saved_snapshot = dict(snapshot)
        with service.cache_lock:
            if not progress_ok:
                progress = service.progress.get((rec["job"], number))  # 本轮没读到：沿用上一次的进度
        saved_progress = {key: progress[key] for key in
                          ("schema", "job", "round", "kind", "phase", "summary", "artifacts", "updated_at")
                          if key in progress} if progress else None
        messages.atomic_json(service.state / "serve-snapshots" / (rec["job"] + ".json"),
            dict(user_id=service.user_id, generation=service.generation(rec),
                 snapshot=service.redact(saved_snapshot), raw_round=raw_round,
                 progress=service.redact(saved_progress)))
        with service.cache_lock:
            service.snapshots[rec["job"]] = snapshot
            service.raw_rounds[rec["job"]] = raw_round  # 只读查询还原 CLI 直跑时的原始 round
            service.complete_snapshots.add(rec["job"])
            service.generations[rec["job"]] = service.generation(rec)
            service.progress[(rec["job"], number)] = progress
            service.snapshot_version += 1
