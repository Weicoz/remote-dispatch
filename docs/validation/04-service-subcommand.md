# 票 04 `rdispatch service`：验收证据

仅修改 remote-dispatch worktree，未改 App，未提交、未 push、未部署、未重启任何 serve。
执行环境：macOS 26.6（arm64），Python 3.14；所有检查串行，使用临时 `RD_STATE`／`RD_CONFIG`、
临时 `HOME` 与 PATH 中的假 `launchctl`。基线：`60418bc`（integration/cli-app-unify，
含 02、03、05、06）。

改动文件：`src/remote_dispatch/cli.py`（`service` 子命令与输出）、
`src/remote_dispatch/rd_service.py`（新增：plist 生成、launchctl、status 快照）、
`src/remote_dispatch/rd_serve.py`（只抽出 `SOCKET_NAME`／`VERSION_ROUTE` 常量，供 service 复用）、
`tests/test_rd_service.py`（新增）、`docs/serve.md`、`README.md`、`CHANGELOG.md`、
`docs/validation/04/`。App 与 `contracts/v1` 未改：本票没有新增或修改 `/v1` 接口或字段，
`status` 复用票 02 的 `GET /v1/version` 与 `version.json`。

## 验收逐项映射

| 验收项 | 实现及证据 |
| --- | --- |
| `install` 生成 plist（当前 `rdispatch` 绝对路径、`RD_USER_ID`、`RD_STATE`、有设置时的 `RD_CONFIG`、日志写状态目录），经 `launchctl` 加载；重复执行覆盖并重新加载 | `rd_service.document()` 写 `Label`、`ProgramArguments`（当前运行入口 + `serve`）、`EnvironmentVariables`（`RD_STATE`／`RD_USER_ID`／`PATH`，仅显式设置时写 `RD_CONFIG`）、`RunAtLoad`、`KeepAlive`、`StandardOutPath=$RD_STATE/serve.log`、`StandardErrorPath=$RD_STATE/serve.err.log`；`write()` 先写同目录临时文件并 fsync，再 `os.replace`，权限 0600；`reload()` 先 `launchctl bootout gui/UID/<Label>` 再 `launchctl bootstrap gui/UID <plist>`。`test_install_writes_launch_agent_plist_and_loads_it` 断言 plist 全部键、环境键集合、调用序列、权限与状态目录创建；`test_install_records_the_running_rdispatch_entry_as_an_absolute_path` 用与 console_scripts 同形的入口脚本断言写绝对路径；`test_install_twice_overwrites_the_plist_and_reloads` 断言第二次覆盖后字节相同且再次 bootout+bootstrap；`test_install_records_the_saved_user_and_omits_an_unset_config` 覆盖 `RD_USER_ID` 缺省取 `default-user.json` 且未设置 `RD_CONFIG` 时不写该键；`test_install_reports_a_launchctl_failure_and_keeps_the_plist` 覆盖 `bootstrap` 失败退出 4 并保留 plist。 |
| `uninstall` 卸载并删除 plist，不删状态目录与日志 | `service.unload()` 先 `bootout`，再用 `launchctl list` 确认已卸载，才删 plist。`test_uninstall_unloads_removes_the_plist_and_keeps_state_and_logs` 断言 plist 删除、状态目录与日志保留、调用序列为 bootout+list；`test_uninstall_without_install_succeeds_without_touching_state` 断言未安装时提示且不动状态；`test_uninstall_keeps_the_plist_when_launchd_cannot_be_queried`、`test_uninstall_keeps_the_plist_while_launchd_still_lists_the_job` 断言无法确认卸载时退出 4、保留 plist。 |
| `restart` 重新加载，用于升级后 | `service.reload()`；`test_restart_reloads_the_installed_plist` 断言 bootout+bootstrap 序列与输出；`test_restart_without_install_fails_with_the_install_hint` 断言未安装退出 4、不生成 plist、不调用 launchctl。 |
| `status` 显示加载状态、进程、socket、包版本、合同版本、配置重载；支持 JSON；未安装时非零退出并提示 `rdispatch service install` | `rd_service.status()` 合并 plist、`launchctl list` 与 `GET /v1/version`。`test_status_reports_the_loaded_serve_version_and_socket` 在真实 serve 子进程（默认 socket）下断言文本字段与 `--json` 全部键（含 `package_version`、`contract_version`、`config_reload.ok`）；`test_status_reports_plist_and_launchd_state_when_serve_is_offline` 断言离线时三项为 `-`/`null` 而 plist 与 launchd 状态照常；`test_status_reports_unknown_load_state_when_launchctl_fails` 断言 launchctl 失败时 `loaded=null` 且给出 `launchctl_error`；`test_status_without_install_exits_nonzero_and_names_the_install_command`、`test_status_json_without_install_reports_installed_false` 断言未安装退出 4、stderr 提示安装、`installed=false` 的完整 JSON；`test_status_does_not_call_a_responding_serve_stopped` 断言 serve 在响应时不再提示「未运行」；`test_status_reports_an_unreadable_plist`、`test_status_does_not_probe_a_socket_it_cannot_attribute_to_the_plist` 断言 plist 损坏时报 `plist_invalid`、不猜 socket；`test_status_survives_a_socket_that_is_not_http` 断言 socket 返回非 HTTP 时 `serve offline` 而不是抛栈。 |
| 常驻 serve 用默认 socket `$RD_STATE/serve.sock`；dev serve 用独立状态目录时可同时运行 | plist 不设 `RD_SERVE_SOCKET`，`serve` 只带 `serve` 参数；`status` 的 socket 取 plist 的 `RD_STATE`。`test_two_state_directories_run_their_own_serve_on_default_sockets` 同时启动两个真实 serve（各自 `RD_STATE`、都不传 `--socket`），断言两个 `serve.sock` 都响应 `/v1/version`，且 `status --json` 报的是 plist 指向的那个。 |
| 测试用 PATH 里的假 `launchctl` 和临时 HOME，断言 plist 内容与调用序列；不碰真实 launchd | `tests/test_rd_service.py` 的 `HOME` 指向临时目录，`PATH` 只含临时 bin 与 launchd 默认路径集合，假 `launchctl` 记录参数并按需返回 `list` 输出/失败；plist 断言落在临时 HOME 的 `Library/LaunchAgents`。假 `launchctl` 的调用日志同时证明实现没有调用真实 launchctl；会话内核对真实 `~/Library/LaunchAgents/io.github.weicoz.remote-dispatch.serve.plist` 不存在。`serve` 子进程使用临时 `RD_STATE`、假 `herdr` 与临时 `HERDR_CONFIG_PATH`／`CLAUDE_CONFIG_DIR`／`CODEX_HOME`。 |

Label 固定为 `io.github.weicoz.remote-dispatch.serve`，已写进 `docs/serve.md` 与 README。
`docs/serve.md` 新增「常驻 serve（票 04）」记录命令、plist 内容、JSON 键、提示行、退出码、
与 dev serve 共存边界，并链接本文件；README 新增同名小节列出四条命令；CHANGELOG 0.3.0 增加一条。

## TDD 与检查

- TDD 接缝按 spec「Testing Decisions」：CLI 子进程 + 磁盘上的 plist + 假 `launchctl` 调用序列；
  在线用例追加真实 `rdispatch serve` 子进程与 Unix socket。
- 红绿顺序：`install`（红：`invalid choice: 'service'`，绿：plist 与调用序列通过）→ 重复安装 →
  `uninstall`（红：`invalid choice: 'uninstall'`）→ `restart`（红：`invalid choice: 'restart'`）→
  `status` 未安装（红：`invalid choice: 'status'`）→ 在线 `status`（真实 serve）→ 离线与 launchctl
  失败 → 审查发现的用例（提示矛盾、卸载确认、plist 损坏、非 HTTP socket、入口形态）。
  把最终测试放到基线 `60418bc` 的代码上运行，21 项全部失败（20 项断言失败、1 项错误；
  绝大多数报 `invalid choice: 'service'`，入口脚本用例因基线没有该命令而退出 2），
  记录在 `tdd-red.log`。
- 事后补写的用例用变异验证敏感性，两次比对后源文件均已复原（sha1 一致）：
  `RD_CONFIG` 改成无条件写入 → `test_install_records_the_saved_user_and_omits_an_unset_config` 失败；
  `entry()` 改成只用解释器加模块 → `test_install_records_the_running_rdispatch_entry_as_an_absolute_path` 失败。
- 本票测试 21 项，全部通过（`service-tests.log`）。
- 本票相关回归（串行）：

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_service -v
PYTHONPATH=src python3 -m unittest tests.test_rd tests.test_rd_config_cli -v
PYTHONPATH=src python3 -m unittest tests.test_rd_serve_device_cli tests.test_rd_serve_version \
  tests.test_rd_serve_host_config tests.test_rd_serve_admin -v
PYTHONPATH=src python3 -m unittest tests.test_rd_serve -v
```

结果：

- `service-tests.log`：21 项通过。
- `cli-related-tests.log`：60 项，8 个错误全部来自 1 个既有用例
  `tests.test_rd.MonitorConfigTests.test_bad_environment_or_config_intervals_stop_cleanly`。
  该用例用 `object.__new__(rd.Config)` 绕过 `__init__`，读不到票 03 新增的 `Config.strict`，
  报 `AttributeError: 'Config' object has no attribute 'strict'`；用 `git archive 60418bc`
  解出的干净基线同样 8 个错误（`baseline-monitor-config-test.log`），与本票无关，本票未改 `Config`。
- `serve-related-tests.log`：37 项通过。
- `serve-core-tests.log`：`tests.test_rd_serve` 共 22 项，其中
  `test_current_user_can_read_cards_details_historical_receipt_and_log_tail_without_writes`
  是基线自带的间歇失败（比较状态目录快照，后台采集与断言竞争）。基线 `60418bc` 连跑 5 次
  3 次失败（`baseline-serve-readonly-flake.log`），本票连跑 3 次 1 次失败；该用例与本票改动
  无关（本票只从 `rd_serve.py` 抽出两个常量）。排除它后其余 21 项通过。
- 未运行全量 595 项，由编排方合入后统一执行。
- 跨平台：CI 在 Linux 上跑全量，`tests/test_rd_service.py` 用
  `@unittest.skipUnless(sys.platform == "darwin", ...)` 整体跳过；模拟 `sys.platform = "linux"`
  导入该模块得 21 项 skip、0 失败。`cmd_service` 在非 macOS 返回 4 和 `service_unsupported`。
- `python3 -m compileall -q src/remote_dispatch tests/test_rd_service.py`：通过（`compileall.log`）；
  审查子代理另用 `ast.parse(feature_version=(3, 9))` 确认 Python 3.9 语法兼容。
- `git diff --check`：通过，无空白错误（`diff-check.log`）。
- 未运行类型检查：本仓无类型检查入口或已安装工具。

## 审查与处理

两轴 `code-review`（Standards / Spec）：比较点 `60418bc`，两个只读子代理并行审查差异与新增文件
（禁止提交、禁止真实 launchctl 与真实状态目录），由本会话核对证据、修复并重跑测试。

Standards 轴发现与处理：

1. 硬性缺陷：`version()` 只捕获 `OSError`／`ValueError`，socket 返回非 HTTP 时
   `http.client.BadStatusLine` 直穿，`service status` 打栈退出 1。已加
   `http.client.HTTPException`（与 `rd_serve.py` 的同一 socket 客户端一致），新增
   `test_status_survives_a_socket_that_is_not_http`。
2. 入口取值：原用 `shutil.which("rdispatch")`，可能记录 PATH 里的另一个/过时入口。
   已改为取当前运行入口（`sys.argv[0]` 是可执行的绝对路径时用它），否则写
   `[sys.executable, "-m", "remote_dispatch"]`；新增 console_scripts 形态的入口用例，
   删除原「PATH 无 rdispatch 报错」用例（该失败模式已不存在）。
3. socket 归属：plist 不可读时仍用 CLI 的 socket 探测，可能把别的 serve 当成这个服务。
   已去掉 fallback：只有 plist 写了 `RD_STATE` 才探测，否则 `socket` 为 `null`；
   新增 `test_status_does_not_probe_a_socket_it_cannot_attribute_to_the_plist`。
4. 重复字面量：`serve.sock`／`/v1/version` 在 `rd_serve.py` 与 `rd_service.py` 各写一份。
   已在 `rd_serve.py` 抽出 `SOCKET_NAME`／`VERSION_ROUTE` 并在三处使用。
5. 文档精度：状态目录权限只对新建目录生效、`loaded=null` 也包含未安装、`executable` 在
   `python -m` 形态下是解释器、缺验证记录链接。已逐条改正。

Spec 轴发现与处理：

1. `status` 自相矛盾：serve 在响应但 launchd 未列出任务时同时输出 `serve online` 与
   「serve 未运行」提示。已修复：提示优先判断 `serve_online`，该状态改为提示「launchd 未加载本任务，
   运行 restart 交给 launchd」；新增 `test_status_does_not_call_a_responding_serve_stopped`。
2. `uninstall` 在卸载确认本身失败时仍删 plist。已修复：查询失败或仍被列出都返回 4 和
   `service_unload_failed` 并保留 plist；未安装时只做一次 bootout；新增
   `test_uninstall_keeps_the_plist_when_launchd_cannot_be_queried`。
3. plist 不可解析时 `status` 报 `installed yes` 且提示出现 `查看 None`。已修复：新增
   `plist_error`（`plist_unreadable`／`plist_invalid`）与重写提示，并对 `/v1/version` 的
   `config_reload` 做类型保护；新增 `test_status_reports_an_unreadable_plist`。
4. 证据漂移：验证文档与 `docs/serve.md` 曾写「先 bootout 再删 plist」「重复执行是空操作」
   和旧测试数。已按当前实现改写并重跑全部本票检查。
5. 文档把「不带环境变量即可连上」说得过宽：CLI 的 socket 仍取自己的 `RD_STATE`／`RD_SERVE_SOCKET`。
   已改为「连上默认状态目录的 serve」，并注明安装时用了自定义状态目录需两边一致。

保留项（说明理由）：`PATH` 不在票面列举的 plist 键内，但 launchd 默认 PATH 找不到 `herdr`
等用户级命令，常驻 serve 的会话发现会退化；保留并写进两份文档，同时把 plist 权限收紧到 0600
（只含路径与用户 ID，不含凭据），测试断言权限。`serve_online`／`cli_version`／`plist_error`／
`launchctl_error` 与提示行超出票面字段清单，用于用户故事 7、8 的排查与升级判断，已全部写进文档。

## 未验证／需编排方执行

- 未执行 `git add`／`commit`／`merge`／`push`；改动留在工作区，由编排方审查后提交并合入。
- 未在真机安装 LaunchAgent、未验证开机自启与崩溃拉起、未执行真实 `launchctl`
  （按票面约定由编排方在 09 号票执行）；本会话未停止、替换或重启任何 serve。
- 未运行全量 595 项；未做 App 端到端（App 显示「serve 未运行」等属 App 票）。
- 未验证升级后 `uv tool upgrade` 与 `service restart` 的真机组合；未验证旧版 0.2.1
  读取新版状态目录。
- 两个既有失败／间歇失败与解决建议（不属本票范围）：
  `tests.test_rd.MonitorConfigTests.test_bad_environment_or_config_intervals_stop_cleanly`
  缺 `self.strict`（基线同样失败，建议在 `setUp` 设 `self.cfg.strict = False` 或改用
  `Config(strict=False)`）；`tests.test_rd_serve` 的只读快照用例存在基线竞态，全量跑时可能失败。
