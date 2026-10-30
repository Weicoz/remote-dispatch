---
name: weicoz-remote-dispatch
version: 1.5.0
description: 把 Codex、DSH 或 AGY（Antigravity CLI）编码代理会话、测试和构建命令经 SSH 派到另一台机器，或在本机以独立 worktree 另起一个 harness 会话执行，本机只负责派单、看进度和取回提交。只在用户主动要求或明确授权把任务放到别的机器或另一个 harness 上跑时使用，例如“派到另一台机器／远端／mac mini／服务器上跑”“让别的机器跑测试”“用双机／多机跑”“本机派单给其他 harness”“remote dispatch”“rdispatch run”“rd run”，或要求添加、检查目标机器。用户没有要求也没有授权时不要使用：派代理、跑测试默认都在当前会话执行；本机负载高时可以建议派到远端，但要等用户同意。
---

# Remote Dispatch

把任务派到指定机器，并按员工固定 ID 选择独立配置。目标机写 `ssh = local` 时就在本机执行，但仍然使用独立 worktree、任务目录与日志。实现是 Python 包 `remote_dispatch` 与 `rdispatch` 命令，本技能只负责调用它。

- 项目源码：`https://github.com/Weicoz/remote-dispatch`（`src/remote_dispatch`、`docs/`、`tests/`）
- 命令名是 `rdispatch`（0.2.0 前叫 `rd`）。旧名 `rd` 在 oh-my-zsh、PowerShell、CMD 里都会被别名或内置命令截走，一律写 `rdispatch`。
- 技能源文件在项目仓库 `skills/weicoz-remote-dispatch/SKILL.md`；Codeberg `weicoz-skills` 里的同名文件由 `make sync-skill` 同步。

需要同时协调本机交互会话与多条远端任务时，由 `weicoz-session-hub` 当总控，本技能负责其中的远端任务。

## 安装与更新

未安装时先装。不要改技能目录里的脚本，也不要复制一份实现到别处。

```bash
uv tool install --from . remote-dispatch                           # 仓库目录内
uv tool install git+https://github.com/Weicoz/remote-dispatch      # 或从 GitHub
pipx install .                                                     # 或 pipx
pip install --user .                                               # 或 pip

rdispatch --version          # 验证入口
```

更新：`uv tool upgrade remote-dispatch`，或重跑上面的安装命令。升级包不需要动技能。卸载：`uv tool uninstall remote-dispatch`，pipx / pip 装法同理。

本机要求 Python 3.9+、git、ssh。员工配置隔离要求两端 Python 3.11+，目标机另需对应代理 CLI 与认证。

## 授权边界

默认由当前会话自己执行，不派单。只有用户主动要求，或对建议明确同意，才派出去。本机目标（`ssh = local`）也算派单，同样需要明确同意。

本机负载高、内存紧、磁盘不够，都不是自行改派的理由。可以建议：说明本机状况、`rdispatch hosts` 看到的远端状况、打算把哪件事派到哪台机器，然后等用户答复。答复含糊不算授权。

授权只覆盖用户同意的那件事。用户说“以后都派到远端”才算持续有效。

## 配置

机器清单在 `~/.config/remote-dispatch/config.ini`，不存在时按 `examples/config.example.ini` 建一份。

```ini
[host:mini]
ssh = user@mini.local
workdir = ~/remote-dispatch
sync = origin
; 只有 dsh 需要；注释必须单独成行，rdispatch 不识别行尾注释
dsh_auth_file = ~/employee/dsh-credentials.yaml

; 本机目标：另起一个隔离会话，不经过 SSH
[host:self]
ssh = local
workdir = ~/remote-dispatch-self
sync = direct
; 本机派 dsh 时同样必填；agy 默认读 ~/.gemini/antigravity-cli/antigravity-oauth-token
dsh_auth_file = ~/.dsh/.credentials.yaml

[project:myrepo]
origin_url = git@github.com:me/myrepo.git
test = uv run pytest -q
```

新增机器、目标机装环境与凭据的步骤见 [目标机准备](https://github.com/Weicoz/remote-dispatch/blob/main/docs/worker-setup.md)。

## 本机派单（不跨机）

同一台机器也能登记成目标机，让另一个 harness 会话并行干活。rdispatch 为每个任务建独立 worktree 与任务目录，不覆盖当前工作区。

```bash
rdispatch hosts                                  # self 显示为「本机」
rdispatch doctor self                            # 检查本机 git、pgrep、Python、代理 CLI
rdispatch run --host self --agent dsh --prompt-file task.md --force-host
rdispatch run --host self --cmd 'make bench'     # 普通命令，不经隔离
```

- `ssh = local` 直接在本机执行，不需要 sshd 或免密。rdispatch 不为本机目标跑外网探针；代理调用模型仍需网络。
- 只支持 POSIX 发起方。Windows 目标必须走 SSH；Windows 发起方写 `ssh = local` 会直接报错。
- `--host auto` 与 `--tag` 不会选本机目标；本机派单必须显式 `--host self`。
- 本机同样按负载、内存、磁盘阈值拦截；超阈值才用 `--force-host`，先确认不会挤压当前会话。
- 交换区只按换页速率判忙；`note: swap … GB used … not counted as busy` 是冷交换区提示，不是拦截，无需 `--force-host`。
- 隔离覆盖 `codex`、`dsh`、`agy`。`kimi` / `claude` 会被拒绝；要本机并行用这两个，直接调它们自己的 CLI，或改用 `--cmd`。
- 本机派 dsh 仍需 `dsh_auth_file`。headless profile 的路由缺 provider key 时报 `MISSING_CREDENTIAL`；rdispatch 保留 `DEEPSEEK_*`、`OPENAI_*`、`ANTHROPIC_*` 环境变量，本机目标可先 `export DEEPSEEK_API_KEY=...`；SSH 目标不转发派单端变量，要写进目标机 `env_file`。也可以换用带 key 的 profile。裸 `dsh headless` 同样报错即与 rdispatch 无关。

## 员工 ID 与配置同步

- 派单前读 `RD_USER_ID`。缺失时询问员工自选 ID 并暂停派单，不随机生成、不猜登录名。ID 只选配置，不是认证。
- **首次登记或主动同步必须询问是否同步**，并告知已配置 ID。先跑 `rdispatch profile check <机器>` 展示目标机、ID、清单、变化与版本指纹，用户确认后再 `rdispatch profile sync`。
- 例外：本机目标（`ssh = local`）派 `codex` 时，配置副本缺失或落后会自动同步并输出一行说明，不需要询问。配置源和目的地是同一台机器、同一个用户；任务仍用独立副本运行。
- 用户只同意派任务，不代表同意复制个人配置。
- 内容不同且本地领先远端超过 7×24 小时才询问更新；不同步就沿用原版本。续跑始终用原任务配置。

```bash
rdispatch profile check mini
rdispatch profile sync mini --confirm-user-id zhangsan --confirm-version <完整指纹>
```

## 派单流程

```bash
rdispatch hosts                        # 探活所有机器
rdispatch doctor mini                  # 在仓库目录执行，检查目标机是否具备条件

rdispatch run --agent codex --prompt-file task.md --ref HEAD
rdispatch run --agent dsh --prompt-file task.md          # DSH headless
rdispatch run --agent agy --permission-mode skip --prompt-file task.md   # AGY；权限档位必填
rdispatch run --test                                     # 跑项目配置的测试命令
rdispatch run --cmd 'make bench' --heavy                 # 普通命令，占重任务锁
rdispatch run --host self --agent codex --prompt-file task.md   # 本机目标加 --host self
rdispatch run --agent dsh --prompt-file task.md --name smoke --project myrepo --timeout 3600   # 命名、项目归属、超时
rdispatch run --agent dsh --prompt-file next.md --wip --branch rd/manual   # 带未提交改动、指定分支

rdispatch status --details             # 阶段、摘要与更新时间
rdispatch watch <job>                  # 发起方等待唤醒：0 完成，1 告警，2 该报进度
rdispatch logs <job>                   # 看任务输出尾部
rdispatch wait <job>                   # 阻塞到结束：0 成功，1 失败，2 仍在跑
rdispatch result <job>                 # 收本轮结构化回执
rdispatch inbox                        # 当前发起会话待收取的结果
rdispatch inbox --wait [--timeout 秒] [--json]  # 只读等待本会话新汇报：有新汇报退出 0，超时退出 2；不签收
rdispatch ack <job> --round 2 --receipt-id <ID>
rdispatch resume <job> --prompt-file next.md   # 续跑沿用首轮凭据；要重新复制目标机认证加 --refresh-auth
rdispatch fetch <job>                  # 取回提交为 rd/<branch>
rdispatch stop <job>                   # 终止运行中的任务
rdispatch clean <job>                  # 删除已完成任务的远端 worktree 与日志
rdispatch clean --expired [--dry-run]  # 按保留期回收过期且安全的 worktree
rdispatch keep <job> --hours 48        # 明确还要续跑时钉住，--hours 0 取消
```

- 新派单用 `--initiator-session` 绑定当前会话 ID；未传时依次读 `RD_INITIATOR_SESSION`、`CODEX_THREAD_ID`。DSH 宿主不设这两个变量，必须显式传 `--initiator-session "$DSH_SESSION_ID"`；宿主没给 ID 就标未绑定，不声称结果会送达某会话。
- `--workdir`、`--include`、`--model`、`--effort` 按需覆盖默认值；完整旗标见 `rdispatch run --help`。
- 派 `agy` 必须带 `--permission-mode`，按任务性质选：改代码并提交用 `skip`；只读分析用 `plan`。`plan`、`accept-edits` 在无人值守时会自动拒绝 shell 命令，包括 git 提交。
- `ack` 只表示已收取，不表示验收通过，也不授权续派。只有新指令才产生下一轮。
- `rdispatch fetch` 取回的是候选，合并与验收由发起方判断。
- `rdispatch clean <job>` 前先收取回执、确认没有需要保留的产物；删除指定任务的远端 worktree 需用户确认。
- **过期 worktree 要记得回收**：每次 `rdispatch ack` 收完结果、每次派新单前，在仓库目录跑一次 `rdispatch clean --expired`，把输出里的 clean / keep 各一行报给用户。它只回收同时满足安全条件与保留期的 worktree（命令任务确认即回收；代理任务已合入 24 小时、已取回未合入 7 天；目标机磁盘不足时收紧），不需要逐个确认。判断不了下一步会不会续跑时不要手动删，交给保留期；已经知道还要续跑就先 `rdispatch keep <job> --hours N`。保留期细节见 [缓存保留](https://github.com/Weicoz/remote-dispatch/blob/main/docs/cache-retention.md)。
- **汇报靠后台 `inbox --wait` 唤醒，不写入输入框**：派单后把 `rdispatch inbox --wait` 放进宿主后台作业（可加 `--timeout`）。退出码 0 表示有新汇报：跑 `rdispatch inbox` 读结果，处理后 `rdispatch ack` 签收，再重挂 `inbox --wait`；退出码 2 表示超时无新汇报，直接重挂。`--wait` 只读，不签收、不写回执；加 `--json` 输出 `{reports:[{job,round,state}]}`（超时为 `timed_out:true`），便于脚本解析；rdispatch 不会向会话输入框写入任何汇报内容。宿主不能后台唤醒时（如 Codex，待实测），每轮开始先跑 `rdispatch inbox`，并如实说明边界。`resume` 默认沿用首轮凭据；目标机认证已变更时加 `--refresh-auth`。
- **派单后立即挂巡检**：把 `rdispatch watch <job>` 放进后台作业。它默认 900 秒返回一次：退出码 2＝该报进度、1＝告警、0＝完成。收到 2 时读 `rdispatch status --details` 与 `rdispatch logs` 尾部，向用户报一行真实进展（阶段、文件数、提交、阻塞、执行机资源），再重挂下一次 watch；收到 0／1 时取回结果并验收。任务结束后终结巡检作业。没有定时唤醒能力时改用 `rdispatch wait` 加超时兜底作业，并如实说明边界。

## 工作项、建议与看板

术语见 [术语表](https://github.com/Weicoz/remote-dispatch/blob/main/docs/glossary.md)。

```bash
rdispatch plan add <工作项> --depends <工作项> --after <派单>   # 登记还没派出的工作项及依赖；另有 set/done/cancel/remove/list
rdispatch suggest --plan-item <工作项> --agent codex --prompt-file task.md   # 只读：能否接入工作线，参数同 run
rdispatch run --plan-item <工作项> --context-from <前序派单> --prompt-file task.md   # 默认方式，带上下文包
rdispatch run --line <线名> --accept <令牌> --plan-item <工作项> --prompt-file task.md   # 接受建议（加 --accept-chain 本链后续都接受）
rdispatch run --no-line --plan-item <工作项> --prompt-file task.md   # 拒绝建议，按默认方式派出
rdispatch board                        # 派单数量、目标机、模型、时长、待派与阻塞
rdispatch gate <ref> [--host H]        # 合入前门禁：对提交跑一次全量测试
rdispatch inbox --orphans              # 没有发起会话的派单，集中签收
rdispatch line prefix <job> <工作项...> [--branch B]   # 线上前 k 个工作项都已验收后，在验收轮 head 建前缀分支供先合入
rdispatch line close <job>             # 关闭工作线；之后该派单按合入状态进入回收
rdispatch takeover <job> --initiator-session <会话>    # 同一用户 ID 的其他会话接手工作线，写入交接记录
rdispatch clean --slim [<job>] [--dry-run]   # 瘦身：只删可再生子路径，保留 worktree 与代码
```

- **派单前先调 `suggest`**：派单模式为 `suggest` 且用户在场时，每次派新工作项前先跑 `rdispatch suggest`（参数同 `run`）。有建议就原文转告用户（线名、目标机、分支、权限档位、令牌），用户接受才用 `--line --accept` 派；用户拒绝用 `--no-line`。不得代用户接受。
- **无人值守**：用户不在场时不调 `suggest` 等人，按默认方式派出；会话已设派单模式 `line` 时由 `run` 自己判断接线。
- **上下文来源**：派新工作项时，用 `--context-from` 取该工作项依赖边上的前序派单（`plan` 里 `--depends` 工作项所关联的派单，以及 `--after` 派单），可重复。不靠自己转述前序内容。
- 工作线管理用 `rdispatch plan chain <线名>`（`--clear`、`--revoke-accept`）。`board` 只读，不连目标机。
- `line prefix` 的工作项必须是线上顺序的前 k 个且已验收，提交需先 `fetch` 取回；`line close` 在线上有运行中轮次或目标机不可达时拒绝。`takeover` 只限同一 `RD_USER_ID` 与工作线，其他情况一律拒绝。
- `clean --slim` 后下次 `resume` 会重新跑 setup；`inbox --orphans` 的输出带可直接执行的 `ack` 命令。
- **局限：rdispatch 不能强制代理先调 `suggest`。** 这条规则只靠本技能提示，代理跳过时 `run` 照常按默认方式派出，不报错。用户要求必须先建议时，编排方自己核对每次派单前有没有跑过 `suggest`。

## serve 与设置命令

`rdispatch serve` 是控制端上常驻的 rdispatch 进程，CLI 与其他客户端共用同一份状态和配置。serve 在运行时，CLI 的写命令（`run`、`resume`、`stop`、`ack`、`review` 等）经本机 Unix socket 交给它执行；不在运行时 CLI 直接写，两条路径共用同一把锁。转交失败时提示"命令可能已执行"，先 `rdispatch status` 查状态，不要重试。

```bash
rdispatch service install              # macOS：用 LaunchAgent 托管常驻 serve，开机自启、崩溃拉起
rdispatch service status [--json]      # 加载状态、进程、socket、包版本、合同版本、配置重载结果
rdispatch service restart              # 升级 rdispatch 后执行，新代码才生效
rdispatch service uninstall            # 卸载并删除 plist，保留状态目录与日志
```

- serve 每秒检查 `config.ini`，变化后自动重载；配置写坏时继续用旧配置，错误显示在 `rdispatch service status`。
- 常驻 serve 用默认 socket `$RD_STATE/serve.sock`。临时调试另起 serve 时用独立 `RD_STATE`，不要与常驻 serve 共用状态目录。
- 只有用户要求时才安装、重启或卸载服务；这些命令会改变本机后台进程。

设置类命令都支持 `--json`：

```bash
rdispatch session-access [show|on|off]        # 会话访问总开关
rdispatch host list                           # 配置中的目标机，含禁用项，不回显凭据
rdispatch host add mini --ssh user@host --workdir ~/remote-dispatch --sync origin
rdispatch host update mini --set max_heavy=2  # --unset FIELD 恢复继承
rdispatch host delete mini                    # 有运行中派单时拒绝，并列出派单 ID

rdispatch device list                         # 已配对设备：配对时间、最近在线时间（需要 serve）
rdispatch device revoke <device-id>           # 立即吊销（需要 serve）
rdispatch pair open | close                   # 打开或关闭配对窗口（需要 serve）
```

- `device`、`pair` 的状态只在 serve 进程里；serve 不在线时这两组命令退出 4 并提示 `rdispatch service install`。把提示转告用户，不要自行安装服务。
- `session-access`、`host` 改的是 `config.ini`：serve 在线时交给 serve，不在线时直接写，写前自动备份。`rdispatch hosts` 仍用于探测目标机状态，`rdispatch host` 只管配置。
- `pair open` 输出的配对码只给用户本人看，不写进日志、文件或回报。
- 删除目标机、吊销设备、关闭会话访问都需要用户明确要求。

其他命令：

```bash
rdispatch review <job> --round 2 --accept            # 发起会话验收本轮
rdispatch review <job> --round 2 --reject "附言"      # 打回并用附言续跑
rdispatch session-default <session> --host mini --harness codex   # 会话派单默认值；--clear 改读配置
rdispatch project hide|show <项目ID>                 # 隐藏或恢复项目；rebuild 从派单与回执重建索引
```

## 代理支持

| 代理 | 状态 | 隔离 |
| --- | --- | --- |
| `codex` | 完整支持 | 每任务独立 `CODEX_HOME` 加员工配置包与认证文件 |
| `dsh` | 支持（headless） | 每任务独立 `DSH_HOME` 加 `dsh_auth_file` 凭据副本；不继承宿主 `~/.dsh` |
| `agy` | 支持（print 模式，仅 POSIX） | 每任务独立 `HOME`，只复制目标机 `agy_auth_file` 登录凭据；不加载个人 `~/.gemini` 技能、MCP、插件 |
| `kimi` / `claude` | 仅模板 | 未实现隔离，`rdispatch run` 会拒绝派单 |

`dsh` 由 `dsh headless --json` 启动，续跑加 `--session-id`；headless 没有 `--model`，模型写在员工 DSH 配置里。本机目标与远端共用同一套模板和隔离。目标机不接受 Windows（本机目标也要求 POSIX）。

`agy` 用 `--print` 加 stream-json 输入输出启动，续跑加 `--conversation`；`--model`、`--effort` 可用。目标机要先由机器主人运行 `agy` 登录；`rdispatch doctor <机器> --agent agy` 分开检查安装与登录。回执出现 `denied_actions` 时 `outcome` 为 `partial`，说明权限档位挡住了操作。

## 发起方为 Windows

直接用 `rdispatch`，或 `python -m remote_dispatch <命令>`、包内 `bin/rdispatch.cmd`、`bin/rdispatch.ps1`。不要用旧名 `rd`：PowerShell 里它是 `Remove-Item` 别名，CMD 里是内置 `rmdir`。

## 参考文档

对应项目仓库的 `docs/` 目录，冲突时以那里为准。

- [代理适配与自定义](https://github.com/Weicoz/remote-dispatch/blob/main/docs/agents.md)
- [平台差异（macOS / Linux / Windows）](https://github.com/Weicoz/remote-dispatch/blob/main/docs/platforms.md)
- [目标机准备](https://github.com/Weicoz/remote-dispatch/blob/main/docs/worker-setup.md)
- [员工配置包格式](https://github.com/Weicoz/remote-dispatch/blob/main/docs/user-profiles.md)
- [多轮交接协议](https://github.com/Weicoz/remote-dispatch/blob/main/docs/handoff.md)
- [术语表](https://github.com/Weicoz/remote-dispatch/blob/main/docs/glossary.md)
- [运行状态与缓存保留](https://github.com/Weicoz/remote-dispatch/blob/main/docs/cache-retention.md)
