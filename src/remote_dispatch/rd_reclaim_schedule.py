"""serve 定时回收：每小时对配置了 max_worktree_gb 的在线目标机回收，默认开启。

每次先回收「已签收且过期」的任务（与 clean --expired 同一判定 cli.reclaim_check + expiry_verdict）和闲置超过
warm_idle_days 的热槽位，不论总用量是否超限；回收后仍超过上限，再对剩余任务按 regenerable_paths 瘦身。
首次启用后前 7 天只生成计划（写入事件日志与 reclaim-schedule.json，看板和 /v1/reclaim 显示），不执行。
删除只走统一判定，不会删被阻塞的派单；被领用或被 keep 钉住、产物未拉回的热槽位绝不回收；
只处理当前用户 ID 的派单；不调用 git clean，不删未声明的文件。
"""
import argparse
import math
import subprocess
import time

from . import cli
from . import rd_warm as warm
from . import rd_events as events
from . import rd_messages as messages
from . import rd_reclaim_plan as store
from .rd_lock import JobBusy, JobLock

INTERVAL_SECONDS = 3600
KB_PER_GB = 1024 * 1024
ERRORS = (OSError, ValueError, SystemExit, subprocess.SubprocessError)


def gb(kb):
    return round(kb / KB_PER_GB, 3)


def host_limit(host):
    """max_worktree_gb 的 KB 值；未配置、非正数或无效返回 None。"""
    try:
        value = float(host.get("max_worktree_gb") or 0)
    except (TypeError, ValueError):
        return None
    return value * KB_PER_GB if math.isfinite(value) and value > 0 else None


def worktree_sizes(host, recs, user_id):
    """目标机上当前用户各 worktree 的大小（KB），键为 worktree 目录名。
    用户根目录取登记的 worker_root（可能自定义），没有登记时用默认的 workdir/users/<ID>。"""
    scoped = cli.scoped_host(host, user_id)
    roots = {rec.get("worker_root") or scoped["workdir"] for rec in recs} or {scoped["workdir"]}
    sizes = {}
    for root in sorted(roots):
        sizes.update(root_sizes(dict(scoped, workdir=root)))
    return sizes


def root_sizes(host):
    # 热槽位（wt/_warm/<槽位>）逐个计入，键为 `_warm/<槽位>`。
    reply = cli.ssh(host, 'for d in "$ROOT"/wt/*; do [ -d "$d" ] || continue; '
                          'if [ "${d##*/}" = _warm ]; then for s in "$d"/*; do [ -d "$s" ] || continue; '
                          'printf "%s\\t_warm/%s\\n" "$(du -sk "$s" 2>/dev/null | cut -f1)" "${s##*/}"; done; continue; fi; '
                          'printf "%s\\t%s\\n" "$(du -sk "$d" 2>/dev/null | cut -f1)" "${d##*/}"; done',
                    check=False, timeout=600)
    sizes = {}
    for line in (reply.stdout or "").splitlines() if reply and reply.returncode == 0 else []:
        size, _, name = line.partition("\t")
        if size.isdigit() and name:
            sizes[name] = int(size)
    return sizes


class ScheduledReclaim:
    def __init__(self, service, clock=time.time):
        self.service = service
        self.clock = clock

    def load(self):
        return store.read(self.service.state, self.service.user_id) or dict(user_id=self.service.user_id)

    def due(self, now):
        last = self.load().get("last_run")
        return not isinstance(last, (int, float)) or now - last >= INTERVAL_SECONDS or now < last

    def loop(self):
        stop = self.service.stop
        while not stop.is_set():
            try:
                if self.service.config.scheduled_reclaim() and self.due(self.clock()):
                    self.run_once()
            except Exception as error:
                # 守护线程不能因单次异常退出：记录后等下一轮。
                print(f"rdispatch: reclaim_unavailable: scheduled reclaim failed: {type(error).__name__}: {error}", flush=True)
            stop.wait(30)

    def stopping(self):
        return self.service.stop.is_set()

    def run_once(self, now=None):
        """对每台在线且配置了上限的目标机执行一次；返回各目标机的计划。功能关闭时返回 None。"""
        config = self.service.config
        if not config.scheduled_reclaim():
            return None
        now = self.clock() if now is None else now
        data = self.load()
        # 起点只在首次启用时写入，serve 重启与反复调用都不重新计时。
        if not isinstance(data.get("enabled_at"), (int, float)):
            data["enabled_at"] = now
        # 「未超限也回收过期任务与闲置槽位」是后加的行为，单独记启用时间：已启用的老用户升级后
        # 这部分同样先 7 天只出计划，期间仍只按旧逻辑（超限才瘦身、再删除）。
        if not isinstance(data.get("sweep_enabled_at"), (int, float)):
            data["sweep_enabled_at"] = now
        data["last_run"] = now
        data.setdefault("hosts", {})
        store.write(self.service.state, data)
        execute = now >= data["enabled_at"] + store.PLAN_ONLY_SECONDS
        sweep = now >= data["sweep_enabled_at"] + store.PLAN_ONLY_SECONDS
        plans = {}
        for host in config.hosts():
            if self.stopping():
                break
            limit = host_limit(host)
            if limit is None or host["platform"] == "windows":
                continue
            try:
                # 离线目标机直接跳过，不报错。
                if not cli.probe(host).get("reachable"):
                    continue
                plan = self.host_run(config, host, limit, execute, now, sweep)
            except ERRORS:
                continue
            if plan is not None:
                plans[host["name"]] = plan
                data["hosts"][host["name"]] = plan
                store.write(self.service.state, data)
        return plans

    def owned(self, name):
        return [rec for rec in self.service.records()
                if rec.get("host") == name and rec.get("user_id") == self.service.user_id]

    def slim_candidates(self, host, recs, states):
        rows = []
        for rec in recs:
            code, _ = cli.slim_block(rec, states.get(rec["job"], {}))
            if code:
                continue
            try:
                removed, _ = cli.slim_worktree(cli.job_host(self.service.config, rec), rec, True)
            except ERRORS:
                continue
            kb = sum(item["kb"] for item in removed)
            if kb > 0:
                rows.append(dict(job=rec["job"], action="slim", kb=kb))
        return rows

    @staticmethod
    def activity(rec):
        try:
            return cli.last_activity(rec)
        except ERRORS:
            return 0

    def delete_candidates(self, config, host, recs, states, sizes, slimmed, over_kb):
        """按最后确认时间从旧到新选可回收的 worktree，直到预计释放量够；被阻塞的派单只记录原因。"""
        picked, blocked, freed = [], [], 0
        now = self.clock()
        pressure = {}
        for rec in sorted(recs, key=self.activity):
            if freed >= over_kb or self.stopping():
                break
            j = rec["job"]
            try:
                code, _, head, reasons = cli.reclaim_check(config, rec, states.get(j, {}), cli.job_host(config, rec),
                                                           mode="expired")
                if code:
                    blocked.append(dict(job=j, code=code))
                    continue
                expired, _, why = cli.expiry_verdict(config, rec, head, reasons, cli.job_host(config, rec), now, pressure)
            except ERRORS:
                blocked.append(dict(job=j, code=cli.BLOCK_UNVERIFIABLE))
                continue
            if not expired:
                blocked.append(dict(job=j, code=why))
                continue
            kb = max(0, sizes.get(messages.job_id(j), 0) - slimmed.get(j, 0))
            picked.append(dict(job=j, action="delete", kb=kb))
            freed += kb
        return picked, blocked

    def idle_slots(self, host, recs, pinned):
        """闲置超期且无人占用的热槽位；返回 (计划行, {行 job 名: 该槽位所在用户根目录的目标机})。
        计划行沿用 job/action/kb 结构（事件契约不变），job 填 `_warm/<槽位名>`。"""
        days = self.service.config.warm_idle_days()
        scoped = cli.scoped_host(host, self.service.user_id)
        roots = sorted({rec.get("worker_root") or scoped["workdir"] for rec in recs} or {scoped["workdir"]})
        rows, hosts = [], {}
        for root in roots:
            where = dict(scoped, workdir=root)
            reply = cli.ssh(where, warm.list_script(pinned, days), check=False, timeout=600)
            for line in (reply.stdout or "").splitlines() if reply and reply.returncode == 0 else []:
                slot, size, _, state, idle = (line.split("\t") + [""] * 5)[:5]
                if state == "free" and idle == "1" and slot:
                    key = "_warm/" + slot
                    rows.append(dict(job=key, action="delete_slot", kb=int(size) if size.isdigit() else 0))
                    hosts[key] = where
        return rows, hosts

    def legacy_execute(self, config, host, limit, plan, recs, states):
        """sweep 安全期内的旧逻辑：只在超限时执行，先瘦身，仍超限再从旧到新删除已过期的派单。"""
        results = []
        for row in self.slim_candidates(host, recs, states):
            if self.stopping():
                break
            results.append(dict(row, **self.slim_one(config, row["job"])))
        sizes = worktree_sizes(host, recs, self.service.user_id)
        after = sum(sizes.values())
        if after > limit and not self.stopping():
            fresh = cli.job_states(config, recs) if recs else {}
            picked, _ = self.delete_candidates(config, host, recs, fresh, sizes, {}, after - limit)
            for row in picked:
                if self.stopping():
                    break
                results.append(dict(row, **self.delete_one(config, row["job"])))
            after = sum(worktree_sizes(host, recs, self.service.user_id).values())
        plan = dict(plan, results=results, after_gb=gb(after))
        self.emit("reclaim.result", host["name"], plan, slimmed_count=self.count(results, "slim"),
                  deleted_count=self.count(results, "delete"),
                  failed_count=sum(1 for r in results if not r["ok"]))
        return plan

    def host_run(self, config, host, limit, execute, now, sweep=True):
        """先回收已签收且过期的任务与闲置超期的热槽位（不论是否超限），超限时再对剩余任务额外瘦身。

        sweep=False（新行为启用后的前 7 天）只把上述回收列成计划，实际仍只按旧逻辑在超限时处理。"""
        name = host["name"]
        recs = self.owned(name)
        sizes = worktree_sizes(host, recs, self.service.user_id)
        used = sum(sizes.values())
        if self.stopping():
            return None
        states = cli.job_states(config, recs) if recs else {}
        picked, blocked = self.delete_candidates(config, host, recs, states, sizes, {}, math.inf)
        pinned = warm.pinned_jobs(recs, now)
        idle, slot_hosts = self.idle_slots(host, recs, pinned)
        gone = {row["job"] for row in picked}
        slims = []
        if used - sum(row["kb"] for row in picked + idle) > limit:
            slims = self.slim_candidates(host, [rec for rec in recs if rec["job"] not in gone], states)
        actions = picked + idle + slims
        if not actions and used <= limit:
            return None
        plan = dict(at=now, mode="execute" if execute and sweep else "plan", limit_gb=gb(limit), used_gb=gb(used),
                    estimated_gb=gb(used - sum(row["kb"] for row in actions)),
                    blocked=blocked, actions=actions)
        self.emit("reclaim.planned", name, plan, blocked_count=len(blocked))
        if not execute or self.stopping():
            return plan
        if not sweep:
            return self.legacy_execute(config, host, limit, plan, recs, states) if used > limit else plan
        return self.execute(config, host, limit, plan, recs, slot_hosts, pinned)

    def execute(self, config, host, limit, plan, recs, slot_hosts, pinned):
        by_job = {rec["job"]: rec for rec in recs}
        results = []
        for row in [r for r in plan["actions"] if r["action"] == "delete"]:
            if self.stopping():
                break
            results.append(dict(row, **self.delete_one(config, row["job"])))
        for row in [r for r in plan["actions"] if r["action"] == "delete_slot"]:
            if self.stopping():
                break
            results.append(dict(row, **self.reap_one(config, slot_hosts[row["job"]], row["job"].split("/", 1)[1], pinned)))
        after = sum(worktree_sizes(host, recs, self.service.user_id).values())
        if after > limit:
            # 回收后仍超限：对没被删的任务瘦身（执行时 slim_one 在锁内重新判定）。
            for row in [r for r in plan["actions"] if r["action"] == "slim"]:
                if self.stopping():
                    break
                results.append(dict(row, **self.slim_one(config, by_job[row["job"]]["job"])))
            after = sum(worktree_sizes(host, recs, self.service.user_id).values())
        plan = dict(plan, results=results, after_gb=gb(after))
        self.emit("reclaim.result", host["name"], plan, slimmed_count=self.count(results, "slim"),
                  deleted_count=self.count(results, "delete") + self.count(results, "delete_slot"),
                  failed_count=sum(1 for r in results if not r["ok"]))
        return plan

    def reap_one(self, config, where, slot, pinned):
        """闲置槽位回收：目标机互斥内重新判定占用与闲置，先写交接快照再删 worktree。"""
        try:
            reply = cli.ssh(where, warm.reap_script(slot, pinned, config.warm_idle_days()), check=False, timeout=600)
        except ERRORS:
            return dict(ok=False, reason=cli.BLOCK_UNVERIFIABLE)
        out = (reply.stdout or "").strip() if reply else ""
        if reply and reply.returncode == 0 and out == "reaped":
            return dict(ok=True)
        return dict(ok=False, reason=out.partition(":")[2] or cli.BLOCK_UNVERIFIABLE)

    @staticmethod
    def count(results, action):
        return sum(1 for r in results if r["action"] == action and r["ok"])

    def slim_one(self, config, job):
        """锁内重新读取登记与状态再判定，计划之后被 resume 的任务不瘦身（与 delete_one 一致）。"""
        try:
            with self.service.job_guard(job), JobLock(self.service.state, job):
                rec = cli.load_job(job)
                state = cli.job_states(config, [rec]).get(job, {})
                code, _ = cli.slim_block(rec, state)
                if code:
                    return dict(ok=False, reason=code)
                cli.slim_worktree(cli.job_host(config, rec), rec, False)
            return dict(ok=True)
        except JobBusy:
            return dict(ok=False, reason="busy")
        except ERRORS:
            return dict(ok=False, reason=cli.BLOCK_UNVERIFIABLE)

    def delete_one(self, config, job):
        """锁内重新读取登记并再走一次统一判定，被阻塞或状态变化即放弃。"""
        try:
            with self.service.job_guard(job), JobLock(self.service.state, job):
                rec = cli.load_job(job)
                host = cli.job_host(config, rec)
                state = cli.job_states(config, [rec]).get(job, {})
                code, _, head, reasons = cli.reclaim_check(config, rec, state, host, mode="expired")
                if code:
                    return dict(ok=False, reason=code)
                expired, _, why = cli.expiry_verdict(config, rec, head, reasons, host, self.clock())
                if not expired:
                    return dict(ok=False, reason=why)
                cli.remove_job(argparse.Namespace(delete_branch=False, discard_reports=False), rec, host)
            return dict(ok=True)
        except JobBusy:
            return dict(ok=False, reason="busy")
        except ERRORS:
            return dict(ok=False, reason=cli.BLOCK_UNVERIFIABLE)

    def emit(self, kind, host, plan, **counts):
        rows = plan.get("results") or plan["actions"]
        data = dict(user_id=self.service.user_id, host=host, mode=plan["mode"], limit_gb=plan["limit_gb"],
                    used_gb=plan["used_gb"], actions=rows, **counts)
        if "after_gb" in plan:
            data["after_gb"] = plan["after_gb"]
        else:
            data["estimated_gb"] = plan["estimated_gb"]
        events.emit(self.service.state, kind, data)
