# 添加一台机器

共享员工配置与目录规则见 [Employee Profiles](user-profiles.md)，员工操作指南见 [README](../README.md)。添加机器不代表已同步员工配置。

先确认目标平台。macOS/Linux 使用现有 POSIX 流程；原生 Windows 的 Python、PowerShell、路径、环境文件和后台进程见 [Platforms](platforms.md)，不要执行本页的 POSIX 示例来安装 Windows 环境。

## 本机目标机

要让同一台机器上的另一个 harness 并行干活，不必配 SSH。加一节：

```ini
[host:self]
ssh = local
workdir = ~/rd-self
sync = direct
```

本机需要 `git`、`pgrep`（Linux 用 procps）、Python 3.11+，以及要派的代理 CLI。`rdispatch doctor self` 会逐项检查。派 dsh 仍需 `dsh_auth_file`；provider key 缺失的处理见 [代理适配](agents.md)。负载、内存、磁盘阈值与远端一致，超阈值派单会被拦。

派 Codex 到本机目标时，员工配置副本缺失或落后于本机配置包会先自动同步，输出一行说明，不要求确认；任务仍用独立副本运行。SSH 目标行为不变，首次仍须确认 ID 与版本指纹，见 [用户配置](user-profiles.md)。

以下步骤按 SSH 目标描述。本机目标跳过免密登录与远端 SSH 安装，但 git、Python、代理 CLI 仍须在本机可用。

## 1. 本机能免密登录

```bash
ssh -o BatchMode=yes <用户>@<地址> true
```

不通就先配好 SSH 密钥。`rdispatch` 不处理密码和交互式登录。

## 2. 写进机器清单

在 `~/.config/remote-dispatch/config.ini` 加一节，字段说明见 `examples/config.example.ini`：

```ini
[host:box2]
ssh = user@10.0.0.12
sync = direct
max_heavy = 1
```

并发怎么定：**没有会话数上限**，一台机器同时能接几个任务由负载决定——每次派单前 `rdispatch` 都会实测负载、空闲内存和磁盘，超过 `max_load_per_core`、低于 `min_free_mem_pct` 或 `min_free_disk_gb` 就不派。交换区看换页速率而不是已用量：持续高速换页（`max_swap_io_mb_s`），或剩余低于 `min_swap_free_gb` 且仍在换页，才不派；已用量超过 `max_swap_used_gb` 只提示。`max_heavy` 不是会话上限，它是重命令的排队口：经 `rd-heavy` 或 `--heavy` 跑的完整测试这类命令同一时刻只放行这么多个，其余排队等，避免几套完整测试同时把内存耗尽。先看项目一个测试进程占多少内存，乘以并发数，不能超过机器的空闲内存；16 GB 的机器一般是 1。

## 3. 远端装好要用的东西

- `git`。
- 要派的代理命令行，并且已经登录。登录是交互步骤，需要机器的主人自己在那台机器上完成。
- 项目自己的工具链（包管理器、运行时、Docker 等）。
- 员工 Codex 配置操作：本机与远端 Python 3.11+。Codex 必须支持 `--disable` 关闭 hooks、plugins、memories 和外部记忆导入。
- DSH：`dsh headless` 可用，`dsh_auth_file` 指向目标机上的凭据文件。
- AGY：`python3` 与支持 `--input-format` 的 `agy`；机器主人在目标机运行一次 `agy` 登录，生成 `agy_auth_file`（默认 `~/.gemini/antigravity-cli/antigravity-oauth-token`）。只支持 POSIX。`rdispatch doctor <机器> --agent agy` 分开报告“已安装”和“已登录”。
- `kimi`、`claude` 当前不启动。

非交互的 SSH 会话不读 `.zshrc`、`.bashrc`，装在用户目录下的命令往往找不到。把这些目录写进该机器的 `path`。默认已包含 `~/.local/bin`、`~/.kimi-code/bin`、`/opt/homebrew/bin`、`/usr/local/bin`。需要的环境变量（例如接口密钥）写进远端的一个文件，用 `env_file` 指过去，任务启动前会被加载。


员工配置包不携带凭据。要共用现有登录，管理员显式设置 `codex_auth_file` 指向已授权的远端认证文件；否则使用配置引用的 API 环境变量。配置不自动读目标机默认 `auth.json` 或钥匙串。后台权限使用员工配置，`approval_policy` 必须是 `never`。

## 缓存目录

目标机配置 `cache_dirs` 声明各生态的缓存目录，派单时自动注入为环境变量（POSIX 运行脚本和 Windows 原生都注入），目录会自动创建。格式是 `NAME=目录`，多项用分号分隔：

```ini
cache_dirs = CARGO_HOME=cargo; PIP_CACHE_DIR=pip; npm_config_cache=npm; PNPM_HOME=/data/rd-cache/{user}/pnpm
```

- 相对目录展开到该用户 ID 根目录下的 `cache/` 子目录（`<workdir>/users/<ID>/cache/<目录>`），不同用户 ID 互不共享。
- 绝对目录必须含 `{user}`，展开为用户 ID。
- 展开后落在任何 worktree（`<workdir>/wt`、`<workdir>/users/<ID>/wt`）内、含 `..` 或变量名不合法的声明，加载配置时报错（退出码 4）。缓存在 worktree 之外，回收 worktree 不会删掉它。
- 没有声明时，行为与之前一致。

## 4. 仓库访问（只在 `sync = origin` 时需要）

远端要能读写 `origin`。几种做法，按你对这台机器的信任程度选：

- 在远端生成 SSH 密钥，作为只对这一个仓库有写权限的部署密钥加到仓库上。权限最小，推荐。此时远端要用 SSH 地址，在项目小节里写 `origin_url = git@github.com:<所有者>/<仓库>.git`。
- 在远端登录 `gh auth login`，或配置带令牌的凭据助手。权限是整个账号的，只适合完全属于自己的机器。

不想在远端放任何仓库凭据，就用 `sync = direct`。

## 5. 项目配置

```ini
[project:myrepo]
setup = <在新 worktree 里装依赖的命令>
test = <完整测试命令>
```

小节名是 `origin` 地址的仓库名（没有 `origin` 时是目录名）。

- `setup` 在每个任务第一次启动前执行一次，失败则任务以 97 结束。新 worktree 没有依赖环境，不配这一项的话，代理第一件事就是自己摸索怎么装，既慢又不稳定。
- `test` 是 `rdispatch run --test` 跑的命令，默认按重命令排队。不需要排队时加 `test_heavy = false`。
- `origin_url` 只在远端访问仓库的地址和本机不同时才写。

## 6. 体检

在项目目录里执行：

```bash
rdispatch doctor box2
rdispatch run --cmd 'git log -1 --oneline' --host box2 --name smoke
```

第二条跑通（`rdispatch wait` 返回 0，`rdispatch logs` 能看到提交）说明同步、启动、日志都正常。跑完 `rdispatch clean <job> --delete-branch`。

## 远端目录结构

都在该机器的 `workdir` 下（默认 `~/remote-dispatch`），整个目录可以随时删掉重来，前提是没有未取回的任务：

```
locks/                                      整机重命令排队锁
users/<ID>/profiles/<指纹>/                  员工配置版本（不自动删除）
users/<ID>/repos/<项目>-<仓库标识>.git        员工裸仓库
users/<ID>/wt/<job>/                         任务 worktree
users/<ID>/jobs/<job>/                       提示词、输出、退出码
users/<ID>/jobs/<job>/codex/                 任务独立配置与会话
users/<ID>/bin/rd-heavy                      使用整机锁的排队命令
```
