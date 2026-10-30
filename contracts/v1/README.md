# 本机 serve v1 契约样例

本目录是 rdispatch 与 App 假 serve 共用的接口样例。复制时记录来源提交号。
票 20、22、25 在本目录追加；`/v1` 同版本内只添加可选字段，客户端忽略未知字段。
合同版本由 `GET /v1/version` 返回，当前为 1。只在 `/v1` 出现不兼容变化，
或新增 App 依赖的字段时递增；仅添加客户端可忽略的可选字段不递增。

每个 JSON 文件含 `request: {method, path, headers?, body?}`、
`response: {status, body}`、`events: [{seq, time, type, data}]`。
路径和请求体中的示例标识需要替换为夹具中的真实标识；时间、回执 ID、项目 ID
是示例值，不作为字符串快照比较。契约测试按样例递归校验必需字段和 JSON 类型，
另用行为断言覆盖实际值、用户隔离、纯查询、锁、转交与补读。

- `version.json`（票 02）：`GET /v1/version` 返回 `package_version`（与
  `rdispatch --version` 一致）、`contract_version`（整数合同版本）和
  `config_reload`（`ok/at/error`）。`at` 是带时区 ISO 8601 时间，`error` 成功时为 null；
  票 03 起记录最近一次配置重载：成功是本次读取时间，失败是失败时间；
  配置未变化时重复读取保持同一个启动加载时间。接口只读内存状态；
  样例由 `tests/test_rd_serve_version.py` 经真实 serve 子进程校验。

- `hosts.json` / `hosts-populated.json`：空列表与已配置目标机。配置顺序固定；
  响应顶层另带 `config_reload`，与 `version.json` 同形，成功和失败都返回；
  它是客户端可忽略的新增字段，合同版本保持 1。
  票 03 起由真实 serve 子进程在临时配置变化后验证。
  `hosts-config-reload-error.json` 展示解析或校验失败时的 `config_reload`，
  此时 `hosts` 继续返回上一次成功加载的目标机。
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
- `board.json`：`GET /v1/board?all=1`（该用户全部派单）或 `?initiator_session=<会话>`（单个发起会话），
  二者都缺返回 400。结构与 `rdispatch board --json` 相同：`summary`（已派、运行中、待派、阻塞）与逐派单 `rows`，另有 `reclaim_plan`（serve 定时回收的计划与结果，未启用为 null）；
  老记录缺的 `harness`、`model`、`created`、`elapsed_seconds` 为「未记录」；`regenerable_kb`（路径到 KB 大小）仅在有记录时出现；`credential_suspect`（true）与 `credential_hint` 仅在本轮输出有明确认证失败时出现。
  只读本地快照，不探测目标机；`reclaim_block` 只含不连目标机可判的原因，合入与未提交检查仍由 `clean` 给出。
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

- `write-runs-suggestion.json`：`POST /v1/runs` 带 `plan_item` 且有工作线建议时的两步派单第一步，
  返回 200 `{suggestion}`（内容同 `rdispatch suggest --json`），此时不派单。App 确认后用新幂等键
  再 POST 同一请求体并加 `suggestion`，成功返回 201 `job, round, line`；详情见 `docs/serve.md`。
  样例由 `tests/test_rd_serve_writes.py` 经真实 serve 校验结构。
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
  票 15 在 `config` 只读追加 `dispatch_mode`（`suggest` / `line` / `ticket`，缺省 `suggest`）。
  会话级派单模式用独立接口 `GET/PUT /v1/sessions/{窗格ID}/dispatch-mode`，体为
  `{dispatch_mode}`；整体替换 `dispatch-defaults` 不会清掉它。

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
- 汇报提醒改版（票 01）：`reports.json` 的 `delivery` 改为 `status`（`notified`／`inbox_only`）与 `channels`；
  `report-delivered.json` 的 `data` 改为成功通道名列表 `channels`，仅在提醒成功后发布；
  `reports-unsubmitted.json` 标为历史状态样例，老记录的旧状态与原因键原样返回。
- 票 61：`reports-unsubmitted.json`（历史状态样例）展示文本已写入、回车失败的
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


票 73 设置补充（只增加字段，原有含义不变）：

- `session-access-get/put/invalid.json`：仅 Unix socket 的总开关读写。PUT 的 enabled
  必须是 JSON 布尔值，成功返回新值并持久保存；设备级权限独立。
- `network-pairing-window-close.json`：DELETE 立即关闭当前配对窗口，已配对设备不受影响。
- `network-devices.json`：每个设备新增 paired_at / last_seen_at，带时区 ISO 字符串，
  未知为 null；旧设备 paired_at 不补造。最近访问取成功鉴权时间，查询含内存新值，
  后台每 60 秒合并落盘，正常停服补刷；鉴权失败不更新，心跳不重计。
- `network-addresses*.json` / `network-pairing-window.json`：候选和配置项新增 kind；
  新地址事件也带 kind，旧事件可省略。按顺序判断：tailscale = 100.64.0.0/10 或
  fd7a:115c:a1e0::/48；easytier = 10.144.144.0/24 或 10.126.126.0/24；
  lan = 10/8、172.16/12、192.168/16、169.254/16、fc00::/7、fe80::/10；
  其余 other（包含回环）。规则按网段，不保证识别用户自定义 VPN 网段。
- `host-config-active.json`：409 原 reason 不变，error.running_count 是跨用户完整计数，
  error.running_job_ids 排序且最多 20 项，未知执行状态仍阻止删除。
- `host-config-max-heavy-invalid.json`：max_heavy 为 >=1 的整数字符串，无 8 的上限，
  非整数或小于 1 返回原 reason、field 和新增可读 text；null 沿用清除覆盖含义。

票 05 的设备与配对 CLI 只调用上述 `network-devices.json`、`network-pairing-window*.json`
和 `network-revoke.json` 对应接口，不新增路径、字段或样例；`--json` 原样转发 serve 响应。

票 06 的 `rdispatch session-access` 与 `rdispatch host` JSON 输出直接复用
`session-access-*.json`、`host-config-*.json` 的 HTTP 响应结构，没有新增 HTTP
路径或字段，因此不增加合同样例。CLI 在线时调用上述接口，离线时用同一套配置读写
模块；差异只在错误输出走 `rdispatch:` stderr，不改变 HTTP 合同。
