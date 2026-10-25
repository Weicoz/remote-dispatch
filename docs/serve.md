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
派单详情增加首轮 `prompt`、各轮 `prompt/started_at/progress_events`；
新派单与续跑在控制端保存用户原文（不包含执行器追加的交付约定和迁移提示），
打回轮次的附言取上一轮验收记录。未保存原文的旧派单返回 `null`。
各轮进展历史只读持久事件日志，包含轮转段，按事件时间及 seq 排序，
与 SSE 使用相同的事件白名单、用户过滤和脱敏；最新进度 `progress` 仍然保留。
开始时间优先取回执报告的 `started_at`，当前轮也可取后台目标机快照，未知为 `null`。
回执（包括详情内嵌回执）增加 `changed_files` 与 `changed_files_available`：
从已保存的报告或回执同名字段读取路径和增删行数，二进制行数为 `null`；
统计缺失或无效时返回空列表及 false。读取不执行 diff，不产生额外远端计算。
没有统计的旧回执继续可读；执行方提供的统计仍需发起方验收。
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
## 票 21：HTTPS、配对、设备与推送

网络接线在 `rd_serve_network.py`，推送决策与发送接口在 `rd_push.py`。
上文的 Unix socket、CLI 转交和只读接口继续适用；配置了下列地址时，serve 同时提供 HTTPS：

```ini
[serve]
listen_addresses = [{"address":"192.168.1.20","port":8443,"enabled":true},{"address":"100.90.1.20","port":8443,"enabled":false}]
```

列表由 Mac 设置页提供。`enabled` 缺省为 true，表示列出的候选默认勾选；
没有列表或列表为空时只启动 Unix socket。只绑定启用项的具体 IP，禁用项不绑定。
允许 LAN 私网、链路本地、Tailscale CGNAT、IPv6 ULA 和回环地址；
拒绝公网、主机名、通配地址（包括 `0.0.0.0`、`::`）、重复项和无效端口。
地址须已属于控制端接口，否则启动失败并回收本次已打开的监听器与 socket。
修改地址配置后由编排方重启；运行中的服务不重读配置。
`GET /v1/network`（仅 Unix socket）返回实际已绑定的 `candidates: [{address, port}]`
和 `spki_sha256`。启动时与已记录列表不同才发布 `network.addresses_changed`，
事件携带相同的候选列表和指纹；清空配置也发布空列表。所有候选共用证书。

首次启动使用系统 `openssl` 生成 EC P-256 自签证书，之后用标准库 `ssl` 加载，
最低 TLS 1.2。证书和私钥保存在 `RD_STATE/network/server.pem`、`server.key`，
新目录 0700、文件 0600。依赖 `openssl req/x509/pkey`；没有 openssl 时仍可用
Unix-only 服务，HTTPS 和配对不可用，相应测试跳过。私钥或证书仅剩一个时拒绝
自动换证，避免无声改变已固定身份；由编排方恢复原证书对或安排重新配对。

### 本机开启窗口与扫码

Mac 用户通过 Unix socket 手动调用 `POST /v1/pairing-window`，JSON body `{}`，
带 `Idempotency-Key`。返回 `code / expires_in: 300 / candidates / spki_sha256`，
供 App 生成二维码。窗口不因服务启动或重启自动开启；300 秒边界起即拒绝新配对。
再次手动开启使旧码失效；成功一次即关闭窗口。二维码和返回的令牌均不应记入日志。

iPhone 先校验 TLS 证书的 **SPKI DER SHA-256**（64 位小写十六进制，非整张证书哈希），
再调用 `POST /v1/pair`，body `{"code":"扫码所得的一次性码","name":"Phone"}`。
带 `X-Rdispatch-SPKI: <固定指纹>` 与 `Idempotency-Key`；此端点不要求已有设备令牌。
成功返回 `device_id / device_token / session_access_enabled: false`。
令牌不过期，客户端须存 Keychain；服务端保存令牌摘要供鉴权。

之后每个 HTTPS 请求必须同时带 `X-Rdispatch-SPKI` 与
`Authorization: Bearer <device_token>`，包括 SSE 重连。
**头中的指纹不能代替客户端在 TLS 握手时对服务器公钥的固定校验。**
指纹不符为 403 `fingerprint_mismatch`，无令牌、令牌无效或已吊销为 401
`device_unauthorized`；配对窗口关闭、过期或码无效为 403 `pairing_closed`。

### 设备管理与幂等

| 接口 | 通道与行为 |
| --- | --- |
| `GET /v1/devices` | 仅 Unix socket；设备 ID、名称、吊销状态、会话访问权限，不含任何令牌 |
| `POST /v1/devices/{id}/session-access` | 仅 Unix socket；body `{"enabled":true}`，允许／禁止该设备访问会话 |
| `DELETE /v1/devices/{id}` | 仅 Unix socket；立即吊销，关闭该设备已连接的 SSE 和其他 HTTPS 连接 |
| `POST /v1/push-tokens` | 已认证设备登记自身 APNs token：`{"token":"…"}`；不能通过 body 冒认别的设备 |
| 同一 push-tokens 接口 | Unix socket 可显式给 `device_id` 登记；不得给已吊销设备登记 |

网络调用本机管理接口或 `/_cli/v1` 返回 403 `local_only`。
已配对设备会话权限默认关闭，`/v1/sessions` 及其子路径返回 403
`session_access_disabled`；权限打开后继续交由会话接口处理（票 25）。

以上所有写接口要求 `Idempotency-Key`，缺失或空值返回 400
`idempotency_key_required`。认证后的同一调用方同键返回首次状态码和 body，
即使重启、请求正文改变也不重新执行。配对重试按一次性码划分调用方。
鉴权失败不持久缓存。过期窗口的开启请求重试仍返回首次结果，需用新键手动开新窗口。
设备状态和幂等结果保存在私有 `network/devices.json`（0600）；为能重放首次
配对响应，幂等结果包含发放的令牌，不能把此文件作为展示、日志或产物导出。
吊销状态持续落盘，重放旧配对结果不能恢复已吊销令牌。
写入复用 `JobLock` 文件锁和现有状态／事件回滚边界；配对、登记、权限和吊销发
`device.changed`（仅 ID、用户、权限与吊销状态）。事件不含配对码、设备令牌或 APNs token。
业务写仍复用 CLI 的 job 锁与实现。本票没有改动现有私有 CLI 转交通道的协议。

### APNs 发送接缝

`Service(..., push_sender=sender)` 可注入实现 `PushSender` 的发送器：
`send(token, payload, *, stop)`。发送器必须在 `stop` 置位后及时返回，且自行处理
凭据和传输；真实网络超时等参数须先在目标环境测量。本票不带真实 APNs 传输，
CLI 默认未注入发送器，不连接 Apple，也不读取用户推送密钥。

后台消费当前用户可见事件，只交给发送器以下计数通知：

- `receipt.ready`：`receipt_ready: 1`；`outcome=blocked` 时为 `blocked: 1`，不重复推两次。
- 派单 `dead/unreachable` 或目标机不可达持续 **超过** 300 秒：`dead: 1` 或 `unreachable: 1`。
  计时从事件时间开始，恢复清零，轮次变化重新计时，持续同一异常只推一次。
- `suppress_push=true` 的事件不推，并清除该派单的异常计时。
  主动停止、进度、开始、签收、验收和会话事件均不触发通知。

payload 为 `{"aps":{"content-available":1},"counts":{"receipt_ready":1}}` 等，
不包含派单名、摘要、正文或密钥。发送在设备锁外进行，发送前重查设备有效性；
吊销不等待已在途的发送。游标与异常计时在 `network/push.json` 持久化，正常重启
和补读不重发；发送成功后、检查点落盘前崩溃属于至少一次投递，可能重复。
没有活动设备时事件仍被消费，不向以后新配对设备补推旧通知。

### 检查与编排方／端到端复测

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_serve_network tests.test_rd_push tests.test_rd_serve tests.test_rd_events -v
```

测试仅绑定 127.0.0.1 随机端口，使用短 Unix socket 路径；结束后关闭服务并删除 socket。
覆盖五分钟窗口、单次码、幂等重试、鉴权与重启、吊销空闲 SSE、阻塞发送器、
未完成 TLS 握手、设备权限、地址事件、状态／事件失败回滚和假 APNs 计数规则。
新增契约样例为 `contracts/v1/network-*.json`，可与 App 假 serve 共用。

由编排方按现有安装流程更新 Python 包，在隔离 RD_STATE 配置控制端实际 LAN／VPN
地址后启动 serve（勿在实现会话部署或重启测试栈）。用 Unix socket 手动开窗口，
扫码后从 iPhone 校验 SPKI、保存 Keychain 令牌、读取接口并连 SSE；从 Mac 吊销，
核对立即断流和重连被拒。修改候选地址并由编排方重启，核对事件补读、同一指纹、
LAN／Tailscale／EasyTier 路由与只绑定勾选地址；退出后核对监听端口与 socket 已释放。
真实 APNs 需要用户密钥及可取消发送器，由编排方／端到端执行；本票测试从不连 Apple。
App 二维码、Keychain、实际握手固定校验、VPN 切换和真机推送不属于 Python 夹具验证。
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
## 会话接口（票 25）

会话路由在 `rd_serve_sessions.py`，所有 Herdr 子进程调用与保存快照读取在
`rd_herdr.py`。只调用已有 Herdr 的 `agent list`、`pane read`、
`pane send-text/send-keys` 与 `terminal session observe`，不启动或续接 harness。
Herdr 的环境和命名 session 沿用服务进程；可用 `HERDR_CONFIG_PATH` 指定配置文件。
实时状态来自 `agent list`；`session.json` 仅用于身份定位。
实时 `agent_session` 与滞后保存的身份冲突时，以实时身份为准；配置的本机目标执行目录
及已有本机派单的执行根下的 pane 不纳入发起会话。

- `GET /v1/sessions`：仅返回可映射到 Git 项目的 Claude Code、Codex、Kimi、AGY
  agent pane。`id` 等于不透明的 `pane_id`；路径使用该值。
  `status` 为 `working/idle/blocked/unknown`，`status_text` 为对应显示文字；
  Herdr `done` 映射为 `idle` 与 `has_new_reply=true`。
  `session_id` 不可得时为 null，模糊匹配时 `probable=true` 与
  `identity_text="可能是这个会话"`。
- `GET /v1/sessions/{id}/screen`：`{pane_id,text}`，当前可见 ANSI 屏幕。
- `GET /v1/sessions/{id}/terminal`：SSE 的 `event` 为 `terminal.frame` 或
  `terminal.closed`，`data` 保留对应 Herdr NDJSON 行的原始字节，不解码或重编码帧。
  这不是持久事件流，不提供 `Last-Event-ID` 补读；重连重新观察当前终端。
  断连或服务停止会结束并回收本次观察子进程。
- `GET /v1/sessions/{id}/messages?after=<消息ID>`：复用票 24 转换器，返回
  `supported/messages`；`after` 不含自身，未知游标返回 400。
  先按 Herdr v3 保存快照的 `agent_session {agent,kind,value}` 定位；支持 ID 和路径引用。
  读不到身份时才按 cwd 与修改时间匹配。Claude 读取
  `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl`（默认 `~/.claude`），Codex 读取
  `$CODEX_HOME/sessions/**/rollout-*.jsonl`（默认 `~/.codex`）。不支持的格式返回
  `supported=false/reason=unsupported_format/text="不支持"`，可继续使用终端。
- `POST /v1/sessions/{id}/input`：JSON 只接受 `{text}` 或 `{keys:[...]}`，必须带
  `Idempotency-Key`（1–256 个可打印 ASCII 非空白字符）。文本写入后发送 Enter；
  按键接受 `enter/esc/up/down/tab/ctrl+c`。本接口直接作用于原 pane。
  成功返回 `{pane_id,accepted:true}`。
- `GET /v1/jobs/{job}/session`：当前用户派单的会话反查，先匹配 `initiator_session`，
  再匹配 `initiator_herdr.pane_id`。返回 `session/matched_by`；都不存在时返回
  `session=null/reason=session_closed/text="会话已关闭"`。未知或其他用户派单返回 404。
- `GET /v1/projects`：合并持久索引与当前可见会话项目，`session_only` 区分仅会话项目；
  仅会话项目 `dispatch_count=0`，不写索引，会话关闭即消失。项目摘要由票 22 扩展。
  Herdr 不可用时仍返回持久项目；公开项目字段不包含 origin 凭据。

配置 `[serve] session_access=false` 关闭会话访问（默认 true）。关闭时所有会话路由及
反查返回 403 `session_access_disabled`，项目不混入仅会话项，普通事件流不泄露会话事件。
Unix socket 请求默认是本机。设备鉴权层须将可信的设备记录放在 handler 的
`session_device` 属性；设备记录缺少 `session_access=true` 时默认拒绝，HTTP 请求头不能
开启权限。真实配对与 HTTPS 接入由票 21 编排复测。

输入复用 serve 写入串行通道和独立 `locks/sessions/input.lock`，事件锁仍在最内层。
幂等结果按用户、可信设备与键隔离，保存于 `RD_STATE/sessions/inputs/`，不保存输入正文。
同键重试和服务重启均返回首次结果。跨外部写入崩溃时保留
409 `session_input_result_unknown`，不自动重发；Herdr 写入失败返回并记住
503 `session_input_unavailable`，需核对原 pane。输入无效返回 400 `invalid_session_input`；
输入锁竞争返回 409 `session_input_busy`。

后台采集追加 `session.changed`（含 `removed`）与 `session.message`
（仅消息 ID、种类和时间，不含正文），复用 `/v1/events` 的序号、轮转和补读。
输入成功也发布 `session.changed`，与首次幂等结果在同一事务发布，重试不重复发布。
查询本身不发布事件；Herdr 读取失败不当作会话关闭。新增 `contracts/v1/sessions-*.json`
给出本票请求、响应和事件样例；终端样例另用 `frames` 保存原始 NDJSON。

检查：`PYTHONPATH=src python3 -m unittest tests.test_rd_serve_sessions -v`。
测试将假 `herdr` 放在临时 PATH，使用临时配置、会话记录与短 Unix socket 路径。
编排方安装本票包后，在隔离环境复测四种 harness 的列表、状态、ANSI 帧、审批按键，
以及 App 消息增量和派单反查。不要把 Herdr 的 idle 当成菜单识别成功；Kimi 信任菜单
存在已知漏报。实现会话不连接真实 Herdr、不部署测试栈、不重启既有服务。
