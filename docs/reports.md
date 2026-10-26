# 状态变化汇报

`rdispatch serve` 在控制端消费 `job.state_changed`、`receipt.ready` 和
`progress.updated` 事件，在派单变为已完成、卡住、离线、失败时生成汇报。
汇报带派单标题（旧记录使用派单 ID）、目标机、轮次、新状态与摘要。
卡住使用回执中等回话的摘要，失败使用回执原因或执行进程退出码；离线使用
最新进度，没有进度时明确说明目标机离线。不会读取或复制提示词及原始日志。

每个派单的每轮按状态变化去重；恢复执行后再次离线属于新的变化。
游标、已观察状态、待合并内容和汇报保存在控制端
`RD_STATE/reports/<用户>.json`，重启会延续待合并窗口，不重放已报告变化。
无发起会话或 App 发起的派单仍不生成汇报；发起会话已关闭或不在 Herdr 时保留汇报供 `inbox` 读取。

同一发起会话在固定窗口内的多条变化合并为一条汇报，`entries` 按发生顺序逐单列出。
默认窗口 **5 秒**，可在控制端配置中设置（修改后由编排方重启 serve）：

```ini
[monitor]
report_merge_window_seconds = 5
```

值必须为有限非负数，`0` 表示本次事件消费完成后立即生成。
这是本地汇报合并规则，不是外部服务超时。窗口从第一条变化开始，不会因新变化无限延长。
后台另每 **60 秒** 从 serve 已采集的执行快照和本地回执扫描一次，补出未经过事件路径的变化；
扫描不自行连接目标机，目标机采集仍沿用 serve 的轮询设置。

`rdispatch inbox --initiator-session <会话>` 的 JSON 保留 `results`（待处理回执）与
`unreachable`，新增 `reports`。汇报不自动签收回执。CLI 读取汇报本身不会生成汇报。
`rdispatch watch JOB…` 在原有事件、状态和退出码之外增加 `progress`（最新进度）与
`result_summary`（回执摘要），读取失败时标记 `summary_unavailable=true`，保留原唤醒语义。
没有交接协议的命令派单没有进度汇报。

只读接口 `GET /v1/reports` 返回当前用户的汇报；可用
`?initiator_session=<会话>` 过滤。字段样例见 `contracts/v1/reports.json`。
每条合并汇报发布一次 `report.created`，事件只包含用户、汇报 ID、发起会话与条目数，
样例见 `contracts/v1/report-events.json`。正文经过显示字段投影和 serve 敏感文本清理后落盘。
文件和事件使用现有回滚边界；普通写盘失败后可重试。与现有状态写入一样，
不提供跨多个文件的断电原子事务。

## 经 Herdr 送达（票 48）

serve 通过现有 `rd_herdr.Herdr.input` 写入文本并发送 `enter`，只允许已验证的
Codex、Claude Code。每次送达前重新发现会话，按 `initiator_session` 匹配，
不凭失效的会话列表向旧 pane 写入。按目录推测的身份只留 inbox，不自动发送。会话工作中、等待输入或状态未知时继续排队，
空闲后按汇报生成顺序先进先出；不同会话互不阻塞。

`GET /v1/reports` 与 `inbox` 的每条汇报新增 `delivery`：

| 字段 | 含义 |
| --- | --- |
| `status` | `queued` 排队；`sending` 已保存发送意图；`delivered` 已成功写入；`inbox_only` 只留 inbox |
| `reason` | 原因键，正常送达时为 null，见下表 |
| `pane_id` | 送达目标（显示值，已脱敏），未定位时为 null |
| `delivered_at` | 成功写入时间，未送达为 null |

| 原因键 | 含义（界面占位文字由编排方定稿） |
| --- | --- |
| `session_not_idle` | 会话尚未空闲，等待发送 |
| `session_closed` | 会话已关闭或不在 Herdr |
| `session_identity_unconfirmed` | 无法可靠确认发起会话身份，只留 inbox |
| `herdr_unavailable` | Herdr 无法查询，只留 inbox |
| `harness_not_verified` | 此 harness 尚未验证汇报写入，只留 inbox |
| `session_access_disabled` | 会话访问总开关已关闭，只留 inbox |
| `session_input_failed` | 写入失败，只留 inbox |
| `delivery_result_unknown` | 上次写入结果无法确认，只留 inbox |

成功写入后追加一次 `report.delivered`，只含用户、汇报 ID、发起会话、pane 和送达时间，
不携带正文；样例见 `contracts/v1/report-delivered.json`。送达不会自动签收或验收。
Kimi、AGY 等未验证 harness 和其他不可送达情况均不尝试自动重发，汇报记录仍保留。
总开关关闭期间生成的汇报只留 inbox，重新打开不会重放这些汇报。

发送意图先落盘，再调用 Herdr；发送失败或进程在外部写入后中断均不自动重发，
避免文本已写入但结果未知时重复输入。成功状态与待发布事件先一起落盘，
事件写盘失败时仅补发事件，绝不再次写入会话。与已有状态写入相同，
此机制不承诺多个文件断电原子性，也无法把 Herdr 的外部写入纳入本地事务。
汇报暂不自动清理，不改变回执签收、验收和保留期规则。

## 编排方部署与复测

合入后按现有流程安装 Python 包，由编排方重启 serve；无 Swift/App 构建。
在隔离状态目录中让一个可定位的发起会话产生已完成、卡住、离线和失败变化，
检查 `inbox`、`GET /v1/reports`、`report.created` 与 `report.delivered`；窗口内同会话两单应合并。
重启后确认不重放，漏事件的已采集快照应在 60 秒兜底扫描后补报。
真实目标机失联／恢复、真实 Herdr 会话关闭识别和测试栈部署由编排方执行。
本票不执行服务部署或重启。编排方在测试栈复测：

1. 使用 Codex、Claude Code 的隔离 Herdr 发起会话，空闲时触发汇报，核对一次文本和回车、一次送达事件。
2. 会话工作中连续产生两条不同汇报；确认无即时写入，空闲后按先后顺序收到。
3. 在合并窗口及排队期间关闭会话，确认 inbox 保留、没有向复用 pane 误发。
4. 用 Kimi、AGY 窗格及关闭 `[serve] session_access` 的配置验证只留 inbox，原因键正确。
5. 重启 serve 后确认成功、失败及结果未知的汇报均不重复输入；真实写入故障后检查 inbox。

本地自动化使用隔离的假 Herdr，不占 TCP 监听端口，不访问真实 harness 或外部服务。
真实会话识别、状态切换时序、真实写入失败、测试栈部署与服务重启列为需编排方／端到端执行。
