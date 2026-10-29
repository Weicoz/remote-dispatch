"""工作线：派单记录里的「工作项 → 轮次」映射、按工作项取基线，以及接线条件判定。

判定只读，输入是调用方收集好的请求与事实，输出（可接线, 原因）；run 自动接线、
后续的 suggest、会话轮换与建议令牌都复用同一个 decide，避免条件各写一份。
"""

FALSE_VALUES = ("0", "false", "no", "off")


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


def merge_blocker(rec):
    """线未关闭时的合入阻塞原因；线关闭或不是线时为 None。前缀合入不改变派单的合入状态。"""
    line = rec.get("line")
    if line and not line.get("closed"):
        return f"工作线 {line['name']} 未关闭（用 rdispatch line close 关闭后才按已合入处理）"
    return None


def prefix_items(rec, requested):
    """校验前缀合入请求：返回 (工作项列表, 原因)。请求必须恰好是线上工作项顺序的前 k 个，不跳项。"""
    line = rec.get("line")
    if not line:
        return None, "派单不在工作线上"
    order = list(line["items"])
    requested = list(requested or [])
    if not requested:
        return None, "需要至少一个工作项"
    unknown = [item for item in requested if item not in line["items"]]
    if unknown:
        return None, "工作项不在线上：" + "、".join(unknown)
    if requested != order[:len(requested)]:
        return None, (f"只支持前缀合入：应为 {'、'.join(order[:len(requested)])}（按线上顺序，不跳项），"
                      f"本次请求 {'、'.join(requested)}")
    return requested, None


def accepted_round(rec, item, state_of):
    """工作项最近一个验收通过的轮次；没有则为 None。state_of(number) 返回该轮验收状态。"""
    for number in reversed(rec["line"]["items"][item]):
        if state_of(number) == "accepted":
            return number
    return None


def close_blocker(rec, state):
    """关闭线的阻塞原因；None 表示可以关闭。state 为目标机上最新轮次的运行状态。"""
    line = rec.get("line")
    if not line:
        return "派单不在工作线上"
    if line.get("closed"):
        return "工作线已关闭"
    if state in ("running", "starting"):
        return "线上有运行中的轮次；先 rdispatch stop 或等它结束"
    if state == "unreachable":
        return "目标机不可达，无法确认线上没有运行中的轮次"
    return None


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
    if line and line.get("closed"):
        return False, "工作线已关闭，不接新工作项"
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


def receipt_health(receipt):
    """上一轮回执是否健康：(健康, 原因)。有运行故障分类即不健康。"""
    if not receipt:
        return False, "上一轮回执不健康：没有回执"
    report = receipt.get("report") or {}
    failure = report.get("runtime_failure")
    if failure:
        return False, f"上一轮回执不健康：运行故障 {failure.get('kind') or '未分类'}"
    return True, None


def reuse_limits(parser, agent):
    """读 [line:<harness>] 的续用阈值：(单次调用上下文 token 上限, 间隔秒数上限)；未配置为 None。"""
    section = f"line:{agent}"

    def number(key):
        value = parser.get(section, key, fallback="").strip() if parser.has_section(section) else ""
        try:
            return float(value) if value and float(value) >= 0 else None
        except ValueError:
            return None

    return number("reuse_max_context_tokens"), number("reuse_max_interval_seconds")


def session_policy(receipt, limits, idle_seconds):
    """会话策略：返回 (动作, 原因)。动作 rotate 为策略轮换，reuse 为续用，refuse 为不做策略轮换。

    默认每个工作项轮换。配置了阈值且全部满足才续用；缺用量数据时按不满足处理。
    """
    healthy, reason = receipt_health(receipt)
    if not healthy:
        return "refuse", reason
    context_limit, interval_limit = limits
    if context_limit is None and interval_limit is None:
        return "rotate", "默认每个工作项轮换会话"
    if context_limit is not None:
        calls = ((receipt.get("report") or {}).get("rollout_calls"))
        if not isinstance(calls, dict) or not calls.get("count"):
            return "rotate", "缺少单次调用用量，按默认轮换"
        average = calls["input_tokens"] / calls["count"]
        if average > context_limit:
            return "rotate", f"单次调用上下文约 {average:.0f} token，超过续用上限 {context_limit:g}"
    if interval_limit is not None and idle_seconds > interval_limit:
        return "rotate", f"距上一轮 {idle_seconds:.0f} 秒，超过续用间隔上限 {interval_limit:g}"
    return "reuse", "单次调用上下文与间隔均在续用阈值内"
