# 票 06：会话访问总开关与目标机增删改 CLI

范围仅 `remote-dispatch`；未改 App，未部署、未重启测试栈、未 push、未 commit、未 merge。
分支 `rd/weicoz/u06-20261028-115022-261556000`，基线 `main` `e296f32`（0.3.0）。
票据 `.rd-context/cli-app-unify/06-session-access-host-cli.md`，规格
`.rd-context/cli-app-unify/spec.md`。

## 实现概要

- 新增 `src/remote_dispatch/rd_host_config.py`，把 serve 原有的配置解析、字段校验、
  名称校验、session-access 候选文件、目标机候选文件、备份与原子替换、删除保护
  抽成共用模块。
- `rd_serve_host_config.py` 改为调用共用模块，HTTP 行为、错误码、幂等、锁与运行时
  刷新保持不变；`tests/test_rd_serve_host_config.py`、`test_rd_serve_admin.py` 全通过。
- 新增 `rdispatch session-access [show|on|off] [--json]`。
- 新增 `rdispatch host list|add|update|delete`，支持 `--json`、公共字段选项、
  `--set FIELD=VALUE` 与更新时的 `--unset FIELD`。
- serve 在线时，设置命令通过 Unix socket 调用现有
  `GET/PUT /v1/config/session-access` 与 `GET/POST/PUT/DELETE /v1/config/hosts`；
  socket 不存在或连接被拒绝时直接读写 `RD_CONFIG`。连接成功后出错不自动改走直接写入。
- 直接写与 serve 写共用 `config.ini.lock`、0600 备份、fsync 与原子 replace，保留注释、
  换行、禁用项和其他段。
- 删除目标机检查全部用户的本地登记任务；未知/损坏状态仍阻止删除，输出
  `running_count` 与最多 20 个 `running_job_ids`。

## 逐条验收证据

测试源码 `tests/test_rd_config_cli.py`（12 项）；在线用例启动真实
`python -m remote_dispatch serve --poll-interval 3600` 子进程，经 Unix socket 与 CLI
子进程驱动；状态和配置都在 `tempfile.TemporaryDirectory()` 的临时目录，未连接真实
serve、真实 `~/.local/state/remote-dispatch` 或 `~/.config/remote-dispatch`。

| 验收项 | 实现及证据 |
| --- | --- |
| 会话访问总开关：查看、开、关 | `test_session_access_offline_show_toggle_backup_and_json`：离线默认 `true`，`off` 后配置为 false、备份原字节且权限 0600，`show` 输出“关”，`on` 再写 true；JSON 为 `{"enabled": bool}`。`test_online_session_access_and_hosts_are_written_by_serve`：serve 在线时 CLI 关闭后 `GET /v1/config/session-access` 立即为 false、`GET /v1/sessions` 返回 403；再打开恢复 true 与 200。 |
| 目标机：列出、新增、修改、删除；支持 JSON | `test_hosts_offline_list_add_update_delete_json_backup_and_validation`：列表含禁用项、继承默认值与 `protected_fields`，凭据值不入 stdout；新增返回 201 语义的 `{host: ...}`；修改支持 `--set max_heavy=3 --unset enabled`；删除返回 `{"deleted": "second"}`；全部 `--json` 可解析。`test_online_session_access_and_hosts_are_written_by_serve` 经真实 serve 完成新增、修改、删除并读回配置。 |
| 删除有运行中派单的目标机失败，输出运行中数量与派单 ID | `test_hosts_offline_delete_rejects_active_dispatches_with_count_and_ids`：两个跨用户任务被计入，stderr 含 `host_has_active_dispatches`、`running_count=2`、两个派单 ID，配置原字节不变且无新备份；删除任务记录后删除成功。`test_online_delete_reports_active_dispatch_conflict` 在 serve 在线时走同一语义，先失败后成功。 |
| 直接写先备份再原子替换，与 serve 写接口一致 | 共用 `rd_host_config.load/replace`：先 0600 创建 `config.ini.bak.<uuid>`、写入并 fsync 原字节，再同目录临时文件 fsync 后 `os.replace`。CLI 测试核对备份字节与 0600；既有 `test_create_is_backed_up_atomic_preserves_comments_and_replays`、`test_replace_failure_preserves_original_and_backup_without_partial_config`、`test_backup_failure_blocks_write_and_bare_delete_is_supported` 覆盖备份/替换失败。 |
| CLI 与 serve 共用一套校验和写入逻辑 | `rd_host_config.py` 被 `rd_serve_host_config.py` 与 `cli.py` 同时引用；名称、字段名、值、继承后必填/枚举/数值校验、候选文件生成、备份和 replace 只有一份。serve 原有 27 项配置/管理测试全通过。 |
| 测试覆盖 serve 在线与不在线两种情况 | 离线用例不创建 socket；在线用例启动真实 serve 子进程。`test_online_errors_keep_reason_and_exit_codes` 覆盖在线 `host_exists`、`not_found`、`invalid_host_field` 的退出码；`test_stale_socket_falls_back_to_direct_write` 覆盖陈旧 socket 回退；`test_connection_loss_after_request_does_not_fall_back_or_write` 覆盖连接中断后退出 4 且不写配置；`test_online_rejects_cli_with_a_different_config_path` 覆盖 CLI 配置路径与 serve 不一致时返回 `cli_config_mismatch` 且两边都不写。 |
| README、docs 同步 | `README.md` 增加“会话访问与目标机设置”命令；`docs/serve.md` 增加“设置类 CLI 子命令（票 06）”并更新 CLI 转交说明；`contracts/v1/README.md` 说明 CLI JSON 复用现有 HTTP 结构、无新增 HTTP 路径/样例。 |

## TDD 接缝与记录

约定接缝是本票 CLI 的外部行为：进程退出码、stdout/stderr、临时配置文件与备份，
以及真实 serve 的 Unix socket/HTTP 结果；不测内部函数调用顺序。

- `docs/validation/06/tdd-session-access-red.log`：先写离线 `session-access` 用例，
  命令未注册，argparse 退出 2。
- `docs/validation/06/tdd-session-access-green.log`：实现共用模块与命令后通过。
- `docs/validation/06/tdd-hosts-offline-red.log`：目标机用例首轮失败，原因是
  argparse 先拒绝了非法 `--sync` 取值；用例改用 `--set sync=other` 以验证共用校验。
- `docs/validation/06/tdd-hosts-offline-green.log`：离线列表、增改删、备份、
  注释保留、校验、错误路径通过。
- `docs/validation/06/tdd-delete-active.log`：删除保护用例通过。
- `docs/validation/06/tdd-online-green.log`：真实 serve 子进程在线写、读回与运行时
  开关通过。
- `docs/validation/06/tdd-online-errors.log`：在线错误 reason 与退出码通过。
- `docs/validation/06/tdd-forward-fallback.log`：陈旧 socket 回退、连接中断不重试通过。
- `docs/validation/06/tdd-first-write.log`：无配置文件时首次写入通过。
- `docs/validation/06/tdd-config-mismatch.log`：CLI 与 serve 配置路径不一致时拒绝通过。
- `docs/validation/06/tdd-timeout-nonascii.log`：在线请求超时与非 ASCII 配置路径通过。

## 检查与环境

Darwin arm64，Python 3.14.6；仓库没有 mypy/pyright 配置，未执行静态类型检查，
`compileall` 只证明语法可编译。相关测试串行执行，未跑全量（编排方合入后统一跑）。

| 命令 | 结果 |
| --- | --- |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_config_cli -v` | 12 项通过，12.642 秒；日志 `config-cli-tests.log`。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_serve_host_config tests.test_rd_serve_admin -q` | 27 项通过，24.792 秒；日志 `related-serve-recheck.log`。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_config_cli tests.test_rd_serve_host_config tests.test_rd_serve_admin -q` | 39 项通过，34.294 秒；日志 `related-cumulative-tests.log`。高负载下曾出现一次既有 admin 测试的 BrokenPipe，单跑与复跑均通过，日志 `related-cumulative-recheck.log`、`admin-recheck.log`。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_serve -v` | 22 项中 21 通过、1 项既有时序失败；失败为 `test_current_user_can_read_cards_details_historical_receipt_and_log_tail_without_writes` 的状态目录快照比较，查询期间后台采集写入 `results/fixture-job/r1.json`。同测试单跑通过；在 `git archive e296f32` 的基线副本上跑同一套件，复现同一失败（`baseline-serve-tests.log`），证明与本次改动无关。日志 `serve-regression-tests.log`、`serve-regression-recheck.log`。 |
| `python3 -m compileall -q src tests` | 通过，仅语法编译。 |
| `git diff --check` | 通过。 |

## 改动文件

- `src/remote_dispatch/rd_host_config.py`（新增）：共用配置解析、校验、候选文件、
  备份与原子替换、删除保护。
- `src/remote_dispatch/rd_serve_host_config.py`：改为调用共用模块，行为不变。
- `src/remote_dispatch/cli.py`：`session-access`、`host` 命令、在线转交、直接写、
  删除保护、JSON 与错误映射。
- `tests/test_rd_config_cli.py`（新增）：12 项 CLI/真实 serve 行为测试。
- `README.md`、`docs/serve.md`、`contracts/v1/README.md`：命令与双路径说明。
- 本验证记录与 `.rd-context/06-evidence/` 下的原始日志（后者不纳入实现提交）。

## Code review

按 `~/.agents/skills/code-review/SKILL.md` 的固定点 `e296f32` 做 Standards/Spec 两轴
只读子代理审查；不修改工作区、不跑测试。两轴审的是同一份工作区，以下按发现来源记录。

### Standards

1. **文档与在线错误文案不一致（硬问题）**：`docs/serve.md` 示例为
   `invalid_host_field: 字段 sync 取值无效`，在线路径实际输出
   `invalid_host_field field=sync`。已统一 `config_error()` 的文案，在线与离线现在
   都输出 `invalid_host_field: 字段 sync 取值无效`、`host_config_busy: 配置正在写入，请稍后重试`；
   `running_count`/`running_job_ids` 也由同一函数生成。
2. **在线请求无超时**：`serve_config_request` 使用生产 `UnixHTTPConnection` 时
   默认无限等待。已在连接后设置 30 秒 socket 超时；超时按 `cli_forward_failed`
   退出 4，不改走直接写入。`test_online_request_times_out_without_a_reply` 用
   accept 后不回复的假 socket 验证 0.2 秒超时。
3. **错误归因**：`host_config_unavailable` 的兜底捕获同时覆盖了损坏派单记录。已把
   `local_job_states` 的损坏回执视为未知状态（继续阻止删除）；配置读写失败仍用
   既有 `host_config_unavailable` 文案。派单记录本身无法解析时仍会落到该兜底，
   这是保留的边界，未声称已完全区分。
4. **命名**：`ACTIVE_STATES` 实际是终态集合，改为 `TERMINAL_STATES`。
5. **session-access 候选值类型**：`session_access_candidate` 增加布尔类型检查，
   防止字符串 `"false"` 被 `bool()` 误判为真。
6. **Middleware**：删除 `HostConfigRoutes.load` 这层纯转交，直接调用共用
   `load_config`；保留 `same_config` 作为配置路径校验接缝。复查发现原始路径放进
   HTTP 头在非 ASCII 路径下会 latin-1 编码失败；改为发送
   `X-Rdispatch-Config-SHA256`（绝对路径的 SHA-256），并加非 ASCII 路径在线测试
   `test_online_accepts_non_ascii_config_path`。
7. **保留的判断题**：CLI 离线分支与 serve 写路径仍各自持有存在性检查、锁、幂等/
   运行时刷新和删除保护编排；校验、候选文件、备份与原子替换已共用。两边的锁与
   幂等语义不同（CLI 无 HTTP 幂等键、serve 需更新运行时并唤醒采集），强行合并会
   引入行为差异，本次保留为判断题并在本节说明。CLI 直接读时按当前配置重算敏感值，
   serve 的运行时敏感集合会保留已移除的旧凭据；这是既有的内存脱敏语义，未改。
8. **测试补强**：在线 CLI 读（`session-access show`、`host list`）、在线
   `--unset`、`host_config_busy`、在线 503 已加入测试；`host_config_outcome_unknown`
   仍需在“备份或替换中断”这类进程级故障下构造，列为未验证边界。
9. **未采纳**：首写空备份断言保留，因为它验证“写前原字节备份”在配置不存在时也成立；
   恢复空备份等于恢复“无配置”，不是丢数据。测试辅助函数与 `ServeTests` 少量重复
   是为满足本票“真实 serve 子进程”接缝，接受。

### Spec

Spec 轴的两轮只读子代理未在会话时限内返回（已停止），由主实现会话按同一份
Spec 提示词逐条对照票据与规格完成；结论如下。

- 票据 6 项全部有实现和测试：会话访问查看/开/关（离线与在线）、目标机增删改查与
  JSON、删除保护、直接写备份/原子替换、CLI 与 serve 共用校验/写入、在线/离线测试。
- 未发现范围外行为：未改 App、未实现 `service`/设备配对/配置自动重载/版本接口。
  `X-Rdispatch-Config-SHA256` 与 30 秒超时是双路径写入的正确性保护，不新增用户可见
  业务能力。
- 规格第 75 行“全部支持 JSON 输出”按现有 CLI 约定解释为成功结果 JSON；错误仍用
  `rdispatch:` 前缀的 die 文本（reason、字段、数量与 ID 可解析）。若编排方要求错误也
  输出 JSON，需要另定统一错误契约，本票未擅自改变。
- `contracts/v1` 没有新增 HTTP 路径/字段，因此没有新增样例，只在
  `contracts/v1/README.md` 说明 CLI JSON 复用现有 `session-access-*.json`、
  `host-config-*.json` 结构；符合“新增或修改接口时同步样例”的边界。
- 未验证边界：`host_config_outcome_unknown` 的进程级中断、真实安装包/默认 socket、
  全量套件；这些已列入“需编排方／端到端执行”和“未做／未验证”。

复审：修复后重新运行 39 项相关测试全通过；Standards 复审确认上述 1–6 已闭合，
7–9 为保留判断题。Spec 逐条对照未发现缺失或越界。

## 需编排方／端到端执行

1. 按常规流程审查并提交本工作区；本会话不执行 `git add/commit/merge/push`。
2. 合入后跑全量 595 项（本机其他票并行，未在本会话跑全量）。
3. 在真实 `RD_STATE`／`RD_CONFIG` 上用已安装的 `rdispatch` 做一次在线与离线各一遍：
   `session-access` 查看/开关、`host list/add/update/delete`，核对真实 serve 运行时
   与备份文件；不要连接 `/private/tmp/claude-501/v4preview` 的 dev serve。
4. 如需要，补一次 App 端到端：App 不应因新增 CLI 命令改变；本票未改 App。
5. 真实 macOS LaunchAgent、真实 Herdr、真实 SSH 目标机与 Windows 目标机不在本票
   范围内，未验证。

## 未做／未验证

- 全量测试、类型检查（仓库无配置）、真实安装包升级、真实默认 socket 的常驻 serve。
- `service`、设备/配对、配置自动重载、版本接口属于其他票据，未实现、未验证。
- CLI 帮助/文档为中文；未新增 shell completion。
