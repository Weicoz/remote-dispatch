"""上下文包／交接包拼装：只用结构化数据，不调用模型。

`build` 是唯一的拼装与截断入口。`run --context-from` 和工作线交接包都调用它。
来源由调用方整理成字典列表，本模块不读磁盘、不执行 git。
"""

# 约 4k token 上限。
LIMIT_TOKENS = 4000
# 单项长度上限，防止一条记录占满预算。
ITEM_CHARS = 300
SUMMARY_CHARS = 600
# 截断优先级：数字小的先保留，大的先丢弃。
SECTIONS = (("summary", "摘要"), ("unfinished", "未完成项"), ("decisions", "决策"),
            ("commits", "提交"), ("files", "改动文件"))
PRIORITY = {key: rank for rank, (key, _) in enumerate(SECTIONS)}
LABEL = dict(SECTIONS)
# 追加截断说明时预留的 token。
NOTE_RESERVE = 80


def estimate_tokens(text):
    """粗估：ASCII 约 4 字符 1 token，其他字符（含中文）约 1 字符 1 token。"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return -(-ascii_chars // 4) + (len(text) - ascii_chars)


def _clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _items(source):
    """按固定顺序列出来源的可截断条目：(优先级, 来源序号, 分段, 序号, 文本)。"""
    rows = []
    for key, _ in SECTIONS:
        value = source.get(key)
        values = [value] if key == "summary" and value else value if isinstance(value, (list, tuple)) else []
        limit = SUMMARY_CHARS if key == "summary" else ITEM_CHARS
        for number, item in enumerate(values):
            if item not in (None, ""):
                rows.append((key, number, _clip(item, limit)))
    return rows


def _header(source):
    bits = [f"第 {source['round']} 轮"] if source.get("round") else []
    for key, label in (("review_state", "验收"), ("outcome", "结果"), ("checks", "检查"), ("head", "head")):
        if source.get(key):
            bits.append(f"{label} {str(source[key])[:12] if key == 'head' else source[key]}")
    return f"## 来源 {source['job']}" + (f"（{'，'.join(bits)}）" if bits else "")


def _render(sources, kept, dropped, limit=LIMIT_TOKENS):
    lines = ["# 上下文包（rdispatch 由前序派单的结构化数据拼装，未经模型总结）"]
    for index, source in enumerate(sources):
        lines.append(_header(source))
        for key, label in SECTIONS:
            chosen = [text for number, text in sorted(kept.get((index, key), []))]
            if not chosen:
                continue
            if key == "summary":
                lines.append(f"{label}：{chosen[0]}")
            else:
                lines.append(f"{label}：")
                lines.extend("- " + text for text in chosen)
    if dropped:
        lines.append("[已截断：超出约 %d token 上限，已按优先级（摘要 > 未完成项 > 决策 > 提交 > 改动文件）省略 %s]"
                     % (limit, "、".join(f"{LABEL[k]} {n} 项" for k, n in dropped.items())))
    return "\n".join(lines) + "\n"



def build(sources, limit=LIMIT_TOKENS):
    """把来源列表拼成上下文包文本；超过 limit 时按固定优先级截断并注明。

    来源字典键：job（必填）、round、review_state、outcome、checks、head，
    以及 summary（字符串）和 unfinished／decisions／commits／files（字符串列表）。
    """
    if not sources:
        return ""
    pool = [(PRIORITY[key], index, number, key, text)
            for index, source in enumerate(sources) for key, number, text in _items(source)]
    pool.sort(key=lambda row: row[:3])

    def select(budget):
        kept, dropped, blocked = {}, {}, set()
        used = estimate_tokens(_render(sources, {}, {}))
        for _, index, number, key, text in pool:
            cost = estimate_tokens(text) + 3
            if (index, key) in blocked or used + cost > budget:
                # 同一分段只保留连续的前缀，不跳着取。
                blocked.add((index, key))
                dropped[key] = dropped.get(key, 0) + 1
                continue
            used += cost
            kept.setdefault((index, key), []).append((number, text))
        return kept, dropped

    kept, dropped = select(limit)
    if dropped:
        kept, dropped = select(limit - NOTE_RESERVE)
    ordered = {k: dropped[k] for k, _ in SECTIONS if k in dropped}
    text = _render(sources, kept, ordered, limit)
    # 来源头本身过多时的最后保险。
    while estimate_tokens(text) > limit and len(text) > 1:
        text = text[:max(1, int(len(text) * 0.9))]
    return text
