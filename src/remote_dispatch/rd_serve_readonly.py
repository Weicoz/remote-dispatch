"""serve 在线时，用进程内快照缓存直接生成只读命令（inbox、status）的输出。

不起子进程、不做同步 ssh。输出格式和退出码与 CLI 直跑一致（对照测试见 tests/test_rd_serve_readonly.py）。
缓存缺失、过旧（超过 2 个轮询周期且不是已降频的终态）或需要先收取回执时，返回 handled=false，
CLI 回退到原有路径（inbox 走 serve 子进程执行，status 本地直跑）。
"""
import json
import re
import time
from datetime import datetime, timezone

from pathlib import Path

from . import cli
from . import rd_messages as messages
from .rd_query import result_path
from .rd_reports import read_reports
from .rd_serve_history import instant

TERMINAL = ("done", "dead", "missing")
# 缓存可信的最长快照年龄 = 轮询周期的倍数（再加上最近一轮采集耗时）。
MAX_AGE_CYCLES = 2


class NotCached(Exception):
    """缓存不足以生成与 CLI 直跑一致的输出；调用方回退到原路径。"""


class CachedReads:
    COMMANDS = ("inbox", "status")

    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.clock = clock

    # 入口 -----------------------------------------------------------------
    def handle(self, request):
        service = self.service
        if not isinstance(request, dict) or request.get("user_id") != service.user_id:
            raise ValueError("invalid_cli_user")
        if request.get("config") != str(Path(cli.CONFIG).resolve()):
            raise ValueError("cli_config_mismatch")
        command, params = request.get("command"), request.get("params")
        if command not in self.COMMANDS or not isinstance(params, dict):
            raise ValueError("invalid_cli_arguments")
        try:
            stdout = getattr(self, "render_" + command)(params)
        except (NotCached, SystemExit, OSError, ValueError, KeyError, TypeError):
            return dict(handled=False)
        return dict(handled=True, exit_code=0, stdout=stdout, stderr="")

    # 缓存 -----------------------------------------------------------------
    def max_age(self):
        collector = self.service.collector
        return MAX_AGE_CYCLES * self.service.poll_interval + collector.last_cycle_seconds

    def states(self, recs):
        """每个 job 取缓存快照，还原为 CLI 直跑时 job_states 返回的形状；任一缺失或过旧则 NotCached。"""
        service = self.service
        states = {}
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        for rec in recs:
            job = rec["job"]
            with service.cache_lock:
                snapshot = service.snapshots.get(job)
                generation = service.generations.get(job)
                raw = service.raw_rounds.get(job, "")
                complete = job in service.complete_snapshots
            if snapshot is None or generation != service.generation(rec) or not complete:
                raise NotCached(job)
            if not service.collector.is_settled(rec):
                taken = instant(snapshot.get("snapshot_at"))
                if taken is None or (now - taken).total_seconds() > self.max_age():
                    raise NotCached(job)
            state = {key: value for key, value in snapshot.items() if key != "snapshot_at"}
            if raw == "":
                pass
            elif raw is None:
                state.pop("round", None)
            else:
                state["round"] = raw
            states[job] = state
        return states

    def require_receipts(self, rec, state):
        """终态但回执尚未落盘：CLI 直跑会在此收取，缓存路径做不到，回退。"""
        if state.get("state") in TERMINAL:
            number = int(state.get("round") or 0) or rec["rounds"]
            if messages.read_json(result_path(self.service.state, rec, number)) is None:
                raise NotCached(rec["job"])

    # inbox ----------------------------------------------------------------
    def render_inbox(self, params):
        service = self.service
        owner = params.get("initiator_session")
        if not isinstance(owner, str) or not owner:
            raise NotCached("initiator_session")
        uid = service.user_id
        recs = [r for r in cli.all_jobs() if r.get("user_id") == uid and r.get("initiator_session") == owner]
        states = self.states(recs)
        query = service.query()
        receipts, unavailable = [], []
        for rec in recs:
            snapshot = states.get(rec["job"], {})
            if snapshot.get("state") == "unreachable":
                unavailable.append(rec["job"])
            else:
                self.require_receipts(rec, snapshot)
            directory = result_path(service.state, rec, 1).parent
            for path in sorted(directory.glob("r*.json")):
                if not re.fullmatch(r"r[0-9]+\.json", path.name):
                    continue
                receipt = messages.read_json(path)
                receipt.update(query.review(rec, receipt["round"]))
                ack = query.acknowledgement(rec, receipt)
                if not ack:
                    receipts.append(dict(receipt, delivery_state="pending"))
                elif ack.get("acked_by") and ack["acked_by"] != owner:
                    receipts.append(dict(receipt, delivery_state="received", delegated=True,
                                         acknowledgement=ack, summary=messages.result_summary(receipt)["summary"]))
        return json.dumps(dict(initiator_session=owner, results=receipts, unreachable=unavailable,
                               reports=read_reports(service.state, uid, owner)), ensure_ascii=False, indent=2) + "\n"

    # status ---------------------------------------------------------------
    def render_status(self, params):
        service = self.service
        job = params.get("job")
        if job is not None and not isinstance(job, str):
            raise NotCached("job")
        recs = ([cli.load_job(job)] if job else
                [r for r in cli.all_jobs() if not r.get("user_id") or r["user_id"] == service.user_id])
        if not recs:
            return "no jobs.\n"
        states = self.states(recs)
        if params.get("json"):
            return json.dumps(service.query().jobs(recs, states, user_id=service.user_id),
                              indent=2, ensure_ascii=False) + "\n"
        return "".join(cli.describe(rec, states.get(rec["job"], {})) + "\n" for rec in recs)
