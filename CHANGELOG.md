# 变更记录

## 未发布

- **每轮记录 setup 与主体耗时**：回执 `report` 新增 `setup_seconds`、`body_seconds`（命令派单为命令耗时，`--test` 为测试耗时）；跳过 setup 时 `setup_seconds` 为 0 并带 `setup_note: 已跳过`；setup 失败只有 setup 耗时。POSIX 运行脚本与 Windows 原生 runner 都记录。新增 `rdispatch timings [--project] [--host] [--kind test|command|agent]` 与 `rd_timings.history()` 按项目、目标机查询已保存回执，供门禁选机使用。
- **可选 rollout 增强**：`[features] rollout_stats = true` 时，Codex 轮次回执附 `rollout_calls`（逐次调用数与用量汇总）；默认关闭，读取失败只记「未记录」。
- **回执记录实际模型与 token 用量**：回执和 `status --json` 新增 `model`（优先取 harness 输出，其次派单参数）与 `usage`（`input` 不含缓存、`cache_read`、`cache_write`、`output`）。Codex `--json`、Claude／Kimi／AGY `stream-json`、DSH `--json` 在 `rd_usage.py` 集中解析；没有用量或解析失败记「未记录」，老记录同样显示「未记录」。`status --json` 读取已收取的回执，未收取时显示「未记录」。
- 派单缺发起会话时 stderr 输出警告，退出码不变。

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
