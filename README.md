# Remote Dispatch（`rd`）

把编码代理任务和测试命令，从本机经 SSH 派到另一台机器执行。

本机写代码、看结果；远端拉分支、跑代理、跑测试；结果按轮次回传。适合「笔记本配置不够、家里有台 Mac mini 或服务器」的场景。

- 任务在一台机器发起、在另一台机器执行，走 git 分支同步（`origin` 或 `direct` 两种模式）。
- 每轮是一个独立回合：派单 → 收结果（结构化回执）→ 确认 → 续派，不靠猜日志。
- 支持多台机器：探活、按标签挑机、重任务限流（`rd-heavy` 共享锁）。
- 员工配置隔离：Codex 每个任务独立 `CODEX_HOME`；DSH 每个任务独立 `DSH_HOME`。宿主个人配置、skills、登录态不继承。

## 安装

需要本机 Python 3.9+、`git`、`ssh`。尚未发布到包索引与 Codeberg，先从本地目录或源码安装。

```sh
uv tool install --from /path/to/remote-dispatch remote-dispatch   # 或者
pipx install /path/to/remote-dispatch                             # 或者
pip install --user /path/to/remote-dispatch
```

在仓库目录内可以直接 `uv tool install .`。发布之后才能用：

```sh
uv tool install git+https://codeberg.org/jy_project/remote-dispatch
uv tool upgrade remote-dispatch
```

升级当前形态：重跑上面的安装命令。检查版本：`rd --version`。

## 五分钟上手

1. 写配置 `~/.config/remote-dispatch/config.ini`（从 `examples/config.example.ini` 起步）：

```ini
[host:mini]
ssh = weicoz@192.168.1.20
workdir = ~/remote-dispatch
sync = origin

[project:myrepo]
origin_url = git@github.com:me/myrepo.git
test = uv run pytest -q
```

2. 看机器活不活：

```sh
rd hosts
```

3. 在仓库里派一单代理任务：

```sh
rd run --agent codex --prompt-file task.md --ref HEAD
```

4. 看状态、取结果：

```sh
rd status <job>
rd result <job>          # 结构化回执；--summary 只要摘要
rd fetch <job>           # 把提交取回为 rd/<branch>
```

5. 续跑一轮：

```sh
rd resume <job> --prompt-file next.md
```

## 支持的代理

| 代理 | 状态 | 隔离方式 |
| --- | --- | --- |
| codex | 完整支持 | 每任务独立 `CODEX_HOME`，员工配置包 + 认证文件 |
| dsh | 支持（headless） | 每任务独立 `DSH_HOME`，员工凭据文件；不继承宿主 `~/.dsh` |
| kimi / claude | 仅模板 | 未实现隔离，`rd run --agent` 会拒绝派单 |

DSH 需要在机器清单里给出员工凭据文件：

```ini
[host:mini]
ssh = weicoz@192.168.1.20
dsh_auth_file = ~/employee/dsh-credentials.yaml
```

## 安全边界

- 只做派单与取回，不代替你验收；检查状态是执行方自报，最终验收由发起方判断。
- 凭据文件只在目标机读取，复制进任务目录后 `chmod 600`，不写回执、不进日志。
- 删除远端 worktree 需要显式 `rd clean`，默认保留未提交与未取回的成果。
- 不自动跳过宿主账号的权限边界；`rd` 不提升权限、不改系统配置。

## 文档

- `docs/agents.md`：代理模板与自定义
- `docs/platforms.md`：macOS / Linux / Windows 差异
- `docs/worker-setup.md`：目标机准备
- `docs/user-profiles.md`：员工配置包格式
- `docs/handoff.md`：多轮交接协议
- `docs/cache-retention.md`：运行状态与缓存保留

## 开发

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

代码结构：`src/remote_dispatch/cli.py` 是命令行主入口，`rd_profiles.py` 负责目标机上的配置隔离（同时以单文件形式上传到执行机），`rd_messages.py` / `rd_cache.py` 管回执与缓存，`rd_health.py` 管探活与日志取样，`rd_native.py` 是 Windows 适配层。

## 许可

MIT。见 `LICENSE`。
