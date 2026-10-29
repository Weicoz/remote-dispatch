"""定时进度摘要：按发起会话分组取看板运行中的派单，指纹变化才生成一条汇报。

摘要作为 kind="digest" 的汇报行写入现有汇报存储，送达 Herdr 与 report.created 事件
复用状态变化汇报的路径。指纹与到期时间保存在汇报存储的 digest 字段，重启后不重复推送。
"""
import hashlib
import json

from . import rd_board as board
from . import rd_messages as messages

DEFAULT_INTERVAL = 900


def enabled(config):
    """[features] progress_digest：默认开启；取值无效按开启处理。"""
    value = config.cp.get("features", "progress_digest", fallback="true").strip().lower()
    return value not in ("0", "false", "no", "off")


def interval(config):
    """[monitor] digest_interval_seconds 优先，其次 report_interval_seconds；无效值回退默认。"""
    raw = config.cp.get("monitor", "digest_interval_seconds", fallback=None)
    try:
        if raw is not None:
            value = int(raw)
        else:
            value = config.report_interval()
    except (Exception, SystemExit):
        return DEFAULT_INTERVAL
    return value if value > 0 else DEFAULT_INTERVAL


def fingerprint(summary, rows):
    """只含会随进展变化的字段；已运行时长每次都变，不参与。"""
    content = [summary, [[r["job"], r["host"], r["rounds"], r["state"], r["progress"]] for r in rows]]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def entry(row):
    progress = row["progress"]
    text = f"{progress['phase'] or ''} {progress['summary'] or ''}".strip() if progress else "无进度"
    return dict(job=row["job"], title=row["unit"], host=row["host"], round=row["rounds"], state="running",
                state_text="运行中", summary=f"已运行 {board.duration(row['elapsed_seconds'])}；{text}")


def groups(service):
    """{发起会话: 看板运行中的行}，只含本人派单。"""
    own = [rec for rec in service.records()
           if rec.get("user_id") == service.user_id and rec.get("initiator_session")]
    if not own:
        return {}
    query = service.query()
    data = board.build(service.state, query, own, query.states(own), user_id=service.user_id)
    result = {}
    for row in data["rows"]:
        if row["state"] in board.RUNNING_STATES:
            result.setdefault(row["initiator_session"], dict(rows=[], summary=data["summary"]))["rows"].append(row)
    return result


def step(service, saved, now):
    """到期后生成摘要行并追加到 saved["reports"]，返回 report.created 事件数据列表。"""
    state = saved.setdefault("digest", dict(due=now + interval(service.config), fingerprints={}))
    # 间隔调小后立即生效：到期时间不晚于当前时间加新间隔。
    state["due"] = min(state["due"], now + interval(service.config))
    if not enabled(service.config) or now < state["due"]:
        return []
    state["due"] = now + interval(service.config)
    created = []
    current = groups(service)
    for owner, group in sorted(current.items()):
        print_ = fingerprint(group["summary"], group["rows"])
        if state["fingerprints"].get(owner) == print_:
            continue
        state["fingerprints"][owner] = print_
        entries = [service.redact(entry(row)) for row in group["rows"]]
        report_id = hashlib.sha256(json.dumps(["digest", owner, print_, now], separators=(",", ":")).encode()).hexdigest()
        saved["reports"].append(dict(id=report_id, kind="digest", initiator_session=owner,
                                     created_at=messages.utc_now(), entries=entries,
                                     delivery=dict(status="queued", reason=None, pane_id=None, delivered_at=None)))
        created.append(dict(report_id=report_id, user_id=service.user_id, initiator_session=owner,
                            job_count=len(entries)))
    # 已无运行中派单的会话清掉指纹，之后再有运行中派单时视为新变化。
    for owner in [o for o in state["fingerprints"] if o not in current]:
        del state["fingerprints"][owner]
    return created
