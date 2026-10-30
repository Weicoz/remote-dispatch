# 用量样本

按各 harness 公开文档的事件格式构造，不是真实录制：
codex（`--json` 的 turn.completed）、claude（stream-json 的 system/init 与 result）、
kimi、agy、dsh（公开文档未给出用量事件格式，按通用 usage 对象构造）。
有真实录制样本后直接替换同名文件。

`agy-recorded.jsonl` 取自 2026-10-30 本机目标一次真实 AGY 派单的输出，删去回复正文、换掉会话 ID：用量在 `result` 事件的 `result.usage`，`input_tokens` 按含缓存命中处理，`init` 不带模型。
