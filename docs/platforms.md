# Platforms

| 目标机 | platform | 运行方式 | 依赖 |
|---|---|---|---|
| macOS | `macos` 或 `posix` | SSH＋POSIX shell，Darwin 资源探针 | git、pgrep、Python 3.11+、Codex |
| 本机（macOS / Linux） | `macos` 或 `linux` 或 `posix` | `ssh = local`，直接在本机执行 | git、pgrep、Python 3.11+、要派的代理 CLI |
| Linux | `linux` 或 `posix` | SSH＋POSIX shell，`/proc` 资源探针 | git、procps、Python 3.11+、Codex |
| Windows | `windows` | OpenSSH＋PowerShell＋Python 原生 worker | OpenSSH Server、PowerShell 5.1+、git、Python 3.11+、Codex |

本机目标把 `ssh` 写成 `local`，进程直接在本机启动，不经过 sshd，也不跑外网探针。Windows 目标必须走 SSH，`local` 与 `platform = windows` 同时出现会被拒绝；Windows 发起方也不能配置 `ssh = local`，运行时会直接报错。本机目标同样使用独立 worktree、任务目录与负载阈值，且不会被 `--host auto` 选中，必须显式指定。

管理员在机器清单一次配置平台，员工只选择机器和自己的 ID。`platform` 默认 `posix`，旧 macOS/Linux 清单继续使用。Windows 必须显式设置 `platform = windows`；不能把检测失败当成空闲机器。原生 Windows 无需 Git Bash 或 WSL；WSL 通过 Linux SSH 服务连接时按 Linux 主机登记，不混用 Windows 和 Linux 的路径。

## Windows Setup

```ini
[host:win]
ssh = worker@windows-host
platform = windows
workdir = C:/rd
python = C:/Python312/python.exe
sync = direct
; 可选：Windows 用分号分隔；空值继承目标机原有 PATH
path = C:/Program Files/Git/cmd;C:/Python312;%APPDATA%/npm
; Windows 是 JSON 环境文件，不是 source 的 shell 脚本
env_file = C:/worker-private/api-env.json
; 仅在管理员明确批准后设置，共用目标机既有登录
; codex_auth_file = C:/Users/worker/.codex/auth.json
max_cpu_pct = 85
max_heavy = 1
```

`python` 是目标机 Python 可执行文件或 PATH 内命令名，不是一串 shell 参数。Windows 的 `path` 不带 POSIX `$HOME`，使用 Windows 路径或 `%USERPROFILE%`、`%APPDATA%`。`workdir` 推荐短路径，源码里的 Git 操作按调用启用 `core.longpaths`，不改全局 Git 配置。很深的项目仍需管理员确认系统长路径策略。

Windows 环境文件由管理员保存在目标机，并配置目录 ACL；内容是字符串键值 JSON。API 凭据只引用环境变量，不放进配置包或聊天。Windows 的 `chmod(0600)` 不提供 POSIX 权限隔离，访问控制依赖目标目录 ACL。不要把执行根目录放到所有人可读写的公共共享目录。

项目命令使用 PowerShell 语法。跨平台同样有效的 `npm ci`、`npm test` 可以继续用 `setup`、`test`；需要差异时补 `setup_windows`、`test_windows`。Windows 后台非交互脚本不读取个人 PowerShell profile，错误返回失败。

```ini
[project:myrepo]
setup = npm ci
test = npm test
setup_windows = npm ci
test_windows = npm test
```

## Transport and Lifecycle

Windows 的元数据使用 JSON，配置和二进制包按 Base64 分块经 SSH stdin 发送与回传，避免 PowerShell 5.1 对原始二进制流的文本转换。启动命令只包含短 PowerShell `EncodedCommand`。不把大配置包放进命令行，不用 POSIX `sh`、`nohup`、`pgrep` 或 `/proc`。`direct` 使用 Git bundle 回传，保留任务分支与员工目录规则。

后台采用 Python `DETACHED_PROCESS`、`CREATE_NEW_PROCESS_GROUP` 和 `CREATE_BREAKAWAY_FROM_JOB`，标准输入关闭，输出重定向任务日志，不继承 SSH 管道。目标 OpenSSH 不允许脱离 Job Object 时返回失败，不回退到断线后被杀的前台进程。首次设备验收必须在 SSH 命令返回后回读任务状态和最终输出。超时、停止使用 `taskkill /T /F`；重任务锁仍为整机级目录锁。

CPU 使用 Windows 采样利用率，默认上限 `max_cpu_pct=85`；空闲内存和磁盘沿用统一阈值；页面文件只报已用量、不采样换页速率，因此只提示不拦。资源探针失败时不派单。Windows 的 CPU 采样不是 Unix 一分钟 load average，不混用两者。

## Acceptance

```bash
rdispatch doctor win --agent codex
rdispatch hosts
rdispatch profile check win
```

初次同步仍先询问员工 ID、是否同步，并审阅清单和指纹。确认同步后，在已授权的测试仓库执行最小命令任务，保存 job id，等待、读日志并取回。确认 SSH 已退出后任务仍运行，再验 Codex 首次启动、提交回传和同一配置版本续跑。登录和系统权限步骤由用户接管。

不得把 PowerShell 编码测试或 macOS 上的 worker 协议测试报告成真实 Windows 实跑。仓库测试使用临时本地 worker 和假 Codex，不发模型 API 请求。当前实际验证范围以本次交付报告为准。

## Windows 作为发起方（Initiator）

当本机（派单控制端）为 Windows 时，需特别注意命令行环境与系统别名：

1. **旧命令名 `rd` 的别名陷阱**：0.2.0 起命令改名为 `rdispatch`，正是因为 `rd` 在 PowerShell 是 `Remove-Item` 别名（`rd run ...` 会删除名为 `run` 的文件）、在 CMD 是内置 `rmdir`、在 oh-my-zsh 是 `rmdir` 别名。Windows 上不要用旧名 `rd`。
   - **入口**：`rdispatch`，或 `python -m remote_dispatch <command>`，或包内 `bin/rdispatch.cmd`、`bin/rdispatch.ps1`。旧的 `bin/rd-remote.cmd`、`bin/rd-remote.ps1` 保留一个版本后移除。
2. **CMD 内置命令**：CMD 的内置 `rd`（`rmdir`）优先于同名脚本，任何名为 `rd.cmd` 的包装都不会被执行，所以包内不再提供。
3. **本地仓库与目录自定义**：
   - 默认使用当前终端所在目录（`cwd`）。
   - 支持通过 `-C <路径>` 或 `--dir <路径>` 指定 Windows 绝对路径（例如 `-C D:\Projects\myrepo`）。
   - 支持通过 `--workdir <路径>` 覆盖远端的目标存储路径。
4. **状态与配置目录**：自动映射至 `%USERPROFILE%\.config\remote-dispatch\config.ini` 与 `%USERPROFILE%\.local\state\remote-dispatch`，支持环境变量 `RD_CONFIG` 和 `RD_STATE` 自定义。
5. **不能配置本机目标**：Windows 发起方写 `ssh = local` 不受支持，`rdispatch` 会直接报错。本机目标只支持 POSIX 发起方；Windows 上的并行会话请登记成 SSH 目标机。

实现依据：[Microsoft OpenSSH 配置](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)、[Python subprocess Windows flags](https://docs.python.org/3/library/subprocess.html#subprocess.CREATE_BREAKAWAY_FROM_JOB)、[OpenSSH 维护者关于后台进程与 SSH 管道的记录](https://github.com/PowerShell/Win32-OpenSSH/issues/2452)。
