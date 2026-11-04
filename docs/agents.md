# 代理适配

每个代理是一组命令模板加一份能力登记，都在 `src/remote_dispatch/rd_agents.py`：`AGENTS` 是模板，`SPECS` 登记可派平台、配置隔离方式、凭据项、目标机预检和权限档位。机器清单里的同名 `[agent:NAME]` 小节只能覆盖内置模板的字段；`SPECS` 里没有的代理一律拒绝派单，新增代理必须先在代码里完成隔离适配。

目标机写 `ssh = local` 时，命令直接在本机执行，模板、隔离、worktree 与远端一致；不经过 SSH，也不要求本机运行 sshd。限制是必须为 POSIX，Windows 目标只能走 SSH。

本版共享员工模式启用 Codex、DSH（`dsh headless --json`）与 AGY（`agy` print 模式）；`kimi`、`claude` 模板保留但派单被阻止。员工 `CODEX_HOME`、路径映射、认证和续跑版本由 [Employee Profiles](user-profiles.md) 管理。Codex 的 sandbox 不再强制完全访问，使用员工配置；后台要求 `approval_policy = "never"`。新版员工模式须独立验收，旧版实际跑通不代表新版已在目标机实跑。

## 内置代理

| 名称 | 启动方式 | 换模型 | 推理强度 | 验证情况 |
| --- | --- | --- | --- | --- |
| `codex` | `codex exec`，员工权限配置、JSON 输出 | `--model` | `--effort` | 旧版启动、续跑、提交回传实跑；新版员工模式另行验证 |
| `dsh` | `dsh headless --json` | 不支持，由目标机的 headless profile 决定 | 不支持 | 本机目标（`ssh = local`）已用临时状态实跑派单、隔离 `DSH_HOME` 与凭据复制，默认配置无留存记录；模型调用因 provider 路由缺 key 报 `MISSING_CREDENTIAL`，回复质量未验证 |
| `agy` | `agy --print`，stdin 传 stream-json 消息、stream-json 输出 | `--model`（`agy models` 列出） | `--effort` | 本机目标（`ssh = local`）用真实 agy 实跑派单、提交、回执与 `--conversation` 续接；远端 SSH 目标未实跑 |
| `kimi` | `kimi --auto`，stream-json 输出 | `--model`（别名见远端 `~/.kimi-code/config.toml`） | 不支持 | 命令取自帮助文档，未实跑 |
| `claude` | `claude -p`，跳过权限询问，stream-json 输出 | `--model` | 不支持 | 命令取自帮助文档，未实跑 |

未实跑的组合第一次使用时要确认两件事：启动后 `rdispatch logs <job>` 有输出、文件确实写进了 worktree；续跑时 `rdispatch resume` 打印出了会话编号。会话编号取不到，就在日志里找到它的字段名，按下面的方法改 `session_regex`。

### 凭据只在首轮复制

Codex（`codex_auth_file`）、DSH（`dsh_auth_file`）、AGY（`agy_auth_file`）的认证文件只在首轮复制进任务隔离目录，续跑沿用。首轮没有配置凭据、之后才补配时，同样需要 `--refresh-auth` 才会复制。AGY 续跑时任务内 token 文件缺失，不再自动从 `agy_auth_file` 补复制，而是报错并提示 `rdispatch resume <job> --refresh-auth`。`rdispatch resume JOB --refresh-auth --prompt-file ...` 只在本轮重新复制认证文件、权限 0600；员工配置副本、会话与 worktree 不动。POSIX 与 Windows 原生目标（仅 Codex）都支持。

本轮失败输出出现明确认证失败（Codex 的 `401 Unauthorized`、AGY 的 `authentication failed`／未登录、DSH 的 `MISSING_CREDENTIAL`）时，回执记 `credential_suspect: true` 和 `credential_hint`，`result`、`inbox`、`board` 显示重新复制的命令。rdispatch 不自动重复制、不自动续跑；识别不了的失败不标记。判定集中在 `rd_messages.credential_suspect()`。

本轮非零退出且输出出现已知失败模式时，回执另记 `failure_hint` 与一句中文说明 `failure_note`：`auth_failed`（认证失效，优先）或 `network_unreachable`（`workspace routing discovery failed`、TLS 握手失败、连接超时／重置／拒绝、DNS 失败等）。只看最后一条失败事件（最后一条 `turn.failed` 或 `error`；没有 JSON 事件时取末尾 20 行里最后一条纯文本行），瞬时重连（`Reconnecting… waiting for network`、`Reconnecting… 1/5`）不算，其后又出现 `item.completed`／`turn.completed` 的错误视为已重试掉、也不算；纯文本输出不用于判定（只有 DSH 取末尾 20 行里最后一条以 error／fatal 开头的行）；不看代理的回复正文，识别不了不标记。`result`、`inbox`（摘要）、`board` 显示该提示。派单前网络探针不通时，`run` 退出码 3 拒绝并写明 TLS 握手失败／超时／DNS 等原因，建议换主机或稍后重试；`--host auto` 跳过该机，`--force-host` 跳过检查。判定集中在 `rd_messages.failure_hint()`。

### dsh 的凭据

`rdispatch run --agent dsh` 要求目标机配置 `dsh_auth_file`，本机目标也不例外。隔离 `DSH_HOME` 里只复制这份凭据与 `AGENTS.md`。

headless profile 的模型路由需要 provider key。缺失时报：

```text
llm-deepseek: no API key for provider route "deepseek-official"
code: MISSING_CREDENTIAL
```

裸跑 `dsh headless --json '只回复 OK'` 也报同样错误时与 rdispatch 无关。处理：把 key 写进凭据文件，或把它导出到目标机 `env_file` 指向的文件。派单端 `export DEEPSEEK_API_KEY=...` 只对本机目标（`ssh = local`）有效；SSH 不转发派单端环境变量。rdispatch 只向 DSH 保留 `RD_`、`DSH_`、`DEEPSEEK_`、`OPENAI_`、`ANTHROPIC_` 前缀与代理网络变量（`HTTP(S)_PROXY`、`ALL_PROXY`、`NO_PROXY`、CA 证书路径），其余不传。

### agy 的登录、隔离与权限

AGY 没有配置目录变量，rdispatch 给每个任务一个独立 `HOME`（任务目录下的 `agy-home/`），只把目标机自己的登录凭据复制进去，权限 0600。个人 `~/.gemini` 里的技能、MCP、插件、设置与会话都不加载，任务会话也只写在 `agy-home/` 里。凭据不进员工配置包，也不跨机器同步。

- 凭据位置由 `agy_auth_file` 指定，默认 `~/.gemini/antigravity-cli/antigravity-oauth-token`。目标机先由机器主人运行一次 `agy` 完成登录；缺这个文件时 `rdispatch run` 在推送任务分支前停止。凭据只在首轮复制进任务目录，之后续跑沿用任务内副本；目标机上刷新过凭据时，用 `rdispatch resume JOB --refresh-auth --prompt-file ...` 重新复制覆盖（见下文「凭据只在首轮复制」）。
- `HOME` 换成任务目录后，git 通过 `GIT_CONFIG_GLOBAL` 继续读目标机账号的 `~/.gitconfig`，提交身份与凭据助手不变。
- `--permission-mode` 必填，无默认值：`plan`、`accept-edits`、`auto`、`skip`（`--dangerously-skip-permissions`）。无人值守时 AGY 无法弹出确认，`plan` 与 `accept-edits` 会自动拒绝 shell 命令（包括 git 提交）；`auto` 在部分账号不可用，会退回默认模式。要代理改代码并提交，只能用 `skip`。续跑沿用原档位，显式传 `--permission-mode` 才改。
- 被拒绝的权限操作不会让 AGY 报错：退出码仍为 0、状态仍为 `SUCCESS`。rdispatch 读到终态里的 `denied_actions` 时，把回执的 `outcome` 降为 `partial` 并列出被拒动作，`rdispatch result --summary` 可见。
- 提示词不走命令行参数：rdispatch 用 python3 把提示词编码成一条 `{"event":"user","message":{"role":"user","content":...}}`，经 stdin 传给 `agy --input-format stream-json`，长度不受参数上限限制。
- 终态 `result.response` 写入 `last.r<轮次>.txt`，`rdispatch logs <job> last` 可读；会话编号取 `conversation_id`。
- 未登录时 agy 约 5 秒内退出（退出码 1、`status = ERROR`），回执归类为 `authentication` 故障。
- 连通探针默认检查 `https://cloudcode-pa.googleapis.com`，可用 `network_model_url` 覆盖。
- 只支持 POSIX 目标；Windows 目标拒绝派 AGY。

## 模板里能用的变量

命令在任务的 worktree 目录里执行，由 `sh` 解释。

| 变量 | 含义 |
| --- | --- |
| `$RD_WT` | worktree 路径（也是当前目录） |
| `$RD_JOB_DIR` | 这个任务的日志目录 |
| `$RD_PROMPT` | 本轮提示词文件 |
| `$RD_ROUND` | 第几轮（首次为 1，每次续跑加 1） |
| `{model_args}` | 由 `--model`、`--effort`、`--permission-mode` 及隔离开关生成的参数；模板必须保留 |
| `{session}` | 续跑时的会话编号 |

命令的标准输出存为 `stdout.r<轮次>.log`，会话编号就是用 `session_regex` 从这里匹配出来的。

## 覆盖内置模板

```ini
[agent:dsh]
; 换成目标机上的包装脚本；隔离、凭据与回执仍由 rdispatch 处理
start = my-dsh headless --json {model_args} - < "$RD_PROMPT"
resume = my-dsh headless --json --session-id {session} {model_args} - < "$RD_PROMPT"
```

覆盖只改命令写法，不改隔离方式；Codex 的沙箱与权限仍取员工配置包，不能靠模板收紧。`start` 与 `resume` 必须保留 `{model_args}`，否则派单前拒绝。Windows 目标只接受内置 Codex 模板。

各字段：

- `start`：首次启动。
- `resume`：续跑。不写就不能 `rdispatch resume`。
- `model`、`effort`：对应 `--model`、`--effort` 的参数写法。不写表示不支持，用户传了会报错。
- `session_regex`：从输出里取会话编号的正则，第一个括号是编号。默认匹配 `session_id`、`sessionId`、`thread_id` 三种字段名；`agy` 内置为 `conversation_id`。
- `bin`：`rdispatch doctor` 用来检查是否安装的命令名，默认取 `start` 的第一个词；`agy` 内置为 `agy`。

只覆盖一个字段时，其余字段沿用内置定义。
