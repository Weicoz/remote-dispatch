# 验收与续跑

```sh
rdispatch review JOB --round 1 --accept
rdispatch review JOB --round 1 --void
rdispatch review JOB --round 1 --reject '请完成剩余验收项'
rdispatch review JOB --round 1 --reject '请继续' --host OTHER --model MODEL
rdispatch resume JOB --prompt-file prompt.md --host OTHER --model MODEL
```

`--accept`、`--reject 附言`、`--void` 必须且只能选一项。每轮只有一个
`pending / accepted / rejected / voided` 验收状态，落盘后不可改。
completed、partial 可通过、打回或作废；blocked、failed、dead、missing
只能打回或作废。非零执行退出码也不能通过。

`--reviewed-by` 指定验收方会话 ID 或设备名，默认当前会话，无会话时为
`cli`。`--via cli|serve` 默认 cli。同一用户可跨会话验收；未签收回执会
复用签收实现，签收方取验收方。已有签收方和签收时间保留。

验收、代签和打回续跑在同一把最外层 job 锁内完成。打回先检查目标机和
恢复条件，再写签收与验收记录并启动。资源不足、失联、迁移或启动失败时，
撤销本次本地记录和轮次变更，保留原回执及已有签收，不排队。
本地写入失败也撤销自动代签。通过、作废后仍可正常 resume，新轮次为 pending。

验收记录位于 `results/JOB/rN.review.json`，字段为 `review_state`、
`reviewed_by`、`reviewed_at`、`review_note`、`checks_at_review`（验收时执行方报告的 checks 状态，未报告记 `not_reported`；仅留痕，不影响能否通过；旧记录没有该字段）。`result`、`result --summary`、
`status --json`、`status --details` 只读叠加该记录，未验收默认 pending；
回执 ID 不变。inbox 和清理扫描不会把验收文件误当成回执。

续跑可改 model、目标机，`--agent`（别名 `--harness`）若提供必须与原派单
一致；换 harness 应另派一单。切换目标机时先检查资源、harness 与原用户配置，
经控制端传输当前提交、Git 索引、工作树和 setup 脚本，以 direct 模式续跑。
目标机不得已有同名派单或同名分支。原目标机副本保留，位置记入
`worker_history`，供核对与恢复；本票未增加历史目标机副本的自动清理。
新目标机使用同一个 harness 的新上下文，并在 `context_history` 记下原会话，
附言提示沿用迁移工作树和原授权。不会把原目标机的凭据或 harness 运行目录迁走。
工作树归档递归排除 `.env*`、`*.pem`、`*.key`、`id_*`（大小写不敏感）以及
`.ssh`、`.aws`、`.gnupg` 目录；普通源码、未跟踪业务文件和 setup 脚本仍迁移。
此排除仅针对工作树文件；Git bundle 仍包含当前提交可达历史和索引快照，索引仍原样迁移，
已经提交或暂存的凭据不会由文件名过滤从 Git 对象中清除。
迁移沿用现有安全解包规则，仅支持普通文件和目录，遇软链接或设备文件会失败并保留原轮次。

错误写入 stderr，退出码非零；锁与验收冲突退出码 4，资源准入失败退出码 3。
可供后续 serve 映射的稳定标识：

| 标识 | 含义 |
| --- | --- |
| `review_not_allowed` | 不允许通过，或打回附言为空 |
| `already_reviewed` | 已验收，附原执行者、时间、结果 |
| `round_in_progress` | job 锁被占用、轮次尚未结束或打回历史轮次。CLI 直接调用时先有界等待锁（默认 60 秒，`RD_LOCK_WAIT_SECONDS` 覆盖，0 表示不等），超时才报此错；serve 代执行的写接口不等待，立即返回 409 |
| `receipt_host_unavailable` | 当前轮回执未落盘且目标机暂时连不上，可重试（退出码 4，serve 503） |
| `review_write_failed` | 无法保存签收或验收，本次操作已撤销 |
| `harness_change_not_allowed` | 续跑试图更换 harness |
| `resume_not_allowed` | 非代理派单或缺原隔离配置 |
| `resume_host_unavailable` | 目标机失联或无法探测 |
| `resume_resource_check_failed` | 目标机满载、网络检查或其他资源准入失败 |
| `resume_session_not_found` / `resume_session_mismatch` | 接收会话缺失或不匹配 |
| `resume_recovery_not_allowed` | 现有同类故障恢复预算或上下文策略拒绝续跑 |
| `resume_profile_mismatch` | 新目标机用户配置与原派单不一致 |
| `resume_transfer_failed` | 工作树迁移失败，原目标机与轮次保留 |
| `resume_launch_failed` | 启动失败；重试前需核对目标机执行状态 |

目标机收到启动请求但连接在确认前中断时，仍需用 status 核对是否已经启动；
本票没有新增跨目标机的分布式提交或远端幂等启动协议。

编排方合入后按现有流程更新 Python 包即可，无构建、端口或服务重启。
复测命令见 `tests/test_rd_review.py`、`tests/test_rd_locking.py` 和
`tests/test_rd_dispatch.py`。真实 SSH、不同操作系统间的迁移、各 harness
在新目标机的新上下文，以及启动确认中断后的核对由编排方端到端执行。
