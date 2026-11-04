# 变更记录

## 未发布

- **一台目标机的配置错误只影响它自己**：`enabled` 读取时宽松解析（`1／true／yes` 启用，`off`、空值、`false`、`no`、`0` 等一律停用，保持旧行为），已停用的目标机不做校验；已停用的接单字段 `max_swap_io_mb_s`、`min_swap_free_gb`、`swap_active_mb_s` 任何值都忽略。已启用的目标机有非法值（NaN、inf、负数、`10G` 这类带单位的数字、`timeout = 0` 等）时标记为「配置无效」：从 `--host auto`／`--tag` 候选排除，显式 `--host` 退出码 4 并指出字段与修改建议（带单位的值提示「去掉单位」），`rdispatch hosts` 与 serve 的 `GET /v1/hosts`（新增 `config_errors`）显示该错误，其他目标机与 serve 照常运行。serve 启动时同样隔离；热重载只拦截「旧配置里有效、新配置里变无效」的目标机（保留旧配置并报错，不让在用的主机悄悄消失），新增的或本就无效的目标机逐台隔离，同一次修改里的其他好修改照常生效。已登记的在途任务查目标机（`job_host`、serve 轮询）宽松读取、不校验字段，主机配置写坏也能继续监控与收尾；续跑和派新单一样严格校验。`ConfigError` 改为 `ValueError` 子类，现有的 `ValueError` 捕获路径都能接住；`--force-host` 与 `--host` 一样校验已停用主机的字段。
- **指纹核对的 setup 计入超时预算**：派单前指纹核对同步跑的 setup 耗时从随后启动任务的 `timeout` 里扣除（至少 1 秒），setup 加主体总时长不再最多翻倍。Windows 原生 runner 的 setup 超时与 setup 失败（97）现在也写结果回执，`--test` 的 `checks` 按失败处理，与 POSIX 一致。
- **setup 纳入超时预算**：setup 与主体共用 `--timeout`／`timeout` 同一预算：setup 卡住会在预算用完时被终止，退出码 124，回执 `report.timeout_phase` 为 `setup`；setup 用掉一部分时间后主体只拿剩余时间（至少 1 秒），主体超时 `timeout_phase` 为 `body`。setup 失败仍是退出码 97。POSIX 运行脚本与 Windows 原生 runner 都按此处理。
- **配置读取与 CRUD 共用校验**：`Config.host` 改调 `rd_host_config.validate`，读取路径也拒绝 NaN、inf、负数和越界百分比（此前 `max_load_per_core=nan` 读取接受、写入拒绝，会放过高负载门禁），`sync`、`launcher`、`enabled`、`platform` 等枚举与布尔统一解析；`DEFAULTS` 移到 `rd_defaults`、`parse_cache_dirs` 移到 `rd_host_config`，`cli` 继续导出。serve 重载遇到无效配置仍保留旧配置。
- **网络不可达的拒绝原因与失败提示**：派单前的目标机网络探针（模型服务与 Codex 端点）不通时，`run` 仍退出码 3 拒绝，现在写明具体原因（TLS 握手失败、超时、DNS 解析失败等）并建议换主机或稍后重试，`--host auto` 自动跳过该机、全部不通时逐机列出，`--force-host` 跳过检查。代理任务失败回执新增 `failure_hint`（`network_unreachable`／`auth_failed`）与中文说明 `failure_note`：扫描本轮输出里的 `workspace routing discovery failed`、TLS 握手、连接超时／重置、DNS、401 等已知模式，识别不了不标记；`result`、`inbox`、`board` 显示。判定集中在 `rd_messages.failure_hint()`。详见 `docs/agents.md`。
- **测试回执自动检查与防测旧代码**：`run --test` / `--test-full` 的命令任务，收尾兜底回执按退出码写 `checks=passed`（0）或 `failed`（非 0），并在回执 `report.test_command` 记录被执行的命令；代理任务、执行方自己写了结果回执的任务不变。测试类派单遇本地未发送的改动（未提交、未 `--wip`、未被 `--include` 覆盖）退出码 4 拒绝，提示 `--wip`、`--include` 或先提交，确认要测 HEAD 时用 `--allow-dirty`；非测试派单仍继续，改为 stderr 警告。详见 `docs/handoff.md`。
- **假终态与并发写修复**：派单登记为 `preparing`／`launching` 期间，`Query.result` 不再把目标机上尚未建好的 `missing`／`dead`（`launching` 时 `done` 仍可信）当终态，不生成也不落盘回执，避免假终态被缓存后任务真完成仍返回旧回执；`run` 从登记到 started 持有任务锁。`rdispatch keep` 改为持任务锁读改写，不再覆盖并发续跑写入的新轮次。产物取回状态按轮次保存（`artifacts_rounds`，含每轮目标路径），续轮重新取回，不再沿用上一轮的「已取回」；`artifacts_state` 保留最近一次并新增 `round`、`paths`。详见 `docs/locking-and-queries.md`。
- **SSH 连接复用与启动打包上传**：目标机 `ssh_opts` 未含任何 `Control*` 选项时自动追加 `-o ControlMaster=auto -o ControlPath=~/.ssh/rd-%C -o ControlPersist=60 -o ServerAliveInterval=10 -o ServerAliveCountMax=3`（ssh、git 的 `GIT_SSH_COMMAND`、rsync `-e` 都受益；写了 `ControlMaster=no` 等 Control 选项则原样保留；Windows 发起方跳过）。POSIX 目标的 launch 阶段由逐文件 7～10 次 ssh 改为一次：启动文件打成 tar 流上传到 `$ROOT` 下临时目录，逐个 `mv` 原子发布并在同一次 ssh 里启动；公共脚本（`rd_messages.py`、`rd_cache.py`、`rd_profiles.py`、`bin/rd-heavy`）按内容摘要存在 `$ROOT/lib/<摘要>/`，已存在且大小一致不重传（目标机首次或脚本更新时多一次重试）。Windows 原生路径不变。详见 `docs/worker-setup.md`。
- **非 Git 产物取回 `run --artifact`**：声明仓库内相对路径（文件或目录，可重复；绝对路径、含 `..`、`.git`、通配符退出码 4），任务结束后 `wait`／`result`／回执收取时拉回本地同一路径一次（macOS／Linux 目标用 rsync，含空格路径安全；Windows 目标经原生 worker 的 base64 通道取 tar），输出与回执 `artifacts` 列出文件数和大小，远端缺失单独报告、不算失败。新增 `rdispatch fetch JOB --artifacts` 手动重拉。产物未成功拉回时回收判定返回 `artifacts_pending`（`clean --expired` 与定时回收不回收，单个 clean 可 `--force`）。指向已被 Git 跟踪文件的 `--artifact`（含目录内有已跟踪文件）在 `run` 阶段退出码 4；拉回时本地目标已被跟踪则跳过并报告「已跳过」（终态，不阻塞回收）；失败保持待拉可重试。macOS、Linux、Windows 目标均支持。
- **派单前环境指纹核对**（macOS、Linux、Windows 目标均支持；Windows 优先 `fingerprint_windows`，PowerShell 语法）：`[project:<名>] fingerprint = <shell 命令>` 设置后，`run` 在目标机同步并 setup 之后比较本机与目标机 stdout；不一致则任务不执行主体，退出码 95，`run` 退出 5，输出 unified diff，派单记录与回执记两边指纹。`--host auto` 逐台尝试候选，全部不一致才失败。`--skip-fingerprint` 跳过并警告；`doctor` 在配置了指纹时也比较；`fingerprint_timeout` 默认 120 秒。配了指纹的派单在 `run` 阶段同步执行 setup。`doctor` 本机指纹失败时输出 MISSING 行并返回 1，不再中断。详见 `docs/worker-setup.md`。
- **汇报与推送第二轮审查修复**：摘要汇报的 `report.delivered` 不带 `rounds`，不再抵消同轮终态回执推送；事件投影此前丢弃了 `rounds`，现保留（`job:round` 字符串列表），推送按轮次去重才真正生效；汇报投递改为至少一次（`queued` 在重启或下一次轮询重投，回写失败 30 秒后重试）；通道异常记 `error:<异常类型>` 并写 stderr（不含正文）；推送每个到期项发送后立即落盘，延后时间设下限。详见 `docs/reports.md`。
- **汇报审查修复**：`inbox --wait` 在线时跳过摘要汇报，去重键改为汇报 id（先摘要后完成不再漏唤醒），两路基线都在命令开始时取（在线期间结束的派单在 serve 离线后仍唤醒）；提醒通道改在独立线程池调用，不阻塞汇报线程，新汇报先以 `delivery.status=queued` 落盘，通道返回后回写，事件写入失败重试不再重复通知；`inbox --json` 帮助与 Skill 补 `--wait`。详见 `docs/reports.md`。
- **App 推送按派单与轮次去重**：同一轮的 `receipt.ready` 与 `report.delivered`（app 通道）只推一次，汇报优先（`report_ready`）；完成回执先延后「汇报合并窗口 + 30 秒」，超时无汇报仍推 `receipt_ready`；`blocked` 即时推送。`report.delivered` 新增 `rounds` 字段（`job:round`，不含正文），延后与已推送状态随推送检查点持久化。详见 `docs/reports.md`。
- **App 推送通道**：`[monitor] notify` 含 `app` 时，汇报成功提醒后由 `rd_push` 按 `report.delivered` 事件向已配对设备发计数推送（`{"report_ready": 1}`，不含正文），沿用检查点；无推送设备记 `no_device`。详见 `docs/reports.md`。
- **提醒通道与 Herdr 界面通知**：新增 `[monitor] notify`（`herdr`、`macos`、`app` 逗号分隔，默认三者全开，`none` 全关；非法取值退出码 4）。本版实现 `herdr` 通道：每条合并汇报调用 `herdr notification show`（失败用 `request` 提示音，其他 `done`），文本只含派单标题、目标机、轮次、状态文字，不含执行方摘要，不触碰 pane 输入；不可用记 `herdr_unavailable` 不重试；`app` 暂记 `not_implemented`。新增 `macos` 通道：macOS 控制端经 `osascript` 发系统通知（文本以独立参数传入，不可注入；serve 作为 launchd 用户代理也能发），非 macOS 记 `unsupported_platform`，失败记 `notify_failed`，不影响其他通道。结果写入 `delivery.channels`，至少一个通道成功为 `notified` 并发布 `report.delivered`。进度摘要默认不提醒，`[monitor] notify_digest = true` 时提醒。详见 `docs/reports.md`。
- **汇报不再对已结束轮次报离线**：一轮已报完成、失败或卡住后，后续连接不可达观察被忽略，不生成汇报、不改已记录状态；运行中轮次的离线汇报不变。详见 `docs/reports.md`。

- **接单判定去掉 swap 检查**：`assess` 不再按换页速率或交换区剩余拒绝派单；`max_swap_io_mb_s`、`min_swap_free_gb`、`swap_active_mb_s` 已停用（旧配置照常加载）。探针仍采集 swap，`hosts`、看板、serve 指标照常显示；load、内存、磁盘等阈值不变。
- **`rdispatch inbox --wait`**：只读后台等待本会话新汇报（`--timeout` 秒、`--json`）；新汇报退出 0，超时退出 2，缺发起会话退出 4。输出每条 `<派单ID> 第N轮 <状态>` 与查看命令，不含执行方摘要；serve 离线时靠派单状态进入终态唤醒。不签收、不写回执。详见 `docs/reports.md`。
- **汇报不再写入会话输入框**：serve 生成汇报后只存 inbox 并发布 `report.created`，不再调用 Herdr 会话输入，`[serve] session_access` 不再影响汇报。`delivery` 改为 `status`（`notified`／`inbox_only`）加 `channels` 列表，当前没有启用通道，新汇报固定 `inbox_only`；老记录的 `queued`、`sending`、`delivered`、`text_delivered` 与旧原因键原样返回。提醒步骤集中在 `Reports.notify`。详见 `docs/reports.md`。
- **回执模型没传记「默认」**：回执 `model` 取值顺序为 harness 输出、`--model`、员工配置包 `config.toml` 的 `model`（Codex；清单 `manifest.model`，派单时随配置快照写入派单记录，回执读取时不访问目标机）、`默认`。新回执不再出现「未记录」的模型；老回执与未收取回执的 `status --json` 原样显示。配置包清单在下次同步时才带 `model`，之前的快照记「默认」。
- **凭据只在首轮复制**：Codex、DSH、AGY 的认证文件只在首轮复制进任务隔离目录，续跑沿用（AGY 原先每轮重新复制，DSH 原先每轮覆盖，均改为沿用）。新增 `rdispatch resume JOB --refresh-auth`：本轮重新复制认证文件覆盖，不动员工配置副本、会话与 worktree，POSIX 与 Windows 原生路径都支持；首轮缺凭据配置时之后补配也要它才复制。本轮失败输出有明确认证失败时，回执记 `credential_suspect: true` 与 `credential_hint`，`result`、`inbox`、看板给出重新复制的命令提示；不自动重复制、不自动续跑，识别不了不标记。详见 `docs/agents.md`。
- **工作线会话轮换**：工作线默认每个工作项换新会话，同一 worktree，提示词前附交接包（复用上下文包拼装）。上下文历史用 `reason: policy`（策略轮换）与 `context`（`--new-context` 故障换新）区分；循环重建保护只对故障换新生效。上一轮回执不健康时不做策略轮换并输出原因。可按 harness 在 `[line:<harness>]` 配置 `reuse_max_context_tokens`、`reuse_max_interval_seconds`，满足时续用当前会话。详见 `docs/session-dispatch-defaults.md`。
- **工作线前缀合入与 `rdispatch line prefix|close`**：`line prefix <派单> <工作项...>` 在第 k 个工作项验收轮回执的 `head` 建前缀分支（只支持前 k 个，跳项或未验收退出码 4），可直接 `rdispatch gate <分支>`；线关闭前合入检查与 `clean --merged|--expired` 不把派单当作已合入；`line close` 在线上无运行中轮次时关闭线，之后按正常合入判定回收。详见 `docs/session-dispatch-defaults.md`。
- **serve 定时回收**：`[features] scheduled_reclaim = true`（默认关闭）后，serve 每小时按目标机 `max_worktree_gb` 检查 worktree 总量，超限先瘦身、仍超限再按保留期从旧到新删除，删除只走统一回收判定、只处理当前用户 ID、离线目标机跳过；启用后前 7 天只出计划（起点持久化，重启不重计），计划与结果写入事件 `reclaim.planned` / `reclaim.result`，并显示在看板 `reclaim_plan`、`GET /v1/board` 和新增的 `GET /v1/reclaim`。详见 `docs/cache-retention.md`。
- **工作线生命周期**：线上有待派工作项且无人推进超过 `RD_LINE_STALE_DAYS`／`[line] stale_days`（默认 7 天）或发起会话缺失时，看板显示「停滞」；再过同样天数 `clean --expired` 按已取回未合入的保留期回收。新工作项接入前记录 worktree 未跟踪文件清单，写入回执 `residue` 与看板，不删除。新增 `rdispatch takeover JOB`：同一用户 ID 的其他发起会话接手工作线并记录，跨用户 ID 与非工作线退出码 4。`resume --host` 换机时换新会话并附交接包，输出提示先瘦身。详见 `docs/session-dispatch-defaults.md`。
- **看板 `rdispatch board`**：一屏显示已派、运行中、待派、阻塞四个数字，及每个派单的工作单元、目标机、harness 与模型、派出时刻、已运行时长、轮次、最新进度、状态、回收阻塞原因和可再生目录大小（有记录时）。默认当前发起会话，`--all` 为该用户全部派单；支持 `--json`。serve 新增只读 `GET /v1/board`，结构相同。不写状态。详见 `docs/board.md`。
- **上下文包 `run --context-from <派单>`**（可重复，仅代理派单）：rdispatch 从前序派单最新验收轮（没有验收轮时取最新回执）的结构化数据拼装上下文包，放在提示词前面：回执摘要、提交列表、改动文件（取回后才有提交列表）、决策与未完成项（执行方用 `rd-report result --decision/--unfinished`（可重复）写入回执 `report.decisions`、`report.unfinished`，没有则省略）。不调用模型；约 4k token 上限，超出时按「摘要 > 未完成项 > 决策 > 提交 > 改动文件」截断并注明。前序派单不属于当前用户 ID 或没有回执时退出码 4。拼装与截断集中在 `rd_context_pack.build()`，工作线交接包复用它。
- **工作线接线**：派单模式为 `line` 且带 `--plan-item` 时，前序工作项已验收、harness／模型／权限档位／`--include` 与线一致、不带 `--wip`、分支未指定或等于线分支，新工作项作为前序派单的新轮次追加（复用续跑路径），输出线名、分支、权限档位和基线；任一条件不满足按默认方式派出并写明原因。派单记录新增 `line`（工作项 → 轮次列表、各工作项基线）；`RD_BASE` 按工作项取上一个工作项验收轮回执的 `head`；打回只续跑当前工作项。`[features] line = false` 不接新线，已有线继续。本版会话沿用当前会话。详见 `docs/session-dispatch-defaults.md`。
- **`suggest` 与建议令牌**：新增只读 `rdispatch suggest`（参数同 `run`，`--json` 输出结构化建议）。严格接线条件与前序目标机在线可接单都满足才建议，并发 30 分钟单次有效的令牌（绑定内容指纹、用户 ID、发起会话）。`run --line <线名> --accept <令牌>` 接受（`--accept-chain` 设常驻同意），`run --no-line` 拒绝并记「本链不再建议」，`plan chain <线名> --clear` 恢复；令牌无效或无令牌直接接线退出码 4。详见 `docs/session-dispatch-defaults.md`。
- **接受建议时同步 integration 新头**：`suggest` 在控制端取回前序验收提交，用 `git merge-tree` 预检它与 integration 新头能否无冲突合并，把「需要同步」和预检结果写入建议；有冲突不建议。接受后把新头推到目标机 bare 并在 worktree 合并，结果写入派单 `line.syncs`、回执 `integration_sync` 和看板；实际合并冲突时中止、worktree 恢复，按默认方式派出。
- **每轮记录 setup 与主体耗时**：回执 `report` 新增 `setup_seconds`、`body_seconds`（命令派单为命令耗时，`--test` 为测试耗时）；跳过 setup 时 `setup_seconds` 为 0 并带 `setup_note: 已跳过`；setup 失败只有 setup 耗时。POSIX 运行脚本与 Windows 原生 runner 都记录。新增 `rdispatch timings [--project] [--host] [--kind test|command|agent]` 与 `rd_timings.history()` 按项目、目标机查询已保存回执，供门禁选机使用。
- **可选 rollout 增强**：`[features] rollout_stats = true` 时，Codex 轮次回执附 `rollout_calls`（逐次调用数与用量汇总）；默认关闭，读取失败只记「未记录」。
- **回执记录实际模型与 token 用量**：回执和 `status --json` 新增 `model`（优先取 harness 输出，其次派单参数）与 `usage`（`input` 不含缓存、`cache_read`、`cache_write`、`output`）。Codex `--json`、Claude／Kimi／AGY `stream-json`、DSH `--json` 在 `rd_usage.py` 集中解析；没有用量或解析失败记「未记录」，老记录同样显示「未记录」。`status --json` 读取已收取的回执，未收取时显示「未记录」。
- 派单缺发起会话时 stderr 输出警告，退出码不变。
- **缓存目录**：目标机配置新增 `cache_dirs`，派单时注入各生态缓存目录的环境变量；目录按用户 ID 分开、放在 worktree 之外，落在 worktree 内的声明加载配置时报错。详见 `docs/worker-setup.md`。
- **可再生子路径瘦身**：项目配置新增 `regenerable_paths`（如 `target; node_modules`），非法声明加载配置时报错；回执 `report.regenerable_kb` 记录各路径大小；新增 `rdispatch clean --slim [JOB]`，只删声明路径和 setup 完成标记，保留 worktree 与其余文件，续跑重新 setup。详见 `docs/worker-setup.md`。
- **回收去阻塞**：已声明为 `regenerable_paths` 的未跟踪目录不再算「未提交内容」（已跟踪文件改动与未声明的未跟踪文件仍阻塞）；`clean --merged` / `--expired` 按项目 ID 选择当前用户的派单，从其他 worktree 派出的派单在主仓库也能回收。

## 0.3.1 — 2026-10-28

- **新增 `rdispatch service`**：`install | uninstall | restart | status` 用用户级 LaunchAgent 托管常驻 serve，开机自启、崩溃拉起，日志写状态目录；`status` 报告 launchd 加载状态、进程、socket、包版本、合同版本与最近一次配置重载结果，支持 `--json`，未安装时非零退出并提示安装。只支持 macOS，详见 `docs/serve.md`。
- **设置类子命令**：`rdispatch session-access [show|on|off]`、`rdispatch host list|add|update|delete` 管理会话访问总开关与目标机配置，serve 在线时转交、不在线时直接写（先备份再原子替换），与 serve 共用一套校验与写入；`rdispatch device list|revoke`、`rdispatch pair open|close` 管理已配对设备与配对窗口，必须经 serve。均支持 `--json`。
- **serve 版本接口与配置自动重载**：新增 `GET /v1/version`（包版本、合同版本、配置重载结果）；serve 每秒检查配置文件，变化后自动重载，失败时保留旧配置并在 `/v1/version`、`/v1/hosts` 与 `rdispatch service status` 报告。
- **本机目标派 Codex 不再要求确认同步**：`ssh = local` 时员工配置副本缺失或落后会自动同步并输出一行说明；SSH 目标机仍须确认。
- 配置文件格式错误时 CLI 报 `config_invalid` 并退出 4，不再打印 traceback。

## 0.3.0 — 2026-10-28

- **新增 `rdispatch serve`**：本机 Unix socket 上的 HTTP 与事件流，供 App 等客户端读取派单、项目、会话、目标机与网络设置；serve 在运行时 CLI 写命令经 socket 转交给它，不在时 CLI 照旧直接写，两条路径共用 job 级锁。
- 新增子命令 `project`（项目索引、隐藏与恢复）、`session-default`（会话派单默认值）、`review`（验收与打回续跑）。
- 结果收取、进度汇报改为追加式并按轮次保留；新增发起来源字段、默认用户 ID、整轮锁与事件日志。
- serve 提供目标机配置增删改、设备配对与令牌、会话访问总开关、会话终端 WebSocket 等接口，详见 `docs/serve.md` 与 `contracts/v1`。
- 状态目录与配置兼容 0.2.1：旧版能读新版写过的状态目录并忽略新文件；回退到 0.2.1 后，Claude Code 发起会话收取结果需显式传 `--initiator-session`。

## 0.2.1 — 2026-10-24

- **交换区不再按已用量拦派单**：macOS 不主动回收冷页，已用量只反映历史累积。探针改为采样 3 秒换入+换出速率（macOS `vm_stat`、Linux `/proc/vmstat`），并上报交换区剩余；超过 `max_swap_io_mb_s`（默认 10 MB/s），或剩余低于 `min_swap_free_gb`（默认 1 GB）且换页不低于 `swap_active_mb_s`（默认 1 MB/s）才判忙。
- `max_swap_used_gb` 保留，语义改为只提示：超过时 `rdispatch hosts` / `run` 输出附 `note: … not counted as busy`。旧配置无需迁移。Windows 页面文件只提示不拦。
- 内存压力仍是主判据：macOS `memory_pressure` 空闲百分比、Linux `MemAvailable` 百分比低于 `min_free_mem_pct` 判忙。

## 0.2.0 — 2026-10-23

- **命令改名 `rd` → `rdispatch`**：`rd` 在 oh-my-zsh 核心库是 `rmdir` 别名、rails 插件是 `rails destroy`、PowerShell 是 `Remove-Item`、CMD 是内置 `rmdir`。旧入口 `rd` 保留一个版本，执行时在 stderr 提示改名；Windows 包装改为 `bin/rdispatch.cmd`、`bin/rdispatch.ps1`（走 `python -m remote_dispatch`），删除永远不会被 CMD 执行到的 `rd.cmd`/`rd.ps1`，`rd-remote.*` 保留一个版本。
- 新增 `rdispatch clean --expired`：在 `--merged` 的安全条件之上按续跑可能性分级保留 worktree（命令任务确认即回收、已合入 24 小时、已取回未合入 7 天，目标机磁盘不足时收紧），`[retention]` 可调；新增 `rdispatch keep <job> --hours N` 钉住将续跑的任务。
- 新增 AGY（Antigravity CLI）派单：`agy --print` 经 stdin 传 stream-json 消息、`--conversation` 续跑；每任务独立 `HOME`，只复制目标机 `agy_auth_file` 登录凭据；`--permission-mode`（plan/accept-edits/auto/skip）必填，续跑沿用；终态 `denied_actions` 使回执降为 `partial`；未登录归类为 `authentication` 故障。仅 POSIX。
- 代理能力集中到 `rd_agents.py`（模板、平台、隔离、凭据、预检、权限档位），`rdispatch run` 预检由登记驱动；`[agent:NAME]` 只能覆盖内置模板。
- 代理参数（模型、推理强度、权限档位）在推送任务分支前校验。
- Codex、DSH、AGY 运行环境保留 `HTTP(S)_PROXY`、`ALL_PROXY`、`NO_PROXY` 与 CA 证书变量，与连通预检一致。
- 修 `rdispatch doctor --agent` 把参数列表再包一层导致崩溃；Windows 目标对非 codex 代理直接报不支持；`--agent agy` 分开报告安装与登录。
- 发起方回执上限放宽到 128KiB，接近 64KiB 的合法远端报告不再无法缓存。
- `dsh_auth_file` 接受 `/` 开头的绝对路径。
- 本机目标的 `workdir` / `--workdir` 拒绝相对路径。
- 文档：修正示例目录、旧任务续跑、派单端环境变量只对本机目标有效、行尾注释陷阱等与实际行为不符处；合并主仓库技能里的“派单后立即挂巡检”。
- 新增本机目标 `ssh = local`：直接在本机执行，使用独立 worktree 与任务目录；`codex`/`dsh` 有员工配置隔离，`kimi`/`claude` 仍拒绝派单。
- `--host auto` 与 `--tag` 不再选本机目标；本机必须显式 `--host self`。
- 本机目标跳过外网探针；`rdispatch hosts` 标注「(本机)」，`rdispatch doctor` 说明跳过。
- `workdir` 统一展开 `~` 与 `$HOME`，修复 `$HOME/x` 被当作字面路径。
- Windows 发起方配置 `ssh = local` 时直接报错，不再抛 `FileNotFoundError`。
- 修 `rdispatch doctor --agent <name>` 崩溃；显式指定的代理缺 CLI 时判失败。
- 新增本机端到端回归、GitHub Actions 测试与技能副本链接检查。

## 0.1.0 — 2026-10-20

首个独立版本，从 `weicoz-skills` 的 `weicoz-remote-dispatch` 技能迁出。

- 打包为 `remote-dispatch`，命令行入口 `rd`；配置仍在 `~/.config/remote-dispatch/config.ini`。
- 新增 DSH 派单：`dsh headless --json` 启动与 `--session-id` 续跑，每任务独立 `DSH_HOME`，凭据文件复制进任务目录并 `chmod 600`。
- `rd run --agent` 现在接受 `codex` 与 `dsh`；`kimi` / `claude` 仍只保留模板，未实现员工配置隔离前拒绝派单。
- 保留 130 项既有回归测试，新增 10 项 DSH 测试。
