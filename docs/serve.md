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

## 项目、历史分页与 harness 统计（票 22）

`GET /v1/projects` 合并当前用户的持久索引、当前派单与保留的历史记录，不写索引。
返回 `projects`，每项保留索引字段并增加：

| 字段 | 含义 |
| --- | --- |
| `needs_attention_count` / `in_progress_count` | 待处理派单数／starting、running 派单数 |
| `recent_event` | 此项目最近的可见事件；没有时为 null |
| `hosts` | 按目标机名索引，值含 `state/job/updated_at`；无派单的已配置目标机为 idle |
| `week_dispatch_count` | 控制端本地时间本周一零时以来的新派单数，续跑不重复计数 |
| `last_accepted_at` / `last_harness` | 最近通过的时间／最近派单的执行 harness；未知为 null |
| `session_only` / `sessions` | 本票为 false／空数组，供票 25 的仅会话项目接入 |

目标机小格的 `state` 按优先级取一条：`needs_reply`（未签收的 blocked，需回话）、
`interrupted`（异常中断／失联／丢失）、`failed_pending`（失败待签收）、
`pending_receipt`（待签收，含 partial）、`pending_review`（待验收，含主动停止）、
`running`、`idle`。同级按更新时间倒序，包含进度／报告、签收、验收时间；
并纳入最近一次状态变更事件的时间；时间相同时按 job ID 升序。已通过／已作废为 idle。会话状态由票 25 补充。
项目按名称和 ID 稳定排序，“未归属”在最后；origin URL 的 userinfo 不对外返回。
缺少元数据的历史只返回已知内容，不推测 harness、时间或原用户。

`GET /v1/jobs?project=<项目ID>` 过滤当前登记派单；不传 project 保持原完整列表。
`state=active` 排除已通过／作废，`state=history` 包含已通过／作废及已清理的保留历史。
历史按派单创建时间、job ID 倒序，支持 `limit=1..200`（默认 50）和不透明 `cursor`，
返回 `jobs/next_cursor`。首次读取固定本次分页的卡片快照，新派单、补录和状态变化不改变
后续页；原游标可重读。分页快照只在内存中保存，服务重启后旧游标返回 400，须从首页重读。
后续请求须保持原 project、state 和 limit。查询不落盘、不探测或收取回执。
历史元数据优先取现存 job，其次精简登记和按轮次保留的回执；clean 后不依赖目标机文件。
旧登记缺字段时为 null，已被全部删除的报告／登记无法恢复。

`POST /v1/projects/{id}/hide|show` 接受空 JSON 对象，必须有 `Idempotency-Key`（1–255 字符）。
成功返回 `{"project": <索引项目>}`；未知／其他用户项目为 404。
复用 CLI 的项目变更实现及同一把项目锁，只在内容变化时产生 `project.changed`；
新派单仍经 CLI 登记自动恢复隐藏项目。
同一用户同键返回首次已完成结果（包括 404），跨 hide/show 路径亦如此，服务重启后仍有效。
结果持久化在 `RD_STATE/serve-project-requests/`；成功的索引、结果与事件批次在普通 I/O
异常时一起回滚。锁冲突／写失败尚未完成请求，可用原键重试。
不提供跨进程崩溃／断电时的多文件原子提交保证。

| HTTP | `error.reason` | 含义 |
| --- | --- | --- |
| 400 | `idempotency_key_required` | 缺少、空白或过长的幂等键 |
| 400 | `invalid_project_request` | 项目请求体／请求格式无效 |
| 409 | `project_busy` | 项目索引正被占用，可稍后重试 |
| 503 | `project_write_failed` | 未能保存变更，检查存储后重试 |

`GET /v1/harness-stats?recent=N` 返回 `harness_stats`，按 harness 名称排序，
N 为 1–200，默认 20。每项含：

- `today_round_count`：控制端本地今日结束并保留回执的轮次数，不按 job 去重。
- `recent_round_limit/duration_sample_count/average_duration_seconds`：最近 N 个已结束轮次中，
  有有效起止时间的样本数和平均秒数；无样本为 null。
- `current_rounds`：仍在执行的各轮 `job/round/elapsed_seconds`，支持同一 harness 并行多单。
  已用时间随查询更新；未知开始时间为 null。
- `consecutive_accepted/consecutive_rejected`：按轮次结束时间排序的末尾连续通过／打回次数；
  pending、voided 或另一种结果会中断连续计数。
- `last_result/last_checks/last_job/last_round/last_finished_at`：最后一个已结束轮次的原始结果。

启动时间来自目标机现有 `started.rN` 标记；finish 将其保存在报告，结束时间用
`finished_at`，旧报告依次回退到 `updated_at`、回执 `captured_at`。
缺少开始时间不以派单时间代替，避免把准备和前轮执行计入本轮。
后台收取回执后计算默认 N 的统计，只在持久统计值变化时发 `harness.stats_changed`，
含用户、harness 及上述标量统计字段；不把持续变化的 `current_rounds` 放入事件。
按用户＋harness 持久去重，重启及事件轮转不重复发布；HTTP 查询本身不发布事件。

新增契约样例是 `projects-summary/projects-hide/projects-show/projects-key-required`、
`jobs-project/jobs-history`、`harness-stats`，位于 `contracts/v1/`，由
`tests.test_rd_serve_projects_stats` 通过真实 Unix HTTP/SSE 校验结构及关键行为。

编排方合入后按现有方式更新包并重启所管理的 serve（实现会话不部署／重启）：
用临时 RD_STATE 复测 projects → hide → show → 新派单自动恢复、history 连续分页，
运行并结束两轮后读取 harness-stats、断线重连 SSE 核对统计事件。
真实 SSH／Windows 目标机的启动时间采集、App 读取新增字段由编排方端到端执行。
本票测试仅用短路径 Unix socket，不监听 TCP；结束后关闭服务并删除 socket。
