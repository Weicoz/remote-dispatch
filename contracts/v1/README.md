# 本机 serve v1 契约样例

本目录是 rdispatch 与 App 假 serve 共用的接口样例。复制时记录来源提交号。
票 20、22、25 在本目录追加；`/v1` 同版本内只添加可选字段，客户端忽略未知字段。

每个 JSON 文件含 `request: {method, path, headers?, body?}`、
`response: {status, body}`、`events: [{seq, time, type, data}]`。
路径和请求体中的示例标识需要替换为夹具中的真实标识；时间、回执 ID、项目 ID
是示例值，不作为字符串快照比较。`tests/test_rd_serve.py` 按样例递归校验必需字段和
JSON 类型，另用行为断言覆盖实际值、用户隔离、纯查询、锁、转交与补读。

- `hosts.json` / `hosts-populated.json`：空列表与已配置目标机。配置顺序固定；
  `local/reachable/full` 是布尔值，资源值沿用探针的字符串或数字，未采集到为 `null`。
  票 45 追加可选 `cpu_pct`、`disk_total`、`metrics`；新服务固定返回三项指标。
  `metrics.cpu/memory/disk` 各含数值 `value`（未知为 null）、`unit`、数值
  `warning/pressure` 和 `level=normal/warning/pressure/unknown`。
  CPU 的 unit 为 POSIX `load_per_core` 或 Windows `cpu_pct`；内存为 `free_pct`，
  磁盘为 `free_gb`，磁盘另有数值 `total`（旧探针或缺测为 null）。
  两个磁盘字段沿用探针的 GB 口径（实际为 1024³ 字节，整数截断），可用总量画已用比例。
  警告含等号；高压严格沿用 assess 的大于上限／小于下限，等于高压点仍未触发拒绝。
  高压优先于警告。`full` 继续使用 assess 的完整拒绝结论，包括换页、网络、
  必需指标缺测等原因，不能仅用三项级别重算；离线与未探测时 full=false。
  `hosts-warning/pressure/unknown/windows.json` 展示对应状态，`hosts-cold-start.json`
  展示首次探测前的 null/unknown；探测失败同样不把未知标为正常。
  `host.changed` 新增可选 `cpu_pct/disk_total`；客户端收到事件后重读指标。
  票 59 追加 `last_seen_at`：后台最近一次成功探测的控制端时间（带时区 ISO 8601），
  在线时每次成功探测刷新；离线、冷启动时保留持久记录，从未成功则为 null。
  `hosts-offline.json` 展示离线仍保留时间。查询不探测，单纯时间刷新不发事件。
- `jobs.json`：卡片列表，只含当前用户和没有用户字段的旧记录，跨发起会话。
  卡片与详情各轮可选 `snapshot_at` 是后台执行快照的控制端采集时间；
  冷启动回退本地回执时取 `captured_at`，未知为 null。
  `jobs-cold-start.json` 展示尚未完成首次采集时的已知状态与未知状态。
  续跑或迁机不沿用上一代际的快照，查询不联系目标机。
  `round` 为整数；`outcome/checks/executor_exit` 在回执不可用时为 `null`；
  `delivery_state` 为 `not_available/pending/received`；验收状态沿用已有四个取值。
  来源、模型、签收、停止者、更新时间等未知值均可为 `null`。
- `job-details.json`：卡片、显示参数、从 1 起升序的各轮执行状态、进度、回执与验收。
  顶层 `prompt` 是首轮原文；`rounds[].prompt` 是该轮原文或续跑附言，
  打回产生的新轮次从上一轮验收记录读取 `review_note`。旧记录没有保存原文时为 `null`。
  `started_at` 来自回执报告或当前轮的目标机快照，未知时为 `null`。
  `progress_events` 是事件日志中本用户本派单本轮的完整 `progress.updated` 事件，
  包含 `seq/time/type/data`，按进度 `updated_at`（缺少时用事件时间）排序，
  同时间按 seq 排序；包含轮转段。后台从追加历史补采未观察过的进度事件，
  尚无事件时为空列表。`job-details-timeline.json` 展示有原文的运行中轮次。
  `progress` 是最新采集到的 reporter 显示字段，无进度时为 `null`。
  历史未缓存或尚未结束的轮次，其 `receipt` 为 `null`。来源字段位于 `job`。
- `receipt.json`：回执与报告、证据、签收和验收叠加。`report` 可为 `null`；
  `changed_files` 为保存的报告 `changed_files`（兼容回执同名字段）的显示投影，
  每项含 `path/additions/deletions`，二进制文件行数为 `null`。
  `changed_files_available=false` 且列表为空表示没有有效统计；空列表与 true 表示确实无改动。
  `receipt-changes.json` 展示有统计的回执。查询不执行 Git diff，也不探测目标机。
  `evidence` 等沿用 reporter 的可选字段。签收对象包含
  `receipt_id/received_at/acked_by/via`（旧记录可能只有前两个字段）。
  未验收时 `reviewed_by/reviewed_at/review_note` 为 `null`。
- `progress.json`（票 44）：`GET /v1/jobs/{job}/rounds/{n}/progress` 返回
  `progress: [{updated_at, phase, summary}]`，无进度为空数组。后台完整回传目标机
  `reports/progress.rN.jsonl`，按 `updated_at` 升序，同时间保持追加顺序；不加百分比。
  旧目标机无追加文件时只返回最新一条。详情的 `progress_events` 同时补入这些更新，
  按进度 `updated_at`（旧事件缺字段时用事件时间）和 seq 排序；重复轮询不重复发布。
  查询只读本地快照，首次采集前为空，断线和服务重启保留上次采集的历史。
- `logs-out.json` / `logs-err.json`：UTF-8 日志尾部，`text` 保留换行。
  `stream` 只接受 `out/err`，`tail` 默认 40，接受 1–10000 的整数。
  请求立即返回后台缓存并触发异步刷新；可选 `snapshot_at` 为缓存采集时间，
  首次尚未就绪时为 null 且 text 为空。采集失败返回既有 read_unavailable。
- `events.json`：SSE 响应的 JSON body 为 `null`，`events` 表示帧数据。
  帧格式为 `id: <seq>\nevent: <type>\ndata: <完整事件 JSON>\n\n`。
  冒号开头的注释是心跳，客户端应忽略。`Last-Event-ID` 优先于 `after`；
  游标默认 0，必须是非负整数，返回严格大于游标的事件。
  事件按用户过滤，所以可见的 seq 允许有间隔，不能当作丢事件。
- `not-found.json` / `invalid-request.json`：统一 JSON 错误结构。
- `cli-ack.json`：本机私有 CLI 转交协议样例，**不是 App 的写接口**；
  仅本机 socket 可调用。`exit_code/stdout/stderr` 原样交回 CLI。
  该通道承载现有 CLI 输出，不能当作对外展示字段接口使用。

本票未实现 `/v1` 写接口、项目摘要、统计、会话、HTTPS、配对或推送。
项目过滤和历史分页由票 22 追加；本票的 `/v1/jobs` 返回当前登记的完整列表。
所有公共只读接口按显示字段白名单投影，并剔除敏感字段、配置敏感值和私钥文本；
自由正文继续遵循 reporter 的脱敏约定，不能把原始凭据作为提示词、报告或日志提交。

- `network-addresses.json`：本机读取实际候选与证书指纹；可选字段
  `listen_addresses` 返回完整配置（含禁用项，缺省 enabled=true、port=8443）。
- `network-addresses-write.json`：Unix socket 的 `POST /v1/network`，带幂等键，
  完整替换并保存勾选列表，立即重新绑定；样例展示取消监听后的空候选事件。
  成功 body 与 GET 相同。同键且请求指纹相同返回首次结果，失败保留原选择。
- `network-addresses-write-invalid.json`：禁用项也不能含通配地址，返回
  400 `invalid_listen_address`；其他失败与本机边界见 `docs/serve.md`。
- `sessions-list.json` / `sessions-projects.json` / `sessions-resolve.json`：会话快照
  来自最近一次成功的后台发现，HTTP 列表与反查不执行 Herdr；
  冷启动尚未发现时为空，发现失败保留上次列表。屏幕与终端仍读本机 Herdr。
  增加可选 `updated_at`（带时区 ISO 8601 字符串，未知为 `null`）。来源为当前用户、
  同一 pane／会话 ID／harness／项目最近一次持久 `session.changed` 或
  `session.message` 的事件 `time`，包含轮转段；详情见 `docs/serve.md`。
  老服务可省略此字段。事件结构不变，查询和无变化的轮询不补造更新时间。
- `harness-stats.json`（票 41）：追加可选 `total_round_count`（累计已结束且保留回执），
  顶层 `date_basis=controller_local`、`host_stats[].host/daily_round_counts[]`。
  日序列固定 14 项升序，含今天和前 13 个本地日，空日为 0；每项含 `date/round_count`。
  `harness.stats_changed` 附带同样的累计值与机器日序列；只有机器统计时 harness 为 null。
  客户端应据事件刷新接口；统计不依赖事件日志，时间与旧记录口径见 `docs/serve.md`。
- `projects-summary.json`（票 41）：`hosts[目标机].accepted_round_count` 是此项目×目标机
  明确验收通过的累计轮次数；`project.changed` 的统计变体另带 `host/accepted_round_count`。
  机器迁移前的轮次按回执目标机归属。App 同步契约时保留可选字段兼容。

- `write-idempotency-reused.json` / `network-idempotency-reused.json`：同调用方的幂等键
  先执行可选 `previous_request`，再执行同键的 `request`；幂等键
  已绑定其他方法、路径或规范 JSON 请求体时返回 422 `idempotency_key_reused`，不执行动作。
  `network-pair-replay.json`：配对码／设备令牌只在首次响应返回；相同配对或开启窗口
  请求重放返回 403 `pairing_closed`，缓存不含两类秘密明文。
- `sessions-terminal.json`：保留帧结构和 ANSI；含敏感正文时对 Base64 解码数据脱敏后重新编码。
- `session-dispatch-defaults-get.json` / `session-dispatch-defaults-put.json` /
  `session-dispatch-defaults-clear.json`：会话默认值 GET/PUT，PUT 必须带幂等键，完整替换
  `{host, harness}`，缺项或 null 读配置；变化事件携带 `dispatch_defaults`。
  清除样例先执行 `previous_request`；详情见 `docs/session-dispatch-defaults.md`。
  票 59 的 GET 增加 `config: {host, harness}`，只读当前 `[defaults]` 配置，
  不受会话覆盖影响；host 缺省为 auto，harness 未配置为 null。PUT 和事件结构保持不变。

- 票 57：`projects-summary.json` / `projects-summary-breakdown.json` 固定
  `blocked_count/offline_count/failed_count`，总字段 `needs_attention_count` 等于三项之和。
  已完成（含待会话验收）、主动停止、已通过／已作废不计入；签收不清除卡住或失败。
  `hosts[].state` 词表为 `blocked/failed/offline/running/completed/none`，优先级依此顺序。
  `execution_state` 保留原始状态，区分 `dead/missing/unreachable`，无派单时为 null。
  `sessions-list/projects/resolve.json` 固定 `cwd` 与 `updated_at`，目录来自 Herdr pane，
  前景目录优先；`sessions-cwd-unknown.json` 展示实时目录读不到时的 null。
  字段对旧服务仍为可选；新服务固定输出。会话与项目接口的敏感扫描覆盖目录和新摘要字段。

- 票 48：`reports.json` 追加可选 `delivery`（新服务固定返回），含
  `status/reason/pane_id/delivered_at`；旧汇报读取时补排队显示字段，不写回状态。
  `report-delivered.json` 是成功写入 Herdr 后的 `report.delivered` 事件，
  仅含汇报标识与送达元数据，不含正文；状态和原因键见 `docs/reports.md`。
- 票 61：`reports-unsubmitted.json` 展示文本已写入、回车失败的
  `delivery.status=text_delivered/reason=session_submit_failed`；保留写入时间，
  不发布 `report.delivered`、不重发。`session-dispatch-defaults-closed.json`
  展示给不存在或已关闭窗格 PUT 默认值的 404 `session_closed`，不保存默认值或发布事件。

- 票 64：`host-config-list/create/update/delete/invalid/active.json` 是目标机配置 CRUD
  合同；独立路径 `/v1/config/hosts`，保持指标接口 `/v1/hosts` 不变。values 为普通配置
  字符串，protected_fields 只含字段名及 is_set；认证内容不回显。
  PUT 更新提交字段，null 删除本段覆盖；名称不可改。写前私密备份、保留注释、原子替换、
  热更新及删除任务保护的错误词表见 `docs/serve.md`，App 合同副本由编排方随后同步。

- 票 68：`sessions-screen.json` 增加 `width/height/cursor`，`text` 是同一完整帧 ANSI。
  `sessions-terminal.json` 展示 `data`（统一 Base64 字段）、真实 `width/height`、`full` 和
  `cursor={x,y,visible}`（从 0 起算）；尺寸变化通过 `full:true` 全量替换，连接内 `seq` 递增。
  `sessions-input-text.json` 保留默认带 Enter 的旧请求；`sessions-input-raw-text.json` 的
  `submit:false` 不追加 Enter；显式 `true` 仍提交，只有 JSON 布尔值合法。
  `sessions-input-keys.json` 列出已实测可用的 14 个键，未知键仍 400；
  `delete/home/end/pageup/pagedown` 不支持。App 每次输入仍须带新的 `Idempotency-Key`。
  从旧服务升级时，新响应字段在客户端解码应作为可选字段；旧请求行为保持不变。
  接入与 resize 检查间隔见 `docs/serve.md`，本票未增加滚动历史／alt-screen 状态字段。

- 票 70：`sessions-terminal-ws.json` 是双向终端 Upgrade／输入／帧／关闭合同。
  上行原始字节为 `bytes`，下行沿用票 68 的 `data`，每次连接首帧 full。
  busy 默认拒绝，只有 `takeover=true` 显式接管；详见 `docs/terminal-websocket.md`。

- 票 72：`sessions-list.json` / `sessions-stale.json` 在顶层新增 `collection`，
  用 `last_attempt_succeeded` 区分失败沿用旧列表和成功空列表；连续失败达到 3 次时
  `stale=true`，成功重置。`reason` 仅异常类别，`last_success_at` 可为空。
  `sessions` 及原有行字段含义不变，旧客户端忽略新增字段；新客户端兼容旧服务缺少此字段。
