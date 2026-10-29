"""按项目与目标机查询历史轮次耗时；只读本地已保存的回执，不连接目标机。

门禁选机（按历史全量测试耗时）调用 history(...) 取行，再用 median_body_by_host(...) 取各机中位数。
"""
import statistics

from . import rd_messages as messages
from .rd_query import result_path


def kind_of(rec):
    """test：--test 派单；command：--cmd 派单；agent：会话派单。"""
    if rec.get("agent"):
        return "agent"
    return "test" if rec.get("test") else "command"


def history(state, records, project=None, host=None, kind=None):
    """返回已有耗时记录的轮次行；没有本地回执或没有耗时字段的轮次跳过。"""
    rows = []
    for rec in records:
        if project is not None and rec.get("project") != project:
            continue
        if host is not None and rec.get("host") != host:
            continue
        if kind is not None and kind_of(rec) != kind:
            continue
        for number in range(1, int(rec.get("rounds") or 1) + 1):
            receipt = messages.read_json(result_path(state, rec, number))
            report = (receipt or {}).get("report") or {}
            if not isinstance(report.get("body_seconds"), (int, float)):
                continue
            rows.append(dict(job=rec["job"], round=number, project=rec.get("project"),
                             host=(receipt.get("host") or rec.get("host")), kind=kind_of(rec),
                             setup_seconds=report.get("setup_seconds"), setup_note=report.get("setup_note"),
                             body_seconds=report["body_seconds"], exit=report.get("executor_exit"),
                             finished_at=report.get("finished_at")))
    return rows


def median_body_by_host(rows, success_only=False):
    """各目标机主体耗时中位数（秒）；success_only 只统计退出码为 0 的轮次。"""
    groups = {}
    for row in rows:
        if success_only and row.get("exit") != 0:
            continue
        groups.setdefault(row["host"], []).append(row["body_seconds"])
    return {host: statistics.median(values) for host, values in groups.items()}
