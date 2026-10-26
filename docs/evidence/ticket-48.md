# 票 48：Herdr 汇报送达交付证据

实现范围：remote-dispatch 的票 48。验收来源为任务上下文中的
`tickets/48-report-delivery-herdr.md`，规格 v0.4 §5.3、§5.3.1、§6；
Codex 与 Claude Code 可发送，其余 harness 只留 inbox，以本票约定为准。
采用 implement → tdd → code-review；未执行部署、服务重启、push、合并或分支切换。

## 仓库与基线

- 仓库：remote-dispatch，rdispatch 创建的本票 worktree。
- 分支：`rd/weicoz/v4-48-20261026-134145-446247000`。
- 实现起点：`4b389b6117b7857bc203ea977c29b8feca35bd8a`（合入票 47）。
- 起始工作区干净；未修改 cli.py，避免其他票并行修改的共用入口。
- 本 worktree 没有可解析的 `integration/desktop-board-v3` 本地引用，无法独立核对基线落后量；保持派单指定 HEAD，不换基线。未见冲突。
- `.rd-context/` 已被忽略，票据、规格、术语表及原始运行日志均不加入提交。
- 最终提交号由派单回执和会话最终答复记录，避免在提交自身内容中循环引用。

## 逐项验收

以下用例均在 `tests/test_rd_report_delivery.py`；使用隔离的假 Herdr 可执行文件，
通过真实事件、serve 查询、Unix socket HTTP/SSE 与外部进程输入边界观察行为。

| 验收项 | 证据用例／结果 |
| --- | --- |
| 空闲时经 Herdr 写入，成功后记 report.delivered | `test_idle_report_is_written_to_herdr_and_delivered_once_across_restart` 检查文本、enter、一次事件；`test_http_and_sse_follow_delivery_contracts` 验证实际 HTTP/SSE 与样例 |
| 工作中排队，空闲后 FIFO | `test_working_reports_wait_and_leave_in_fifo_order_after_restart` 检查重启前排队、重启后按先后发送；`test_session_becoming_idle_between_checks_cannot_let_second_report_overtake` 检查发现状态切换时不超车 |
| 写入失败不重试轰炸，只留 inbox | `test_failed_input_stays_in_inbox_without_retry_and_does_not_block_next_report` 验证单次失败、重启不重发、后项继续；`test_herdr_outage_keeps_inbox_without_using_cached_session_or_repeated_input` 验证查询失败兜底 |
| 会话关闭或不在 Herdr 不发送 | `test_closed_or_missing_herdr_session_keeps_report_in_inbox_without_writing`、`test_session_closing_after_collection_does_not_use_stale_pane`；原汇报测试同时覆盖合并窗口内会话关闭后仍保留 inbox |
| Kimi、AGY 不发送，并记录原因 | `test_only_verified_codex_and_claude_harnesses_receive_reports` 检查 Kimi／AGY 为 inbox_only + harness_not_verified，Claude 正常送达；Codex 见空闲用例 |
| 会话访问总开关关闭不发送 | `test_session_access_switch_off_keeps_inbox_without_even_querying_herdr` 检查 session_access_disabled 且不查询／写入 Herdr |
| 同一汇报不会重复发送 | 空闲与失败用例跨重启验证；`test_crash_after_external_send_never_repeats_an_uncertain_delivery` 验证外部发送后本地记账失败；`test_failed_delivery_event_is_recovered_without_sending_report_again` 验证仅补事件 |
| 假 Herdr 四种状态及现有测试通过 | 前述空闲、工作中、失败、关闭用例；检查结果见下节 |

附加边界：`test_report_input_cannot_interleave_with_user_text_and_enter` 验证用户输入与汇报
共用输入临界区；`test_replacement_pane_with_only_probable_old_identity_never_receives_report`
拒绝推测身份；`test_waiting_for_input_and_unknown_status_never_receive_automatic_text`
覆盖等待输入、未知状态，以及 Herdr done → 空闲的映射。
`test_delivery_contract_and_sensitive_scan_cover_new_fields` 覆盖新增 delivery 字段、事件字段、
落盘元数据和送入会话正文的脱敏。汇报始终保留，送达不签收、不验收。

## TDD 证据

测试接缝沿用票据约定的 serve 汇报／事件／inbox 与假 Herdr 边界，没有为私有算法编写镜像测试。
每个纵向切片先跑失败测试，再实现并验证；原始输出位于本任务忽略目录 `.rd-context/evidence-48/`。

| 红灯日志 | 预期失败 | 绿灯日志 |
| --- | --- | --- |
| 01-idle-red.log | 无 Herdr 写入，0 != 2 | 02-idle-green.log |
| 03-queue-red.log | 工作中被立即写入 | 04-queue-green.log |
| 05-failure-red.log | 写入异常逸出，未保存兜底 | 06-failure-green.log |
| 07-closed-red.log | 关闭会话汇报被跳过或缺少送达状态 | 08-closed-green.log |
| 09-gates-red.log | 未验证 harness 被发送，总开关未拦截查询 | 10-gates-green.log |
| 11-crash-red.log | 结果未知仍停留 sending，未归入 inbox_only | 12-crash-green.log |
| 13-event-red.log | 成功状态随事件失败回滚 | 14-event-green.log |
| 15-contract-red.log | 新增 pane 元数据包含敏感标记 | 16-contract-green.log |
| 17-input-lock-red.log | 用户输入期间被插入汇报文本和回车 | 18-input-lock-green.log |
| 19-fifo-red.log | 第二条在第一条仍排队时先发 | 20-fifo-green.log |
| 21-identity-red.log | 同目录替换 pane 被错误送入旧会话汇报 | 22-identity-green.log |
| 23-herdr-outage-red.log | 查询 Herdr 失败未归入 inbox | 24-herdr-outage-green.log |

输入锁的初版同步回调测试在修复后的非重入锁上阻塞，该次相关测试已中断（退出 130）；
测试改为两个真实调用线程，使用事件协调输入边界后通过。只有该并发场景使用两个线程；
所有测试命令串行执行。其余契约补充用例验证已实现行为，不制造无意义红灯。

## 检查命令与结果

执行方式：Codex 当前会话在本票 worktree 实现，两个只读审查代理按 code-review 技能分轴审查，未覆盖宿主模型。
执行设备：本任务 M4（10 个逻辑 CPU）；在任务目录用 uv 创建 `.venv`，安装当前包成功。
没有 Swift/App 构建。未改 hook、未使用 --no-verify。

| 检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=.uv-cache uv venv .venv` 与 `uv pip install --python .venv/bin/python -e .` | 通过，Python 3.14.4 |
| `.venv/bin/python -m unittest tests.test_rd_report_delivery tests.test_rd_reports tests.test_rd_serve_sessions` | 60 项通过，37.159 秒；日志 25-related.log |
| `.venv/bin/python -m unittest tests.test_rd_events` | 9 项通过，0.089 秒；日志 26-events.log |
| `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | 510 项通过，283.692 秒，退出 0；日志 27-full-suite.log / .exit；全量实际执行 1 次 |
| `.venv/bin/python -m compileall -q src tests` | 通过，退出 0；日志 28-compileall.log / .exit |
| `git diff --cached --check` | 通过 |

仓库 pyproject.toml 未配置静态类型检查；现场没有 mypy／pyright。
compileall 是语法检查，不冒充静态类型检查。core.hooksPath 未设置，Git 默认 hooks 目录
只有 .sample 文件，无启用的提交 hook；实际测试 hook 执行 0 次，正常提交保留这一配置。
全量测试仅在提交前执行一次，不通过 hook 重复运行。正常 Git 提交 1 次，提交号见最终回执。

## Standards 审查

按 code-review 技能由独立审查代理只读审查当前改动，相对固定起点
`4b389b6117b7857bc203ea977c29b8feca35bd8a`，使用 `git diff <起点> --`（包含已暂存新增文件）。
无远端 issue tracker，直接以用户指定的本地票据为来源。

发现 2 项：输入线程未复用现有输入锁（并发正确性，P1）；FIFO 状态切换可能超车（硬验收问题，P2）。
已共用 `service.writer → sessions/input.lock`，发现会话、发送意图、文本与回车都在临界区内；
用 waiting 集合阻止本次扫描中队首未出队的同会话后项发送。均有回归测试。
独立只读复核结论：两项已消除，未发现新增正确性问题。

## Spec 审查

独立规格审查发现 2 项，均列 P1：FIFO 超车；接受 probable 身份可能把旧会话汇报发给同目录替换 pane。
FIFO 按上节修复；自动发送只接受可靠会话身份，推测匹配只留 inbox，并优先选择可靠身份。
独立只读复核结论：两项已消除，未见新增验收缺口。
两轴合计 4 条原始发现（3 个不同问题），全部处理；各轴无剩余未修复项。

## 改动文件

- `src/remote_dispatch/rd_reports.py`：送达状态、FIFO、持久发送意图与事件待发布记录、关闭会话保留 inbox。
- `src/remote_dispatch/rd_events.py`：report.delivered 显示字段白名单。
- `tests/test_rd_report_delivery.py`：16 个送达边界与契约用例。
- `tests/test_rd_reports.py`：关闭会话改为保留 inbox 的预期。
- `contracts/v1/reports.json`、`contracts/v1/report-delivered.json`：新增送达字段和事件样例。
- `contracts/v1/README.md`、`docs/reports.md`：契约、状态原因与部署复测说明。
- `docs/evidence/ticket-48.md`：本交付证据。

## 文案与编排方端到端检查

本票不定稿 App 界面文案；`delivery.reason` 的候选文字见 `docs/reports.md` 原因键表，包含
session_not_idle、session_closed、session_identity_unconfirmed、herdr_unavailable、
harness_not_verified、session_access_disabled、session_input_failed、delivery_result_unknown。
会话中汇报正文沿用领域术语：“派单汇报”“第 N 轮”“已完成／卡住／离线／失败”。

需编排方／端到端执行：按 `docs/reports.md` 部署复测节安装当前包、重启测试栈 serve，
验证真实 Codex／Claude Herdr 往返、工作中转空闲的实际时序、关闭及复用 pane、真实输入失败、
Kimi／AGY 与总开关兜底。实现会话未启动真实 harness、未连外部服务；新增送达测试只使用 Unix socket。
全量中的既有网络测试包含本地回环监听检查，本轮通过；真实网络与外部服务复测仍单列为编排方执行。
未写测试栈部署目录、未部署或重启服务；这些不计为本票自动化失败。

未验证：真实 Herdr／harness 端到端、断电跨文件原子性（不提供）、基线分支落后量。
发送成功但本地记账丢失时只能记录结果未知并停止重发，以保证不重复输入；
不能将外部终端写入与本地文件纳入同一事务。
