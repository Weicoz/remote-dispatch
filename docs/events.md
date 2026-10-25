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
| `receipt.ready` | 首次收取或主动停止改写回执；receipt_id、outcome、checks、执行状态 |
| `receipt.acked` | 签收、代签、停止自动签收；receipt_id、acked_by、received_at、via |
| `review.updated` | 通过、打回、作废；review_state、reviewed_by、reviewed_at |
| `host.changed` | 后台显式观察到目标机变化；host、reachable、负载、资源、full |
| `network.addresses_changed` | serve 启动或本机写接口改变实际监听候选；user_id、candidates（address/port）、spki_sha256；幂等重放和失败不发事件 |
| `project.changed` | 登记、hide、show、rebuild 改变项目；用户、项目 ID、计数、时间、hidden、removed；后台累计通过数变化另带 host、accepted_round_count，按用户＋项目＋目标机去重 |
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
历史段不删除，保证任意旧 seq 可补读；本票没有引入保留期或磁盘自动清理。
追加采用“原活动段完整行 + 新批次”写入私有临时文件、flush + fsync 后原子替换，
因此查询只会读到整个批次。轮转时先把原活动段保存在历史段，再发布新活动段；
发布失败时新事件一条也不可见，原历史仍可补读。
旧日志若有中断的半行，查询忽略半行，下一次追加先修复活动段尾部。
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
