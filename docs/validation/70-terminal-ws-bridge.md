# 票 70：serve 真实终端 WebSocket 桥接交付证据

实现范围仅 remote-dispatch 当前任务 worktree；未修改 App、部署目录、integration 或其他分支，未 push。
真实 Herdr 不在本 M4 会话中运行，以下自动化全部使用假 Herdr 可执行文件。
实现采用 implement → TDD → 双轴 code-review → 修复复核 → 全量检查 → 当前分支提交。

仓库：remote-dispatch；分支 `rd/weicoz/v4-70-20261027-094955-260757000`；
基线 `987b6448ebb4d596b4daf8cae7025e1f2e94a6ff`。最终提交号随派单回执返回，
可由本分支 HEAD 核对。当前不存在本地 origin/main ref，未 fetch，无法判断远端基线是否前进；
实现中无合并冲突。

## 逐条验收

| 验收项 | 实现／证据 | 结果与边界 |
| --- | --- | --- |
| 标准库 WS；Unix/TLS；session_access 与设备鉴权 | rd_websocket.py；rd_terminal_ws.py；test_tls_device_auth_session_grant_and_live_revocation、test_handshake_query_mapping_and_session_access_are_checked_before_upgrade | 实际 Unix socket 与 loopback TLS 夹具通过；无第三方依赖 |
| 原始字节 input／resize／scroll／mouse／release；整条粘贴；1 MiB；at-most-once | test_raw_input_paste_controls_and_restore_before_release、test_input_size_limit_and_oversize_fragment_totals、test_fragmented_input_with_interleaved_ping_is_one_command_and_never_replayed | base64 保留任意字节；输入不合并、不拆分成多条、不持久化、不重放；分片重组一条 NDJSON |
| terminal.frame／closed；seq／尺寸／full／cursor；跨帧脱敏；重连 full | test_redacts_credentials_across_frames_ansi_and_split_osc_without_logging_body、test_pem_and_quoted_credentials_remain_masked_across_many_paints、test_credential_whitespace_and_escaped_quotes_do_not_expose_value、test_json_credential_field_name_split_across_frames_is_scrubbed | 下行 data 与票 68 对齐；首帧必须由 Herdr 提供 full，否则不 Upgrade；CSI/UTF-8 跨帧解析；OSC 等文本控制串丢弃 |
| 单 pane lease；busy；显式 takeover；审计无正文 | test_busy_takeover_waits_for_old_cleanup_and_reconnect_starts_full、test_external_busy_and_closed_reasons_are_stable_without_herdr_text；rd_events.FIELDS 固定白名单 | 等旧控制进程完全清理才接管；上游 reason 不透传；普通 TUI 行为未真机验证 |
| 断开／心跳／停止恢复尺寸、release、回收；有界背压 | test_disconnect_reaps_control_and_restores_current_layout、test_heartbeat_timeout_and_shutdown_restore_release_and_reap、test_slow_client_closes_with_backpressure_and_reaps_process、test_shutdown_finishes_partial_input_before_restore_and_release | 读取最新 layout；64 帧／8 MiB 下行界限；持续排空 stdout；杀进程组并 wait；layout 不可用或管道损坏时 restored=false，仍回收进程 |
| 保留 terminal SSE 与 HTTP input；聊天不受影响 | related-regression.log：64 项旧会话／网络／事件测试；全量套件 | 旧路由逻辑保留，只在会话路由前增加 WS 分支；旧 SSE 脱敏不作协议变更 |
| 假 Herdr 夹具、合同样例、真实端到端步骤 | tests/fixtures/herdr_control.inc；tests/test_rd_terminal_ws.py；contracts/v1/sessions-terminal-ws.json；docs/terminal-websocket.md；本文件下方 | 覆盖输入、粘贴、控制、busy/takeover、closed、脱敏、背压、断开、恢复、回收、TLS、权限、RFC 边界 |
| App 消息与错误码回报 | docs/terminal-websocket.md 完整表；合同 JSON | 上行 terminal.input(bytes)/resize/scroll/mouse/release；下行 terminal.frame(data)/terminal.closed；详见下方摘要 |

## 检查命令与结果

所有测试串行，环境 `PYTHONPATH=src RD_RUN_SOCKET_TESTS=1`。真实 socket 在此 M4 可运行，已执行。

- TDD 首条 `python3 -m unittest tests.test_rd_terminal_ws -v`：先 404 失败，再通过；
  首次导入测试类误带入了 38 项旧回归，随后改为模块导入，后续没有重复发现导入类。
- 14 项首轮 WS 汇总通过：`70/ws-final.log`，16.677 秒（后续另补审查反例）。
- `python3 -m unittest tests.test_rd_serve_sessions tests.test_rd_serve_network tests.test_rd_events -v`：
  64 项通过，54.986 秒；`70/related-regression.log` 与 `.exit`。
- 审查修复 3 项 RED→GREEN：`70/review-red.log`（退出 1）／`review-green.log`（退出 0，3.161 秒）。
- 最终完整检查命令：`python3 -m unittest discover -s tests -p 'test_*.py'`；
  **572 项通过，338.508 秒，退出码 0**，见 `70/full-tests.log` 与 `70/full-tests.exit`。
  全量后将拼接使用的 Herdr 脚本片段从 .py 改名 .inc（内容不变，避免误作独立 Python 模块）；
  随后单独重跑最终 16 项 WS 接缝测试，见 `70/ws-reviewed-final.log`。
- 静态检查、Python 版本、类型检查配置、hook 情况见 `70/static-checks.log`。
  仓库未配置 mypy/pyright 类型检查；编译／Python 3.9 语法检查不能替代静态类型检查，作为覆盖缺口保留。
- **完整套件实际执行一次；pre-commit hook 实际执行 0 次**。当前仓库 hook 文件不存在，
  core.hooksPath 未配置，因此没有可复用的 hook 全量结果；独立执行全量一次，没有绕过、禁用或修改 hook。
- 首次静态差异检查发现 unittest 失败日志一处行尾空白，已仅去除证据日志行尾空白后重检；
  原始失败记录见 `70/static-checks-initial.log`，测试结果与正文未改。

## 审查与处理

详见 [双轴审查](70/code-review.md)。Standards 首轮 2 项、Spec 首轮 2 项（其中 1 项重合）；
完整 NDJSON 写锁、清理 stdout drain、恢复结果准确性、JSON 字段凭据脱敏均已修复。
两轴复核均无未解决阻断项。重检包含针对性 3 项与最终完整套件。

## 改动文件

- `src/remote_dispatch/rd_websocket.py`：RFC 6455 握手、分片、mask、Ping/Pong、关闭帧与消息界限。
- `src/remote_dispatch/rd_terminal_ws.py`：WS 路由、单 owner lease、双向桥接、队列、鉴权复查与生命周期。
- `src/remote_dispatch/rd_terminal_redact.py`：增量 UTF-8／ANSI、凭据／配置秘密／PEM 脱敏。
- `src/remote_dispatch/rd_herdr.py`：TerminalControl 子进程、完整 NDJSON 串行写入、排空与清理；原 run/viewport 兼容新增可选 timeout。
- `src/remote_dispatch/rd_serve_sessions.py`：接入 WS、服务关闭、terminal 审计事件权限过滤。
- `src/remote_dispatch/rd_events.py`：四类终端审计事件白名单。
- `tests/test_rd_terminal_ws.py`、`tests/fixtures/herdr_control.inc`：隔离夹具与接缝测试。
- `contracts/v1/sessions-terminal-ws.json`、`contracts/v1/README.md`：合同与样例。
- `docs/terminal-websocket.md`、`docs/serve.md`：App 接口与服务说明。
- `docs/validation/70-terminal-ws-bridge.md`、`docs/validation/70/*`：本证据、测试／审查日志与检查记录。

## App 对接与待定稿文案

上行文本 JSON：`terminal.input.bytes`（base64）、`terminal.resize.cols/rows`、
`terminal.scroll.direction/lines`、`terminal.mouse.action/button/column/row`、`terminal.release`。
下行 `terminal.frame` 使用 seq/encoding/width/height/full/**data**/cursor；
`terminal.closed` 使用固定 reason 与 reconnect_full。默认尺寸为 pane layout，显式 takeover=true 才接管。
每次重连首帧 full，输入送达结果不确定时也不自动重发。

HTTP 错误键：invalid_websocket_request、device_unauthorized、fingerprint_mismatch、
session_access_disabled、session_closed、terminal_busy、terminal_unavailable、serve_stopping。
WS Close：1000 正常、1001 离开／超时／停止、1002 协议、1003 二进制消息不支持、
1007 UTF-8、1008 无效消息／撤权、1009 超限、1011 上游失败、1013 背压。

App 文案不写入本仓界面，由编排方定稿，建议占位键：

| 键 | 占位文字 |
| --- | --- |
| terminal_busy | 终端正在被控制 |
| terminal_takeover_confirm | 接管当前终端控制？ |
| terminal_fit_window | 适配本窗口 |
| terminal_resize_shared_notice | 调整尺寸会影响桌面终端排版 |
| terminal_scroll_shared_notice | 滚动位置与桌面共享 |
| terminal_reconnect_required | 连接中断，请重新连接 |
| terminal_input_result_unknown | 输入送达结果未知，请核对后继续 |
| terminal_access_disabled | 未授权访问会话 |

## 需编排方／端到端执行

实现会话未部署、未写测试栈目录、未重启服务。编排方审查提交后按原有流程安装本仓包、
部署测试栈并重启 serve；复测沿用既有配置与设备配对，不在生产 pane 上发送测试输入。

1. 在真实目标宿主创建专用临时 workspace／pane，记录 pane layout、原 PTY `stty size`；
   映射到 `/v1/sessions`，从 Unix socket 和已配对的 TLS 设备分别连接 WS，确认默认尺寸与首帧 full。
2. 输入方向键、退格、Ctrl-C、UTF-8 中文、IME、多行与完整括号粘贴，用测试程序只记长度／hex
   验证字节一致；不保存真实会话正文。确认断线重连未重放输入。
3. 对临时 pane 运行 vim，验证插入中文、正常保存／退出；鼠标 SGR、scroll 与桌面共享位置；
   App 使用 SwiftTerm 对 data 喂字节，验证 full 重置、光标和后台回前台重连。
4. 第二条连接默认 busy，takeover=true 才接管；另开普通 Herdr TUI，确认未被踢、不抢焦点。
   专用 terminal attach 应被显式 takeover 替换，确认桌面输入仍可用。
5. 显式适配 80×24，在控制期间改变桌面 layout；分别正常 release、直接断 WS、失联、
   撤销会话权限、慢消费、停止 serve，确认 SIGWINCH 与最终 PTY 恢复为**最新** layout。
   查无遗留 control/helper 进程；核对终端审计只含元数据，restored=false 时调查上游故障。
6. 实测目标网络 RTT、移动网络断连、慢消费、实际控制进程启动／退出时间，再决定是否调整已有
   request_timeout；本票没有据猜测新增超时或限流数值。验证 1 MiB 边界与粘贴体验。
7. 关闭自建临时 workspace／pane。只提交脱敏结果、尺寸、计数与固定错误码，不上传终端正文。

这些检查需要真实 Herdr／App／目标测试栈，按约定不计为本实现会话失败。

## 未做／未验证

- 未执行真实 Herdr TUI、终端应用、SIGWINCH 或 App／SwiftTerm 真机端到端；调查文档不是本次实测。
- 未增加独立历史、图形／kitty 支持、App 代码、部署脚本或新依赖。
- 协议无 PTY 尺寸确认；restored=true 依据完整恢复／release 写入及 control 正常退出，
  不是实际应用尺寸的独立确认。上游已退出／不可写／layout 不可用只能记录恢复失败并回收。
- 已配置秘密在帧尾的候选前缀会保守遮盖；未知且无凭据标签的任意秘密无法识别。
- 本次只核对当前仓库已有 refs，未 fetch；远端实时基线是否有新提交由编排方合入时确认。
