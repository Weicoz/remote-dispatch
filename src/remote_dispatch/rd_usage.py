"""实际模型与 token 用量：只读 harness 公开的结构化输出，集中在此解析，按 harness 分别实现。

取不到或解析失败一律返回「未记录」，不抛错，不影响派单与回执。
输入口径统一为「不含缓存」的新输入；缓存读写分开记。
"""
import json

UNRECORDED = "未记录"
FIELDS = ("input", "cache_read", "cache_write", "output")


def _events(text):
    """逐行解析 JSON 事件；残缺行与非对象行直接跳过。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _finish(values):
    """四项中至少有一项可读才算记录；其余项记「未记录」。"""
    if all(values.get(name) is None for name in FIELDS):
        return None
    return {name: UNRECORDED if values.get(name) is None else values[name] for name in FIELDS}


def _codex(text):
    """Codex `--json`：每个 turn.completed 带 usage，input_tokens 含缓存命中，需扣除。"""
    total, seen, model = dict.fromkeys(FIELDS, 0), False, None
    for event in _events(text):
        if isinstance(event.get("model"), str):
            model = event["model"]
        usage = event.get("usage") if event.get("type") == "turn.completed" else None
        if not isinstance(usage, dict):
            continue
        new, cached, output = (_count(usage.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
        if new is None and output is None:
            continue
        seen = True
        total["cache_read"] += cached or 0
        total["input"] += max((new or 0) - (cached or 0), 0)
        total["output"] += output or 0
    result = dict(total, cache_write=None) if seen else None
    return model, _finish(result) if result else None


def _anthropic_usage(usage):
    return dict(input=_count(usage.get("input_tokens")), cache_read=_count(usage.get("cache_read_input_tokens")),
                cache_write=_count(usage.get("cache_creation_input_tokens")), output=_count(usage.get("output_tokens")))


def _claude(text):
    """Claude stream-json：system/init 给模型，末尾 result 事件给本轮累计用量。"""
    model, usage = None, None
    for event in _events(text):
        if event.get("type") == "system" and isinstance(event.get("model"), str):
            model = event["model"]
        if event.get("type") == "result" and isinstance(event.get("usage"), dict):
            usage = _finish(_anthropic_usage(event["usage"]))
    return model, usage


def _generic(text):
    """Kimi／AGY stream-json 与 DSH `--json`：取最后一个 usage 对象，兼容 Anthropic 与 OpenAI 两种字段名。"""
    model, usage = None, None
    for event in _events(text):
        if isinstance(event.get("model"), str):
            model = event["model"]
        found = event.get("usage")
        if not isinstance(found, dict):
            continue
        if "prompt_tokens" in found:
            prompt = _count(found.get("prompt_tokens"))
            hit = _count(found.get("prompt_cache_hit_tokens"))
            values = dict(input=None if prompt is None else max(prompt - (hit or 0), 0), cache_read=hit,
                          cache_write=None, output=_count(found.get("completion_tokens")))
        else:
            values = _anthropic_usage(found)
        usage = _finish(values) or usage
    return model, usage


def _agy(text):
    """AGY stream-json：末尾 {"event": "result", "result": {"usage": ...}} 给本轮累计用量（实测格式）。

    input_tokens 按含缓存命中处理，扣除 cache_read_tokens；没有缓存写字段。其他形态回落到通用解析。
    """
    usage = None
    for event in _events(text):
        found = (event.get("result") or {}).get("usage") if event.get("event") == "result" else None
        if not isinstance(found, dict):
            continue
        prompt, hit = _count(found.get("input_tokens")), _count(found.get("cache_read_tokens"))
        usage = _finish(dict(input=None if prompt is None else max(prompt - (hit or 0), 0), cache_read=hit,
                             cache_write=None, output=_count(found.get("output_tokens")))) or usage
    return (None, usage) if usage else _generic(text)


PARSERS = {"codex": _codex, "claude": _claude, "kimi": _generic, "agy": _agy, "dsh": _generic}


def parse(harness, text):
    """返回 (实际模型或 None, 用量字典或 None)。任何异常都按未记录处理。"""
    parser = PARSERS.get(harness)
    if parser is None:
        return None, None
    try:
        return parser(text)
    except Exception:  # 记录功能不得影响回执
        return None, None


def describe(harness, text, requested=None):
    """回执字段：模型取 harness 输出，其次派单参数，再其次「未记录」；用量取不到记「未记录」。"""
    model, usage = parse(harness, text)
    return dict(model=model or requested or UNRECORDED, usage=usage or UNRECORDED)
