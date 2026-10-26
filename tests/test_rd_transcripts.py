"""只使用合成 JSONL，通过公开转换接口验证聊天消息契约。"""
import json
from pathlib import Path
import unittest

from remote_dispatch import rd_transcripts as transcripts


FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


class TranscriptTests(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / name).read_text(encoding="utf-8").splitlines()

    def test_claude_user_is_readable_as_a_unified_message(self):
        result = transcripts.convert_transcript("claude", self.fixture("claude.jsonl"))
        self.assertTrue(result["supported"])
        self.assertEqual(result["messages"][0], {
            "id": "claude:claude-user:0", "time": "2026-01-01T00:00:00Z",
            "kind": "user", "text": "请检查合成示例。",
        })

    def test_claude_blocks_preserve_all_kinds_and_pair_tools(self):
        result = transcripts.convert_claude(self.fixture("claude.jsonl"))
        self.assertTrue(result["supported"])
        messages = result["messages"]
        self.assertEqual([m["kind"] for m in messages], [
            "user", "thinking", "assistant", "tool_call", "tool_result", "system", "error",
        ])
        self.assertEqual(messages[1]["text"], "先检查示例文件。")
        self.assertTrue(messages[1]["collapsed"])
        self.assertEqual(messages[2]["text"], "我会检查示例。")
        self.assertEqual(messages[3]["input"], {"file_path": "example.txt"})
        self.assertEqual(messages[3]["call_id"], "call-read")
        self.assertEqual(messages[4]["call_id"], "call-read")
        self.assertEqual(messages[4]["tool"], "Read")
        self.assertFalse(messages[4]["is_error"])
        self.assertEqual(messages[4]["text"], "示例内容\n+一行 diff")
        self.assertEqual(messages[5]["text"], "合成上下文提示。")
        self.assertEqual(messages[6]["text"], "合成命令失败。")
        self.assertEqual(len({m["id"] for m in messages}), len(messages))
        self.assertNotIn("synthetic-signature", json.dumps(result))
        self.assertNotIn("SYNTHETIC_IMAGE", json.dumps(result))

    def test_codex_messages_summary_and_both_tool_families_are_readable(self):
        result = transcripts.convert_transcript("codex", self.fixture("codex.jsonl"))
        self.assertTrue(result["supported"])
        messages = result["messages"]
        self.assertEqual([m["kind"] for m in messages], [
            "system", "user", "thinking", "tool_call", "tool_result",
            "tool_call", "tool_result", "assistant",
        ])
        self.assertEqual(messages[0]["text"], "合成系统约束。")
        self.assertEqual(messages[1]["text"], "检查合成样例。")
        self.assertEqual(messages[2]["text"], "先核对样例。")
        self.assertEqual(messages[3]["input"], '{"command":"printf example"}')
        self.assertEqual(messages[4]["text"], "example\n+合成 diff")
        self.assertEqual(messages[5]["input"], "合成补丁")
        self.assertEqual(messages[6]["text"], "合成失败输出\n下一行")
        for call, output, call_id, name in [(3, 4, "call-shell", "shell"),
                                            (5, 6, "call-patch", "apply_patch")]:
            self.assertEqual(messages[call]["call_id"], call_id)
            self.assertEqual(messages[output]["call_id"], call_id)
            self.assertEqual(messages[output]["tool"], name)
        self.assertTrue(messages[6]["is_error"])
        self.assertEqual(messages[7]["text"], "样例检查完成。")
        for hidden in ("PRIVATE_REASONING", "SYNTHETIC_CIPHERTEXT", "不显示的元数据"):
            self.assertNotIn(hidden, json.dumps(result, ensure_ascii=False))
        self.assertEqual(transcripts.convert_codex(self.fixture("codex.jsonl")), result)

    def test_unknown_top_level_records_and_fields_preserve_recognized_messages(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                records = [json.loads(line) for line in self.fixture(harness + ".jsonl")]
                for record in records:
                    record["future_field"] = {"nested": ["合成扩展字段"]}
                lines = [json.dumps(record) for record in records]
                lines.insert(2, json.dumps({"type": "future_record", "text": "隐藏的合成元数据"}))
                result = transcripts.convert_transcript(harness, lines)
                self.assertTrue(result["supported"])
                self.assertEqual([m["kind"] for m in result["messages"]],
                                 ["user", "thinking", "assistant", "tool_call", "tool_result", "system", "error"]
                                 if harness == "claude" else
                                 ["system", "user", "thinking", "tool_call", "tool_result",
                                  "tool_call", "tool_result", "assistant"])
                self.assertNotIn("隐藏的合成元数据", json.dumps(result, ensure_ascii=False))

    def test_claude_lifecycle_system_records_do_not_hide_chat(self):
        lines = self.fixture("claude.jsonl")
        for content in ({}, {"content": None}, {"content": {"future": True}}):
            for subtype in ("stop_hook_summary", "turn_duration"):
                with self.subTest(subtype=subtype, content=content):
                    lifecycle = {"type": "system", "subtype": subtype, **content}
                    result = transcripts.convert_claude(lines[:2] + [json.dumps(lifecycle)] + lines[2:])
                    self.assertTrue(result["supported"])
                    self.assertEqual([m["text"] for m in result["messages"]], [
                        "请检查合成示例。", "先检查示例文件。", "我会检查示例。", "",
                        "示例内容\n+一行 diff", "合成上下文提示。", "合成命令失败。",
                    ])

    def test_codex_new_metadata_and_response_types_preserve_chat(self):
        records = [
            {"type": "inter_agent_communication_metadata", "sender": "synthetic-agent"},
            {"type": "compacted", "replacement_history": [{"text": "不重复压缩历史"}]},
            {"type": "response_item", "payload": {"type": "additional_tools", "tools": []}},
            {"type": "response_item", "timestamp": "example-time", "payload": {
                "type": "agent_message", "content": [
                    {"type": "input_text", "text": "不展示合成跨代理消息"},
                    {"type": "encrypted_content", "encrypted_content": "SYNTHETIC_CIPHERTEXT"},
                ]}},
            {"type": "response_item", "payload": {"type": "future_response", "future": True}},
        ]
        for record in records:
            with self.subTest(record=record):
                lines = self.fixture("codex.jsonl")
                result = transcripts.convert_codex(lines[:3] + [json.dumps(record)] + lines[3:])
                self.assertTrue(result["supported"])
                self.assertEqual([m["text"] for m in result["messages"]], [
                    "合成系统约束。", "检查合成样例。", "先核对样例。", "", "example\n+合成 diff",
                    "", "合成失败输出\n下一行", "样例检查完成。",
                ])

    def test_changed_or_malformed_formats_return_unsupported_without_partial_content(self):
        cases = [("claude", self.fixture("claude-unsupported.jsonl")),
                 ("codex", self.fixture("codex-unsupported.jsonl")),
                 ("unknown", []), ("agy", []), ("kimi", []), ("claude", ["{broken"]),
                 ("codex", ["[]"]), ("claude", ["null"])]
        for harness, filename in [("claude", "claude.jsonl"), ("codex", "codex.jsonl")]:
            original = json.loads(self.fixture(filename)[1])
            for key in (("timestamp", "type", "uuid") if harness == "claude" else ("timestamp", "type", "payload")):
                changed = dict(original)
                del changed[key]
                cases.append((harness, [json.dumps(changed)]))
            cases.extend((harness, [line]) for line in ("{}", '{"type": ""}', '7'))
            if harness == "codex":
                for payload in ({}, {"type": None}, {"type": []}):
                    cases.append((harness, [json.dumps(dict(original, payload=payload))]))
        for harness, lines in cases:
            with self.subTest(harness=harness, lines=lines):
                prefix = self.fixture(harness + ".jsonl") if harness in ("claude", "codex") else []
                self.assertEqual(transcripts.convert_transcript(harness, prefix + lines), {
                    "supported": False, "messages": [], "reason": "unsupported_format", "text": "不支持",
                })

    def test_metadata_and_attachments_do_not_become_chat_messages(self):
        ignored = {
            "claude": ["attachment", "file-history-snapshot", "file-history-delta", "api-request",
                       "api-request-shape", "api-request-blob", "queue-operation", "ai-title",
                       "last-prompt", "mode", "atis-latch", "custom-title", "agent-name",
                       "cost-state", "permission-mode", "continued-in"],
            "codex": ["event_msg", "turn_context", "session_meta", "token_usage_record",
                      "token_count", "world_state"],
        }
        for harness, kinds in ignored.items():
            with self.subTest(harness=harness):
                lines = [json.dumps({"type": kind, "payload": {"text": "隐藏的元数据"}})
                         for kind in kinds]
                self.assertEqual(transcripts.convert_transcript(harness, ["\n", *lines, ""]),
                                 {"supported": True, "messages": []})
                base = self.fixture(harness + ".jsonl")
                self.assertEqual(transcripts.convert_transcript(harness, lines + base),
                                 transcripts.convert_transcript(harness, base))
        records = [
            ("claude", {"type": "user", "uuid": "attachments", "timestamp": "example-time",
                        "message": {"role": "user", "content": [
                            {"type": "text", "text": "保留文字"},
                            {"type": "image", "source": {"data": "SYNTHETIC_IMAGE"}},
                            {"type": "document", "source": {"data": "SYNTHETIC_DOCUMENT"}},
                        ]}}),
            ("codex", {"type": "response_item", "timestamp": "example-time", "payload": {
                "type": "message", "role": "user", "content": [
                    {"type": "input_image", "image_url": "SYNTHETIC_IMAGE"},
                    {"type": "input_text", "text": "保留文字"},
                ]}}),
        ]
        for harness, record in records:
            with self.subTest(harness=harness, attachments=True):
                result = transcripts.convert_transcript(harness, [json.dumps(record)])
                self.assertTrue(result["supported"])
                self.assertEqual([m["text"] for m in result["messages"]], ["保留文字"])

    def test_long_text_and_tool_input_are_bounded_and_marked(self):
        # 阈值是显示契约，以字符计；用中文验证不是按 UTF-8 字节截断。
        limit = transcripts.MAX_CONTENT_CHARS
        for harness in ("claude", "codex"):
            for size in (limit, limit + 1):
                with self.subTest(harness=harness, size=size):
                    text = "字" * size
                    if harness == "claude":
                        record = {"type": "user", "uuid": "long", "timestamp": "example-time",
                                  "message": {"role": "user", "content": text}}
                    else:
                        record = {"type": "response_item", "timestamp": "example-time", "payload": {
                            "type": "function_call_output", "call_id": "long-result", "output": text}}
                    message = transcripts.convert_transcript(harness, [json.dumps(record)])["messages"][0]
                    self.assertEqual(message["text"], "字" * limit)
                    self.assertEqual(message.get("truncated", False), size > limit)
        record = {"type": "assistant", "uuid": "long-tool", "timestamp": "example-time", "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": "long-call", "name": "Write",
                                               "input": {"content": "x" * (limit + 1)}}]}}
        message = transcripts.convert_claude([json.dumps(record)])["messages"][0]
        self.assertTrue(message["truncated"])
        self.assertIsInstance(message["input"], str)
        self.assertEqual(len(message["input"]), limit)
        self.assertEqual(message["call_id"], "long-call")
        record = {"type": "response_item", "timestamp": "example-time", "payload": {
            "type": "custom_tool_call", "call_id": "long-call", "name": "apply_patch", "input": "x" * (limit + 1)}}
        message = transcripts.convert_codex([json.dumps(record)])["messages"][0]
        self.assertTrue(message["truncated"])
        self.assertEqual(message["input"], "x" * limit)

    def test_nested_format_changes_never_escape_or_leak_as_supported_messages(self):
        cases = [(harness, {"type": value})
                 for harness in ("claude", "codex") for value in (123, None, [], {})]
        claude = json.loads(self.fixture("claude.jsonl")[1])
        for content in ([123], [{"type": "thinking", "thinking": []}],
                        [{"type": "text", "text": 7}],
                        [{"type": "tool_result", "tool_use_id": "x", "content": [
                            {"type": "future_text", "text": "不能猜测的内容"}]}],
                        [{"type": "tool_use", "id": [], "name": "Read", "input": {}}],
                        [{"type": "tool_use", "id": "x", "name": 7, "input": {}}]):
            record = dict(claude, message={"role": "assistant", "content": content})
            cases.append(("claude", record))
        cases.append(("claude", dict(claude, message={"role": "future", "content": "不该支持"})))
        cases.append(("claude", dict(claude, uuid=123)))
        codex = json.loads(self.fixture("codex.jsonl")[2])
        for payload in ([], {"type": "message", "role": "future", "content": []},
                        {"type": "message", "role": "user", "content": [{"type": "output_text", "text": "角色不符"}]},
                        {"type": "message", "role": "user", "content": [{"type": "future_text", "text": "不该支持"}]},
                        {"type": "reasoning", "summary": [{"type": "reasoning_text", "text": "不能泄漏"}]},
                        {"type": "function_call_output", "call_id": "x", "output": [{"type": "future_text", "text": "不该支持"}]},
                        {"type": "function_call_output", "call_id": "x", "output": "示例", "is_error": "false"}):
            cases.append(("codex", dict(codex, payload=payload)))
        for harness, record in cases:
            with self.subTest(harness=harness, record=record):
                result = transcripts.convert_transcript(harness, [json.dumps(record)])
                self.assertFalse(result["supported"])
                self.assertEqual(result["messages"], [])

    def test_message_ids_are_unique_and_stable_when_new_lines_arrive(self):
        records = [
            {"type": "response_item", "ordinal": 1, "timestamp": "example-time", "payload": {
                "type": "message", "id": None, "role": "user", "content": [{"type": "input_text", "text": "一"}]}},
            {"type": "response_item", "ordinal": 2, "timestamp": "example-time", "payload": {
                "type": "message", "id": "1", "role": "assistant", "content": [{"type": "output_text", "text": "二"}]}},
        ]
        lines = [json.dumps(record) for record in records]
        first = transcripts.convert_codex(iter(lines[:1]))["messages"]
        both = transcripts.convert_codex(iter(lines))["messages"]
        self.assertEqual(both[:1], first)
        self.assertEqual(len({m["id"] for m in both}), 2)
        for harness in ("claude", "codex"):
            lines = self.fixture(harness + ".jsonl")
            self.assertEqual(transcripts.convert_transcript(harness, iter(lines)),
                             transcripts.convert_transcript(harness, lines))
