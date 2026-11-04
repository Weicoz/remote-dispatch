"""回执与收取：结果查询、收取持久化、inbox、看板、签收与验收。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import contextlib
import json
import re
import sys
import time
from . import rd_native as native
from . import rd_messages as messages
from . import rd_artifacts as artifacts
from . import rd_query as query
from . import rd_review as reviews
from . import rd_events as events
from . import rd_board as board
from .rd_lock import JobBusy, JobLock
from .rd_reports import read_reports
from . import cli

__all__ = [
    "acknowledge_receipt",
    "add_receipt_extras",
    "cmd_ack",
    "cmd_board",
    "cmd_inbox",
    "cmd_inbox_wait",
    "cmd_orphans",
    "cmd_result",
    "cmd_review",
    "collect_result",
    "delivery",
    "live_receipt",
    "persist_receipt",
    "queries",
    "reconcile_dispatch_state",
    "remote_report",
    "round_output",
    "serve_online",
    "wait_state_name",
    "wait_state_text",
]


def remote_report(host, rec, number, kind):
    relative = "reports/" + kind + ".r" + str(messages.round_id(number)) + ".json"
    if host["platform"] == "windows" or kind == "progress":
        return native.request(host, "report-read", dict(job=rec["job"], round=number, kind=kind),
                              timeout=cli.REMOTE_READ_TIMEOUT)
    path = '"$ROOT/jobs/' + messages.job_id(rec["job"]) + '/' + relative + '"'
    reply = cli.ssh(host, f'if [ -L {path} ]; then exit 4; fi; if [ -f {path} ]; then head -c {messages.MAX_BYTES + 1} {path}; fi', check=False, timeout=cli.REMOTE_READ_TIMEOUT)
    if reply is None or reply.returncode:
        raise ValueError("无法读取远端回执；连接错误不代表任务失败。")
    if not reply.stdout.strip():
        return None
    if len(reply.stdout.encode("utf-8")) > messages.MAX_BYTES:
        raise ValueError("远端回执超过64KiB。")
    return messages.validate_report(json.loads(reply.stdout), rec["job"], number, kind)


def round_output(host, rec, number):
    """读本轮 harness 输出日志的尾部（用量事件在末尾）；读不到返回 None。"""
    number = messages.round_id(number)
    if host["platform"] == "windows":
        return native.request(host, "logs", dict(job=rec["job"], file=f"stdout.r{number}.log", lines=2000))
    reply = cli.ssh(host, f'tail -c 1048576 "$ROOT/jobs/{messages.job_id(rec["job"])}/stdout.r{number}.log" 2>/dev/null',
                check=False, timeout=30)
    return reply.stdout if reply is not None and not reply.returncode else None


def queries(cfg):
    """Adapt target reads to the reusable, write-free query interface."""
    return query.Query(cli.STATE, states=lambda recs: cli.job_states(cfg, recs),
                       report=lambda rec, number, kind: cli.remote_report(cli.job_host(cfg, rec), rec, number, kind),
                       output=lambda rec, number: cli.round_output(cli.job_host(cfg, rec), rec, number) if cfg else None,
                       receiver=lambda rec: cli.find_session(cli.job_host(cfg, rec), rec,
                                                         rec.get("agent_config") or cfg.agent(rec["agent"])),
                       project_names=[section.split(":", 1)[1] for section in cfg.cp.sections()
                                      if section.startswith("project:")] if cfg else ())


def reconcile_dispatch_state(cfg, rec, snapshot):
    """cmd_run 被强杀或在 launching 阶段异常退出，会让状态永远停在 launching／preparing。
    这两个状态只在 cmd_run 持锁期间存在：能拿到任务锁就说明 cmd_run 已退出。
    拿到锁后重新查询目标机（拿锁前的快照可能已过期，cmd_run 可能刚启动完就被杀）：
    已有启动痕迹（run.rN.sh、pid、退出码）-> started；确认没有任何痕迹 -> preparation_failed；
    连不上、锁被占时不动。就地更新 rec。"""
    if rec.get("dispatch_state") not in ("launching", "preparing") or snapshot.get("state") in (None, "", "unreachable"):
        return
    lock = None
    if rec["job"] not in cli.HELD_JOBS:
        try:
            lock = JobLock(cli.STATE, rec["job"]).acquire()
        except JobBusy:
            return
    try:
        fresh = cli.load_job(rec["job"])
        stage = fresh.get("dispatch_state")
        if stage in ("launching", "preparing"):
            current = cli.remote_state(cli.job_host(cfg, fresh), [fresh["job"]]).get(fresh["job"], {})
            state = current.get("state")
            try:
                launched = state in ("running", "done", "dead") or int(current.get("round") or 0) >= 1
            except ValueError:
                return
            if state not in (None, "", "unreachable"):
                fresh["dispatch_state"] = "started" if launched else "preparation_failed"
                cli.save_job(fresh)
        rec["dispatch_state"] = fresh["dispatch_state"]
    finally:
        if lock is not None:
            lock.release()


def collect_result(cfg, rec, snapshot):
    """Explicit write path used by inbox, resume and cleanup, never by queries."""
    cli.reconcile_dispatch_state(cfg, rec, snapshot)
    events.EventLog(cli.STATE).observe_job(rec, snapshot)
    number = int(snapshot.get("round") or 0) or rec["rounds"]
    receipt = cli.queries(cfg).result(rec, number, snapshot)
    if receipt is not None and messages.read_json(cli.result_path(rec, number)) is None:
        receipt = cli.add_receipt_extras(cfg, rec, snapshot, receipt)
        cli.persist_receipt(rec, receipt)
    return receipt


def add_receipt_extras(cfg, rec, snapshot, receipt):
    """首次落盘回执时附上产物取回结果与环境指纹记录，并重算回执 ID。"""
    results = cli.auto_pull_artifacts(cfg, rec, snapshot)
    extras = {}
    if results is not None:
        extras["artifacts"] = dict(paths=rec["artifact_paths"], results=results)
    if rec.get("fingerprint"):
        extras["fingerprint"] = rec["fingerprint"]
    if not extras:
        return receipt
    receipt = dict(receipt, **extras)
    receipt["receipt_id"] = query.receipt_id(receipt)
    return receipt


def persist_receipt(rec, receipt):
    path = cli.result_path(rec, receipt["round"])
    previous = messages.read_json(path)
    if previous == receipt:
        return
    with events.write_transaction([path]):
        messages.atomic_json(path, receipt, limit=messages.RECEIPT_BYTES)
        report = receipt.get("report") or {}
        events.emit(cli.STATE, "receipt.ready", events.job_data(rec, **receipt,
                    outcome=report.get("outcome"), checks=report.get("checks")))


def delivery(rec, receipt):
    return cli.queries(None).delivery(rec, receipt)


def cmd_result(a, cfg):
    rec = cli.load_job(a.job)
    cli.require_initiator(rec, a)
    cli.job_host(cfg, rec)
    receipt = messages.read_json(cli.result_path(rec, a.round or rec["rounds"]))
    if receipt is None:
        snapshot = cli.job_states(cfg, [rec]).get(rec["job"], {})
        if a.round and (int(snapshot.get("round") or 0) or rec["rounds"]) != a.round:
            cli.die("该历史轮次尚无本地回执；先在原轮次收取结果。", 4)
        receipt = cli.queries(cfg).result(rec, a.round or rec["rounds"], snapshot)
    if receipt is None:
        cli.die("结果尚未取得；用 rdispatch status 查看运行或连接状态。", 2)
    result = dict(dict(receipt, **cli.queries(cfg).review(rec, receipt["round"])), delivery_state=cli.delivery(rec, receipt),
                  acknowledgement=cli.queries(cfg).acknowledgement(rec, receipt))
    if rec.get("artifact_paths") and "artifacts" not in result:
        pulled = cli.auto_pull_artifacts(cfg, rec, {"state": "done"})
        if pulled is not None:
            result["artifacts"] = dict(paths=rec["artifact_paths"], results=pulled)
            print("\n".join(["产物取回："] + artifacts.artifact_lines(pulled)), file=sys.stderr)
    if getattr(a, "summary", False):
        result = messages.result_summary(result)
        result["receipt_path"] = str(cli.result_path(rec, receipt["round"]))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_orphans(a, cfg):
    """列出当前用户 ID 下没有发起会话的派单及其待收取回执，供集中签收。"""
    uid = cli.require_user()
    recs = [r for r in cli.all_jobs() if r.get("user_id") == uid and not r.get("initiator_session")]
    states = cli.job_states(cfg, recs)
    rows = []
    for rec in recs:
        snapshot = states.get(rec["job"], {})
        row = dict(job=rec["job"], host=rec.get("host"), project=rec.get("project"), branch=rec.get("branch"),
                   state=snapshot.get("state"), rounds=rec.get("rounds"), pending_receipts=[])
        if snapshot.get("state") != "unreachable":
            cli.collect_result(cfg, rec, snapshot)
            for path in sorted(cli.result_path(rec, 1).parent.glob("r*.json")):
                if re.fullmatch(r"r[0-9]+\.json", path.name):
                    receipt = messages.read_json(path)
                    if not cli.queries(cfg).acknowledgement(rec, receipt):
                        row["pending_receipts"].append(dict(round=receipt["round"], receipt_id=receipt["receipt_id"]))
        rows.append(row)
    if getattr(a, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("没有无主派单。")
    for row in rows:
        pending = "；".join(f"第 {p['round']} 轮待收取，签收：rdispatch ack {row['job']} --round {p['round']} --receipt-id {p['receipt_id']}"
                            for p in row["pending_receipts"]) or "无待收取回执"
        print(f"{row['job']} [{row['state'] or '未知'}] {row['host']}：{pending}")


def serve_online():
    """serve 是否在线；只探测版本接口，不发送任何命令。"""
    from .rd_serve import ServeFailed, ServeUnavailable, VERSION_ROUTE, serve_call
    try:
        serve_call("GET", VERSION_ROUTE, timeout=3)
    except (ServeUnavailable, ServeFailed):
        return False
    return True


def wait_state_text(snapshot):
    return "已完成" if snapshot.get("exit") == "0" else "失败"


def wait_state_name(snapshot):
    return "completed" if snapshot.get("exit") == "0" else "failed"


def cmd_inbox_wait(a, cfg):
    """只读等待本会话新汇报：不签收、不写回执、不留常驻进程。执行方文字不输出。"""
    owner = cli.initiator_session(a)
    if not owner:
        cli.die("inbox 需要 --initiator-session 或 RD_INITIATOR_SESSION。", 4)
    uid = cli.require_user()
    deadline = time.monotonic() + a.timeout if a.timeout else None

    signed = set()  # 签收不可撤销：已签收的 (派单, 轮次) 不再重读
    probes = {}  # (派单, 轮次) -> 只读探测到的回执；失败不缓存
    failed_at = {}  # (派单, 轮次) -> 最近一次探测失败的时刻，间隔 PROBE_RETRY_SECONDS 后才重试

    def valid(receipt):
        return isinstance(receipt, dict) and bool(receipt.get("receipt_id")) and receipt.get("round") is not None

    def probe(rec, number):
        """本地无回执时只读探测（不落盘、不签收）；探测不到（含派单已删、目标机不可达）返回 None。"""
        key = (rec["job"], number)
        if key in probes:
            return probes[key]
        if number < rec["rounds"]:
            return None  # 历史轮次无本地回执无法重建，不发 ssh
        if time.monotonic() - failed_at.get(key, -cli.PROBE_RETRY_SECONDS) < cli.PROBE_RETRY_SECONDS:
            return None
        try:
            receipt = cli.queries(cfg).result(rec, number, cli.job_states(cfg, [rec]).get(rec["job"], {}))
        except (Exception, SystemExit):  # host 已删、用户不匹配等会 die()，同样按探测不到处理
            receipt = None
        if valid(receipt):
            probes[key] = receipt
            return receipt
        failed_at[key] = time.monotonic()
        return None

    def status(rec, number):
        """acked / unacked / unknown：签收判定复用 inbox 的 acknowledgement；回执缺失或缺 receipt_id、round 按无回执处理。"""
        key = (rec["job"], number)
        if key in signed:
            return "acked"
        receipt = messages.read_json(cli.result_path(rec, number))
        if not valid(receipt):
            receipt = probe(rec, number)
        if receipt is None:
            return "unknown"
        if cli.queries(cfg).acknowledgement(rec, receipt):
            signed.add(key)
            return "acked"
        return "unacked"

    def own_jobs():
        return {r["job"]: r for r in cli.all_jobs() if r.get("user_id") == uid and r.get("initiator_session") == owner}

    def report_rows():
        # 摘要汇报（kind=digest）是运行中进度，不唤醒。
        return [row for row in read_reports(cli.STATE, uid, owner) if row.get("kind") != "digest"]

    def stored(baseline):
        # 与 inbox 一致：派单仍在、属于本会话的任意轮次，未签收且回执可得就唤醒；
        # 回执取不到时，命令开始前已存在的汇报算已存在，只有新汇报才唤醒，避免空转。
        jobs = own_jobs()
        found = {}
        for row in report_rows():
            for e in row["entries"]:
                rec = jobs.get(e["job"])
                if rec is None:
                    continue
                state = status(rec, e["round"])
                if state == "unacked" or (state == "unknown" and row["id"] not in baseline):
                    found[(row["id"], e["job"], e["round"])] = e["state"]
        return found

    def terminal_all():
        recs = list(own_jobs().values())
        if not recs:
            return {}
        states = cli.job_states(cfg, recs)
        return {(rec["job"], int(states[rec["job"]].get("round") or rec["rounds"])): (rec, states[rec["job"]])
                for rec in recs if states.get(rec["job"], {}).get("state") in ("done", "dead", "missing")}

    def terminal(baseline):
        # 离线时看当前终态轮次；回执取不到（含 dead/missing 无回执）时，开始前已是终态的算已存在，新进入终态才唤醒。
        found = {}
        for key, (rec, snapshot) in terminal_all().items():
            state = status(rec, key[1])
            if state == "unacked" or (state == "unknown" and key not in baseline):
                found[key] = snapshot
        return found

    seen_ids = {row["id"] for row in report_rows()}
    seen_terminal = set(terminal_all())
    while True:
        if cli.serve_online():
            found = list(stored(seen_ids).items())
        else:
            found = [(key, cli.wait_state_name(snap)) for key, snap in terminal(seen_terminal).items()]
        if found:
            rows = [dict(job=key[-2], round=key[-1], state=state,
                         state_text={"completed": "已完成", "blocked": "卡住", "offline": "离线",
                                     "failed": "失败"}.get(state, state),
                         view=f"rdispatch result {key[-2]}")
                    for key, state in sorted({(key[-2:], state): (key, state) for key, state in found}.values())]
            if a.json:
                print(json.dumps(dict(initiator_session=owner, reports=rows), ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['job']} 第{row['round']}轮 {row['state_text']}")
                    print(f"  查看：{row['view']}")
            sys.exit(0)
        if deadline and time.monotonic() > deadline:
            if a.json:
                print(json.dumps(dict(initiator_session=owner, reports=[], timed_out=True), ensure_ascii=False))
            else:
                print("等待超时，没有新汇报。")
            sys.exit(2)
        time.sleep(a.interval)


def cmd_inbox(a, cfg):
    if getattr(a, "wait", False):
        return cli.cmd_inbox_wait(a, cfg)
    if getattr(a, "orphans", False):
        return cli.cmd_orphans(a, cfg)
    owner = cli.initiator_session(a)
    if not owner:
        cli.die("inbox 需要 --initiator-session 或 RD_INITIATOR_SESSION。", 4)
    uid = cli.require_user()
    recs = [r for r in cli.all_jobs() if r.get("user_id") == uid and r.get("initiator_session") == owner]
    states = cli.job_states(cfg, recs)
    receipts, unavailable = [], []
    for rec in recs:
        snapshot = states.get(rec["job"], {})
        if snapshot.get("state") == "unreachable":
            unavailable.append(rec["job"])
        else:
            cli.collect_result(cfg, rec, snapshot)
        directory = cli.result_path(rec, 1).parent
        for path in sorted(directory.glob("r*.json")):
            if not re.fullmatch(r"r[0-9]+\.json", path.name):
                continue
            receipt = messages.read_json(path)
            receipt.update(cli.queries(cfg).review(rec, receipt["round"]))
            ack = cli.queries(cfg).acknowledgement(rec, receipt)
            if not ack:
                receipts.append(dict(receipt, delivery_state="pending"))
            elif ack.get("acked_by") and ack["acked_by"] != owner:
                receipts.append(dict(receipt, delivery_state="received", delegated=True,
                                     acknowledgement=ack, summary=messages.result_summary(receipt)["summary"]))
    print(json.dumps(dict(initiator_session=owner, results=receipts, unreachable=unavailable,
                         reports=read_reports(cli.STATE, uid, owner)), ensure_ascii=False, indent=2))


def cmd_board(a, cfg):
    """看板：只读，不收取回执、不写状态。默认当前发起会话，--all 为该用户全部派单。"""
    uid = cli.require_user()
    owner = None
    if not a.all:
        owner = cli.initiator_session(a)
        if not owner:
            cli.die("board 需要 --initiator-session 或 RD_INITIATOR_SESSION；--all 查看全部派单。", 4)
    recs = [r for r in cli.all_jobs() if r.get("user_id") == uid and (a.all or r.get("initiator_session") == owner)]
    result = board.build(cli.STATE, cli.queries(cfg), recs, cli.job_states(cfg, recs) if recs else {}, user_id=uid,
                         scope="all" if a.all else "session", initiator_session=owner,
                         stale_days=cfg.line_stale_days())
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else board.render(result))


@cli.locked_job
def cmd_ack(a, cfg):
    rec = cli.load_job(a.job)
    cli.job_host(cfg, rec)
    signer = messages.identifier(getattr(a, "acked_by", None) or cli.initiator_session(a) or "cli", "签收方")
    via = getattr(a, "via", "cli")
    path = cli.result_path(rec, a.round)
    receipt = cli.live_receipt(cfg, rec, a.round)
    if not receipt or receipt["receipt_id"] != a.receipt_id:
        cli.die("回执 ID 或轮次不匹配；先收取并核对 rdispatch result。", 4)
    cli.acknowledge_receipt(rec, receipt, signer, via)
    print(f"结果已由 {signer} 签收；此确认不代表验收通过，不会自动续派。")


def acknowledge_receipt(rec, receipt, signer, via):
    path = cli.result_path(rec, receipt["round"])
    with events.write_transaction([path, path.with_suffix(".ack.json")]):
        if messages.read_json(path) is None:
            cli.persist_receipt(rec, receipt)
        acknowledgement = dict(receipt_id=receipt["receipt_id"], received_at=messages.utc_now(), acked_by=signer, via=via)
        messages.atomic_json(path.with_suffix(".ack.json"), acknowledgement)
        events.emit(cli.STATE, "receipt.acked", events.job_data(rec, round=receipt["round"],
                    suppress_push=receipt.get("suppress_push", False), **acknowledgement))


def live_receipt(cfg, rec, number):
    """取本轮回执；未落盘且目标机连不上时报 receipt_host_unavailable，不误报为轮次进行中或回执不匹配。"""
    snapshot = None
    if messages.read_json(cli.result_path(rec, number)) is None:
        snapshot = cli.job_states(cfg, [rec]).get(rec["job"], {})
        # 历史轮次保留原报错：无本地回执即无法重建，与目标机是否在线无关。
        if snapshot.get("state") == "unreachable" and messages.round_id(number) == rec["rounds"]:
            cli.die(f"receipt_host_unavailable: {rec['host']} 暂时连不上，回执未取得；稍后重试。", 4)
    return cli.queries(cfg).result(rec, number, snapshot)


@cli.locked_job
def cmd_review(a, cfg):
    rec = cli.load_job(a.job)
    cli.job_host(cfg, rec)
    reviewer = messages.identifier(a.reviewed_by or cli.initiator_session(a) or "cli", "验收方")
    receipt = cli.live_receipt(cfg, rec, a.round)
    if receipt is None:
        cli.die("round_in_progress: 轮次尚无可验收回执。", 4)
    action = "rejected" if a.reject is not None else a.review_action
    note = a.reject or ""
    if action == "rejected" and not note.strip():
        cli.die("review_not_allowed: 打回必须提供附言。", 4)
    record = reviews.decide(cli.STATE, rec, a.round, receipt, action, reviewer, note)
    if action == "rejected" and a.round != rec["rounds"]:
        cli.die("round_in_progress: 只能打回当前轮次。", 4)
    path = cli.result_path(rec, a.round)
    with reviews.rollback_files([cli.job_path(rec["job"]), path, path.with_suffix(".ack.json"),
                                 reviews.review_path(cli.STATE, rec, a.round)]), contextlib.ExitStack() as undo, events.transaction():
        def write_review():
            try:
                if not cli.queries(cfg).acknowledgement(rec, receipt):
                    cli.acknowledge_receipt(rec, receipt, reviewer, a.via)
                messages.atomic_json(reviews.review_path(cli.STATE, rec, a.round), record)
                events.emit(cli.STATE, "review.updated", events.job_data(rec, round=a.round, **record))
            except OSError as error:
                raise ValueError("review_write_failed: 无法保存签收或验收记录；本次操作已撤销。") from error
        if action == "rejected":
            cli.resume_job(a, cfg, rec, prompt=note, before_launch=write_review, rollback_stack=undo)
        else:
            write_review()
    print(json.dumps(record, ensure_ascii=False, indent=2))
