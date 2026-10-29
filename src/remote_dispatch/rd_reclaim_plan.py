"""定时回收的持久化状态：首周只出计划的起点、上次执行时间、各目标机最近一次计划与结果。

看板（rd_board）与 serve 接口只读本文件，不连目标机。纯标准库，避免与 cli 循环导入。
"""
from pathlib import Path

from . import rd_messages as messages

NAME = "reclaim-schedule.json"
# 启用后这段时间内只生成计划，不执行瘦身与删除。
PLAN_ONLY_SECONDS = 7 * 86400


def path(state):
    return Path(state) / NAME


def read(state, user_id):
    """返回当前用户的回收状态；不存在、损坏或属于其他用户时返回 None。"""
    try:
        data = messages.read_json(path(state))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("user_id") != user_id:
        return None
    return data


def write(state, data):
    messages.atomic_json(path(state), data)


def view(data, now):
    """对外展示结构：起点、只出计划的截止时间、当前模式与各目标机计划。"""
    if not data:
        return None
    enabled_at = data.get("enabled_at")
    until = enabled_at + PLAN_ONLY_SECONDS if isinstance(enabled_at, (int, float)) else None
    return dict(enabled_at=enabled_at, plan_only_until=until,
                mode="plan" if until is None or now < until else "execute",
                last_run=data.get("last_run"), hosts=data.get("hosts") or {})
