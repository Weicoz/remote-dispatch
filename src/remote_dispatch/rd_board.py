"""看板：一屏回答派了多少、在哪台目标机、用什么模型、跑了多久、进展到哪、还有多少没派。

CLI `board --json` 与 serve `GET /v1/board` 共用 build() 的返回结构。
只通过纯查询层读取，不写状态、不收取回执、不创建目录。
"""
import time

from . import rd_plan as plans
from . import rd_suggest as suggestions
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


def line_view(rec, plan_items):
    """派单的模式与工作线信息：单项或线、所属线、分支、线上工作项顺序、后续待派数、是否共用一个任务分支。"""
    line = rec.get("line")
    order = sorted(line["items"], key=lambda item: min(line["items"][item] or [0])) if line else []
    current = line["current"] if line else rec.get("plan_item")
    pending = len(suggestions.descendants(plan_items, current)) if current and plan_items else 0
    return dict(mode="line" if line else "single", line=line["name"] if line else None, branch=rec.get("branch"),
                line_items=order, pending_items=pending, shared_branch=len(order) > 1,
                line_hint=rec.get("line_hint"))


def row_for(query, rec, snapshot, project_row, unit, referrers, now, plan_items=None):
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
               reclaim_block=reclaim_block(query, rec, snapshot, receipt, referrers),
               **line_view(rec, plan_items))
    if isinstance(sizes, dict) and sizes:
        row["regenerable_kb"] = sizes
    return row


def build(state, query, records, states, *, user_id, scope="all", initiator_session=None, now=None):
    """records 已按范围筛好；states 是 {job: 执行快照}。返回看板数据结构。"""
    now = time.time() if now is None else now
    user_plans = plans.user_rows(state, user_id)
    unit_of = {job: item["id"] for rows in user_plans.values() for item in rows for job in item["jobs"]}
    project_rows = {row["job"]: row for row in query.jobs(records, states, user_id=user_id)}
    rows, plan_items_of = [], {}
    for rec in records:
        snapshot = states.get(rec["job"], {})
        project = project_rows[rec["job"]]
        referrers = plans.PlanStore(state, user_id, rec["project"]).referrers(rec["job"]) if rec.get("project") else []
        plan_items = plan_items_of.setdefault(rec.get("project"), plans.PlanStore(state, user_id, rec["project"]).read()
                                              if rec.get("project") else {})
        rows.append(row_for(query, rec, snapshot, project, unit_of.get(rec["job"]) or rec["job"], referrers, now, plan_items))
    rows.sort(key=lambda row: (row["created"] if row["created"] != UNRECORDED else "", row["job"]))
    counts = dict.fromkeys((plans.PENDING, plans.BLOCKED), 0)
    for plan_rows in user_plans.values():
        for item in plan_rows:
            if item["state"] in counts:
                counts[item["state"]] += 1
    summary = dict(dispatched=len(rows), running=sum(row["state"] in RUNNING_STATES for row in rows),
                   pending=counts[plans.PENDING], blocked=counts[plans.BLOCKED])
    chains = [dict(project=project, line=name, suggested=setting["suggested"], accepted=setting["accepted"],
                   rejected=setting["rejections"])
              for project in sorted(user_plans) for name, setting in plans.PlanStore(state, user_id, project).chains().items()]
    return dict(scope=scope, initiator_session=initiator_session, summary=summary, chains=chains, rows=rows)


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
                     f"已运行 {duration(row['elapsed_seconds'])}  第{row['rounds']}轮  {row['state']}  {text}")
        if row["mode"] == "line":
            shared = "，多个工作项共用任务分支" if row["shared_branch"] else ""
            lines.append(f"    工作线 {row['line']}：{' -> '.join(row['line_items'])}（分支 {row['branch']}，待派 {row['pending_items']}{shared}）")
        else:
            lines.append(f"    单项（分支 {row['branch']}，待派 {row['pending_items']}）")
        if row.get("line_hint"):
            lines.append("    提示：" + row["line_hint"]["message"])
        if row["reclaim_block"]:
            lines.append(f"    回收阻塞 [{row['reclaim_block']['code']}]：{row['reclaim_block']['reason']}")
        if row.get("regenerable_kb"):
            lines.append("    可再生目录：" + size_text(row["regenerable_kb"]))
    for chain in board.get("chains") or []:
        lines.append(f"线 {chain['project']}/{chain['line']}：建议 {chain['suggested']} 次，接受 {chain['accepted']} 次，拒绝 {chain['rejected']} 次")
    return "\n".join(lines)
