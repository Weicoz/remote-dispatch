# 票 68：终端原始输入与真实视口尺寸验收证据

范围仅 remote-dispatch；未修改 App、部署目录或其他 worktree。起始工作区干净。
基线 `4a445edae9886db424cdb33ea7ebaae591d9790e`。
分支 `rd/weicoz/v4-68-20261027-005018-799752000`；最终提交号随交付回传。
未切分支、未 push、未合并。当前仓库没有 main/integration 引用，未联网同步基线，
无法判断相对编排方最新集成分支是否落后；本轮没有遇到冲突。

## 实现与逐条验收

| 验收项 | 实现与证据 |
| --- | --- |
| 初始屏幕和终端帧带当前 pane 尺寸／光标 | `Herdr.viewport` 读取 `pane layout --pane` 的目标 pane `rect`，以 `--cols/--rows` 启动只读 observer。`/screen` 直接使用首个完整帧，返回 `pane_id/text/width/height/cursor`；SSE 也返回尺寸及 `cursor={x,y,visible}`。`test_screen_and_frames_use_target_pane_viewport_and_cursor` 覆盖 149×45 与 83×27、显示与隐藏光标，并安排不同的 tab area／其他 pane 尺寸以排除误读。`tdd-viewport-red/green.log`。 |
| 解释并修复 120×40、resize 重发 | 旧代码省略 observe 尺寸，上游默认 120×40；本轮显式传 pane 尺寸。沿用服务 `--poll-interval`（默认 15 秒）检测 layout，静默 pane 也检查；变更时先回收旧 observer，再以新尺寸连接获得全量帧，不改真实 pane。连接内 seq 保持递增。`test_quiet_pane_resize_restarts_observer_with_full_frame_and_monotonic_sequence` 验证 149×45 → 91×31、full=true、新内容／光标、旧 PID 已回收。`tdd-resize-red/green.log`。 |
| 不追加 Enter 的文本通道及旧行为兼容 | `{text,submit:false}` 仅 send-text；省略 submit 或显式 true 仍 send-text 后 send-keys enter。submit 只接受布尔值，keys 不能混用 submit。`test_raw_text_does_not_submit_and_replays_without_resending`、`test_supported_terminal_keys_and_strict_submit_validation`、旧默认输入测试；`tdd-input-red/green.log`、`tdd-keys-red/green.log`。 |
| 扩展白名单及非法键 400 | 精确允许 backspace、left、right、ctrl+d、shift+tab、ctrl+a、ctrl+u、space、enter、esc、up、down、tab、ctrl+c。上述 keys 测试检查全部 CLI 参数；delete/home/end/pageup/pagedown、未知键和大小写变体均 400。没有凭空添加未获 Herdr 支持的键。 |
| 敏感扫描／幂等保持不变 | raw 输入缺 Idempotency-Key 仍 400；同键改变正文／submit 或重启后都重放首次结果，CLI 仅写一次，状态不保存正文。原有事件／设备／失败重放测试继续通过。`test_bytes_frames_and_initial_screen_redact_secrets_without_losing_cursor` 覆盖实际 bytes 字段、屏幕与 SSE 同源、ANSI 分隔敏感词和 OSC 标题脱敏，并核对光标。原有含二进制帧脱敏测试亦通过。`sensitive-checks.log`、相关与全量日志。 |
| 必需测试与假 Herdr | HTTP、SSE、输入及进程回收都使用临时 PATH 中的假 Herdr，可执行文件边界替换，不访问真实 pane。不同尺寸、resize、submit:false、新键、非法键均有行为测试。真实 Unix socket 已在本机启用检查，无须转交此部分。 |
| 合同文档和样例 | 更新 `docs/serve.md`、`contracts/v1/README.md`、screen／terminal／keys 样例；新增 `sessions-input-raw-text.json`，保留旧 text 样例。`test_session_v1_contract_examples_match_http_and_sse_responses` 实际执行旧、新输入和屏幕／SSE 合同。 |
| 滚动回看与 alt-screen 只调查 | 调查结果及后续方案见下节；本轮未实现历史接口、状态字段或 App 功能。 |

## Herdr 调查与 App 对接

本机 CLI 为 Herdr 0.9.3，仅执行了 --version、--help 和 bundled schema，不接触真实 pane。
本机帮助／schema 证据：`68/herdr-interface-checks.log`。
对应版本的上游一手来源：

- [CLI 默认尺寸与参数解析](https://github.com/herdrdev/herdr/blob/v0.9.3/src/cli.rs#L624)：默认 120×40，接受 --cols 和 --rows。
- [observer 的 NDJSON 输出](https://github.com/herdrdev/herdr/blob/v0.9.3/src/client/terminal_sessions.rs#L123)：输出 bytes、width、height、full，没有独立 cursor 字段。旧转发只取 data 还存在实际字段兼容缺口，本轮一并修复。
- [ANSI blitter 的末尾光标状态](https://github.com/herdrdev/herdr/blob/v0.9.3/src/protocol/render_ansi.rs#L834)：在绘制后写入绝对 CUP 与光标可见性；本轮从同帧提取 0 基 x/y 和 visible，保留 ANSI 本身。
- [pane read 参数](https://github.com/herdrdev/herdr/blob/v0.9.3/src/cli/pane.rs#L468) 与 [pane surface 状态](https://github.com/herdrdev/herdr/blob/v0.9.3/src/protocol/wire.rs#L1113)：前者支持 recent/recent-unwrapped 和行数，后者有 scroll 指标与 alternate_screen_active。

App 需要接入：

1. `/screen` 的 width、height、cursor；text 现在是完整 ANSI repaint，含清屏／定位／样式，
   不能再假定为逐行文本。新增字段解码宜可选，以兼容旧服务。
2. `terminal.frame` 的 data（统一 Base64 字段）、width、height、full、cursor、seq、encoding；
   上游 bytes 只作输入兼容，不在响应保留第二份正文。全量帧替换网格；seq 仅当前连接内递增，
   重连无历史补读。不会用新尺寸重标旧 ANSI；上游尺寸异常则终止流，客户端重新连接。
3. 逐字、粘贴、输入法已提交文本用 `{text,submit:false}`，Enter 单独用 `{keys:["enter"]}`；
   聊天整行提交保留旧 `{text}`。按键采用合同的 14 个名字，每次请求仍生成新幂等键，
   重试同一请求应复用原键。
4. resize 检查沿用既有采集间隔，默认最迟下一轮（15 秒加 CLI 执行时间）观察到；
   本票未宣称即时事件驱动或实机性能数字。若需更低延迟，先在目标机测量 CLI 开销和
   多 observer 负载，再由编排方确定间隔或单独做共享 layout 事件订阅。

滚动回看方案：现有 observe 发送渲染后的可见区域，不是完整 PTY 输出历史；不能仅靠这些
增量 repaint 反推出被擦除／滚走的内容。后续可先提供独立只读历史快照接口，使用
pane read recent/recent-unwrapped，并以 revision 固定快照、按游标分页、限制返回量；当前
CLI --lines 只支持尾部行数，任意向前分页需要缓存快照或上游扩展。不要调用 pane scroll
来实现某个 App 客户端回看，否则可能改变共享 pane 的滚动位置。历史也要先脱敏，分页
边界保持字符／ANSI 完整。App 本地历史缓存、容量和过期策略需另票确定。

alt-screen 方案：ANSI observe 是 server 渲染后的差异，原始应用切换 ?1049 的事件不保证
透传，所以不宜靠 App 在 repaint 中寻找该序列判断状态。Herdr pane surface 协议已有
alternate_screen_active，但当前 observe NDJSON 没有暴露；后续优先扩展／使用只读语义
元数据源，把 alternate_screen 与 frame revision 一并发出。进入／离开时发送全量帧，
App 分开维护主屏和替代屏，并按产品约定处理历史；本轮不新增这些字段。

## 检查记录与覆盖缺口

所有测试串行执行，未关闭／修改／绕过 hook。资源检查见 `68/environment-checks.log`：
10 核、16 GiB 内存、检查时内存可用比例 37%、磁盘可用 62 GiB；按本票约定串行运行。

命令前缀除静态检查外均为 `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest`。

| 检查命令（追加到上述前缀） | 结果／证据 |
| --- | --- |
| `tests.test_rd_serve_sessions.SessionTests.test_raw_text_does_not_submit_and_replays_without_resending -v` | 红：原实现 400；绿：raw 与旧默认输入共 2 项通过；`68/tdd-input-red.log`、`68/tdd-input-green.log`。 |
| `tests.test_rd_serve_sessions.SessionTests.test_supported_terminal_keys_and_strict_submit_validation -v` | 红：原白名单 400；绿：新键和非法结构覆盖通过；`68/tdd-keys-red.log`、`68/tdd-keys-green.log`。 |
| `tests.test_rd_serve_sessions.SessionTests.test_screen_and_frames_use_target_pane_viewport_and_cursor -v` | 红：两种尺寸均缺 width；绿：初始屏幕和帧通过；`68/tdd-viewport-red.log`、`68/tdd-viewport-green.log`。 |
| `tests.test_rd_serve_sessions.SessionTests.test_quiet_pane_resize_restarts_observer_with_full_frame_and_monotonic_sequence -v` | 红：静默 resize 无新帧，客户端读超时；绿：连同尺寸测试 2 项通过；`68/tdd-resize-red.log`、`68/tdd-resize-green.log`。 |
| `tests.test_rd_serve_sessions tests.test_rd_serve_review_fixes tests.test_rd_report_delivery -v` | 首轮 69 项，4 个失败、1 个错误，均为旧初始屏幕夹具／按原行字节比较 JSON 的断言；切换为完整帧夹具与 JSON 语义比较后，69 项通过，54.066 秒。`68/related-tests.log`、`68/related-final-tests.log` 及各自 exit 文件。 |
| 新 bytes 初始屏幕／SSE 脱敏测试与 raw 幂等测试（各完整名称见日志） | 2 项通过，`68/sensitive-checks.log`。 |
| `tests.test_rd_serve_sessions.SessionTests.test_invalid_herdr_layout_returns_service_unavailable_for_screen -v` | 审查修复红：两种非对象 JSON 导致断连；绿：5 种非法结构完整返回 503。`68/review-layout-red.log`、`68/review-layout-green.log`。 |
| `python3 -m compileall -q src tests` | 通过（退出 0），`68/compileall.log`、`68/compileall.exit`；这是编译／语法检查，不冒充静态类型检查。 |
| `git diff 4a445edae9886db424cdb33ea7ebaae591d9790e --check` | 最终通过。初次暂存检查发现新增日志的行尾空格（此前普通 diff 未包含未跟踪日志），命令未阻止首次提交；随后仅清理日志行尾空白并修订提交。原始检查见 `68/initial-staged-diff-check.log`，最终见 `68/diff-check.log`／exit。日志未改动检查结论或测试正文。 |
| `discover -s tests -p 'test_*.py' -v` | 556 项全部通过，317.216 秒，退出 0、无跳过；`68/full-suite.log`、`68/full-suite.exit`。 |

`core.hooksPath` 未配置，实际 hooks 目录仅有 sample，没有生效的 pre-commit 等提交 hook。
正常提交保留配置，hook 实际执行次数 **0**；全量结果来自手动执行，不声称 hook 覆盖。
完整套件实际执行次数：**1 次**。静态类型检查缺口：项目 pyproject／Makefile／CI 没有
mypy/pyright 入口，环境也未安装，未擅自增加依赖，未运行类型检查。无 build 独立入口；
本票未跑包构建，编排方如需发布包应自行构建。

TDD 接缝使用票据明确要求的 HTTP 输入、屏幕与 SSE；所有 Herdr 替换仅在可执行文件
外部边界。implement、tdd（含 tests.md/mocking.md）、code-review 已完整读取并执行。
指定检查约定原 `/Users/weicoz/...` 路径不存在，已定位并完整读取实际
`/Users/mac/.agents/skills/weicoz-agent-implement/SKILL.md`。审查结果见
[双轴报告](68/code-review.md)：Standards 发现 1 项 P2，已红绿修复并复核关闭；Spec 未发现
实现缺陷，要求补齐的调查／E2E 报告内容已写入本文。

## 改动文件

- `src/remote_dispatch/rd_herdr.py`：pane 视口、observe 尺寸／重连／光标、bytes 兼容、submit 控制。
- `src/remote_dispatch/rd_serve_sessions.py`：完整初始屏幕、扩展按键、严格 submit 验证。
- `tests/test_rd_serve_sessions.py`：假 Herdr 与 HTTP/SSE 行为测试、更新合同接缝。
- `contracts/v1/sessions-screen.json`、`sessions-terminal.json`、`sessions-input-keys.json`，
  新增 `sessions-input-raw-text.json`；原 `sessions-input-text.json` 保持旧请求样例。
- `docs/serve.md`、`contracts/v1/README.md`：接口合同和兼容说明。
- `docs/validation/68-terminal-raw-io.md`、`docs/validation/68/`：本报告、审查及检查证据。

待定稿文案键：**无新增**。继续使用既有 `invalid_session_input`、`read_unavailable` 等错误键；
未添加 App 文案或占位界面，界面呈现由编排方定稿。

## 需编排方在本机端到端验证

本票实现会话不写测试栈部署目录、不部署、不重启服务。编排方审查并合入本票提交后，
按现有测试栈方式更新 remote-dispatch 包并重启 serve，保持用户／设备会话访问授权和
Herdr 环境选择；本机 App 需另行接入以上字段。本轮未验证真实 Herdr 按键或帧渲染，
本机真实行为无法在此 M4 环境复现，这不计为本票单元检查失败。

1. 在本机选专用测试 pane，记录 `herdr pane layout --pane <id>` 的目标 rect，读取
   `/screen` 和首个 terminal.frame，比较 width/height 与光标位置／可见性；同时观察
   149 列附近长行、中文宽字和底部行是否与 pane 一致。保留脱敏 JSON 与截图。
2. 保持 SSE，调整窗口／分屏造成 pane 尺寸变化（包含缩小、扩大和静默状态），等下一次
   serve 既有轮询，确认新尺寸 full=true、内容完全重绘、连接内 seq 递增且真实 pane
   尺寸未被 observer 改写。重连后首屏同样正确。
3. 在无副作用的交互输入缓冲中发送 `{text:"测试",submit:false}`，确认未执行 Enter、
   字符实时出现；再验证 left/right/backspace、Tab 补全、Ctrl-A/U、Shift-Tab、space，
   以及已授权安全场景的 Ctrl-C/D。Ctrl-D 可能结束 shell，须用专用一次性 pane。
4. 验证中文输入法提交片段与粘贴按 App 的 submit:false 路径送达；按 Enter 仅发送一次
   keys enter。聊天旧 `{text}` 仍完整提交一次。逐请求新幂等键、同请求重试复用键，
   核对无重复字符／回车；非法键仍为 400。
5. 注入无敏感性的合成 marker 测脱敏，确认 `/screen` 与 SSE 已遮盖、尺寸／光标保留；
   关闭 App／断网与服务退出后，确认无遗留 observer。并测量目标环境采集间隔／负载，
   再决定是否另票降低 resize 延迟。

未做／未验证：真实本机 Herdr 与 App 联调、测试栈部署和重启、性能时延实测、App 构建、
包构建、静态类型检查、滚动历史与 alt-screen 功能实现。已运行的 Unix socket 测试使用
假 Herdr，不等同于真实 Herdr／App E2E。最终验收由发起方根据证据判断。
