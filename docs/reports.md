# 状态变化汇报

`rdispatch serve` 在控制端消费 `job.state_changed`、`receipt.ready` 和
`progress.updated` 事件，在派单变为已完成、卡住、离线、失败时生成汇报。
汇报带派单标题（旧记录使用派单 ID）、目标机、轮次、新状态与摘要。
卡住使用回执中等回话的摘要，失败使用回执原因或执行进程退出码；离线使用
最新进度，没有进度时明确说明目标机离线。不会读取或复制提示词及原始日志。

每个派单的每轮按状态变化去重；恢复执行后再次离线属于新的变化。
游标、已观察状态、待合并内容和汇报保存在控制端
`RD_STATE/reports/<用户>.json`，重启会延续待合并窗口，不重放已报告变化。
首次创建此文件时，游标直接定位到事件日志末尾（含轮转段），并把现有派单的
当前状态记为基线；历史终态不生成汇报、不发汇报事件、不写入会话。
首次远端采集尚未完成的现有轮次会持久保留“待建立基线”标记，其第一次可信观察只建基线；
即使中间重启也不把迟到的历史终态当新变化。第一次观察为执行中时，此后的终态正常汇报。
汇报线程等待首轮目标机采集完成；采集失败或跳过的轮次继续等待基线之后的新鲜观察，
旧缓存的分钟扫描不会提前消费标记。基线起始时间与标记一同持久保存。
后续新派单或基线之后的状态变化仍照常生成汇报。
无发起会话或 App 发起的派单仍不生成汇报；汇报一律保留在 `inbox` 供读取。

同一发起会话在固定窗口内的多条变化合并为一条汇报，`entries` 按发生顺序逐单列出。
默认窗口 **5 秒**，可在控制端配置中设置（修改后由编排方重启 serve）：

```ini
[monitor]
report_merge_window_seconds = 5
```

值必须为有限非负数，`0` 表示本次事件消费完成后立即生成。
这是本地汇报合并规则，不是外部服务超时。窗口从第一条变化开始，不会因新变化无限延长。
汇报 ID 使用发起会话、排序后的各条目 `job:round:state` 与到期时间的规范 JSON 数组
计算 SHA-256；与字典字段顺序、条目显示文本无关，不同到期窗口生成不同 ID。
后台另每 **60 秒** 从 serve 已采集的执行快照和本地回执扫描一次，补出未经过事件路径的变化；
扫描不自行连接目标机，目标机采集仍沿用 serve 的轮询设置。

`rdispatch inbox --initiator-session <会话>` 的 JSON 保留 `results`（待处理回执）与
`unreachable`，新增 `reports`。汇报不自动签收回执。CLI 读取汇报本身不会生成汇报。
`rdispatch watch JOB…` 在原有事件、状态和退出码之外增加 `progress`（最新进度）与
`result_summary`（回执摘要），读取失败时标记 `summary_unavailable=true`，保留原唤醒语义。
没有交接协议的命令派单没有进度汇报。

`rdispatch inbox --wait [--timeout 秒] [--json]` 只读等待本会话新汇报：以命令开始时已有汇报为基线，
出现新汇报退出 0，超时退出 2，缺发起会话退出 4。输出每条 `<派单ID> 第N轮 <状态>` 加查看命令
`rdispatch result <派单ID>`，不含执行方撰写的摘要或进度正文（`--json` 同）。serve 在线时读汇报存储；
离线时轮询本会话派单状态，进入终态视为新汇报，输出格式一致。不签收、不写回执，不新增常驻进程，
不转发给 serve。

只读接口 `GET /v1/reports` 返回当前用户的汇报；可用
`?initiator_session=<会话>` 过滤。字段样例见 `contracts/v1/reports.json`。
每条合并汇报发布一次 `report.created`，事件只包含用户、汇报 ID、发起会话与条目数，
样例见 `contracts/v1/report-events.json`。正文经过显示字段投影和 serve 敏感文本清理后落盘。
文件和事件使用现有回滚边界；普通写盘失败后可重试。与现有状态写入一样，
不提供跨多个文件的断电原子事务。

## 定时进度摘要

serve 按间隔检查看板数据，按发起会话分组，只汇总运行中的派单。某会话的摘要指纹
（派单、目标机、轮次、状态、最新进度与看板计数，不含已运行时长）与上一份不同才生成一条
`kind: "digest"` 的汇报；无变化不推送，运行结束的会话清除指纹，状态变化汇报仍照常。
摘要写入同一汇报存储，沿用 inbox 与 `report.created` 事件（App 经事件取得）。
指纹和到期时间随 `RD_STATE/reports/<用户>.json` 持久保存，重启后不重复推送上一份；
首次启用时从一个完整间隔之后开始。

```ini
[features]
progress_digest = true        # false 关闭

[monitor]
report_interval_seconds = 900 # 摘要默认间隔；同时仍是 watch 的超时
digest_interval_seconds = 900 # 可选，单独指定摘要间隔
```

## 汇报提醒与 inbox（票 48 改写）

汇报生成后只存 inbox 并发布 `report.created`。任何路径都不再写入会话输入框
（不调用 `rd_herdr.Herdr.input`，不取会话输入锁），`[serve] session_access`
总开关也不再影响汇报。App 终端里用户主动输入（`Sessions.write_input`）不变。
生成汇报后的提醒步骤是 `Reports.notify` 单一入口；提醒通道在此接入。

`GET /v1/reports` 与 `inbox` 的每条汇报带 `delivery`，记录提醒结果：

| 字段 | 含义 |
| --- | --- |
| `status` | `notified` 至少一个通道成功；`inbox_only` 没有启用任何通道或全部失败 |
| `channels` | 通道结果列表，每项 `{channel, ok, reason}`；没有通道时为空列表 |

当前没有启用任何通道，新汇报固定为 `inbox_only`、`channels` 为空。

老记录兼容：此前写入的 `queued`、`sending`、`delivered`、`text_delivered` 状态和
`reason`、`pane_id`、`delivered_at` 字段原样返回，不迁移、不重算；
原因键（`session_not_idle`、`session_closed`、`session_identity_unconfirmed`、
`herdr_unavailable`、`harness_not_verified`、`session_access_disabled`、
`session_input_failed`、`session_submit_failed`、`delivery_result_unknown`）仅出现在老记录。
无 `delivery` 字段的更老记录读取时补 `queued` 显示，不写回。

`report.delivered` 事件不再因写入会话产生；它改为在提醒通道成功后发布（后续票据接入），
字段为用户、汇报 ID、发起会话、成功通道名 `channels` 和时间，不含正文。
样例见 `contracts/v1/report-delivered.json`；`contracts/v1/reports-unsubmitted.json`
是历史状态样例（老记录的 `text_delivered/session_submit_failed`）。
汇报暂不自动清理，不改变回执签收、验收和保留期规则。

## 编排方部署与复测

合入后按现有流程安装 Python 包，由编排方重启 serve；无 Swift/App 构建。
在隔离状态目录中让一个可定位的发起会话产生已完成、卡住、离线和失败变化，
检查 `inbox`、`GET /v1/reports` 与 `report.created`；窗口内同会话两单应合并。
重启后确认不重放，漏事件的已采集快照应在 60 秒兜底扫描后补报。
真实目标机失联／恢复、真实 Herdr 会话关闭识别和测试栈部署由编排方执行。
本票不执行服务部署或重启。编排方在测试栈复测：

1. 使用隔离的 Herdr 发起会话（空闲、工作中、已关闭各一）触发汇报，确认会话输入框始终没有新文本，汇报只在 inbox。
2. 关闭 `[serve] session_access` 后再触发，确认汇报生成与 `delivery` 不变。
3. 重启 serve 后确认汇报不重放，`report.created` 只发一次，没有 `report.delivered`。
4. 使用已有历史派单但没有汇报存储的隔离状态目录首次启动，确认历史记录不生成汇报；
   再完成一条新派单，确认能正常汇报。
5. 读取带老状态（`queued`、`delivered`、`text_delivered`）的老汇报存储，确认状态与原因键原样返回。

本地自动化使用隔离的假 Herdr，不占 TCP 监听端口，不访问真实 harness 或外部服务。
真实会话识别、状态切换时序、真实写入失败、测试栈部署与服务重启列为需编排方／端到端执行。
