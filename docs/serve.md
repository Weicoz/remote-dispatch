# 本机 Unix socket 服务

票 44 的进度接口：`GET /v1/jobs/{job}/rounds/{n}/progress` 返回
`{"progress": [{"updated_at": "时间", "phase": "阶段", "summary": "摘要"}]}`。
后台按轮次回传全部追加进度，按更新时间升序，同时间保持追加顺序；单条摘要上限仍为
8000 字，无百分比。旧任务没有追加文件时回退到最新一条；没有进度时返回空数组。
只读取当前用户可见派单，未知派单／轮次返回 404。查询不连接目标机、不写状态，
首次采集前为空；采集后的历史存于控制端 `serve-progress/`，断线与重启仍可读。
详情的 `progress_events` 补入轮询之间的全部进度，排序优先进度时间，重复采集不重发。

编排方合入后更新控制端包及后续轮次使用的 reporter，再按现有流程重启所管理的 serve。
在隔离派单中连续执行两次 `progress`，间隔小于实际轮询周期，等待下一次采集后读取
上述接口和派单详情，核对两条均存在；续跑后检查前轮历史仍可读。
Windows 真机经 PowerShell／SSH 的追加与回传、App 时间线显示由编排方端到端复测。
本实现会话不部署、不重启测试栈。

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
服务启动时读取配置和用户；运行中每 1 秒（`--poll-interval` 更小时按其值）检查
`RD_CONFIG` 的修改时间，变化后自动重载，不需要重启 serve。
重载先完整解析并校验候选配置；失败时保留旧配置和正在运行的采集。
重载只替换内存中的配置对象并唤醒后台采集，不重启 socket，也不取消已接受的请求或写命令。
会话派单默认值 GET 的 `config` 例外：每次只读当前本地配置文件，反映 CLI 派单时使用的默认值。

只读接口、字段和 SSE 帧以 [contracts/v1](../contracts/v1/README.md) 为准。
serve 可查看当前用户所有发起会话的详情、日志、回执；其他用户的 job 返回 404。
纯查询使用 `Query`、已有项目推断、签收和验收叠加，不写状态、不探测目标机、不收取回执。
所有派单、项目、统计和会话列表查询只读控制端本地状态与后台快照；
HTTP 请求不探测目标机、不发起 SSH、不收取回执，也不执行 Herdr 会话发现。
项目名称推断在一次查询内复用，避免逐卡片重扫全部派单。
日志端点立即返回缓存，并排队交给独立后台采集器读取最多 10000 行；
首次未就绪时 `text=""/snapshot_at=null`，客户端稍后重读。
采集失败后返回既有 503 `read_unavailable`，后续读取可触发后台重试。
日志缓存按派单代际、轮次与输出流隔离，不落盘；停止时等待采集器退出。
后台启动即采集，之后默认每 15 秒采集，可用
`--poll-interval` 设置正的有限秒数；周期是每轮采集完成后的等待时间。
`/v1/hosts` 启动即返回配置中启用的全部目标机，响应另带 `config_reload`
（与 `/v1/version` 同形，成功与失败都返回）；首次探测前
`probed=false`、`reachable=false`、`full=false`，资源数值均为 `null`，三项指标级别均为未知。
会话使用独立采集线程；派单刷新器先探测目标机并发布机器缓存，再批量采集派单。
慢派单不会延迟本轮机器结果，也不占用会话的刷新周期。
探测尝试后 `probed=true`，失败时 `reachable=false`，资源数值为 `null`；
客户端用 `probed` 区分尚未探测和探测不可达。无启用目标机时返回空列表。

每台目标机固定返回 `last_seen_at`（控制端时钟的带时区 ISO 8601 字符串）。
后台每次探测成功时更新，即使资源满载也记录；离线时保留最后一次成功时间，
从未成功时为 `null`。记录原子保存到 `RD_STATE/serve-host-last-seen.json`，
重启后首次探测前也可读取；损坏记录忽略，不妨碍服务启动。查询不触发探测或落盘。
时间更新不单独产生 `host.changed`，该事件继续只通知探测状态／资源变化。

票 59 由编排方更新控制端 Python 包并按现有方式重启 serve 后复测：
等待目标机一次成功探测，读取 `/v1/hosts` 的时间；令目标机离线并等待后台探测，
核对时间保留；重启 serve 再读，核对仍保留；从未成功过的目标机应为 `null`。
App 将此时间转换为本地 HH:MM，界面文案由编排方定稿。

`/v1/hosts` 的 `metrics` 提供 CPU、内存、磁盘的当前值、警告点、高压点与级别；
字段与状态样例见 [契约说明](../contracts/v1/README.md)。磁盘总量来自目标机执行目录
所在文件系统，与可用量在同一次查询取得；旧探针无总量时返回 null，仍可判断可用量级别。
POSIX CPU 使用 1 分钟负载除以核数，Windows 使用 CPU 百分比，不能混用。

警告点配置可写在 `[defaults]` 或各 `[host:名称]`，缺省值如下：

| 配置键 | 缺省警告点 | 高压点（沿用原配置） |
| --- | --- | --- |
| `warn_load_per_core` | 1.0 | `max_load_per_core`，缺省 1.5 |
| `warn_free_mem_pct` | 20% | `min_free_mem_pct`，缺省 10% |
| `warn_free_disk_gb` | 20 GB | `min_free_disk_gb`，缺省 10 GB |
| `warn_cpu_pct`（Windows） | 70% | `max_cpu_pct`，缺省 85% |

警告点只影响显示，不拦派单或续跑。高压点保持原来的严格越界比较：CPU 大于上限、
内存／磁盘空闲量小于下限才算高压；警告点含等号。高压级别优先，未触发高压时才看警告。
自定义拒绝线时应同时按需要调整警告点，使负载警告不高于上限、空闲量警告不低于下限。
警告配置必须是有限非负数；百分比须在 0–100。缺测或非有限数值级别为 unknown。
`full` 保留 assess 的所有拒绝原因（包括网络与必需指标缺测；swap 与换页已不参与判定，读数仍显示），离线时为 false。
派单状态在锁外按 host 批量查询；随后在对应 job 锁内重读并复核代际、查询进度并收取回执；
竞争或代际改变时跳过本轮。
网络探测与取回不持全局 writer 锁，其他 job 的写操作可继续。
同 job 的进程内协调先于共享写队列，等待该 job 的采集不会占住其他 job 的队列。
目标机使用现有探针和资源阈值。采集变化通过 `EventLog.observe_job/observe_host`
与 `collect_result` 发布；显示字段不变不追加事件。单个目标机失败不会中断其他采集。
执行快照保存在控制端 `RD_STATE/serve-snapshots/`，只保存显示字段；
服务重启后立即复用同一用户、轮次、目标机与执行根的最近观察。
首次采集前优先用该快照，再用本地回执、派单记录；完全未知才返回 starting。
卡片（含历史分页）与详情各轮增加可选 `snapshot_at`：控制端采集时间，
回退到回执时为 `captured_at`，未知为 null。它独立于业务 `updated_at`，
无状态变化的轮询只推进快照时间，不额外发布状态事件。
续跑／迁机后旧代际的快照和进度不沿用。查询不会写快照文件。
会话列表、项目内嵌会话与派单反查复用最近一次成功的后台发现；
冷启动尚未发现时为空，Herdr 读取失败保留上次成功列表，不当作会话关闭。
屏幕和终端流继续通过控制端本机 Herdr 读取，聊天消息只读本地转录。
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

## 常驻 serve（票 04）

`rdispatch service` 用用户级 LaunchAgent 托管常驻 serve，只支持 macOS。
Label 固定为 `io.github.weicoz.remote-dispatch.serve`，plist 写入
`~/Library/LaunchAgents/io.github.weicoz.remote-dispatch.serve.plist`。
常驻 serve 使用默认 socket `$RD_STATE/serve.sock`，plist 不设 `RD_SERVE_SOCKET`，
App 与 CLI 不带任何环境变量即可连上默认状态目录的 serve。plist 记录安装时的 `RD_STATE`；
CLI 仍按自己的 `RD_STATE`／`RD_SERVE_SOCKET` 连接，安装时用了自定义状态目录就要在两边保持一致。

```sh
rdispatch service install     # 生成并加载 plist；开机自启、崩溃拉起
rdispatch service status      # 加载状态、进程、socket、包版本、合同版本、配置重载结果
rdispatch service status --json
rdispatch service restart     # 重新加载 plist，升级 rdispatch 后用
rdispatch service uninstall   # 卸载并删除 plist，保留状态目录与日志
```

plist 记录安装时 shell 的 `RD_STATE`、`RD_USER_ID`（未设置时取 `default-user.json`）、
`RD_CONFIG`（仅当显式设置时写入，否则 serve 用默认配置路径）与 `PATH`
（launchd 默认 PATH 找不到 `herdr` 等用户级工具）。`ProgramArguments` 是当前运行
`rdispatch` 的那个入口加 `serve`：经 `rdispatch` 命令调用时写该入口的绝对路径，
用 `python -m remote_dispatch` 调用时写解释器与 `-m remote_dispatch`。`RunAtLoad` 与
`KeepAlive` 均为真；标准输出与标准错误分别写到 `$RD_STATE/serve.log` 与
`$RD_STATE/serve.err.log`；`install` 会先建好状态目录（新建时 0700，已存在时不改权限）。

写入沿用状态目录的原子替换：先写同目录临时文件并落盘，再 `os.replace`；plist 权限 0600，
只含路径与用户 ID，不含凭据。
`install` 与 `restart` 都执行 `launchctl bootout gui/$UID/<Label>` 再
`launchctl bootstrap gui/$UID <plist>`，因此重复 `install` 覆盖旧 plist 并重新加载。
`bootout` 在未加载时返回非零，按幂等处理；`bootstrap` 失败返回 4
并保留 plist，便于排查后重试。`uninstall` 在未安装时只做一次 `bootout` 并提示，
不删状态目录与日志；无法确认 launchd 已卸载（查询失败或仍被列出）时返回 4 和
`service_unload_failed` 并保留 plist，避免留下没有 plist 的孤儿 serve。

`status` 对已安装的 plist 与 `launchctl list` 求值：`loaded` 为 `true`/`false`/
`null`（未安装或 launchctl 不可用时为 `null`），`pid` 为 launchd 报告的进程号，
`running` 表示该进程仍存在。serve 在线时经 plist 的 socket 读取 `GET /v1/version`，
得到 `package_version`、`contract_version` 与 `config_reload`；serve 不在线时三项为
`null`，plist 与 launchd 状态照常报告。JSON 键：

| 键 | 含义 |
| --- | --- |
| `installed` / `plist` / `label` | 是否安装、plist 路径与固定 Label |
| `executable` | plist 中 `ProgramArguments` 的第一项（`rdispatch` 入口或 Python 解释器） |
| `state_dir` / `config` / `socket` | 状态目录、配置文件（未设置时 `null`）、serve 的 socket（plist 不可读或未写 `RD_STATE` 时为 `null`，此时不探测该 socket） |
| `log` / `error_log` | 标准输出与标准错误日志路径 |
| `loaded` / `pid` / `running` | launchd 加载状态、进程号、进程是否存活 |
| `serve_online` | socket 是否响应 `/v1/version` |
| `package_version` / `contract_version` / `config_reload` | 来自 `/v1/version`；离线时为 `null` |
| `cli_version` | 当前 CLI 的包版本，用于发现 serve 还是旧代码 |
| `plist_error` / `launchctl_error` | plist 无法解析、launchd 状态无法读取的原因，正常为 `null` |

文本输出在发现异常时追加一行 `hint`：plist 无法读取时提示重新 `install`；
serve 在响应但 launchd 未加载本任务时提示 `restart` 交给 launchd；serve 未运行或
未响应 socket 时给日志路径与 `restart`；serve 版本与当前 CLI 不同时提示 `restart`。
`--json` 只输出字段，不含提示行。

退出码：`status` 未安装返回 4，并在 stderr 提示 `rdispatch service install`；
`restart` 未安装返回 4；`install` 写盘或 launchd 失败返回 4；`uninstall` 无法确认
卸载返回 4；非 macOS 返回 4 和 `service_unsupported`。

dev serve 用独立的 `RD_STATE`（必要时再设 `RD_SERVE_SOCKET`）与常驻 serve 并存，
两者各自使用状态目录下的默认 socket，互不影响；同一个 `RD_STATE` 由服务锁独占，
第二个 serve 退出并报告 `serve_already_running`，日志在 `serve.err.log`。
升级 rdispatch 后 `restart` 会让新代码生效；可执行文件路径变化（例如换安装方式）时
要重新 `install`。

检查：

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_service -v
```

测试把 `HOME` 指向临时目录，在 `PATH` 前置假 `launchctl`（记录参数、按需返回 `list` 输出）
与假 `rdispatch`，断言 plist 内容、`launchctl` 调用序列、重复安装覆盖、卸载保留状态与日志、
未安装时的退出码与提示；在线用例启动真实 `rdispatch serve` 子进程读 `/v1/version`，
并核对两个状态目录各自使用默认 socket。测试不调用真实 `launchctl`，不写真实
`~/Library/LaunchAgents`，不启动、停止或替换任何正在运行的 serve。
真机安装、开机自启与崩溃拉起由编排方在 09 号票执行。

验证记录见 [validation/04-service-subcommand.md](validation/04-service-subcommand.md)。

## 版本接口（票 02）

`GET /v1/version` 是只读接口，经 Unix socket 读取；已认证的 HTTPS 客户端同样可读：

```json
{
  "package_version": "0.3.0",
  "contract_version": 1,
  "config_reload": {"ok": true, "at": "2026-10-28T00:00:00+00:00", "error": null}
}
```

`package_version` 与 `rdispatch --version` 一致。`contract_version` 是 `/v1` 合同的整数版本，
只在接口出现不兼容变化，或新增 App 依赖的字段时递增；同版本内只添加客户端可忽略的可选字段。
`config_reload` 记录最近一次配置加载：`ok` 为布尔值，`at` 是带时区的 ISO 8601 时间，
`error` 是失败时的脱敏文本、成功时为 `null`。成功时 `at` 是本次读取配置的时间，
失败时是失败时间；配置未变化时重复读取返回同一个启动加载时间。

接口处理本身只读内存中的版本与配置状态，不探测目标机；经 Unix socket 读取时
不读状态目录、不写文件。已认证 HTTPS 的设备鉴权层仍按既有规则更新最近访问时间。
重复读取返回相同的启动加载时间。样例见 `contracts/v1/version.json`。

```sh
curl --unix-socket "$RD_STATE/serve.sock" http://localhost/v1/version
PYTHONPATH=src python3 -m unittest tests.test_rd_serve_version -v
```

测试在临时 `RD_STATE`／`RD_CONFIG` 下启动真实 `rdispatch serve` 子进程，
经 Unix socket 读取并与合同样例比对。

## 配置自动重载（票 03）

serve 在后台检查 `RD_CONFIG` 的修改时间（含大小、inode 与权限）；变化后重新读取，
并在替换前校验所有 `[host:*]` 段、`[serve] execute_timeout`、`[serve] session_access`
和 `[monitor] report_merge_window_seconds`。
校验通过才一次性替换内存配置：新增或重新启用的目标机立即出现在 `/v1/hosts`，
禁用的目标机从列表移除，同名目标机修改后重置探测状态并由后台重新探测。
成功重载同时刷新命令执行超时、会话访问开关与汇报合并窗口；
`[serve] listen_addresses`、TLS 与请求超时等监听参数仍只在启动时读取，修改后需重启 serve。
验证失败或 INI 解析失败时保留旧配置、继续服务；`/v1/version` 与 `/v1/hosts` 的
`config_reload` 返回 `{"ok": false, "at": "失败时间", "error": "脱敏错误"}`。
错误来自配置校验时包含目标机与字段名；解析错误可能引用原始行，因此所有错误都经脱敏。
修好配置后，下一次文件变化会清除错误并返回成功时间。

重载只交换内存中的配置对象，不重启 Unix socket、采集线程或请求处理线程，
也不取消已接受的请求和转发写命令。`config_reload` 是客户端可忽略的新增字段，
合同版本保持 1。测试在临时 `RD_STATE`／`RD_CONFIG` 下启动真实 `rdispatch serve`
子进程，改写临时配置文件并用 CLI 与 Unix socket 驱动：

```sh
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_config_reload -v
```

样例见 `contracts/v1/hosts-config-reload-error.json`；失败时 `hosts` 继续返回
上一次成功加载的目标机。

## CLI 转交协议

本地 CLI 的 `run/resume/ack/review/stop/project`，以及同样会写 RD_STATE 的
`inbox/keep/clean`，先尝试连接同一 socket。`project rebuild` 也走转交。
没有 socket 或连接被拒绝时才直接执行现有路径。
设置类命令 `session-access`、`host` 的写操作在 serve 在线时直接调用
`/v1/config/session-access` 与 `/v1/config/hosts`，不走本节 `/_cli/v1` 通道；
连接成功后出错同样不自动改走直接写入。
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
和来源上下文；不修改服务进程的全局环境或输出流。
`[serve] execute_timeout=120` 配置每条 CLI 子进程的超时秒数，必须为正的有限数。
超时终止子进程并释放 writer；公开写接口保存待核对结果，不自动重试副作用。子进程仍调用原 CLI 命令实现，
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

## 设备与配对 CLI（票 05）

`device` 与 `pair` 子命令只经本机 Unix socket 调用 `GET /v1/devices`、
`DELETE /v1/devices/{id}`、`POST /v1/pairing-window` 和 `DELETE /v1/pairing-window`，
与 App 使用同一套 serve 逻辑；不读也不写 `network/devices.json`，不缓存配对码或令牌。
它们不走 `/_cli/v1` 转交，也不在 serve 不在线时回退为直接执行。

```sh
rdispatch device list [--json]
rdispatch device revoke <device-id> [--json]
rdispatch pair open [--json]
rdispatch pair close [--json]
```

`device list` 的人类输出为 ID、名称、配对时间、最近在线时间和状态（`active`/`revoked`）；
`last_seen_at` 尚未成功鉴权过时显示 `-`。`pair open` 的人类输出为配对码、剩余秒数与
绝对过期时间、证书指纹和候选地址；配对码只用于本次扫码或输入，不写日志。
`--json` 原样输出 serve 响应，字段见 `contracts/v1/network-devices.json`、
`network-pairing-window*.json` 和 `network-revoke.json`。

写请求每次生成新的 `Idempotency-Key`；重复 `pair open` 会使旧码失效，重复
`pair close` 返回 `{"open":false}`。serve 不在线、socket 不存在或连接被拒绝时，
四条命令都返回 4 和 `serve_unavailable`，提示 `rdispatch service install`，
不直接写任何文件。serve 返回的业务错误（如 `device_not_found`）同样返回 4，
错误写 stderr，`--json` 不输出半截结果。请求已发出但响应失败时返回
`serve_request_failed`，先核对状态再重试；`pair open` 不确定时先运行
`rdispatch pair close` 关闭可能已打开的窗口。

检查：

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_serve_device_cli -v
```

测试在临时 `RD_STATE`/`RD_CONFIG` 下启动真实 `rdispatch serve` 子进程，经 Unix socket
和 CLI 覆盖在线、离线、stale socket、配对窗口、设备列表与吊销、错误映射和 JSON 输出。
不连接或修改任何真实状态目录。编排方在真实控制端启动常驻 serve 后，用 CLI 打开窗口、
在 App 或手机完成一次配对、列出并吊销设备，确认 CLI 与 App 看到同一份设备状态；
本实现会话不部署或重启现有 serve。

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
| `/v1/runs` | `project, prompt`；可选 `harness, host, initiator_session, model, plan_item, after, suggestion` | 201：`job, round`；有工作线建议时先 200：`suggestion`（见下） |

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

### 工作线建议：两步派单

`/v1/runs` 可选传 `plan_item`（待派登记里的工作项）和 `after`（前序派单 ID 数组）。
传了 `plan_item` 且没有 `suggestion` 时，serve 先在同一用户、发起会话和参数下执行只读的
`suggest --json`：

1. 有建议：返回 200，body 为 `{"suggestion": {...}}`，内容与 `rdispatch suggest --json`
   相同（含 `line、previous、host、branch、items、fingerprint、token、expires_at`）。此时不派单。
   令牌单次有效、限时、绑定发起会话，所以确认请求必须带同一个 `initiator_session`。
2. 没有建议（`suggest=false`、建议命令失败或没有 `plan_item`）：直接按默认方式派出，行为与现在一致，返回 201 `job, round`。

App 确认后再次 POST `/v1/runs`，请求体与第一步相同，另加 `suggestion`：

- 接受：`{"action":"accept","line":"<线名>","token":"<令牌>"}`，可加 `"accept_chain":true`
  （本链后续工作项常驻同意）。令牌与接线条件在接线时再校验一次。成功返回 201
  `job, round, line`：`job` 是前序派单，`round` 是新追加的轮次。
- 拒绝：`{"action":"decline"}`。按默认方式派出新 job（201 `job, round`），并记「本链不再建议」。

确认请求是新请求，必须使用新的 `Idempotency-Key`。第一步的键配第二步的请求体会返回
422 `idempotency_key_reused`，不执行动作。`suggestion` 缺字段、动作未知，或没有
`plan_item` 时返回 400 `invalid_request`。建议响应里的 `token` 是唯一不做字段脱敏的
`token` 字段；它随幂等结果缓存在 `serve-writes/`，重放时可能已过期，过期后须用新键重新发起第一步。

错误结构为 `{"error":{"reason":"标识",...}}`：

- 400：`idempotency_key_required`（缺少／空白幂等键）、`invalid_request`（格式或字段无效）。
- 404：`not_found`，包括其他用户的派单。
- 409：`already_reviewed / round_in_progress / review_not_allowed`。
  `already_reviewed` 另含 `reviewed_by, reviewed_at, review_state`。
- 503：`receipt_host_unavailable`（签收或验收时回执未落盘且目标机暂时连不上，可重试）。
- 422：`resume_*`、`harness_change_not_allowed`、资源准入等命令失败；
  保留 CLI 稳定原因，未带稳定标识的错误为 `command_failed`。
  命令错误含脱敏 `message` 和原 `exit_code`；显示正文沿用摘要的 600 字符上限，
  超长时另含 `message_truncated=true`，稳定原因和结构化附加字段保留。
- 503：`write_unavailable`（写通道或存储不可用）、`write_outcome_unknown`（执行结果待核对）。

幂等结果按 RD_STATE、用户与可信设备保存到 `serve-writes/`，键经哈希，不保存请求正文。
缓存另存方法、路径与规范 JSON 请求体的 SHA-256 指纹（对象字段顺序不影响指纹）。
同键且指纹相同才返回首次 HTTP 状态码与 JSON；方法、路径或请求体不同返回
422 `idempotency_key_reused`，不执行动作；失败结果也缓存。旧缓存缺少指纹时拒绝复用，需换新键。
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
未通过下文写接口保存过选择时，启动读取此配置；之后使用 RD_STATE 中保存的选择。
直接修改 INI 不会覆盖已保存的选择，也不会触发运行中的服务重读。
`GET /v1/network`（仅 Unix socket）返回实际已绑定的 `candidates: [{address, port}]`
和 `spki_sha256`，另有可选字段 `listen_addresses: [{address, port, enabled}]`，
包含已禁用项，供设置页读回勾选状态。启动时与已记录列表不同才发布 `network.addresses_changed`，
事件携带相同的候选列表和指纹；清空配置也发布空列表。所有候选共用证书。

### 本机更新监听地址（票 39）

`POST /v1/network` 仅 Unix socket 可调用，沿用 `local_only` 鉴权边界；
已认证 HTTPS 请求返回 403 `local_only`。带 `Content-Type: application/json`、
非空 `Idempotency-Key`，body 为：

```json
{"listen_addresses":[{"address":"192.168.1.20","port":8443,"enabled":true},{"address":"100.90.1.20","port":8443,"enabled":false}]}
```

这是完整替换列表；`enabled` 缺省 true、`port` 缺省 8443，端口 0 让系统分配随机端口。
地址须为明确的 LAN／VPN／回环 IP，所有条目（包括禁用项）都拒绝通配、公网、主机名、
重复项及无效字段。空列表或全禁用时只保留 Unix socket，禁用项仍可从 GET 读回。
成功 200，body 与 `GET /v1/network` 相同，包含 `listen_addresses/candidates/spki_sha256`。
配置端口 0 在 `listen_addresses` 中仍为 0，`candidates` 返回实际绑定的端口。

接口复用网络状态锁与文件锁，将选择和首次响应保存在私有
`RD_STATE/network/devices.json`，重启后优先于 INI。相同本机调用方同键返回首次状态码
和 body（包括失败），即使正文或网络管理路径改变；重放不重新绑定、不补发事件。
重启后端口 0 的实际端口可能改变，当前地址以 GET 为准。
新增监听成功、状态和事件写入成功后，才释放被取消的监听器；未改变的监听器保留。
失败保留原选择、原监听器和候选列表，回收本次新增监听器。
已接受的 HTTPS 连接可继续使用，设备吊销和服务退出仍会关闭连接。
候选实际列表变化时发布 `network.addresses_changed`，字段与读取候选事件相同，
所有地址使用原有证书指纹；仅改禁用项或原样保存不产生地址事件。

错误 reason：400 `idempotency_key_required / invalid_request / invalid_listen_address`；
409 `listen_address_unavailable`（地址不属于本机或端口占用）；
503 `network_unavailable`（证书、存储或事件不可用）或 `serve_stopping`。
业务失败结果可换新键重试；存储／事件失败会回滚，修复后可用原键重试。
契约为 `contracts/v1/network-addresses-write*.json`，测试覆盖即时绑定、释放、
安全校验、幂等重放、重启恢复、失败回滚与 SSE 事件。

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
重启后继续有效；方法、路径或规范 JSON 请求体指纹不同返回 422 `idempotency_key_reused`。
配对入口单独划分调用方。鉴权失败不持久缓存。配对码与设备令牌只在首次响应返回，
相同配对／开启窗口请求重放返回 403 `pairing_closed`，需用新键手动开启新窗口。
设备状态和幂等结果保存在私有 `network/devices.json`（0600）；缓存仅保存指纹与
无秘密的响应占位，不保存配对码或设备令牌明文，设备令牌仅存 SHA-256。
启动时清除旧格式缓存响应中的秘密；APNs token 仍供发送器使用，不可导出该文件。
吊销状态持续落盘，重放不能恢复已吊销令牌。
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
核对立即断流和重连被拒。用 Unix socket 调用 `POST /v1/network` 修改勾选地址，核对即时重新绑定、失败保留原选择、
同键重放与重启恢复、事件补读、同一指纹、
LAN／Tailscale／EasyTier 路由与只绑定勾选地址；退出后核对监听端口与 socket 已释放。
真实 APNs 需要用户密钥及可取消发送器，由编排方／端到端执行；本票测试从不连 Apple。
App 二维码、Keychain、实际握手固定校验、VPN 切换和真机推送不属于 Python 夹具验证。
## 项目、历史分页与 harness 统计（票 22）

`GET /v1/projects` 合并当前用户的持久索引、当前派单与保留的历史记录，不写索引。
返回 `projects`，每项保留索引字段并增加：

| 字段 | 含义 |
| --- | --- |
| `needs_attention_count` / `in_progress_count` | 卡住 + 离线 + 失败之和／starting、running 派单数 |
| `blocked_count` / `offline_count` / `failed_count` | 需要用户处理的卡住数／离线数／失败数；仅会话项目均为 0 |
| `recent_event` | 此项目最近的可见事件；没有时为 null |
| `hosts` | 按目标机名索引，值含 `state/execution_state/job/updated_at/accepted_round_count`；无派单的已配置目标机为 none |
| `week_dispatch_count` | 控制端本地时间本周一零时以来的新派单数，续跑不重复计数 |
| `last_accepted_at` / `last_harness` | 最近通过的时间／最近派单的执行 harness；未知为 null |
| `session_only` / `sessions` | 本票为 false／空数组，供票 25 的仅会话项目接入 |

目标机小格的 `state` 使用执行状态词表：`blocked`（卡住）、`failed`（失败）、
`offline`（离线）、`running`（执行中）、`completed`（已完成）、`none`（无）。
注意优先级依次为卡住 > 失败 > 离线 > 执行中 > 已完成 > 无。
`starting/running` 为执行中；`unreachable` 为离线；`dead/missing` 为失败；
其他已结束轮次按回执 `outcome=blocked/failed/completed/partial` 映射，partial 为已完成。
`execution_state` 保留底层值，供客户端区分 `dead`（异常中断）、`missing`（已丢失）、
`unreachable`（离线）；无派单时为 null。非零执行退出码同样归为失败。
签收不改变执行状态，也不改变卡住／失败计数。已完成且待会话验收不计入需要你；
主动停止、已通过／已作废的轮次不计入三项，机器格保留其执行状态，但降到已完成的注意优先级。
同级按更新时间倒序，包含进度／报告、签收、验收时间；并纳入最近一次状态变更事件的时间；
时间相同时按 job ID 升序。会话等待输入是独立的会话状态，不改变派单分项计数。
项目按名称和 ID 稳定排序，“未归属”在最后；origin URL 的 userinfo 不对外返回。
缺少元数据的历史只返回已知内容，不推测 harness、时间或原用户。

`hosts[目标机].accepted_round_count` 是当前用户在此项目、此目标机上明确验收为
`accepted` 的累计轮次数；不是 `outcome=completed` 或 `checks=passed` 的次数。
签收、待验收、打回和作废均不增加。每轮按保留回执的 `host` 归属，旧回执未记录
目标机时回退到派单元数据；迁机续跑不会把已有回执的通过数移到新目标机。
历史通过的目标机即使已从配置删除仍返回；没有通过记录的目标机为 0。
统计读取派单／回执及逐轮验收，不依赖事件日志；清理 job 登记后仍可读回。

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
- `total_round_count`：此 harness 的累计已结束且保留回执的轮次数，包含失败、
  打回、作废和未知结束时间；不包含尚无回执的执行中轮次，不受 recent=N 限制。
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

响应还增加 `date_basis="controller_local"` 与 `host_stats`。后者按目标机名排序，
每项含 `host` 和 `daily_round_counts: [{date: "YYYY-MM-DD", round_count: 整数}]`。
日序列固定 14 项，按日期升序，含控制端本地今天和前 13 个日历日，空日补 0；
今天只累计到查询时刻。按每轮结束时间转换为控制端操作系统本地时区后归日，
正确处理夏令时的 23／25 小时日；不使用目标机时区，也不以固定 24 小时切桶。
无时区的旧时间沿用控制端本地时间解释。结束时间的回退顺序与上面一致；
无效／缺失时间和未来结束时间不进入日计数。所有 harness 和普通命令的回执均计入，
仍执行中且没有回执的不计入。目标机归属规则与项目通过数相同，包含已配置及
当前用户保留历史中的目标机；空目标机列表返回空数组。统计始终只取当前用户，
兼容既有无用户字段的旧记录，不按发起会话过滤。

`harness.stats_changed` 新增 `total_round_count/date_basis/host_stats`，
让客户端同时刷新履历和机器日序列。没有具名 harness 但存在目标机时，后台发布
`harness=null`、只含用户及机器日序列的统计事件，不补造 harness 历史；
HTTP 的 `harness_stats` 仍为空。首次后台观察、轮次／验收变化和跨本地零时
导致持久统计变化时才发布，执行中秒数变化不触发。
项目累计通过数变化时，后台按用户＋项目＋目标机发布 `project.changed`，新增
`host/accepted_round_count`；保留现有项目标量字段。没有通过记录时不额外发统计事件。
索引变更事件与逐目标机统计事件分别持久去重，重启、轮转或两个项目交错轮询不重复发出。

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
`rd_herdr.py`。只调用已有 Herdr 的 `agent list`、`pane layout`、
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
  可选 `updated_at` 是当前用户、同一 pane／会话 ID／harness／项目最近一次
  `session.changed` 或 `session.message` 的控制端事件 `time`（带时区 ISO 8601），
  包含轮转段，重启后仍可读取；不是消息正文时间、查询时间或 Herdr 快照文件修改时间。
  尚无匹配事件时为 null；pane 换会话或移除后不沿用旧会话时间。
  项目内嵌会话和派单反查使用相同字段，供 App 冷启动时与派单按更新时间混合排序。
- `GET /v1/sessions/{id}/screen`：`{pane_id,text,width,height,cursor}`，当前可见 ANSI 屏幕。
  `text` 取按 pane 真实尺寸启动的只读 observer 首个完整帧，包含清屏、定位和样式序列；
  `width/height` 是字符单元格数，`cursor={x,y,visible}` 为从 0 起算的列、行及可见性。
  光标取自该帧 ANSI 末尾的 CUP 与 `?25h/l`，与屏幕正文属于同一帧。
- `GET /v1/sessions/{id}/terminal`：SSE 的 `event` 为 `terminal.frame` 或
  `terminal.closed`，Base64 帧正文先解码、脱敏再编码；ANSI 控制序列和帧元数据保留。
  新服务固定输出 `type/seq/encoding/width/height/full/data/cursor`（示例见合同）；
  `data` 是 Base64 ANSI，兼容上游 `bytes` 或 `data` 字段，统一输出 `data`。
  OSC 标题等控制序列正文也按相同规则脱敏，新增字段仍走敏感扫描。
  连接前从 `herdr pane layout --pane <id>` 的目标 pane `rect.width/height` 读取尺寸，
  显式传入 `observe <id> --cols <width> --rows <height>`，不使用整个 tab 的 area。
  旧调用遗漏参数，Herdr 0.9.3 默认 120×40，因而出现与真实 pane 不同的换行和截断。
  活跃流沿用 serve 的 `--poll-interval` 检查 layout（默认 15 秒），静默 pane 也检查；
  尺寸变化时回收旧 observer，再以新尺寸重连，首帧为 `full=true`。
  此过程中不向 Herdr 发送 resize、不改变真实 pane 尺寸；当前 HTTP 流的 `seq` 保持递增，
  App 收到全量帧应清空旧网格并重绘。检查失败或上游尺寸不匹配时终止流，客户端重连。
  这不是持久事件流，不提供 `Last-Event-ID` 补读；重连重新观察当前终端。
  断连或服务停止会结束并回收本次观察子进程。
- `GET /v1/sessions/{id}/messages?after=<消息ID>`：复用票 24 转换器，返回
  `supported/messages`；`after` 不含自身，未知游标返回 400。
  先按 Herdr v3 保存快照的 `agent_session {agent,kind,value}` 定位；支持 ID 和路径引用。
  读不到身份或 session ID 未命中文件时，按 cwd 匹配并选修改时间最新的转写，
  此时 `probable=true/identity_text="可能是这个会话"`；没有同 cwd 候选时不借用其他项目。
  显式路径引用未命中不回退。Claude 读取
  `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl`（默认 `~/.claude`），Codex 读取
  `$CODEX_HOME/sessions/**/rollout-*.jsonl`（默认 `~/.codex`）。未知顶层记录、未知
  Codex `response_item` 子类型及额外字段跳过，保留已识别消息；Claude 无字符串
  `content` 的 system 生命周期记录跳过。Codex 跨代理消息、密文及压缩替换历史不进入聊天。
  损坏 JSON、非对象、缺失或非法的 type、已识别消息缺必需字段或内容块非法时，
  整次返回 `supported=false/messages=[]/reason=unsupported_format/text="不支持"`，不返回部分正文，
  可继续使用终端。agy/kimi 尚无转写读取器，仍返回相同 unsupported 响应。
- `POST /v1/sessions/{id}/input`：JSON 只接受 `{text}`、`{text,submit}` 或 `{keys:[...]}`，必须带
  `Idempotency-Key`（1–256 个可打印 ASCII 非空白字符），逐键输入也每次带新键。
  `submit` 必须是 JSON 布尔值，省略或 `true` 时文本写入后发送 Enter，保持聊天页行为；
  `false` 只发送文本，不追加 Enter，可用于逐字回显、粘贴、输入法已提交文本及与 Tab 混用。
  文本仍须非空、不含 NUL；`submit` 不能用于 keys 请求，未知字段一律拒绝。
  按键接受 `backspace/left/right/ctrl+d/shift+tab/ctrl+a/ctrl+u/space/enter/esc/up/down/tab/ctrl+c`。
  `delete/home/end/pageup/pagedown` 在已实测 Herdr 中为 unsupported，不加入白名单；
  其他键名和大小写变体返回 400 `invalid_session_input`。本接口直接作用于原 pane。
  成功返回 `{pane_id,accepted:true}`。同一幂等键仍重放首次结果（包括改变 submit 的重试），
  不重复写入、不重复发事件；落盘只存结果、不存输入正文，失败／不确定结果不自动重发。
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
快照的 `updated_at` 从上述事件派生，不放入 `session.changed` 的变化比较，
因此无变化的采集、普通查询及输入幂等重放不会推进更新时间。

检查：`PYTHONPATH=src python3 -m unittest tests.test_rd_serve_sessions -v`。
测试将假 `herdr` 放在临时 PATH，使用临时配置、会话记录与短 Unix socket 路径。
编排方安装本票包后，在隔离环境复测四种 harness 的列表、状态、ANSI 帧、审批按键，
以及 App 消息增量和派单反查。不要把 Herdr 的 idle 当成菜单识别成功；Kimi 信任菜单
存在已知漏报。实现会话不连接真实 Herdr、不部署测试栈、不重启既有服务。


### 连接、推送与读取资源边界

`[serve] tls_handshake_timeout=5` 限制 TLS 握手等待秒数，
`request_timeout=15` 限制 HTTPS handler 的 socket 读写等待秒数；均须为正的有限数。
`max_connections=32` 为每个监听器同时处理的连接数上限（正整数，含握手和 SSE）；
超限连接立即关闭，不创建额外 handler 线程。这些设置不影响本机 Unix socket。
推送失败按 0.2、0.4、0.8…秒指数退避，上限 30 秒，成功恢复 0.1 秒轮询。
失败批次留在内存重试，成功后继续下一批；读取保存事件段字节偏移，包含轮转段。
会话发现复用同样的增量事件游标；转录保存文件身份、字节与行偏移、工具调用映射，
只解析追加的完整 JSONL 行，文件替换／截短则重建缓存；查询仍不写持久状态。
终端帧先 Base64 解码，再按公开正文规则脱敏并编码，保留 ANSI 控制序列和无敏感内容的原帧。

票 67：本机 Unix socket 的 accept 队列为 128，同时处理的列表 GET 上限为 32；
未完成 HTTP 请求和 SSE 不占列表处理槽。列表按文件代际、最新事件序号、执行快照版本、
会话版本及配置缓存，重建由一个线程完成，后台采集完成后预热。
每次读取仍检查本地文件身份、大小、修改时间与变更时间，无 TTL 陈旧窗口；
回执、签收与验收按读视图批量加载，只有变化的 JSON 重新解析。
当前派单视图只加载当前轮所需文件，历史分页继续保存独立快照。
较新的同轮同目标机状态事件立即更新列表；较新的回执或记录文件使旧事件失效，
目标不可达事件不会覆盖已有终态回执。SSE 使用保留字节偏移的事件游标，
Reports 复用增量事件游标及派单文件缓存，空轮询不重放历史事件或重新解析全部派单。

票 57 补充会话快照字段：`/v1/sessions`、`/v1/projects` 的 `sessions[]` 与会话反查
使用同一行对象，固定带 `cwd` 和 `updated_at`。`cwd` 取 Herdr `foreground_cwd`，
缺省取 pane 的 `cwd`；实时目录读不到时返回 null，可用 Herdr 已保存的目录定位项目，
不把保存值冒充实时目录。无法定位项目的 pane 继续不进入项目会话列表。
目录正文沿用配置敏感值脱敏；`updated_at` 沿用会话事件时间，未知为 null。

## 目标机配置（票 64）

`/v1/config/hosts` 与指标接口 `/v1/hosts` 分开：配置读取每次重新读取控制端
`RD_CONFIG`（默认 `config.ini`），包含禁用的 `[host:名称]`，不联系目标机。
Unix socket 及现有配对设备的 HTTPS 均可访问；HTTPS 继续要求证书 pin 和设备令牌。

| 方法 | 路径 | 请求与结果 |
| --- | --- | --- |
| GET | `/v1/config/hosts` | `200 {hosts: [...]}`，按配置段顺序 |
| POST | `/v1/config/hosts` | `{name, ssh, sync?, workdir?, ...}`；新增，返回 `201 {host: ...}` |
| PUT | `/v1/config/hosts/{name}` | 只更新提交的字段；返回 `200 {host: ...}` |
| DELETE | `/v1/config/hosts/{name}` | 空正文或 `{}`；返回 `200 {deleted: name}` |

每个 host 是 `{name, values, protected_fields}`。`values` 返回合并内置默认值、
`[defaults]`、目标机段后的普通字段，值沿用 INI 的字符串类型，包含 ssh、sync、
workdir、max_heavy、platform、enabled 等。platform 保留配置原值 macos/linux/posix/windows。
`protected_fields` 每项为 `{field, is_set}`；env_file、各 auth_file、ssh_opts 及
未列入普通字段白名单的扩展字段只返回字段名和布尔设置状态，不返回路径或凭据。
普通字段也经过凭据和私钥文本脱敏；ssh 保留正常连接地址，移除 URL 中嵌入的认证内容。
认证字段可在写请求中提交新值；未提交的字段保持原值，不需要 App 读回秘密。
PUT 中 `null` 移除本段覆盖（恢复继承），空字符串显式清空；继承仍有值时 is_set=true。
名称区分大小写，长度 1–128，只允许 ASCII 字母、数字、点、下划线和短横线。
名称是稳定标识，PUT 不重命名；新名称通过新增配置建立。

所有写请求必须带 `Idempotency-Key`，长度不超过 255；JSON 正文最多 8 MiB。
相同用户、设备、键与方法／路径／规范 JSON 正文重放首次成功结果；同键不同请求返回
422 `idempotency_key_reused`。持久缓存仅保存指纹和脱敏响应，不保存请求正文。
在备份或替换阶段中断时，同键返回 503 `host_config_outcome_unknown`，先 GET 核对再用新键操作。
校验失败和冲突不会修改配置或建立备份，可以修正后重试。

必填字段按继承后的有效值校验：ssh、workdir 不得为空；sync 仅 origin/direct；
platform 仅 macos/linux/posix/windows；本机 ssh=local 不支持 windows，workdir 须为
绝对路径或以 ~、$HOME 开头。enabled 接受 1/0、true/false、yes/no（忽略大小写），
launcher 为 nohup/tmux；资源数值须有限非负，百分比不超过 100，max_heavy、timeout
须为正整数。写入值为字符串或 null，不能包含换行或 NUL；密钥内容应保存在凭据文件中。

写入与现有 CLI writer 串行，并用配置旁的 `config.ini.lock` 跨进程互斥。
先完整解析和校验候选文件，随后以 `0600` 创建独立的 `config.ini.bak.<随机标识>`，
写入并 fsync 原始字节。备份成功后才在同目录写临时文件、fsync，再用原子 replace 替换配置，
新文件也为 `0600`。备份失败时保留原配置；替换失败时保留原配置与备份，清除临时文件。
保留所有无关段落的原字节、未修改配置项、整行注释及原换行类型；删除目标机后仍保留其整行注释。
不支持配置文件或父目录的符号链接；文件管理器直接写文件不参与此互斥协议，应避免与接口同时编辑。
备份包含原凭据配置，由管理员按现有私密配置保管方式保存，不自动清理。
写成功立即更新 serve 的配置对象并唤醒后台采集；下一次配置 GET 立即反映变化，无需重启。
指标 `/v1/hosts` 继续遵循原有后台采集语义。

删除检查当前 RD_STATE 中全部用户的本地登记任务，只有 done/dead/missing 被视为已结束；
starting/running/unreachable 或未知状态拒绝删除，避免离线时误判。检查只读快照／回执，
不执行远端探测；其他用户的任务标识不出现在错误中。已结束任务的历史记录仍然保留；
以后若要在该机续跑，需先恢复同名配置。

错误结构沿用 `{error: {reason, field?}}`，不包含配置值：

| 状态 | reason | 含义／建议占位文案（由编排方定稿） |
| --- | --- | --- |
| 400 | `idempotency_key_required` | 请提供有效的请求标识 |
| 400 | `invalid_host_request` | 目标机配置请求格式无效 |
| 400 | `host_field_required` | 请填写必填字段（field） |
| 400 | `invalid_host_field` | 请检查字段取值（field） |
| 404 | `not_found` | 目标机不存在 |
| 405 | `method_not_allowed` | 此路径不支持该操作 |
| 409 | `host_exists` | 目标机名称已存在 |
| 409 | `host_has_active_dispatches` | 目标机仍有进行中或状态未确认的派单 |
| 409 | `host_config_busy` | 配置正在写入，请稍后重试 |
| 422 | `idempotency_key_reused` | 请求标识已用于其他操作 |
| 503 | `host_config_unavailable` | 无法读取或保存目标机配置 |
| 503 | `host_config_outcome_unknown` | 操作结果未确认，请刷新后核对 |

合同样例见 `contracts/v1/host-config-*.json`。App 合同副本由编排方随后同步。

### 双向真实终端（票 70）

`GET /v1/sessions/{id}/terminal/ws` 提供标准库 WebSocket → Herdr session control 桥接。
鉴权、消息、lease、脱敏、错误码和 App 接入见 [终端 WebSocket](terminal-websocket.md)。
现有 terminal SSE 与 HTTP input 保持兼容。


### 会话采集与派单刷新隔离（票据 72）

会话采集使用独立后台线程，默认仍在每次采集完成后等待 15 秒；host 探针、历史派单
SSH、结果／进度收取不会占用这个线程。本地写操作仍会唤醒两个刷新器。退出时唤醒并
等待采集线程退出，再释放 Herdr 和服务锁。采集自身耗时仍会影响其实际周期。

`GET /v1/sessions` 保留原 `sessions` 含义，新增 `collection`：

| 字段 | 含义 |
| --- | --- |
| `last_attempt_succeeded` | 最近一次完整采集是否成功；启动后尚未完成为 `null` |
| `consecutive_failures` | 连续失败次数；成功重置为 0 |
| `stale` | 连续失败达到 3 次时为 `true`；成功立即恢复为 `false` |
| `reason` | 失败的异常类别（例如 `OSError`），成功或尚未采集为 `null` |
| `last_success_at` | 最近成功采集的 UTC 时间；从未成功为 `null` |

失败保留最近成功的列表；成功返回空列表会清空会话并产生原有的 removed 事件。
失败日志仅写异常类别和连续次数，不写异常正文、终端正文、路径或凭据。客户端可用
`collection.stale` 显示占位文案键 `sessions.list_stale`（占位：“会话列表已过期，正在重试”），
由编排方定稿；本票没有修改 App。合同见 `sessions-list.json`、`sessions-stale.json`。
坏转写的 KeyError／TypeError 返回已有的 `unsupported_format`，不会中断其他 pane 采集；
下一次读取会丢弃部分转换缓存重新读取。同次发现对每种 harness 只扫描一次转写目录，
下次发现重新扫描，新增文件不会因缓存永久遗漏。

派单状态按 host 批量查询（保留 user_id／worker_root 的传输隔离），网络查询期间不占用
派单锁；收取前在锁内复核 `(rounds, host, worker_root)`。同一 generation 的终态快照
配合本地终态回执和匹配的收取确认后，停止历史派单的状态／进度／回执远程探测；
新轮次或执行位置改变后重新查询。未确认的回执继续刷新，保留现有收取语义。

不可达的 host 不再逐条查询历史派单；探测或批量状态查询失败后，按现有 poll 周期的
1、2、4、8、16 倍退避（上限 16 倍）。连续三次失败后处于熔断等待，期限到时允许探测；
成功后在同一轮立即查询派单并清除退避。禁用 host 的新派单不影响既有派单的刷新；
批量响应缺项仍按原语义记为 `unreachable`。本地写唤醒不绕过等待；连接配置改变后立即
允许新探测。离线期间保留最后一次 host 观测和派单快照。这里没有修改传输超时；
目标环境的恢复等待和刷新时延仍需实测，由编排方判断是否需另票调整周期策略。

部署与真实环境复测由编排方执行：部署本票提交到测试栈，按现有方式重启 serve；
准备约 200 条历史派单（多数指向受控不可达目标），记录 `GET /v1/sessions` 的时序，
新增／关闭受控 pane，确认默认两个会话周期内可见；检查离线 host 的 SSH 次数按退避
减少，恢复后首次成功探测的同轮刷新派单。合成 Herdr 失败三次，确认旧列表、过期字段
与脱敏日志；恢复为零 pane 后确认空列表且 `stale=false`。确认终态且已确认收取的派单
不再被探测，resume 后重新刷新。最后检查 App 对新增字段兼容及过期文案展示。


## 设置接口补充（票 73）

这些管理操作只通过当前用户的 Unix socket 暴露；HTTPS 即使已配对也返回
403 `local_only`。设备级会话权限保持原接口和默认关闭行为。

| 接口 | 请求与响应 |
| --- | --- |
| `GET /v1/config/session-access` | 返回 `{"enabled":true}`，读取运行时 `[serve] session_access`，缺省 true |
| `PUT /v1/config/session-access` | JSON `{"enabled":false}`，必须是布尔值且无其他字段；成功返回新值 |
| `DELETE /v1/pairing-window` | 无 body 或 `{}`；返回 `{"open":false}`，立即使当前码失效；不吊销已有设备 |

写操作要求 `Idempotency-Key`。总开关沿用目标机配置的锁、幂等缓存、0600 备份及
fsync 后原子替换，保留配置注释、换行与其他段。持久化成功后替换运行时配置，
无需重启；所有会话读写、终端与会话事件立即服从新开关，设备级权限仍须单独允许。
请求验证失败不写配置；备份或替换失败返回 503 `host_config_unavailable`，保留原配置
及运行时开关。同键异请求为 422 `idempotency_key_reused`；写入结果无法确认时沿用
503 `host_config_outcome_unknown`，先 GET 确认实际值再用新键重试。
关闭窗口幂等重放只返回首次结果，不影响后来新开的窗口。服务重启不恢复配对窗口。

`GET /v1/devices` 每项新增 `paired_at`、`last_seen_at`：带时区 ISO 8601 字符串，
新配对时记录配对时间；尚未收到认证请求时最近时间为 null。旧设备未知的配对时间
返回 null，不根据文件修改时间推测。`last_seen_at` 取设备最近一次成功通过指纹和
令牌鉴权的请求时间，包含随后因业务权限被拒绝的请求；鉴权失败和未鉴权的配对请求
不刷新时间，持续 SSE/WS 连接只计最初鉴权，不计心跳。查询立即合并内存最新值。
后台每 60 秒合并写盘，无更新不写；正常停服也合并一次。异常退出最多丢失最近一个
刷盘周期尚未保存的更新，写盘失败保留内存值并在下周期重试。更新不发设备变更事件，
后台 fsync/replace 不占前台鉴权／查询锁，与设备写操作通过既有 NetworkLock 串行，
避免覆盖吊销、权限或幂等记录。

`GET /v1/network` 的 `candidates` 与 `listen_addresses` 每项新增 `kind`；写监听地址
的响应、配对窗口候选及新的 `network.addresses_changed` 事件也返回该字段。
不以接口名、DNS 或外部服务识别，按下表顺序首次匹配；自定义 VPN 网段可能归入 lan。
输入中传入 kind 不影响服务端判断。旧事件可无 kind，客户端收到事件后可重读网络接口。

| kind | 网段／规则 |
| --- | --- |
| `tailscale` | `100.64.0.0/10`、`fd7a:115c:a1e0::/48` |
| `easytier` | `10.144.144.0/24`、`10.126.126.0/24` |
| `lan` | `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16`、`fc00::/7`、`fe80::/10` |
| `other` | 其余地址，包括回环；原有监听地址安全校验不变 |

删除目标机返回的 409 `error.reason=host_has_active_dispatches` 不变，新增
`error.running_count`（该目标机所有用户进行中／未知状态派单总数）、
`error.running_job_ids`（按 ID 排序，最多 20 个）。这是管理接口的全局阻止信息，
不使用当前用户的派单列表推算；done/dead/missing 沿用原规则不阻止删除。

目标机 `max_heavy` 沿用字符串配置字段，整数且 >=1 即可，不设 8 的上限；
JSON 数字、布尔值、小数串及小于 1 的值拒绝，400 `invalid_host_field`，
`field=max_heavy`，新增可读 `text`。null 沿用原有清除覆盖、继承默认值的含义。
待编排方定稿的文案键：`error.invalid_host_field.max_heavy`，占位文字
“并发上限必须是大于等于 1 的整数。”

合同样例见 `session-access-*.json`、`network-pairing-window-close.json`、
`network-devices.json`、`network-addresses*.json`、`host-config-active.json` 和
`host-config-max-heavy-invalid.json`。实现检查及编排方复测步骤见
[票 73 验证记录](validation/73-serve-admin-gaps.md)。

## 设置类 CLI 子命令（票 06）

不开 App、不手改 `config.ini`，用下列命令管理会话访问总开关和目标机。命令名用
`serve`／目标机术语，不把 serve 叫“后台服务”。

```sh
rdispatch session-access            # 查看，缺省为开
rdispatch session-access show       # 同上
rdispatch session-access on         # 打开
rdispatch session-access off        # 关闭
rdispatch session-access on --json  # {"enabled": true}

rdispatch host list --json          # {"hosts": [{name, values, protected_fields}]}
rdispatch host add NAME --ssh SSH [--workdir DIR] [--sync origin|direct]
                       [--platform posix|macos|linux|windows] [--enabled true|false]
                       [--launcher nohup|tmux] [--tags TAGS] [--set FIELD=VALUE ...] [--json]
rdispatch host update NAME [同上] [--unset FIELD ...] [--json]
rdispatch host delete NAME [--json]
```

`host list` 包含禁用的目标机，返回结构与 `GET /v1/config/hosts` 相同。凭据类字段
只返回 `protected_fields` 的字段名和是否设置，不回显路径或内容；普通字段中出现其他
目标机的凭据文本时替换为 `[redacted]`。`host add/update --json` 返回
`{"host": {...}}`，`host delete --json` 返回 `{"deleted": "名称"}`。
`--set` 用于设置任意合法字段，`--unset` 只用于修改，表示移除本段覆盖并恢复继承；
`--set` 与 `--unset` 同时给出同一字段会拒绝。名称用位置参数指定，不能改名。

serve 在线时，以上命令都经 Unix socket 调用现有 `/v1/config/session-access` 与
`/v1/config/hosts` 接口；写操作立即更新 serve 的运行时配置并唤醒采集，读操作读取
serve 当前配置。serve 不在线
（socket 不存在或连接被拒绝）时直接读写 `RD_CONFIG`。CLI 请求带
`X-Rdispatch-Config-SHA256`（配置绝对路径的 SHA-256，避免非 ASCII 路径无法放进
HTTP 头）；serve 发现它与自身配置路径不一致时返回 400 `cli_config_mismatch`，
不写任一配置。在线请求 30 秒超时，超时按 `cli_forward_failed` 退出 4，不自动改走
直接写入。两条路径共用
`rd_host_config.py` 的解析、校验、备份和原子替换：先完整校验候选文件，再以 `0600`
创建独立的 `config.ini.bak.<随机标识>` 并 fsync 原始字节，随后在同目录写临时文件、
fsync，再原子替换。保留未修改的段落、注释、换行与禁用项。备份失败不写配置；
替换失败保留原配置与备份并清理临时文件。

删除目标机时检查当前 `RD_STATE` 中全部用户的本地登记任务；有
starting/running/unreachable 或状态未知的派单时拒绝，退出非零并输出
`host_has_active_dispatches`、`running_count` 与最多 20 个 `running_job_ids`，
语义与 serve 的 409 相同。done/dead/missing 不阻止删除。检查只读本地记录与回执，
不执行远端探测。

serve 在线时写入失败不自动改走直接写入：连接成功后请求、读取或解析失败时退出 4 并
提示 `cli_forward_failed`，先核对状态再重试。校验错误退出 4；不存在、冲突、忙碌或
配置不可用退出 1。错误文案保留 serve 的 reason 与字段名，例如
`invalid_host_field: 字段 sync 取值无效`、`host_config_busy: 配置正在写入，请稍后重试`。
JSON 输出只用于成功结果，错误仍按 `rdispatch:` 前缀写 stderr。

检查使用临时 `RD_STATE`／`RD_CONFIG`、真实 `rdispatch serve` 子进程和 Unix socket，
覆盖在线与离线、备份、注释保留、删除保护、JSON 结构、stale socket 回退与连接中断
不重试；见 `tests/test_rd_config_cli.py` 与
[票 06 验证记录](validation/06-session-access-host-cli.md)。
