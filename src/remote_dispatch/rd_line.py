"""工作线：派单记录里的「工作项 → 轮次」映射、按工作项取基线，以及接线条件判定。

判定只读，输入是调用方收集好的请求与事实，输出（可接线, 原因）；run 自动接线、
后续的 suggest、会话轮换与建议令牌都复用同一个 decide，避免条件各写一份。
"""

import os
import time

FALSE_VALUES = ("0", "false", "no", "off")
# 线上有待派工作项时，无人推进超过这个天数即停滞；再过同样天数视为无人处理，按「已取回未合入」的保留期处理。
STALE_DAYS = 7
# 残留清单写入回执与看板的条数上限；超出只记总数。
RESIDUE_LIMIT = 50


def feature_enabled(parser):
    """[features] line：显式写 false 才关闭；关闭后不接新线，已有线继续。"""
    return parser.get("features", "line", fallback="true").strip().lower() not in FALSE_VALUES


def round_base(rec, number):
    """某一轮的 RD_BASE：线上取该轮所属工作项开始时的基线，其余取派单基线。"""
    line = rec.get("line")
    if line:
        for item, rounds in line["items"].items():
            if number in rounds:
                return line["bases"].get(item) or rec.get("base")
    return rec.get("base")


def first_item(rec):
    return rec.get("plan_item") or rec["job"]


def current_item(rec):
    line = rec.get("line")
    return line["current"] if line else first_item(rec)


def start_item(rec, item, number, base):
    """新工作项从第 number 轮开始；线不存在时先把已有轮次记到第一个工作项上。"""
    line = rec.get("line")
    if not line:
        first = first_item(rec)
        line = rec["line"] = dict(name=first, items={first: list(range(1, number))}, bases={}, current=first)
    line["items"][item] = [number]
    line["bases"][item] = base
    line["current"] = item


def note_round(rec, number):
    """线上打回续跑只追加到当前工作项。"""
    line = rec.get("line")
    if line and number not in line["items"][line["current"]]:
        line["items"][line["current"]].append(number)


def predecessor(items, item, after):
    """取本工作项唯一的前序派单：(派单 ID, 原因)。after 为 --after 传入的派单。"""
    row = items.get(item) or {}
    jobs = list(dict.fromkeys(list(after or []) + list(row.get("after") or [])))
    for dependency in row.get("depends") or []:
        earlier = (items.get(dependency) or {}).get("jobs") or []
        if earlier and earlier[-1] not in jobs:
            jobs.append(earlier[-1])
    if not jobs:
        return None, "没有前序派单（工作项无已派出的依赖，也没有 --after）"
    if len(jobs) > 1:
        return None, "前序派单不唯一：" + "、".join(jobs)
    return jobs[0], None


def decide(rec, request, facts):
    """严格接线条件。返回 (是否可接线, 原因)；原因为 None 表示可接线。

    request：uid、owner、agent、model、permission、include（已规范化列表）、wip、branch、host、
             item（新工作项名）、enabled。
    facts：state（目标机上的运行状态）、review_state（最新轮验收状态）、session、head（验收轮回执 head）。
    """
    if request.get("enabled") is False and not rec.get("line"):
        return False, "[features] line 已关闭，不接新线"
    if not request.get("item"):
        return False, "接线需要 --plan-item"
    if not request.get("agent"):
        return False, "只有代理派单可以接入工作线"
    if not rec.get("handoff_version"):
        return False, "前序派单没有交接协议版本"
    if not rec.get("user_id") or rec["user_id"] != request["uid"]:
        return False, "前序派单不属于当前用户 ID"
    if rec.get("initiator_session") and rec["initiator_session"] != request.get("owner"):
        return False, "发起会话与前序派单不一致"
    if request["agent"] != rec.get("agent"):
        return False, f"harness 与线不一致（线 {rec.get('agent')}，本次 {request['agent']}）"
    if request.get("model") != rec.get("model"):
        return False, f"模型与线不一致（线 {rec.get('model') or '默认'}，本次 {request.get('model') or '默认'}）"
    if request.get("permission") != rec.get("permission_mode"):
        return False, (f"权限档位与线不一致（线 {rec.get('permission_mode') or '默认'}，"
                       f"本次 {request.get('permission') or '默认'}）")
    if sorted(request.get("include") or []) != sorted(rec.get("include_paths") or []):
        return False, "--include 与线不一致"
    if request.get("wip"):
        return False, "带 --wip 的派单不接入工作线"
    if request.get("branch") and request["branch"] != rec.get("branch"):
        return False, f"指定的分支 {request['branch']} 不是线分支 {rec.get('branch')}"
    if request.get("host") not in (None, "auto", rec.get("host")):
        return False, f"目标机 {request['host']} 与线所在的 {rec.get('host')} 不一致"
    line = rec.get("line")
    if line and request["item"] in line["items"]:
        return False, f"工作项 {request['item']} 已在线上"
    if facts.get("state") in ("running", "starting"):
        return False, "前序轮次仍在运行；同一条线同一时间只有一个运行中的轮次"
    if facts.get("state") in (None, "missing", "unreachable"):
        return False, "前序派单的 worktree 已回收或目标机不可达"
    if facts.get("review_state") != "accepted":
        return False, "前序工作项尚未验收通过"
    if not facts.get("head"):
        return False, "验收轮回执缺少 head，无法确定基线"
    if not facts.get("session"):
        return False, "找不到前序派单的接收会话"
    return True, None


def stale_days(parser, environ=None):
    """线停滞天数：环境变量 RD_LINE_STALE_DAYS 优先，其次 [line] stale_days，默认 7。非法值抛 ValueError。"""
    environ = os.environ if environ is None else environ
    value = environ.get("RD_LINE_STALE_DAYS") or parser.get("line", "stale_days", fallback=str(STALE_DAYS))
    try:
        days = float(value)
    except (TypeError, ValueError):
        days = -1
    if not days >= 0:
        raise ValueError("RD_LINE_STALE_DAYS / [line] stale_days 必须是非负天数。")
    return days


def idle_days(activity, now):
    return max(0.0, (now - activity) / 86400)


def stale(rec, referrers, activity, now, days):
    """线是否停滞，返回原因或 None。只有线上还有待派工作项（referrers）时才占盘，才谈停滞。

    原因：发起会话缺失；或最后一次推进（回执确认、新轮次）距今超过 days 天。
    """
    if not referrers or not (rec.get("line") or referrers):
        return None
    if not rec.get("initiator_session"):
        return "发起会话缺失，待派工作项无人推进：" + "、".join(referrers)
    idle = idle_days(activity, now)
    if idle > days:
        return f"待派工作项 {'、'.join(referrers)} 已 {idle:.1f} 天无人推进（超过 {days:g} 天）"
    return None


def abandoned(rec, referrers, activity, now, days):
    """停滞后继续无人处理（再过 days 天）：不再因待派工作项保留，按「已取回未合入」的保留期处理。"""
    return bool(stale(rec, referrers, activity, now, days)) and idle_days(activity, now) > 2 * days


def record_residue(rec, item, paths):
    """记录工作项开始前 worktree 里的未跟踪文件清单；只记录，不删除。"""
    line = rec.get("line")
    if not line or not paths:
        return
    paths = sorted(dict.fromkeys(paths))
    line.setdefault("residue", {})[item] = dict(files=paths[:RESIDUE_LIMIT], total=len(paths))


def residue_for(rec, number):
    """某一轮所属工作项开始前的未跟踪文件清单；没有记录返回 None。"""
    line = rec.get("line") or {}
    for item, rounds in line.get("items", {}).items():
        if number in rounds:
            return (line.get("residue") or {}).get(item)
    return None


def current_residue(rec):
    line = rec.get("line") or {}
    return (line.get("residue") or {}).get(line.get("current"))


def takeover(rec, uid, session):
    """同一用户 ID 的其他发起会话接手工作线，写入交接记录。拒绝时抛 ValueError。"""
    if not rec.get("line"):
        raise ValueError("只有工作线可以被其他发起会话接手；普通派单仍只允许原发起会话。")
    if not rec.get("user_id") or rec["user_id"] != uid:
        raise ValueError("工作线不属于当前用户 ID，拒绝接手。")
    previous = rec.get("initiator_session")
    if previous == session:
        return False
    rec["line"].setdefault("handovers", []).append(
        dict(from_session=previous, to_session=session, at=time.strftime("%Y-%m-%d %H:%M:%S")))
    rec["initiator_session"] = session
    return True
