"""无 I/O 的 harness JSONL → 统一聊天消息转换。

公开入口接受 JSONL 字符串行（可迭代一次），返回 supported/messages。
未知记录类型与额外字段跳过；损坏 JSON 或必需结构非法使整次转换返回
unsupported_format，不带部分正文。
已知元数据和附件跳过；正文与工具输入各保留至多 MAX_CONTENT_CHARS 个字符。
"""
import json
from typing import Iterable


MAX_CONTENT_CHARS = 16_384

_CLAUDE_IGNORED = {
    "attachment", "queue-operation", "ai-title", "last-prompt", "mode", "atis-latch",
    "custom-title", "agent-name", "cost-state", "permission-mode", "continued-in",
}
_CODEX_IGNORED = {"event_msg", "turn_context", "session_meta", "world_state",
                  "inter_agent_communication_metadata", "compacted"}


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def _identifier(value: object) -> str:
    value = _string(value)
    if not value:
        raise ValueError("expected identifier")
    return value


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _array(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError("expected array")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected boolean")
    return value


def _records(lines: Iterable[str]):
    for line_number, line in enumerate(lines):
        if not _string(line).strip():
            continue
        record = _object(json.loads(line))
        _identifier(record["type"])
        yield line_number, record


def _text_blocks(content: object, text_types: tuple, attachments: tuple = ()) -> str:
    texts = []
    for value in _array(content):
        block = _object(value)
        kind = _identifier(block["type"])
        if kind in text_types:
            texts.append(_string(block["text"]))
        elif kind not in attachments:
            raise ValueError("content block")
    return "\n".join(texts)


def _output_text(content: object, text_types: tuple, attachments: tuple = ()) -> str:
    return content if isinstance(content, str) else _text_blocks(content, text_types, attachments)


def _claude(lines: Iterable[str], tools=None) -> list:
    messages = []
    tools = {} if tools is None else tools
    for _, record in _records(lines):
        record_type = record["type"]
        if (record_type in _CLAUDE_IGNORED or record_type.startswith("file-history-")
                or record_type.startswith("api-request")):
            continue
        if record_type not in ("user", "assistant", "system"):
            continue
        if record_type == "system" and not isinstance(record.get("content"), str):
            continue
        time = _string(record["timestamp"])
        source_id = _identifier(record["uuid"])
        if record_type == "system":
            blocks = [{"type": "text", "text": record["content"]}]
            kind = "error" if record.get("level") == "error" else "system"
        else:
            source = _object(record["message"])
            if source["role"] != record_type:
                raise ValueError("message role")
            content = source["content"]
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else _array(content)
            is_meta = _boolean(record.get("isMeta", False))
            kind = "system" if record_type == "user" and is_meta else record_type
        for index, value in enumerate(blocks):
            block = _object(value)
            block_type = _identifier(block["type"])
            if block_type in ("image", "document"):
                continue
            message = {"id": f"claude:{source_id}:{index}", "time": time, "kind": kind, "text": ""}
            if block_type == "text":
                message["text"] = _string(block["text"])
            elif block_type == "thinking":
                message.update(kind="thinking", text=_string(block["thinking"]), collapsed=True)
            elif block_type == "tool_use":
                call_id = _identifier(block["id"])
                name = _identifier(block["name"])
                tools[call_id] = name
                message.update(kind="tool_call", tool=name, input=_object(block["input"]),
                               call_id=call_id, is_error=False)
            elif block_type == "tool_result":
                call_id = _identifier(block["tool_use_id"])
                text = _output_text(block["content"], ("text",), ("image", "document", "tool_reference"))
                message.update(kind="tool_result", text=text, call_id=call_id,
                               tool=tools.get(call_id), is_error=_boolean(block.get("is_error", False)))
            else:
                raise ValueError("content block")
            messages.append(message)
    return messages


def _codex(lines: Iterable[str], tools=None, line_offset=0) -> list:
    messages = []
    tools = {} if tools is None else tools
    for line_number, record in _records(lines):
        line_number += line_offset
        if record["type"] in _CODEX_IGNORED or record["type"].startswith("token_"):
            continue
        if record["type"] != "response_item":
            continue
        item = _object(record["payload"])
        kind = _identifier(item["type"])
        if kind not in ("message", "reasoning", "function_call", "custom_tool_call",
                        "function_call_output", "custom_tool_call_output"):
            continue
        time = _string(record["timestamp"])
        if "ordinal" in record:
            ordinal = record["ordinal"]
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError("record ordinal")
            position = f"ordinal:{ordinal}"
        else:
            position = f"line:{line_number}"
        source_id = position if item.get("id") is None else f"id:{_identifier(item['id'])}:{position}"
        message = {"id": f"codex:{source_id}:0", "time": time, "kind": "", "text": ""}
        if kind == "message":
            role = _identifier(item["role"])
            if role not in ("user", "assistant", "developer", "system"):
                raise ValueError("message role")
            text_type = "output_text" if role == "assistant" else "input_text"
            text = _text_blocks(item["content"], (text_type,), ("input_image",))
            if not text and not any(part["type"] == text_type for part in item["content"]):
                continue
            message.update(kind="system" if role in ("developer", "system") else role, text=text)
        elif kind == "reasoning":
            text = _text_blocks(item["summary"], ("summary_text",))
            if not item["summary"]:
                continue
            message.update(kind="thinking", text=text)
        elif kind in ("function_call", "custom_tool_call"):
            call_id = _identifier(item["call_id"])
            name = _identifier(item["name"])
            tools[call_id] = name
            message.update(kind="tool_call", tool=name, call_id=call_id,
                           input=_string(item["arguments" if kind == "function_call" else "input"]), is_error=False)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            call_id = _identifier(item["call_id"])
            text = _output_text(item["output"], ("input_text", "output_text"), ("input_image", "image"))
            message.update(kind="tool_result", text=text, call_id=call_id,
                           tool=tools.get(call_id), is_error=_boolean(item.get("is_error", False)))
        messages.append(message)
    return messages


def convert_transcript(harness: str, lines: Iterable[str], *, tools=None, line_offset=0) -> dict:
    """将 claude/codex JSONL 转为统一消息；跳过未知记录，损坏格式整体返回“不支持”。

    不读取路径、环境、全局会话或服务。忽略空行；id 在向末尾追加记录时稳定。
    工具 input 保留原类型；超长对象序列化成 JSON 前缀字符串并标 truncated。
    仅当超长时添加 truncated=True；Claude thinking 带 collapsed=True。
    """
    try:
        if harness == "claude":
            messages = _claude(lines, tools)
        elif harness == "codex":
            messages = _codex(lines, tools, line_offset)
        else:
            raise ValueError("harness")
        for message in messages:
            if len(message["text"]) > MAX_CONTENT_CHARS:
                message["text"] = message["text"][:MAX_CONTENT_CHARS]
                message["truncated"] = True
            if "input" in message:
                content = message["input"]
                serialized = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                if len(serialized) > MAX_CONTENT_CHARS:
                    message["input"] = serialized[:MAX_CONTENT_CHARS]
                    message["truncated"] = True
        return {"supported": True, "messages": messages}
    except (ValueError, KeyError, TypeError):
        return {"supported": False, "messages": [], "reason": "unsupported_format", "text": "不支持"}


def convert_claude(lines: Iterable[str]) -> dict:
    """只转换 Claude Code 的 JSONL 行。"""
    return convert_transcript("claude", lines)


def convert_codex(lines: Iterable[str]) -> dict:
    """只转换 Codex 的 JSONL 行；reasoning 只公开 summary。"""
    return convert_transcript("codex", lines)
