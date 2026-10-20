# 变更记录

## 未发布

- 新增本机目标 `ssh = local`：直接在本机执行，使用独立 worktree 与任务目录；`codex`/`dsh` 有员工配置隔离，`kimi`/`claude` 仍拒绝派单。
- `--host auto` 与 `--tag` 不再选本机目标；本机必须显式 `--host self`。
- 本机目标跳过外网探针；`rd hosts` 标注「(本机)」，`rd doctor` 说明跳过。
- `workdir` 统一展开 `~` 与 `$HOME`，修复 `$HOME/x` 被当作字面路径。
- Windows 发起方配置 `ssh = local` 时直接报错，不再抛 `FileNotFoundError`。
- 修 `rd doctor --agent <name>` 崩溃；显式指定的代理缺 CLI 时判失败。
- 新增本机端到端回归、GitHub Actions 测试与技能副本链接检查。

## 0.1.0 — 2026-10-20

首个独立版本，从 `weicoz-skills` 的 `weicoz-remote-dispatch` 技能迁出。

- 打包为 `remote-dispatch`，命令行入口 `rd`；配置仍在 `~/.config/remote-dispatch/config.ini`。
- 新增 DSH 派单：`dsh headless --json` 启动与 `--session-id` 续跑，每任务独立 `DSH_HOME`，凭据文件复制进任务目录并 `chmod 600`。
- `rd run --agent` 现在接受 `codex` 与 `dsh`；`kimi` / `claude` 仍只保留模板，未实现员工配置隔离前拒绝派单。
- 保留 130 项既有回归测试，新增 10 项 DSH 测试。
