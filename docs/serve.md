# 本机 Unix socket 服务

```sh
rdispatch serve
rdispatch serve --socket /private/tmp/rdispatch.sock --poll-interval 15
# 使用自定义路径时，CLI 与 App 连接同一个路径：
RD_SERVE_SOCKET=/private/tmp/rdispatch.sock rdispatch ack JOB --round 1 --receipt-id ID
curl --unix-socket /private/tmp/rdispatch.sock http://localhost/v1/jobs
curl -N --unix-socket /private/tmp/rdispatch.sock -H 'Last-Event-ID: 12' http://localhost/v1/events
```

默认路径是 `$RD_SERVE_SOCKET`，否则 `$RD_STATE/serve.sock`。
服务只绑定 AF_UNIX，不监听 TCP。socket 权限 0600，新建目录权限 0700。
每个 RD_STATE 仅一个服务，独占 `locks/serve/service.lock`，不与名为 serve 的 job 锁混用。
拒绝覆盖普通文件、符号链接或仍在监听的 socket；取得服务锁后可回收拒绝连接的旧 socket。
SIGINT/SIGTERM 会停止采集、等待已接受的写命令和采集结束，再释放服务锁并删除本次 socket。
服务读取启动时的配置和用户；修改配置后由编排方重启服务。

只读接口、字段和 SSE 帧以 [contracts/v1](../contracts/v1/README.md) 为准。
serve 可查看当前用户所有发起会话的详情、日志、回执；其他用户的 job 返回 404。
纯查询使用 `Query`、已有项目推断、签收和验收叠加，不写状态、不探测目标机、不收取回执。
日志端点按请求只读目标机日志。后台启动即采集，之后默认每 15 秒采集，可用
`--poll-interval` 设置正的有限秒数；周期是每轮采集完成后的等待时间。
采集先在共享 job 锁内重读记录、查询状态和进度并收取回执；竞争时跳过本轮。
目标机使用现有探针和资源阈值。采集变化通过 `EventLog.observe_job/observe_host`
与 `collect_result` 发布；显示字段不变不追加事件。单个目标机失败不会中断其他采集。
查询在首次采集前可能返回启动中／无回执，之后读取缓存观察和持久回执。
SSE 仅读取事件日志，补读包括轮转段；15 秒发一次注释心跳，断线后用最后收到的 ID 重连。

## CLI 转交协议

本地 CLI 的 `run/resume/ack/review/stop/project`，以及同样会写 RD_STATE 的
`inbox/keep/clean`，先尝试连接同一 socket。`project rebuild` 也走转交。
没有 socket 或连接被拒绝时才直接执行现有路径。
连接成功后发生请求、读取或解析错误，CLI 返回 4 和 `cli_forward_failed`，
**不自动重试，也不改走直接写入**；应先核对状态，避免重复派单或续跑。
这条私有通道没有跨进程崩溃幂等保证；App 的公开写接口与幂等键留给票 20。

请求是 `POST /_cli/v1`，Content-Type 为 application/json，必须有 Content-Length，
最多 8 MiB，不支持分块请求。字段：

| 字段 | 含义 |
| --- | --- |
| `argv` | 原始 CLI 参数数组，不经 shell 解释 |
| `cwd` | 调用方的绝对工作目录，保留 `-C`、相对提示词与 include 路径语义 |
| `config` / `user_id` | 必须匹配服务的配置路径和用户，否则拒绝，不转为直接写入 |
| `initiator_session` | 调用方已按原有优先级解析的会话 |
| `origin` | 调用方识别的 harness、控制端机器、Herdr 来源；派单时间由实际写入时补齐 |
| `stdin` | `--prompt-file -` 的正文；其他命令为空字符串 |

serve 串行执行接受的写命令，使用自己的子进程隔离各命令的 cwd、stdin、stdout、stderr
和来源上下文；不修改服务进程的全局环境或输出流。子进程仍调用原 CLI 命令实现，
签收／验收／停止渠道强制为 `serve`，签收方等显式参数仍保留。
子进程不再转交。响应 HTTP 200，JSON 为 `exit_code/stdout/stderr`，CLI 复现输出与退出码；
HTTP 400 表示请求无效或身份／配置不匹配，503 表示执行通道不可用。
所有 job 写操作和后台采集仍使用原来的 `JobLock`；项目写使用原索引锁，事件锁始终最内层。

## 检查与编排方复测

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_serve -v
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

测试使用临时 RD_STATE、Unix socket、真实隔离本机 shell／Git 夹具和资源探针替身，
覆盖 HTTP、用户隔离、纯查询、SSE 新事件与轮转补读、CLI 转交／直接写入、共享锁、
连接中断不重试、进程退出清理、敏感字段与样例结构。无构建步骤。
编排方按现有流程更新 Python 包后，在临时 RD_STATE 中启动服务，用上述 curl 读取，
运行签收／停止／验收并断开重连 SSE，再终止服务核对 socket 删除。
真实 SSH 目标机的探测、报告收取与日志读取，以及 App 消费样例的集成由编排方端到端复测；
实现会话不部署测试栈或重启现有服务。

## 公开写接口（票 20）

五个接口都接受 `Content-Type: application/json` 和非空的 `Idempotency-Key`。
请求体必须是 JSON 对象，大小限制沿用 8 MiB；不接受分块请求。
成功响应只含显示字段，不返回 CLI 原始输出。签收、验收、停止始终记为 `via=serve`；
本阶段 `acked_by`、`reviewed_by`、`stopped_by` 从请求体必填，设备身份由后续配对接口接入。

| POST 路径 | 请求体 | 成功响应 |
| --- | --- | --- |
| `/v1/jobs/{job}/rounds/{n}/ack` | `receipt_id, acked_by` | 200：`job, round, acknowledgement` |
| `/v1/jobs/{job}/rounds/{n}/review` | `review_state, reviewed_by`；打回另传 `review_note`；可选 `host, model, harness` | 200：`job, round, review`（验收原轮次） |
| `/v1/jobs/{job}/stop` | `stopped_by` | 200：`job, round` |
| `/v1/jobs/{job}/resume` | `prompt`；可选 `host, model, harness` | 200：`job, round`（新轮次） |
| `/v1/runs` | `project, prompt`；可选 `harness, host, initiator_session, model` | 201：`job, round` |

`review_state` 接受 `accepted / rejected / voided`，空白打回附言返回 409。
所有规则、整轮锁、打回失败回滚、自动代签和停止后的自动签收沿用原 CLI。
续跑在当前用户的原派单发起会话下执行；请求体不能借此改变派单所有者。
`runs.project` 接受既有派单的项目 ID，或没有同名歧义的项目名；
仓库路径取该项目最近派单保存的本地 `repo`，同时读取 job 与清理后保留的派单清单；
旧记录按查询层相同的配置／索引名称推断项目。不接受任意仓库路径、命令或 shell 参数。
找不到记录／仓库路径时返回 422 `project_unavailable`，同名不同 origin 时返回
422 `project_ambiguous`，应改用项目 ID。派单清单保留仓库路径及 harness，新派单清理后仍可再次使用该项目；
旧版已清理且清单缺少路径的历史项目，须先经 CLI 派一次补齐来源。
执行前核对仓库当前 origin，身份改变返回 `project_unavailable`，避免意外新建项目。默认 harness 沿用最近派单，旧命令派单缺 harness 时为 codex；
默认目标机为 auto，资源规则与 CLI 相同（auto 排除控制端本机），不绕过准入。
发起会话未指定时暂记为 `app`；客户端可显式传 `app:<device_id>` 或所选会话 ID。

错误结构为 `{"error":{"reason":"标识",...}}`：

- 400：`idempotency_key_required`（缺少／空白幂等键）、`invalid_request`（格式或字段无效）。
- 404：`not_found`，包括其他用户的派单。
- 409：`already_reviewed / round_in_progress / review_not_allowed`。
  `already_reviewed` 另含 `reviewed_by, reviewed_at, review_state`。
- 422：`resume_*`、`harness_change_not_allowed`、资源准入等命令失败；
  保留 CLI 稳定原因，未带稳定标识的错误为 `command_failed`。
  命令错误含脱敏 `message` 和原 `exit_code`；显示正文沿用摘要的 600 字符上限，
  超长时另含 `message_truncated=true`，稳定原因和结构化附加字段保留。
- 503：`write_unavailable`（写通道或存储不可用）、`write_outcome_unknown`（执行结果待核对）。

幂等结果按 RD_STATE 和用户保存到 `serve-writes/`，键经哈希，不保存请求正文。
同键无论请求体或上述路径是否变化，都返回首次 HTTP 状态码与 JSON；失败结果也缓存。
并发重复请求等待首次执行，重启后仍可复用；修正输入或准入条件后需使用新键。
在调用 CLI 前先持久化 `write_outcome_unknown`，随后原子替换为最终结果。
崩溃发生在远端副作用与结果保存之间时，同键只返回 503，先查询核对，不自动再执行。
这不能提供跨控制端／目标机断电原子事务；记录目前不自动过期，不能随意删除后重试。

事件沿用 `docs/events.md`：ack 产生 `receipt.acked`；review 产生 `review.updated`
及必要的代签事件，打回成功另产生新轮次状态；resume 产生 `job.state_changed`；
新派单产生 `job.created / project.changed / job.state_changed`；主动停止产生带
`suppress_push=true` 的状态、回执与签收事件。原操作无状态变化时不补造事件，
幂等重放不再产生事件。契约见新增 `contracts/v1/write-*.json`，相关检查：

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_serve_writes -v
```

测试以短路径 Unix socket 发 HTTP／读 SSE，结束关闭服务并删除 socket，完全不监听 TCP。
真实 SSH、资源准入、harness 启动和 App 使用契约由编排方端到端复测；编排方更新包后，
在隔离 RD_STATE 中启动 serve，依次派单、签收、验收、续跑、停止，并用同键重放及重启后重放，
最后终止服务核对 socket 删除。实现会话不部署或重启测试栈。
