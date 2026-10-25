# 19-serve-skeleton 实现与检查证据

范围：remote-dispatch，本票分支 `ticket/19-serve-skeleton`。
初始提交 `5ebb440aed05c5bd2932c919b2edf7c1d28a8d18`（合入票 18）。
当前副本没有 `integration/desktop-board-v3` 引用，未自行 fetch、切换分支或更新基线；
无法独立对照该引用的最新性，未遇冲突。交付提交号以本报告所在提交为准。

## 逐项验收

| 票据验收项 | 证据（tests/test_rd_serve.py 中的行为测试） | 结果 |
| --- | --- | --- |
| 临时状态目录启动，HTTP 可读 hosts、jobs、详情、回执、日志 | `test_hosts_contract_is_readable_and_service_removes_its_private_socket`、`test_current_user_can_read_cards_details_historical_receipt_and_log_tail_without_writes`；真实 Unix socket、跨发起会话、其他用户 404、GET 前后文件内容与 mtime 不变 | 通过 |
| SSE 新事件与 Last-Event-ID 断线补读 | `test_sse_delivers_new_events_and_reconnect_replays_every_missing_sequence`；强制轮转、离线期间追加、可见 seq 为 1→3→4→5、其他用户事件过滤、头优先于 query、非法游标拒绝 | 通过 |
| serve 运行时 CLI 签收转交，停止后直接写，共用锁 | `test_cli_ack_is_written_by_serve_then_falls_back_after_shutdown_with_same_job_lock`；HTTP 查询读回 via=serve，停服后 CLI result 读回 via=cli，两种路径锁冲突均退出 4 | 通过 |
| 所有只读响应不含敏感字段 | `test_every_read_response_and_event_omits_configuration_and_scrubs_secrets_in_text`；扫描全部读路径、错误响应和 SSE，涵盖嵌套字段、配置敏感值、私钥正文；`test_log_endpoint_does_not_follow_a_link_to_a_private_file` 拒绝日志链接 | 通过 |
| 本票全部接口契约样例，测试按样例校验响应结构 | `contracts/v1/*.json` 与 README；`test_http_and_sse_responses_follow_the_shared_v1_examples`、hosts 空列表测试及 CLI ack 测试覆盖全部样例的响应／事件结构 | 通过 |

附加行为证据：

- `test_background_collection_exposes_running_progress_and_publishes_the_finished_receipt`：后台观察运行中进度，轮次结束发布 receipt.ready 并持久化回执，HTTP 可读。
- `test_forwarded_run_preserves_cwd_stdin_and_origin_and_stop_review_project_writes`：真实本机隔离 Git／shell 派单，保留会话和 Herdr 来源，project hide/show、stop、review void 走 serve。
- `test_cli_forwards_resume_stdin_and_exit_code_without_retry_after_connection_loss`：续跑转交传递多行 stdin、cwd、会话、原始退出码；请求发送后断连退出 4，不直接重试。
- `test_forwarded_command_without_a_session_does_not_inherit_the_service_session`：无会话调用方签收为 cli，不继承服务会话。
- `test_queries_do_not_attach_a_previous_round_snapshot_or_receipt_to_a_new_round`：缓存按轮次／目标机／根目录归属，续跑前后的观察不串轮。
- `test_serve_cli_starts_and_terminates_cleanly_and_rejects_invalid_intervals`：真实 serve 子进程启动、SIGTERM 清理 socket、拒绝 0／负值／NaN／Inf。
- `test_service_rejects_a_second_writer_and_recovers_a_stale_socket_without_deleting_files`：单服务锁、旧 socket 回收、普通文件保护。

## 检查命令与实际结果

全部检查串行执行；未并发运行测试命令，未监听 TCP。
开发中反复运行单个新增行为测试，按 red→green 补齐实现。

最终相关检查命令：

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_serve tests.test_rd_query tests.test_rd_events tests.test_rd_locking tests.test_rd_ack tests.test_rd_review tests.test_rd_projects -v
```

实际结果：**87 tests，15.845s，OK**，退出码 0。新增 serve 模块共 14 个测试。

提交前完整套件仅执行一次：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

实际结果：**310 tests，119.428s，OK**，退出码 0。296 个原测试 + 14 个新增测试。
输出中保留已有测试故意触发的无模型参数、Windows 本机派发拒绝与模拟退出 7 提示；无失败。
运行时间较派单基线 51 秒增加，实际结果以上述记录为准。

其他检查：`python3 -m compileall -q src`、对改动的两个源码及新增测试使用
`ast.parse(..., feature_version=(3, 9))`，均通过；`git diff --check` 通过。
仓库没有静态类型检查配置／第三方类型检查器；未声称执行 mypy 或 pyright。
无构建步骤。仓库无提交 hook，实际 hook 测试次数 **0**；完整套件手动执行 **1** 次。

检查前资源：10 核、1 分钟负载 6.30（每核约 0.63，小于 1，有空闲），
内存空闲比例 42%，工作盘可用约 42 GiB；swap sysctl 查询被沙箱拒绝，未绕过限制。

## TDD 与 code-review

已完整读取指定 implement、tdd 及 code-review 技能；接缝沿票据已确认的 HTTP/SSE 与 CLI。
可查的 red→green 例子：服务模块不存在；读路径 404；SSE 404；签收 via 为 cli 而非 serve；
旧轮次结果误挂新轮次；无会话调用方误继承服务会话；运行中详情缺执行状态；日志链接错误返回 200。
各失败均先运行行为测试再补实现，最终均通过。

按 code-review 技能分开进行 Standards 和 Spec 两路只读子代理审查，并对修复复核；
审查基准是初始 5ebb440 与工作区改动（含未跟踪文件），未要求安装缺失的 issue-tracker，
因为本票已明确给出票据与规格路径。

- **Standards**：无阻塞违规；可选发现是私有 cli-ack 样例未纳入结构校验，已补响应和事件断言，复核通过。
- **Spec**：发现来源上下文错误回退到 serve 启动会话，已清除子进程的服务会话环境，并补失败先行回归；同样建议补私有契约样例，已处理。复核无新增阻塞。
- 自审补齐：缓存代际隔离；详情各轮执行状态和进度；日志链接拒绝。必需契约对象为 null 时现在会使结构测试失败。

审查者未运行测试，测试结果均由实现会话执行并记录。

## 改动文件

- `src/remote_dispatch/rd_serve.py`：Unix HTTP、SSE、后台采集、显示投影、CLI 私有转交与生命周期。
- `src/remote_dispatch/cli.py`：serve 子命令、最外层写命令转交、派单来源传递；没有重排现有命令实现。
- `tests/test_rd_serve.py`：14 个外部行为测试。
- `contracts/v1/README.md` 与 11 个 JSON 样例：hosts 空／非空、jobs、详情、回执、out／err 日志、events、404、400、私有 CLI ack。
- `docs/serve.md`、本报告、README 文档索引。

未改版本、CHANGELOG、skills 或只读 `.rd-context`。未部署、未重启现有服务、未 push、未切换或合入其他分支。

## 文案待编排方定稿

以下键是本报告的审阅索引，源码沿用现有 CLI 的直接字符串风格，未新增本地化框架。

| 审阅键 | 新增提示／错误原文 |
| --- | --- |
| serve.help | serve local HTTP and events over a Unix socket |
| serve.socket.help | Unix socket path (default RD_SERVE_SOCKET or RD_STATE/serve.sock) |
| serve.poll_interval.help | background poll interval in seconds (default 15) |
| serve.started | rdispatch: serving on {socket_path} |
| serve.already_running | serve_already_running: a service already owns this state directory |
| serve.invalid_socket_path | invalid_socket_path: socket path must not be a symbolic link |
| serve.socket_path_in_use | socket_path_in_use: socket path already exists |
| serve.unix_unavailable | unix_socket_unavailable: Unix sockets are not supported |
| serve.invalid_poll_interval | invalid_poll_interval: poll interval must be positive seconds |
| cli.forward_failed | cli_forward_failed: command may have run; check state before retrying |
| cli.invalid_command | invalid_cli_command: command cannot be forwarded |
| serve.poll_jobs_unavailable | rdispatch: poll_unavailable: cannot read jobs |
| serve.poll_collect_unavailable | rdispatch: poll_unavailable: cannot collect a job |
| serve.poll_host_unavailable | rdispatch: poll_unavailable: cannot probe a host |

HTTP 错误标识：`not_found`、`receipt_unavailable`、`invalid_request`、`read_unavailable`、
`invalid_cli_request`、`cli_unavailable`。内部校验原因通过这些稳定标识返回，不把配置或异常正文放进公共响应。
请求过大／服务拒绝等内部原因统一显示 cli.forward_failed，要求先核对状态。
原签收、停止、验收、派单等命令输出沿用原文，不新增界面定稿文案。

## 需编排方／端到端执行与未验证项

部署／更新 Python 包与真实服务重启由编排方执行；实现会话未写测试栈部署目录。
步骤见 `docs/serve.md`：隔离 RD_STATE → 启动 Unix 服务 → curl 读接口 → CLI 签收／停止／验收
→ 断开重连 SSE → SIGTERM 后检查 socket 清理。

未执行真实 SSH 目标机／Windows 控制端、App 假 serve 消费与多进程服务崩溃恢复的端到端验证。
目标机 RPC 时限沿用现有值，没有估算新的外部服务时限；没有外部服务或设备依赖导致的本票失败。
私有 CLI 通道在发送后连接中断时不会重试，但不提供崩溃后的幂等重放保证。
HTTPS／配对／令牌／APNs、公开写接口、项目摘要与历史分页／统计、会话接口按分票约定未做。
自由日志和报告依然要求执行方遵守脱敏约定；本服务不读取凭据文件，不能识别任意无标签秘密正文。

实现和提交完成不代表编排方已经验收或合入。
