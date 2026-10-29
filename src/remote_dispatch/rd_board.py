"""看板：一屏回答派了多少、在哪台目标机、用什么模型、跑了多久、进展到哪、还有多少没派。

CLI `board --json` 与 serve `GET /v1/board` 共用 build() 的返回结构。
只通过纯查询层读取，不写状态、不收取回执、不创建目录。
"""
import os
import time

from . import rd_line as workline
from . import rd_plan as plans
from . import rd_usage as usage

UNRECORDED = usage.UNRECORDED
RUNNING_STATES = ("running", "starting")
# 与 cli.BLOCK_* 的稳定枚举值一致；看板只做不连目标机的只读判断，合入与未提交检查留给 clean。
BLOCK_TEXT = dict(
    plan_referenced="待派登记中后续工作项仍引用该派单",
    running="任务运行中",
    preparing="任务准备中或启动结果待核实",
    unreachable="连接不可达",
    not_finished="未确认任务已结束",
    receipt_pending="结果回执待收取；先用 rdispatch result / rdispatch ack",
)


def parse_time(value):
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


def safe(call, default=None):
    try:
        return call()
    except (OSError, ValueError, SystemExit, KeyError, TypeError):
        return default


def pending_receipt(query, rec):
    """本地已有回执中是否还有未签收的；只读。"""
    for path in sorted(query.result_path(rec, 1).parent.glob("r*.json")):
        receipt = query.read_json(path)
        if receipt and receipt.get("receipt_id") and query.delivery(rec, receipt) != "received":
            return True
    return False


def reclaim_block(query, rec, snapshot, receipt, referrers):
    """不连目标机的回收阻塞判断，返回 {code, reason} 或 None。"""
    state = snapshot.get("state")
    unlaunched = rec.get("dispatch_state") in ("preparing", "preparation_failed")
    if referrers:
        return dict(code="plan_referenced", reason=BLOCK_TEXT["plan_referenced"] + "：" + "、".join(referrers))
    code = None
    if state == "running":
        code = "running"
    elif state == "starting" or rec.get("dispatch_state") == "preparing":
        code = "preparing"
    elif state == "unreachable":
        code = "unreachable"
    elif state not in ("done", "dead"):
        code = "not_finished"
    elif not unlaunched and (
            (rec.get("handoff_version") and receipt is not None and query.delivery(rec, receipt) != "received")
            or safe(lambda: pending_receipt(query, rec), False)):
        code = "receipt_pending"
    return dict(code=code, reason=BLOCK_TEXT[code]) if code else None


def elapsed_seconds(rec, snapshot, receipt, now):
    """运行中取派出至今；已结束取回执记录的 setup 与主体耗时之和，缺则「未记录」。"""
    if snapshot.get("state") in RUNNING_STATES:
        start = parse_time(rec.get("created"))
        return max(0, int(now - start)) if start is not None else UNRECORDED
    report = (receipt or {}).get("report") or {}
    body = report.get("body_seconds")
    if isinstance(body, (int, float)):
        setup = report.get("setup_seconds")
        return round(body + (setup if isinstance(setup, (int, float)) else 0), 3)
    return UNRECORDED


def last_activity(state, query, rec):
    """最后一次推进：回执确认或登记更新的最晚时间（只读，与 clean --expired 的口径一致）。"""
    times = []
    for path in (query.result_path(rec, 1).parent.glob("r*.ack.json")):
        times.append(path.stat().st_mtime)
    if not times:
        times.append(os.path.getmtime(os.path.join(str(state), "jobs", rec["job"] + ".json")))
    return max(times)


def line_stale(state, query, rec, referrers, now, days):
    """停滞原因或 None；读不到时间时按未停滞处理。"""
    if not referrers:
        return None
    activity = safe(lambda: last_activity(state, query, rec), None)
    if activity is None:
        return workline.stale(rec, referrers, now, now, days)
    return workline.stale(rec, referrers, activity, now, days)


def row_for(query, rec, snapshot, project_row, unit, referrers, now, stale=None):
    number = int(snapshot.get("round") or 0) or rec["rounds"]
    # 回执在断线、读取失败时记为没有，行里相应字段显示「未记录」。
    receipt = safe(lambda: query.result(rec, number, snapshot))
    progress = None
    if rec.get("handoff_version") and snapshot.get("state") in ("running", "done"):
        progress = safe(lambda: query.report(rec, number, "progress"))
    sizes = ((receipt or {}).get("report") or {}).get("regenerable_kb")
    row = dict(unit=unit, job=rec["job"], project=project_row.get("project"), host=rec.get("host") or UNRECORDED,
               harness=rec.get("agent") or UNRECORDED, model=project_row.get("model") or UNRECORDED,
               created=rec.get("created") or UNRECORDED,
               elapsed_seconds=elapsed_seconds(rec, snapshot, receipt, now),
               rounds=rec.get("rounds"), state=snapshot.get("state") or "starting",
               progress=dict(phase=progress.get("phase"), summary=progress.get("summary"),
                             updated_at=progress.get("updated_at")) if progress else None,
               initiator_session=rec.get("initiator_session"),
               reclaim_block=reclaim_block(query, rec, snapshot, receipt, referrers))
    if isinstance(sizes, dict) and sizes:
        row["regenerable_kb"] = sizes
    if stale:
        row["stale"] = stale
    residue = workline.current_residue(rec)
    if residue:
        row["residue"] = residue
    return row


def build(state, query, records, states, *, user_id, scope="all", initiator_session=None, now=None,
          stale_days=workline.STALE_DAYS):
    """records 已按范围筛好；states 是 {job: 执行快照}。返回看板数据结构。"""
    now = time.time() if now is None else now
    user_plans = plans.user_rows(state, user_id)
    unit_of = {job: item["id"] for rows in user_plans.values() for item in rows for job in item["jobs"]}
    project_rows = {row["job"]: row for row in query.jobs(records, states, user_id=user_id)}
    rows = []
    for rec in records:
        snapshot = states.get(rec["job"], {})
        project = project_rows[rec["job"]]
        referrers = plans.PlanStore(state, user_id, rec["project"]).referrers(rec["job"]) if rec.get("project") else []
        stale = line_stale(state, query, rec, referrers, now, stale_days)
        rows.append(row_for(query, rec, snapshot, project, unit_of.get(rec["job"]) or rec["job"], referrers, now, stale))
    rows.sort(key=lambda row: (row["created"] if row["created"] != UNRECORDED else "", row["job"]))
    counts = dict.fromkeys((plans.PENDING, plans.BLOCKED), 0)
    for plan_rows in user_plans.values():
        for item in plan_rows:
            if item["state"] in counts:
                counts[item["state"]] += 1
    summary = dict(dispatched=len(rows), running=sum(row["state"] in RUNNING_STATES for row in rows),
                   pending=counts[plans.PENDING], blocked=counts[plans.BLOCKED])
    return dict(scope=scope, initiator_session=initiator_session, summary=summary, rows=rows)


def duration(value):
    if not isinstance(value, (int, float)):
        return str(value)
    value = int(value)
    hours, rest = divmod(value, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s"


def size_text(sizes):
    def one(n):
        return f"{n / 1024:.0f}MB" if isinstance(n, (int, float)) else str(n)
    return "、".join(f"{path} {one(n)}" for path, n in sorted(sizes.items()))


def render(board):
    """文本看板：头部四个数字，每派单一行，阻塞与可再生目录大小另起缩进行。"""
    s = board["summary"]
    lines = [f"已派 {s['dispatched']}  运行中 {s['running']}  待派 {s['pending']}  阻塞 {s['blocked']}"]
    for row in board["rows"]:
        progress = row["progress"]
        text = f"{progress['phase'] or ''} {progress['summary'] or ''}".strip() if progress else "无进度"
        lines.append(f"{row['unit']}  [{row['host']}]  {row['harness']}/{row['model']}  派出 {row['created']}  "
                     f"已运行 {duration(row['elapsed_seconds'])}  第{row['rounds']}轮  {row['state']}"
                     f"{'  停滞' if row.get('stale') else ''}  {text}")
        if row.get("stale"):
            lines.append("    停滞提醒：" + row["stale"] + "；推进、接手或取消这些工作项，否则将按已取回未合入的保留期回收")
        if row.get("residue"):
            residue = row["residue"]
            lines.append(f"    残留未跟踪文件 {residue['total']} 个（未删除）：" + "、".join(residue["files"][:5]))
        if row["reclaim_block"]:
            lines.append(f"    回收阻塞 [{row['reclaim_block']['code']}]：{row['reclaim_block']['reason']}")
        if row.get("regenerable_kb"):
            lines.append("    可再生目录：" + size_text(row["regenerable_kb"]))
    return "\n".join(lines)
