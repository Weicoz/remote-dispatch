# 变更记录

## 未发布

- **工作线会话轮换**：工作线默认每个工作项换新会话，同一 worktree，提示词前附交接包（复用上下文包拼装）。上下文历史用 `reason: policy`（策略轮换）与 `context`（`--new-context` 故障换新）区分；循环重建保护只对故障换新生效。上一轮回执不健康时不做策略轮换并输出原因。可按 harness 在 `[line:<harness>]` 配置 `reuse_max_context_tokens`、`reuse_max_interval_seconds`，满足时续用当前会话。详见 `docs/session-dispatch-defaults.md`。
- **看板 `rdispatch board`**：一屏显示已派、运行中、待派、阻塞四个数字，及每个派单的工作单元、目标机、harness 与模型、派出时刻、已运行时长、轮次、最新进度、状态、回收阻塞原因和可再生目录大小（有记录时）。默认当前发起会话，`--all` 为该用户全部派单；支持 `--json`。serve 新增只读 `GET /v1/board`，结构相同。不写状态。详见 `docs/board.md`。
- **上下文包 `run --context-from <派单>`**（可重复，仅代理派单）：rdispatch 从前序派单最新验收轮（没有验收轮时取最新回执）的结构化数据拼装上下文包，放在提示词前面：回执摘要、提交列表、改动文件（取回后才有提交列表）、决策与未完成项（执行方用 `rd-report result --decision/--unfinished`（可重复）写入回执 `report.decisions`、`report.unfinished`，没有则省略）。不调用模型；约 4k token 上限，超出时按「摘要 > 未完成项 > 决策 > 提交 > 改动文件」截断并注明。前序派单不属于当前用户 ID 或没有回执时退出码 4。拼装与截断集中在 `rd_context_pack.build()`，工作线交接包复用它。
- **工作线接线**：派单模式为 `line` 且带 `--plan-item` 时，前序工作项已验收、harness／模型／权限档位／`--include` 与线一致、不带 `--wip`、分支未指定或等于线分支，新工作项作为前序派单的新轮次追加（复用续跑路径），输出线名、分支、权限档位和基线；任一条件不满足按默认方式派出并写明原因。派单记录新增 `line`（工作项 → 轮次列表、各工作项基线）；`RD_BASE` 按工作项取上一个工作项验收轮回执的 `head`；打回只续跑当前工作项。`[features] line = false` 不接新线，已有线继续。本版会话沿用当前会话。详见 `docs/session-dispatch-defaults.md`。
- **`suggest` 与建议令牌**：新增只读 `rdispatch suggest`（参数同 `run`，`--json` 输出结构化建议）。严格接线条件与前序目标机在线可接单都满足才建议，并发 30 分钟单次有效的令牌（绑定内容指纹、用户 ID、发起会话）。`run --line <线名> --accept <令牌>` 接受（`--accept-chain` 设常驻同意），`run --no-line` 拒绝并记「本链不再建议」，`plan chain <线名> --clear` 恢复；令牌无效或无令牌直接接线退出码 4。详见 `docs/session-dispatch-defaults.md`。
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
