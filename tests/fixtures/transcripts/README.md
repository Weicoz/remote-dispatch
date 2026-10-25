# 会话记录转换器夹具

本目录 JSONL 全部为手工合成，消息正文、时间、ID、文件名和工具参数均为示例；
没有读取目标机真实会话、私人正文或凭据。

结构依据派单随附的 `claude-shapes.jsonl`、`codex-shapes.jsonl`，不依赖这些只读材料运行测试。
`claude.jsonl` 覆盖 user、assistant、thinking、tool_call、tool_result、system、error；
`codex.jsonl` 覆盖 message、summary、function/custom 工具调用与输出，以及空 summary。
`*-unsupported.jsonl` 在有效消息之后加入未知格式，验证不返回部分结果。
其他元数据、附件、错误字段类型和超长内容在测试中合成，避免大文件夹具。

公开接口在 `remote_dispatch.rd_transcripts`：

```python
convert_transcript("claude", jsonl_lines)
convert_transcript("codex", jsonl_lines)
convert_claude(jsonl_lines)
convert_codex(jsonl_lines)
```

成功为 `{"supported": True, "messages": [...]}`；未知 harness、未知内容类型、
损坏 JSON 或必需字段变化均返回：

```json
{"supported": false, "messages": [], "reason": "unsupported_format", "text": "不支持"}
```

消息含 `id/time/kind/text`。工具消息有 `tool/call_id/is_error`，调用另有 `input`。
结果所在输入片段没有对应调用时，`tool` 为 `None`，仍保留 `call_id`；不会跨调用缓存状态。
ID 是不透明值，在同一记录序列末尾追加行时稳定；Codex 无原生 ID/ordinal 时使用行号。

正文和工具输入分别最多保留 `MAX_CONTENT_CHARS = 16_384` 个 Unicode 字符。
超长时添加 `truncated=True`，不加省略号；较短内容不添加该字段。
工具输入通常保留源类型（Claude 对象，Codex 字符串）；超长 Claude 对象先序列化为 JSON，
再返回前缀字符串，因此 `truncated=True` 时不能把该字符串当完整 JSON 执行或解析。
后续全文接口应从原记录重新读取，本模块不保存全文、不做文件定位。

Claude thinking 添加 `collapsed=True`，不暴露签名；Codex reasoning 只用 `summary_text`，
不读取或展示 reasoning 正文及密文。Codex developer/system 的 input_text 归 system，
assistant 的 output_text 归 assistant，其余角色或类型不猜测。Claude system 的
`level=error` 归 error；工具失败保持 tool_result，并使用源记录的 `is_error`，不解析命令输出猜测成败。
Codex 结构样本没有独立 error 记录，未人为发明这种输入格式。
