# 术语表

统一用词。每条给出定义和 _Avoid_（不该用的近义说法）。

**工作项**：
编排方在待派登记（`rdispatch plan`）里登记的一项工作，可以依赖其他工作项。默认方式下一个工作项对应一个派单；工作线上一个工作项对应同一派单的一个或多个轮次。
_Avoid_：票、任务

**工作线**：
一串前后依赖的工作项共用的一个派单、worktree 和任务分支。关闭后所有派单回到默认方式。
_Avoid_：流水线、长会话

**派单模式**（`dispatch_mode`）：
取值 `suggest`（默认）、`line`、`ticket`，优先级为命令行 `--dispatch-mode` > 会话默认值 > `[defaults] dispatch_mode`。见 [会话派单默认值](session-dispatch-defaults.md)。
_Avoid_：权限模式（`permission_mode` 是另一个字段）、工作模式

**上下文包**：
由 rdispatch 从结构化数据（回执摘要、提交、改动文件、决策、未完成项）拼装的前序工作摘要，约 4k token 上限。派单时用 `--context-from <派单>` 带上，见 [交接协议](handoff.md)。
_Avoid_：背景摘要、模型转述

**交接包**：
内容与上下文包相同，特指工作线内换新会话时附带的那份。
_Avoid_：交接文档、会话摘要

**瘦身**：
只删除项目声明的可再生子路径（`regenerable_paths`）和 setup 完成标记，保留 worktree 与代码，派单仍可续跑。命令为 `rdispatch clean --slim`，见 [目标机准备](worker-setup.md)。
_Avoid_：清理（清理指 `clean` 删除 worktree）、压缩

**门禁**：
合入前对指定提交跑一次全量测试。命令为 `rdispatch gate <ref>`，按历史耗时选目标机。
_Avoid_：卡点、预检（预检指派单前的代理检查）

**建议令牌**：
`rdispatch suggest` 返回的一次性凭据，绑定建议内容指纹、用户 ID 和发起会话，30 分钟内单次有效。用 `run --line <线名> --accept <令牌>` 接受。
_Avoid_：确认码、授权码
