# 看板

`rdispatch board [--all] [--json] [--initiator-session 会话]` 一屏回答：派了多少、在哪台目标机、
用什么模型、跑了多久、进展到哪、还有多少没派。只读：不收取回执、不写状态、不创建目录。

- 默认范围是当前发起会话（`--initiator-session` 或 `RD_INITIATOR_SESSION`，缺失时退出码 4）。
- `--all` 显示当前用户 ID 下全部派单，包括没有发起会话的派单。

头部四个数字：已派 = 范围内派单数；运行中 = 状态为 `running` / `starting`；
待派、阻塞 = 该用户全部项目的 `plan` 登记中 `pending` / `blocked` 的工作项数（登记不分发起会话）。

每行：工作单元（已关联 `plan` 工作项取工作项名，否则为派单 ID）、目标机、harness／模型、派出时刻、
已运行时长（运行中为派出至今，已结束为回执记录的 setup + 主体耗时）、轮次、状态、最新进度。
`reclaim_block` 是回收阻塞原因（`code` 同 `clean --dry-run --json`，可回收为 `null`）。
看板不连目标机，只判断登记引用、运行中、准备中、不可达、未结束、回执待收取；合入、未提交等检查仍由 `clean` 给出。
回执带 `report.regenerable_kb`（可再生目录大小，单位 KB，票 04 记录）；行内字段名为 `regenerable_kb`时，行内追加该字段；没有数据不显示。
本轮回执标记 `credential_suspect` 时，行内追加 `credential_suspect: true` 与 `credential_hint`（重新复制凭据的 `resume --refresh-auth` 命令提示），文本看板另起一行显示；不自动重复制或续跑。
工作线字段：`mode`（`single` 单项／`line` 线）、`line`（线名，单项为 `null`）、`branch`、`line_items`（线上工作项按轮次顺序）、
`pending_items`（当前工作项之后仍待派的数量）、`shared_branch`（线上多个工作项共用一个任务分支）、`line_hint`（见下）。
顶层 `chains` 统计每条链被建议、被接受、被拒绝的次数（`suggested`、`accepted`、`rejected`）。

`run` 没带 `--line`／`--no-line` 且可以接线时：stdin 和 stdout 都是终端则询问 y／n（y 等同 `--accept`，n 等同 `--no-line`）；
非终端不阻塞，按默认方式派出，打印「本可接入工作线」提示，并把提示记入派单的 `line_hint`（看板显示）。

老记录缺字段时，harness、模型、派出时刻、时长显示「未记录」，不迁移数据。

## JSON 与 serve 接口

`board --json` 与 serve `GET /v1/board` 由同一函数 `rd_board.build()` 生成，结构相同：
`{scope, initiator_session, summary:{dispatched, running, pending, blocked}, chains:[...], rows:[...]}`。
接口参数：`?all=1` 为全部派单；`?initiator_session=会话` 为单个会话；二者都缺返回 400。
按 ADR 0004，serve 返回该用户的派单，不要求原发起会话。serve 读本地快照，CLI 读目标机现状，
两者在状态刷新之间可能有短暂差异。示例见 `contracts/v1/board.json`。
