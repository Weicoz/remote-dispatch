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
回执带 `regenerable_sizes`（可再生目录大小，票 04 记录）时，行内追加该字段；没有数据不显示。
老记录缺字段时，harness、模型、派出时刻、时长显示「未记录」，不迁移数据。

## JSON 与 serve 接口

`board --json` 与 serve `GET /v1/board` 由同一函数 `rd_board.build()` 生成，结构相同：
`{scope, initiator_session, summary:{dispatched, running, pending, blocked}, rows:[...]}`。
接口参数：`?all=1` 为全部派单；`?initiator_session=会话` 为单个会话；二者都缺返回 400。
按 ADR 0004，serve 返回该用户的派单，不要求原发起会话。serve 读本地快照，CLI 读目标机现状，
两者在状态刷新之间可能有短暂差异。示例见 `contracts/v1/board.json`。
