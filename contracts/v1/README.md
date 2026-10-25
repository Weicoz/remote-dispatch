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
- `jobs.json`：卡片列表，只含当前用户和没有用户字段的旧记录，跨发起会话。
  `round` 为整数；`outcome/checks/executor_exit` 在回执不可用时为 `null`；
  `delivery_state` 为 `not_available/pending/received`；验收状态沿用已有四个取值。
  来源、模型、签收、停止者、更新时间等未知值均可为 `null`。
- `job-details.json`：卡片、显示参数、从 1 起升序的各轮执行状态、进度、回执与验收。
  顶层 `prompt` 是首轮原文；`rounds[].prompt` 是该轮原文或续跑附言，
  打回产生的新轮次从上一轮验收记录读取 `review_note`。旧记录没有保存原文时为 `null`。
  `started_at` 来自回执报告或当前轮的目标机快照，未知时为 `null`。
  `progress_events` 是事件日志中本用户本派单本轮的完整 `progress.updated` 事件，
  包含 `seq/time/type/data`，按事件时间排序，同时间按 seq 排序；包含轮转段。
  旧事件不会补造，尚无事件时为空列表。`job-details-timeline.json` 展示有原文的运行中轮次。
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
- `logs-out.json` / `logs-err.json`：UTF-8 日志尾部，`text` 保留换行。
  `stream` 只接受 `out/err`，`tail` 默认 40，接受 1–10000 的整数。
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
  成功 body 与 GET 相同。同键返回首次结果，失败保留原选择。
- `network-addresses-write-invalid.json`：禁用项也不能含通配地址，返回
  400 `invalid_listen_address`；其他失败与本机边界见 `docs/serve.md`。
- `sessions-list.json` / `sessions-projects.json` / `sessions-resolve.json`：会话快照
  增加可选 `updated_at`（带时区 ISO 8601 字符串，未知为 `null`）。来源为当前用户、
  同一 pane／会话 ID／harness／项目最近一次持久 `session.changed` 或
  `session.message` 的事件 `time`，包含轮转段；详情见 `docs/serve.md`。
  老服务可省略此字段。事件结构不变，查询和无变化的轮询不补造更新时间。
