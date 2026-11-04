# 事件日志与断线补读

控制端写操作在 `RD_STATE/events.jsonl` 追加 UTF-8 JSON 行：

```json
{"seq":1,"time":"2026-10-25T00:00:00+00:00","type":"job.created","data":{"job":"example","user_id":"user","host":"target","round":1}}
```

`seq` 从 1 开始，在整个 RD_STATE 内递增；跨进程、重启和轮转不重置。
`time` 是控制端写入时间，`data.round` 是事件所属轮次，不借用续跑后的轮次。
事件按字段白名单投影，只包含显示字段；不复制配置、profile、SSH 参数、
`env_file`、`agent_auth_file`、提示词、验收附言、完整回执或原始日志。
进度的 `phase` / `summary` 沿用 reporter 的脱敏正文约定。

| 类型 | 写入点与主要字段 |
| --- | --- |
| `job.created` | 新派单登记；job、用户、项目、目标机、round、harness、model、dispatch_state |
| `job.state_changed` | 派单准备／启动、续跑轮次、显式采集执行快照、主动停止；execution_state、executor_exit、停止者 |
| `progress.updated` | 后台显式观察到进度变化；phase、summary、updated_at |
| `report.created` | 控制端生成合并汇报；user_id、report_id、initiator_session、job_count；正文经 `/v1/reports` 或 `inbox` 读取，见 [汇报说明](reports.md) |
| `receipt.ready` | 首次收取或主动停止改写回执；receipt_id、outcome、checks、执行状态 |
| `receipt.acked` | 签收、代签、停止自动签收；receipt_id、acked_by、received_at、via |
| `review.updated` | 通过、打回、作废；review_state、reviewed_by、reviewed_at |
| `host.changed` | 后台显式观察到目标机变化；host、reachable、负载、资源、full |
| `network.addresses_changed` | serve 启动或本机写接口改变实际监听候选；user_id、candidates（address/port）、spki_sha256；幂等重放和失败不发事件 |
| `project.changed` | 登记、hide、show、rebuild 改变项目；用户、项目 ID、计数、时间、hidden、removed；后台累计通过数变化另带 host、accepted_round_count，按用户＋项目＋目标机去重 |
| `reclaim.planned` / `reclaim.result` | serve 定时回收；目标机、mode（plan/execute）、上限与用量（GB）、阻塞数、`actions`（job、action=slim/delete、kb；result 另有 ok、reason）；见 [回收](cache-retention.md) |
| `harness.stats_changed` | 后台保留回执统计变化；用户、harness、total_round_count、今日轮次、时长样本、连续验收与最近结果；date_basis=controller_local、host_stats 含近 14 个本地日的 date/round_count；无具名 harness 的机器统计事件 harness=null |

停止产生的状态、回执与签收事件均带 `suppress_push=true`，推送渠道必须遵守。
状态文件写入与事件批次使用同一操作的回滚边界，日志 I/O 失败时撤销本地文件。
验收与续跑事件先暂存，成功后才追加；回滚不会留下已撤销的签收、验收或新轮次事件。
打回续跑的目标机 checkpoint 与迁移回滚保护持续到事件批次发布结束。
事件锁只保护日志 I/O，SSH、探测和启动期间不持有事件锁。

大小阈值由环境变量 `RD_EVENT_LOG_MAX_BYTES` 配置，默认 **10485760 字节（10 MiB）**，
必须为正整数。追加会超过阈值时，先把非空活动段重命名为
`events.<末尾 seq 的 20 位十进制数>.jsonl`，再写新的活动段；单条事件大于阈值时独占一段。
同一次操作的事件整批发布；单批超过阈值时独占一段。
追加采用单次 `O_APPEND` 写入（一个批次拼成一块）加 fsync，不再重写整个活动段；
查询只读取以换行结束的完整行，崩溃留下的半行被忽略，下一次追加先在事件锁内把尾部修复到最后一条完整行。
写入失败时尽力截回写前长度，不留半个批次。断电时一个多行批次可能只保留前缀（单行始终完整或缺失），
这是追加写相对原子替换的唯一弱化；批次内每行都是独立有效的事件。
轮转时先把非空活动段重命名为历史段，再向新活动段追加；两步之间崩溃时，下一次写入从最新历史段的末尾 seq 续号。

### 历史保留与旧游标

历史段总量有上限：环境变量 `RD_EVENT_LOG_MAX_ARCHIVES`（正整数，默认 **5**）限定保留的历史段个数，
合计约 (5 + 1) × 10 MiB。每次轮转后删除最旧的超限历史段，磁盘占用有界。
旧游标恢复规则：

- 游标 ≥ 仍保留的最早 seq − 1：补读完整，与以前一致。
- 游标更早（对应事件已被清理）：`read_after` 与 SSE 从仍保留的最早事件开始返回，中间的事件不可恢复；
  客户端比较返回的首个 `seq` 与 `游标 + 1` 即可发现缺口。SSE 在连接时另发一行注释
  `: cursor_expired oldest_seq=<N>`（注释不是事件，旧客户端忽略），客户端应据此重新拉取 `/v1/jobs`、`/v1/hosts` 等快照，
  再从 `oldest_seq` 继续订阅。游标为 0（全新客户端）不触发该提示。
- `seq` 不回退、不复用；清理历史段不影响续号（续号取自活动段，活动段为空时取最新历史段）。

### 内存索引

写入端和 serve 用进程内索引避免扫描历史：每个对象（派单、目标机、项目、harness）最近一次事件用于 `only_if_changed` 去重，
每个派单的 `progress.updated` 位置用于 `GET /v1/jobs/<job>` 的 `progress_events`（`EventLog.read_job(job)`，只按位置读取该派单的行，上限最近 2000 条）。
索引只在内存里，随追加增量更新；活动段被外部替换或缩短时整体重建；serve 启动时预热，一次性 CLI 仅在需要去重时才构建。
清理出保留范围的历史段，其进度行不再出现在派单详情里。

这些保护覆盖普通 I/O 异常；不提供跨目标机、状态文件与事件文件的断电原子事务。

## 供 serve 复用

```python
from remote_dispatch.rd_events import EventLog, read_after

# 纯查询，不创建目录、不加写锁、不写文件。
rows = read_after(state, seq)  # list[dict]，seq 严格大于游标，按 seq 升序

# 只在后台写任务调用；不可放入 Query、status、result、hosts 的读取适配器。
log = EventLog(state)  # 可显式传 max_bytes= 正整数
log.observe_job(rec, snapshot, progress)  # progress 可为 None
log.observe_host(host, probe_info, full=is_full)
```

`observe_job` 接受现有执行快照字段 `state / round / exit` 和 reporter 进度。
`observe_host` 接受现有 host 配置与 probe 字典，仅输出白名单字段。
观察入口依据持久日志去重，忽略快照的采样时间及原始输出；重启和轮转后仍只在显示字段变化时追加。
首次观察发布当前状态。回执收取复用 `cli.collect_result(cfg, rec, snapshot)`；
其他显式写路径可使用 `emit(state, type, data)`，同样经过字段白名单和独立事件锁。
serve 应由后台任务取得执行快照与 progress 后调用这些写入口，SSE 仅调用 `read_after`。
轮询调度、SSE、Last-Event-ID 的 HTTP 接线由票 19 完成；本票不新增服务或轮询进程。

`read_after` 合并活动段和全部游标之后的历史段。先读取活动段、再枚举不可变历史段，
按 seq 去重，避免并发轮转的重命名造成重复或漏掉已捕获的事件。
调用方负责用户过滤：job／project 事件带 `user_id`，目标机事件为控制端级别。
新增日志不会反向补写已有派单的历史事件；第一次后台观察可发布当前状态。

编排方合入后按现有方式更新 Python 包，并在隔离 `RD_STATE` 下复测
`tests.test_rd_events`、签收／验收／派单／项目相关模块即可。无需构建、部署测试栈、
监听端口或重启服务。真实 serve/SSE 重连补读由票 19 接入后端到端验证。
