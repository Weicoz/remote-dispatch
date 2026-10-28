# 票 10：本机目标派 Codex 不再要求确认同步配置

范围仅 `remote-dispatch`；未改 App、未改 serve、未改 skill，未 push、未 commit、未 merge。
分支 `rd/weicoz/u10-20261028-124312-057246000`，基线 `integration/cli-app-unify` `3b41d5f`。
票据 `.rd-context/cli-app-unify/10-local-host-skip-profile-sync.md`，规格
`.rd-context/cli-app-unify/spec.md`。

## 实现概要

- `cmd_profile` 的同步逻辑抽成 `sync_profile(host, uid, local, payload, remote)`：内容一致返回
  `None`，否则执行原有的 Windows RPC / POSIX tar 写入、`.sync-lock`、原子替换与回读校验，
  返回回读后的清单。手动 `rdispatch profile sync` 行为不变，只是成功输出由调用方打印。
- 新增 `prepare_local_profile(host, uid, local, payload, profile)`：本机目标（`ssh = local`）在
  `missing` / `stale` / `different` 时用同一套 `sync_profile` 直接同步，输出一行
  `本机目标 <机器>：已自动同步员工配置副本 version=…；无需确认。`；`same` 经由
  `sync_profile` 的 `None` 直接沿用，不重复写；`remote-newer`（副本比本地配置包新）时停止派单，
  不覆盖较新副本；`unknown`（本机无配置包）沿用已有副本，连副本都没有时停止。
- `agent_preflight` 按目标类型分流：本机目标走 `prepare_local_profile`，SSH 目标保留原
  `profile_report` + “此 ID 尚无远端配置；先询问是否同步…”拒绝路径。
- `agent_preflight(auto_sync=False)` 用于续跑换目标机：本机目标不自动同步，必须先有与原派单
  同版本的副本，否则仍按原逻辑拒绝；避免续跑先改目标机配置指针再失败。
- 隔离不变：任务仍从 `<workdir>/users/<ID>/profiles/<指纹>` 复制出任务自己的 `codex/` 目录，
  不读写用户自己的 Codex 目录。

## 逐条验收证据

测试源码 `tests/test_rd_local.py` 的 `LocalTargetProfileSyncTest`（7 项），全部以真实 CLI 子进程
（`python -m remote_dispatch run/profile/resume …`）驱动；临时 `HOME`、`RD_STATE`、`RD_CONFIG`、
`RD_PROFILE_DIR`，假的 Codex 配置包与假 `codex` 可执行文件，未连接或读写真实
`~/.codex`、`~/.local/state/remote-dispatch`、`~/.config/remote-dispatch`、
`~/remote-dispatch-self`，未连接 dev serve。SSH 目标用假 `ssh` 只替换传输层，目标机逻辑不变。

| 验收项 | 实现及证据 |
| --- | --- |
| `ssh = local` 的目标机派 Codex 时，副本缺失或落后于本机配置则自动同步，不要求确认，输出一行说明 | `test_local_target_syncs_missing_profile_and_runs_with_an_isolated_copy`：副本缺失时 `run --host self --force-host` 退出 0，stdout 含 `本机目标 self：已自动同步员工配置副本 version=…；无需确认。`，`profiles/current.json` 的 ID 与版本目录落盘，输出中无 `--confirm-version`。`test_local_target_updates_a_stale_copy_and_keeps_old_versions`：先手动同步 v1，再改配置包后派单，自动同步到 v2 且保留 v1 版本目录。`test_local_target_does_not_sync_when_the_copy_is_current`：副本与配置源一致时再派单不重复同步，只有一个版本目录。真实 CLI 输出见 `10/local-sync-smoke.log`。 |
| 任务仍使用独立的员工配置副本运行，不直接改用户自己的 Codex 目录 | 同一用例读假 codex 记录的 `CODEX_HOME`：等于任务目录下的 `jobs/<job>/codex`，`HOME` 仍是临时账号目录，任务 `AGENTS.md` 含员工规则标记；断言临时 `HOME/.codex` 不存在。`record["config"]` 由任务副本的 `config.toml` 生成。 |
| SSH 目标机行为不变：首次仍须确认 ID 与版本指纹 | `test_ssh_target_still_requires_profile_confirmation`：假 `ssh` 只替换传输层；`run --host remote` 退出 4，stderr 含 `此 ID 尚无远端配置；先询问是否同步、确认 ID，再执行 rdispatch profile sync`，无自动同步输出，未创建 `profiles/` 与 `jobs/`。SSH 分支代码除签名默认值外未改。 |
| `rdispatch profile check/sync` 对本机目标仍可手动使用 | `test_local_target_updates_a_stale_copy_and_keeps_old_versions` 先跑 `profile check self`（输出 `Remote Dispatch: ID=employee_a host=self`），再跑 `profile sync self --confirm-user-id employee_a --confirm-version <指纹>`，退出 0 且 stdout 含 `已同步：ID=employee_a version=`。 |
| 测试覆盖本机目标自动同步、SSH 目标仍拒绝两种情况 | 7 项测试如上：缺失自动同步、落后自动同步、一致时不重复同步、较新副本不覆盖、无配置包停止、续跑换本机目标不自动同步、SSH 目标拒绝。 |
| `weicoz-remote-dispatch` skill 注明本机目标例外 | 未做，按派单约定并入 08 号票，由编排方处理；本票未改 `skills/`。**待编排方确认**：`.rd-context/cli-app-unify/` 目前只有 02/03/04/05/06/10，没有 08 号票文件；`skills/weicoz-remote-dispatch/SKILL.md` 仍写“首次登记或主动同步必须询问是否同步”，无本机例外。 |

## 回归保护（代码审查发现）

- 续跑换目标机（`resume --host self`）原本也会经过本机目标的 `agent_preflight`。若直接自动同步，
  会把目标副本切到最新本机配置包，随后 `resume_profile_mismatch` 失败，且失败前已改动目标状态。
  修复：`resume_job` 调用 `agent_preflight(..., auto_sync=False)`，续跑换本机目标沿用原派单版本，
  不自动同步。新增 `test_resume_moving_to_the_local_target_keeps_the_original_profile`：远程目标
  派单 v1 后改配置包，再 `resume --host self`；断言退出 0、记录 host/rounds 变为 `self`/2、
  本机副本仍为 v1、第二轮任务 `AGENTS.md` 无新版本标记、输出无自动同步提示。

## TDD 接缝与记录

约定接缝是本票 CLI 的外部行为：`python -m remote_dispatch run/profile/resume` 的退出码、
stdout/stderr、`<workdir>/users/<ID>/profiles/` 与任务目录下的文件；不测内部函数调用顺序。

- `docs/validation/10/tdd-local-auto-sync-red.log`：先写自动同步、落后更新两条用例与 SSH 拒绝
  回归用例。本机目标用例红：退出 4 并输出原确认提示；SSH 用例绿，证明拒绝行为是既有行为。
- `docs/validation/10/tdd-local-auto-sync-green.log`：抽出 `sync_profile`、新增
  `prepare_local_profile` 并在 `agent_preflight` 分流，后再补一致时不重复同步、无配置包停止、
  较新副本保护用例；7 项通过。
- `docs/validation/10/tdd-resume-keep-profile-red.log`：续跑回归用例在“总是自动同步”的实现下
  红：退出 4，`resume_profile_mismatch`，且已写入自动同步行。
- `docs/validation/10/local-sync-smoke.log`：独立 python 驱动同一派单场景，记录真实 CLI
  stdout、任务退出码 0、`CODEX_HOME` 落在任务目录、临时 `HOME/.codex` 不存在。

## 代码审查发现与处理

按 `code-review` 技能做 Standards / Spec 双轴审查（只读子代理并行）。

Standards 轴无硬性违反；判断项处理：

- 续跑换目标机先改目标再失败（阻塞性）：已修复，`auto_sync=False` + 回归用例。
- `prepare_local_profile` 中不可达的 `synced is None` 分支：已简化，`same` 统一经
  `sync_profile` 的 `None` 返回，不再预先枚举状态。
- 本机路径与 SSH 路径重复“指纹相等 / 远端更新”判断：本机路径只保留 `remote-newer` 专用文案，
  相等判断只保留在 `sync_profile`；`remote-newer` 另用本机文案，因为“远端”在本机目标上不准确。
- 并发同步竞争时本机目标仍可能看到共享脚本的 `远端配置已变化，请重新检查并确认`：沿用同一套
  同步脚本（票要求“同一套同步逻辑”），文案仍指示了正确动作；仅在竞争窗口出现，列为可接受。
- 测试 `setUp` 与文件内既有 `LocalEndToEndTest` 结构相似、参数组（host/uid/local/payload/remote）
  未打包、状态词仅在 `remote-newer` 处判断：均为判断项，与本文件/现有函数签名风格一致，
  本次不扩大重构。

Spec 轴缺项与问题：

- 验收第 6 条（skill 文案）按派单约定留给 08 号票，本票未改 skill；已在上方标注待编排方确认。
- 续跑换目标机的语义变化（见“回归保护”）：已修复并有红/绿证据。
- 未发现越界：未碰 serve、App、skill 与其他票文件。

## 检查与环境

Darwin arm64，Python 3.14.6；仓库没有 mypy/pyright 配置，未执行静态类型检查，
`compileall` 只证明语法可编译。相关测试串行执行，未跑全量（编排方合入后统一跑）。
本机同时有其他票在跑，负载较高时用例仍全绿。

| 命令 | 结果 |
| --- | --- |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_local.LocalTargetProfileSyncTest -v` | 7 项通过，20.904 秒；日志 `tdd-local-auto-sync-green.log`。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_local -q` | 26 项通过，25.466 秒；日志 `local-tests.log`。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd -q` | 48 项通过，16.672 秒；日志 `related-test-rd.log`（含 `profile sync` 与假 codex 派单/续跑回归）。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_agy -q` | 21 项通过，8.966 秒；日志 `related-test-rd-agy.log`（`agent_preflight` 回归）。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_local tests.test_rd tests.test_rd_agy -q` | 三文件串行累计 95 项通过，78.502 秒；日志 `related-cumulative-tests.log`。 |
| `python3 -m compileall -q src tests` | 通过，仅语法编译。 |
| `git diff --check` | 通过。 |

## 需编排方或端到端执行

- 全量测试（595 项）由编排方合入后统一跑；本票只跑上述相关文件。
- 真实机器上「本机目标 + 真实 codex」的端到端派单未在本会话执行（测试用假 codex，
  不调用模型）；派单主路径、隔离目录与续跑换目标机已按文件断言。
- skill 文案的本机例外说明并入 08 号票；本票回报中已提示 08 票文件缺失，需编排方确认落点。

## 改动文件

- `src/remote_dispatch/cli.py`：抽取 `sync_profile`，新增 `prepare_local_profile`，
  `agent_preflight` 本机目标分流与 `auto_sync=False` 续跑保护。
- `tests/test_rd_local.py`：新增 `LocalTargetProfileSyncTest`（7 项）与假 codex / 假 ssh。
- `README.md`、`docs/user-profiles.md`、`docs/worker-setup.md`：本机目标自动同步说明。
- `docs/validation/10-local-host-profile-sync.md` 与 `docs/validation/10/*.log`：本文件与证据日志。
