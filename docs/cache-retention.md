# 合票后的缓存保留与回收

## worktree 过期回收（rdispatch clean --expired）

代理任务的 worktree 只被同一任务的 `rdispatch resume` 复用，新派单总是新建 worktree（命令任务默认复用热槽位，见下节）。所以“留不留”判断的是这个任务还会不会续跑：删早了，续跑要重新下载依赖和构建；一直不删，磁盘被旧任务占满。`rdispatch clean --expired` 按续跑可能性分级保留，只回收过期且安全的 worktree。

安全条件与 `--merged` 相同，任一不满足就永不自动回收：任务已结束；没有未取回的提交、合入关系可核验；全部轮次回执已收取并 `rdispatch ack`；worktree 除 `--include` 路径和已声明的可再生路径（项目配置 `regenerable_paths`，只豁免其下的未跟踪文件）外没有未提交内容；已跟踪文件的改动和未声明的未跟踪文件仍然阻塞。批量清理按项目 ID 选择当前用户的派单，不按当前目录：从 `.worktrees/integration` 等其他 worktree 派出的派单，在主仓库执行也能看到并回收；`--project` 可显式指定项目。回收前保存 reports，任务分支保留在目标机仓库，之后仍可 `rdispatch fetch` 或以该分支重新派单，但不能再 `rdispatch resume`。

保留期从最后一次回执确认（`rdispatch ack`）算起：

| 任务 | 默认保留 | 理由 |
| --- | --- | --- |
| 普通命令（`--cmd` / `--test`） | 0 小时 | 不能续跑，确认后即回收 |
| 代理任务，提交已合入目标分支或没有新提交 | 24 小时 | 已验收，留一天给追加修改 |
| 代理任务，已取回但未合入 | 7 天 | 还在审或部分完成，最可能续跑 |

目标机可用磁盘低于 `min_free_disk_gb` 时收紧：已合入的立即回收，未合入的上限 24 小时。

调整保留期写在 `[retention]`（`command_hours`、`accepted_hours`、`unmerged_hours`），环境变量 `RD_RETAIN_<KIND>_HOURS` 优先。明确还要续跑的任务用 `rdispatch keep <job> --hours N` 钉住，`--hours 0` 取消。

```bash
rdispatch clean --expired --dry-run    # 先看计划：每个任务给出 keep / would clean 与理由
rdispatch clean --expired              # 只回收过期且安全的 worktree
```

## 命令任务热工作树

命令任务（`--cmd` / `--test`，非代理）默认复用目标机上的常驻 worktree（热槽位），不再每个任务新建、构建完又删。Xcode DerivedData、CMake、venv 这类按绝对路径命中的构建缓存因此跨任务保留。

- **槽位**：目标机按「用户 + 项目 + 仓库 key」保留固定数量的 worktree，路径固定为 `$ROOT/wt/_warm/<项目>-<仓库key>-<n>`。默认每项目每机 2 个，`[project:x] warm_slots = N` 调整（0 等于关闭）。
- **关闭**：`[project:x] warm = off`、`[features] warm_worktree = false`（整体）、`run --fresh`（单次）。代理任务、`rdispatch gate` 全量门禁一律用冷 worktree（门禁要求干净环境）；Windows 目标首版一律冷。
- **领用**：一次 ssh 脚本完成。互斥内找空闲槽位并 `mkdir` 领用目录（内含任务名、领用时间；任务启动脚本结束时写 `released`），无空闲则在上限内新建槽位，达上限退回冷 worktree（现有行为）。槽位空闲的条件：上一个占用者已结束（有 `exit_code`、或 `pid` 已不存在、或已释放），且没被 `rdispatch keep` 钉住（钉住状态写在目标机 `jobs/<job>/pinned-until`，其他控制端也能看到）、`run --artifact` 声明的产物没有待拉回（目标机 `jobs/<job>/pinned` 标记，拉完清除）。领用后到启动脚本写下 `pid` 之间是准备期：领用时在目标机起一个心跳进程，每 20 秒刷新 `claim/at`，写下 `pid`、释放领用或满 6 小时即停止；`at` 超过 180 秒没刷新才判定准备者已不在，读不出状态一律按占用处理。准备失败（`preparing` 阶段）时控制端会释放尚未启动的领用。
- **精确快照**：领用后 `git checkout -f <任务分支>`，再 `git clean -ffd`（`-ff` 连嵌套 git 仓库一起清；不加 `-x`，被 `.gitignore` 忽略的构建缓存保留），之后才应用 `--include`、`--wip`。上一个任务用 `--include` 解包的路径会先记进 `warm/<槽位>.unpacked`，下次领用前对这些路径 `git clean -ffdx`，被忽略的 include 文件不会漏给下一个任务。清理失败时先 `chmod -R u+rwx` 重试，仍失败就废弃该槽位（上一任务读交接快照）并退冷 worktree。残留 `index.lock` 只在超过 1 分钟、`pgrep` 可用且没有 git 进程引用该目录时删除；探测不到 `pgrep` 视为不确定，不删锁，该任务退回冷 worktree。槽位目录异常（不是有效 worktree）且上一任务还没有交接快照时不删除，退冷。
- **交接快照**：槽位被下一个任务接手（或闲置回收）前，必须完整写出（`final-state.json`、补丁文件等逐项校验）；写不出（任务目录不可写、磁盘满等）就不接手、不回收，槽位保持原状，该任务退回冷 worktree（`warm_cold_reason=snapshot_failed`）。内容：把上一个任务最终的 head、未提交统计、未跟踪文件清单（`git ls-files -o --exclude-standard`）和 `git diff --binary HEAD` 补丁写进 `$ROOT/jobs/<上一任务>/final-state.json`、`final-status.z`、`final-untracked.txt`、`final.patch`；补丁超过 20 MB 不存，`patch_over_limit` 为 true，只留统计。之后该任务的状态、回收判定读这份快照，不读槽位现场。
- **记录**：派单记录 `worktree` 字段为 `cold:<相对路径>` 或 `warm:<槽位名>#<代次>`；旧记录没有该字段，按 `wt/<job>` 冷处理。回执新增 `worktree_mode`（`warm`/`cold`）、`warm_reused`、`warm_prev_commit`。
- **回收**：`clean <job>` 对热槽位任务只释放领用并删日志，不删槽位；`--delete-branch` 遇分支被槽位检出时先把槽位 `checkout --detach`。`clean --slim` 不处理热槽位。槽位计入 `max_worktree_gb`，被领用、钉住的槽位绝不回收；空闲超过 `warm_idle_days`（`[retention] warm_idle_days`，环境变量 `RD_RETAIN_WARM_IDLE_DAYS`，默认 7 天）的槽位由 serve 定时回收删除，删前先写交接快照。
- **限制**：同一任务的 `setup` 仍每个任务执行一次（在槽位里运行，应写成可重复执行）。`fingerprint` 在槽位内执行。

## 回收判定与阻塞原因

单个 `rdispatch clean <job>` 与批量 `--merged` / `--expired` 共用 `reclaim_check` 一处判定。`--dry-run` 每个派单输出一行：`skip <job> [<code>]: 原因`、`keep <job> [<code>]: 原因` 或 `would clean <job> [ok] (说明)`。

`rdispatch clean --merged|--expired --dry-run --json` 输出数组，元素为 `{job, reclaimable, code, reason}`；`--json` 必须与 `--dry-run` 同用。`code` 是稳定枚举值，`reason` 是给人看的中文说明，不保证稳定。

| code | 含义 |
| --- | --- |
| `ok` | 可回收 |
| `running` | 任务运行中 |
| `preparing` | 任务准备中或启动结果待核实 |
| `unreachable` | 目标机连接不可达 |
| `not_finished` | 未确认任务已结束 |
| `needs_confirmation` | 源仓库目录缺失，待人工确认；`reason` 附目标机 bare 仓库的核对结果 |
| `unverifiable` | 无法核验 worktree head、未提交内容或合入关系 |
| `worktree_orphan` | worktree 已不在 git 登记中（仅单个 clean） |
| `not_fetched` | 有未取回的提交 |
| `not_merged` | 已取回但未合入目标（仅 `--merged`） |
| `into_required` | 没有合入目标，需 `--into`（仅 `--merged`） |
| `ref_invalid` | `--into` 的 REF 不存在或不是提交（仅 `--merged`） |
| `receipt_pending` | 回执未收取或未 `ack` |
| `uncommitted` | 有未提交内容（不含已声明的可再生未跟踪路径；批量时另豁免 `--include` 的原输入，见下） |
| `pinned` | 已被 `rdispatch keep` 钉住（仅 `--expired`） |
| `retention_not_expired` | 未到保留期（仅 `--expired`） |
| `artifacts_pending` | `run --artifact` 声明的产物还没成功拉回（缺失、已跳过属终态，不算待拉）；`fetch JOB --artifacts` 后再回收，单个 clean 可 `--force` 放弃 |
| `plan_referenced` | 待派登记里后续工作项仍引用该派单（见下节）；单个 clean 可用 `--force` 跳过 |

批量回收对 `--include` 路径只豁免内容未变的原输入：派单时对 include 路径下的文件记录内容散列（派单记录 `include_hashes`），回收时在目标机重新散列，内容一致的才豁免；目录下新增或改动的文件（执行方写的成果）按 `uncommitted` 阻塞。文件数超过 5 万时不记录散列，该派单退回按目录豁免；没有 `include_hashes` 的旧记录和 Windows 目标同样按目录豁免。热槽位已被下一个任务接手时，现场内容已不存在，也沿用目录豁免。

## 待派登记 `rdispatch plan`

`rdispatch plan [--project 项目] add|set|done|cancel|remove|list` 登记还没派出的工作项及依赖：

- `add <工作项> [--title] [--depends 工作项]... [--after 派单]...`；`set` 只改传入的字段，传空串清空；依赖成环或依赖不存在时退出码 4。
- `list --json` 输出 `project`、`summary`（`dispatched`／`pending`／`blocked`／`done`／`cancelled` 各自数量）和 `items`。待派的工作项只要有依赖的工作项未完成就是 `blocked`。
- `rdispatch run --plan-item <工作项> [--after <派单>]...` 把派单关联到工作项，该项显示为已派。
- 数据存在状态目录 `plan/<用户ID>/<项目>.json`，写入持文件锁，serve 在线时与其他写命令一样转交 serve。
- 尚无派单、未完成未取消的工作项，若 `--after` 点名某派单，或依赖的工作项已关联该派单，该派单回收时返回 `plan_referenced`；工作项派出、完成或取消后不再引用。

## 源仓库缺失与无主派单

源仓库目录已删除时，`rdispatch clean <job>` 改用目标机上的 bare 仓库核对：worktree 提交是否仍在 bare 仓库、是否已合入其他分支，结果写入阻塞原因，code 为 `needs_confirmation`。核对完仍需人工确认才删除：终端内询问 `[y/N]`，默认不删；非终端环境不带 `--force` 时拒绝，退出码 4；`--force` 视为已确认，并先打印核对结果。批量 `--merged` / `--expired` 只报告 `needs_confirmation`，不代为确认。

`rdispatch inbox --orphans` 列出当前 `RD_USER_ID` 下没有发起会话的派单及其待收取回执，文本输出带可直接执行的 `ack` 命令；`--json` 输出数组。

## 合票后的缓存回收

merger实际接受、合入后，在当前授权内执行；本工具不会赋予删除旧目录、工作树或备份的授权。只操作本任务`RD_USER_ID`／`RD_JOB`／`RD_WT`，可以选择其`private/worktrees/`内的票据工作树。

使用随新job提供的`rd_cache.py`，先计划，再按同一组明确路径加`--apply`。以下路径是相对票据工作树的示例：

```bash
python3 "$RD_JOB_DIR/rd_cache.py" \
  --worktree "$RD_WT" --merged-into <已接受的integration引用或提交> \
  --closed-receipt <本轮真实关闭回执JSON> \
  --cache private/DerivedData/Build/Intermediates.noindex \
  --retain private/DerivedData/Build/Products \
  --retain private/evidence \
  --output "$RD_WT/private/cache-retention.json"
# 核对计划和当前授权后，使用相同参数加 --apply。
```

关闭回执要求：`head`等于待清工作树HEAD，或仅落后于后继Markdown收口提交；`all_owned_resources_closed=true`、`source_freeze_ended=true`，且`observed_current=[]`或`current_matching_births=[]`；有`job`时也必须相同。工作树干净，HEAD必须是`--merged-into`的祖先。integration在另一台机器时，先把已接受提交同步为目标库可验证的引用，不伪造合入关系。

只接受已声明的DerivedData中间缓存、Rust target的incremental和node_modules/.cache。源码、Products、最终FFI、原日志／截图等证据、依赖、备份和worktree本身都不属于清理目标；用`--retain`列出当前产物与证据，工具会核前后字节／权限。未知或仍有打开文件的目录保留；缺少lsof或安全目录删除支持时只能保留计划，不能推断已闲置。

工具拒绝跨员工、越界／符号链接、跟踪文件、未合入来源、未关闭过程、重叠路径；每次删除都更新0600回执，失败也保存已删清单。输出回执位于待删缓存之外。没有可用关闭证据时标未清理，继续产品交付，不为回收缓存重跑测试。

POSIX文件与Git夹具可直接验证；Windows不能凭本地夹具声称实际清理通过。此入口不改共享锁、代理、系统配置，也不取代`rdispatch clean`删除worktree所需的现有确认流程。

## serve 定时回收与 worktree 上限

`[features] scheduled_reclaim` **默认开启**（升级后无需配置；显式写 `false` 才关闭）。serve 每小时对在线目标机检查一次；只处理配置了 `[host:NAME] max_worktree_gb = 50`（GB，留空或 0 表示不限，Windows 目标机暂不处理）的目标机。当前用户 ID 名下每台目标机每个周期：

1. 回收「已签收且过期」的派单：不论总量是否超限，与 `clean --expired` 走同一个 `reclaim_check` + 保留期判定，被阻塞的派单（回执未收、有未提交内容、待派登记引用、钉住、未过保留期等）保留。
2. 回收闲置超过 `warm_idle_days` 且无人领用的热槽位（见上文），删前写交接快照。
3. 回收后总量仍超过上限，才对剩余派单瘦身：已结束且声明了 `regenerable_paths` 的派单，只删声明路径和 setup 完成标记。热槽位不瘦身。

只处理当前 `RD_USER_ID` 的派单；离线目标机直接跳过；不调用 `git clean`，不删未声明的文件。总量不超限且没有可回收项时不产生计划。

**安全期**：首次启用（默认开启后 serve 第一次运行也算）后前 7 天只出计划，不瘦身也不删除。「未超限也回收过期任务与闲置槽位」是后加的行为，单独记启用时间（`reclaim-schedule.json` 的 `sweep_enabled_at`）：已经启用过定时回收的老用户升级后，这部分同样先 7 天只出计划，期间仍只按旧逻辑在超限时瘦身、再删除过期派单。默认开启意味着升级后第一次运行就可能列出大量已过期的旧任务，7 天计划期让用户在 `rdispatch board`／`GET /v1/reclaim` 上先看到清单，需要的任务可用 `rdispatch keep` 钉住；这一语义对默认开启仍然合理，因此保留。起点写在 `RD_STATE/reclaim-schedule.json`，serve 重启不重新计时。第 8 天起执行。每台目标机每次有计划时产生 `reclaim.planned` 事件，执行期另产生 `reclaim.result` 事件（逐项结果、回收后用量），热槽位回收行的 `job` 为 `_warm/<槽位名>`、`action` 为 `delete_slot`，见 [事件](events.md)。计划与最近结果同时显示在 `rdispatch board`（`reclaim_plan` 字段与文本末尾）、`GET /v1/board` 和 `GET /v1/reclaim`。
