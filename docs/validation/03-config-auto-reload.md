# 票 03 配置自动重载：验收证据

仅修改 remote-dispatch worktree，未改 App、未提交、未 push、未部署或重启外部 serve。
执行环境：macOS arm64，Python 3.14；所有检查串行，使用临时 `RD_STATE`／`RD_CONFIG`
与 Unix socket。基线：`476f170`（integration/cli-app-unify，含 02 号版本接口）。

## 验收逐项映射

| 验收项 | 实现及证据 |
| --- | --- |
| 检测修改时间并重载；新增／修改目标机出现在 `/v1/hosts` | `Service.config_file_stamp` 检查 mtime、大小、inode 与权限；`poll_config` 默认每秒（`--poll-interval` 更小按其值）调用 `_reload_config`；成功后替换内存配置并唤醒采集。`test_added_host_appears_and_version_reports_the_latest_reload` 新增目标机、修改 `workdir`、禁用再启用，断言 `/v1/hosts` 与 `config_reload.at` 变化。 |
| 解析或校验失败保留旧配置；错误与时间出现在 `/v1/version` 和 `/v1/hosts` | 通过 `cli.Config(strict=True)` 复用 CLI 校验；失败只记录状态，不替换配置。`test_broken_config_keeps_old_hosts_and_reports_error_until_fixed` 覆盖缺 ssh、INI 解析错误（含凭据脱敏）和非法汇报合并窗口，断言旧目标机仍在、两个接口都返回 `{ok:false, at, error}`。合同 `hosts-config-reload-error.json`。 |
| 修好配置后错误清除 | 同一测试写回合法配置，断言 `ok:true`、`error:null`、新旧目标机恢复，且 `/v1/hosts` 与 `/v1/version` 的 `config_reload` 一致。 |
| 重载不打断进行中的请求和写命令 | `test_reload_keeps_an_open_stream_and_forwarded_write_command_running` 在打开 SSE 后启动转发到 serve 的 `session-default` 写命令，期间连续改写配置；断言写命令退出 0、默认值落盘、同一 SSE 继续收到新事件、版本接口仍可读。重载只交换配置对象，不重启 socket／线程。 |
| 测试经真实 serve 子进程改写临时配置 | `tests/test_rd_serve_config_reload.py` 用 `python -m remote_dispatch serve --socket <临时 socket>` 启动子进程，经 Unix socket 读取 `/v1/version`、`/v1/hosts`、`/v1/events`，并用 CLI 转发写命令。 |

`config_reload{ok,at,error}` 字段形状未变；`/v1/hosts` 新增同形字段，属客户端可忽略的新增字段，
合同版本保持 1。仅重载 `[host:*]`、`[serve] execute_timeout`、`[serve] session_access`、
`[monitor] report_merge_window_seconds`；监听地址、TLS 与请求超时仍只在启动时读取，已在
`docs/serve.md` 注明。

## TDD 与检查

- TDD 接缝：spec「Testing Decisions」的真实 serve 子进程 + Unix socket + CLI。
  先写 `tests/test_rd_serve_config_reload.py`，红：3 项失败（新增目标机不出现、旧目标机启动配置不出现）；
  实现后绿：3 项通过。
- 本票相关回归（串行，一次执行 137 项）：

```sh
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest \
  tests.test_rd_serve_config_reload tests.test_rd_serve_version tests.test_rd_serve \
  tests.test_rd_serve_host_config tests.test_rd_serve_host_metrics tests.test_rd_serve_host_last_seen \
  tests.test_rd_serve_read_latency tests.test_rd_serve_admin tests.test_rd_serve_writes \
  tests.test_rd_serve_network tests.test_rd_host_metrics -v
```

结果：`Ran 137 tests in 137.490s`，`OK`，退出码 0。未运行全量 595 项；
全量由编排方合入后统一执行。

- `python3 -m compileall -q src/remote_dispatch tests/test_rd_serve_config_reload.py`：通过。
- `git diff --check`：通过，无空白错误。
- 未运行类型检查：本仓无类型检查入口或已安装工具。

## 审查与处理

两轴 `code-review`（Standards / Spec）审查工作区相对 `476f170` 的差异，发现及处理：

- 已修复：合同错误样例文案与实现不一致、示例未体现旧目标机保留；`--poll-interval --help`
  未说明重载检查周期；启动 stat 的 `OSError` 可能抛栈；配置重载与后台采集交替时旧主机列表
  可能短暂覆盖新列表；修改同名目标机沿用旧探测行；重载错误绕过统一脱敏；无效
  `report_merge_window_seconds` 被误报成功；拒绝请求未读 body 导致测试偶发 `BrokenPipeError`。
- 已补测试：修改已启用目标机、解析错误与凭据脱敏、非法汇报合并窗口、SSE 与转发写命令并发。
- 保留并说明：两处校验文案改为不含 `ssh = ...` 的中文，以免统一脱敏把错误字段变成
  `[redacted]`；`die` 与退出码机制未变。配置 API 写入路径仍先更新内存配置、由重载线程
  在一个检查周期内收敛；未把两条路径合并为同一函数，避免本票扩大 host-config API 改动。

## 未验证／需编排方执行

- 未执行 `git add`／`commit`／`merge`／`push`；改动留在工作区，由编排方审查后提交并合入。
- 未运行全量 595 项；未做 App 端到端、真实 LaunchAgent／常驻 serve、真实用户配置目录验证。
- 真机配置手改后的重载由端到端冒烟覆盖：改 `ssh`／`enabled` 后观察 `/v1/hosts`、重启后配置仍生效。
- RD reporter 因沙箱无权写任务回执目录且无审批通道而未能写入；结果以本文件与最终回报为准。
