# 终端 WebSocket（票 70）

`GET /v1/sessions/{percent-encoded-pane-id}/terminal/ws`，HTTP/1.1 Upgrade，RFC 6455 v13。
使用标准库，无新增依赖。Unix socket 和 HTTPS/TLS 入口共用同一路由；TLS 继续要求
SPKI pin、`X-Rdispatch-SPKI` 与 Bearer 设备令牌。设备必须显式获得 session_access，
服务端 `[serve] session_access` 也须开启。仅允许 `/v1/sessions` 已映射的 pane。

查询参数：`takeover=true|false`（默认 false）；可选 `cols=80&rows=24`（必须一起出现，
整数 1..65535）。不传尺寸时读取 `herdr pane layout --pane` 的当前矩形。
“适配本窗口”必须由客户端显式发 resize 或显式传连接尺寸。显式 takeover 仅替换
Herdr attach/control owner，不向普通 Herdr TUI 发关闭指令；真实 TUI 行为尚待端到端验证。

## App 消息

仅接受 UTF-8 文本 JSON 消息，WebSocket 分片会重组为一条消息；不协商压缩或子协议。
所有客户端帧必须带 mask，服务端帧不带 mask，支持 Ping/Pong 与关闭握手。

```json
{"type":"terminal.input","bytes":"G1syMDB+cGFzdGVkIHRleHQbWzIwMX4="}
{"type":"terminal.resize","cols":80,"rows":24}
{"type":"terminal.scroll","direction":"up","lines":5}
{"type":"terminal.mouse","action":"down","button":"left","column":12,"row":5}
{"type":"terminal.release"}
```

- input：`bytes` 是任意原始字节的标准 Base64，解码后 1..1048576 字节。只接受 bytes，
  不接受 text。每条 JSON 原样成为一条 Herdr NDJSON 输入，不合并、不拆分、不缓存重放。
  括号粘贴的开始、内容、结束必须放在同一条 input。断线时送达结果可能未知；客户端禁止自动重发。
  WebSocket 文本 JSON／分片合计上限为 Base64 1 MiB 加 1024 字节封装。
- resize：cols/rows 为 1..65535 的整数（不接受 JSON 布尔值）。
- scroll：direction 为 up/down；lines 为 1..65535。滚动位置与桌面共享，v1 无独立历史接口。
- mouse：action 为 down/up/move/drag，button 为 left/middle/right/none，column/row 从 0 起算，
  整数 0..65535；应用未开启鼠标上报时，Herdr 可忽略事件。
- release：结束控制；客户端随后收到 terminal.closed 与 WS Close。未约定的字段会被拒绝。

下行沿用票 68 的 `data` 字段，**不是** Herdr 原始 `bytes` 字段：

```json
{"type":"terminal.frame","seq":1,"encoding":"ansi","width":149,"height":45,"full":true,"data":"G1sySmhlbGxvG1szOzVIG1s/MjVo","cursor":{"x":4,"y":2,"visible":true}}
{"type":"terminal.closed","reason":"released","reconnect_full":true}
```

每次连接 seq 从 1 递增，第一帧来自 Herdr 的 full repaint；无 full 时拒绝建立连接，
不把增量帧重标为 full。`cursor={x,y,visible}` 从 0 起算，跟踪跨帧 CSI。
App 解码 data 并喂给终端视图；full 时清除旧视口。ANSI 是 Herdr 渲染视口，
不能把本地模拟器的 scrollback 当作独立历史。

下行先 UTF-8 增量解码，再进行 ANSI 感知、跨帧有状态脱敏：已配置敏感值、凭据赋值、
PEM 私钥；CSI/ESC 定位和样式保留，OSC/DCS/APC/PM/SOS 内容整体丢弃。
在帧边界可能继续组成已配置敏感值的前缀立即用 `*` 遮盖，可能保守遮盖普通前缀，
不会先发送明文再尝试追回。未完成的 ANSI／UTF-8 序列延至下一帧。
终端正文只存在于有界内存缓冲，不进入日志、事件或回执。

## 错误与生命周期

Upgrade 前保持 JSON HTTP 错误格式 `{"error":{"reason":"..."}}`：

| HTTP | reason | 含义 |
| --- | --- | --- |
| 400 | invalid_websocket_request | Upgrade／查询参数不合法 |
| 401 | device_unauthorized | 设备凭据无效 |
| 403 | fingerprint_mismatch | pin 不符 |
| 403 | session_access_disabled | 服务或设备未授权 |
| 404 | session_closed | pane 未映射 |
| 409 | terminal_busy | 同 pane 已有控制者（含外部 Herdr attach/control） |
| 503 | terminal_unavailable | Herdr 启动／首帧／尺寸不可用 |
| 503 | serve_stopping | 服务停止中 |

Upgrade 后 terminal.closed.reason 为 released、connection_closed、taken_over、herdr_closed、
terminal_unavailable、backpressure、heartbeat_timeout、session_access_disabled 或 serve_stopping。
上游 closed.reason 不透传，避免其正文泄露。网络已不可写时不能保证送达 closed。

| WS Close | 含义 |
| --- | --- |
| 1000 | release、takeover、Herdr 正常结束 |
| 1001 | 断开、心跳超时、服务停止 |
| 1002 | RFC 帧格式错误 |
| 1003 | 不支持二进制消息（只接受文本 JSON） |
| 1007 | 非法 UTF-8 |
| 1008 | 消息格式错误、授权撤销 |
| 1009 | 单条输入或消息超限 |
| 1011 | Herdr 帧／进程错误 |
| 1013 | 慢客户端背压；必须重连 full |

单 pane 单 lease；显式 takeover 等待旧进程完成恢复／release／回收后再启动新 owner。
WS 关闭、失联、停止、背压、撤权都会在 release 前重新读取 pane layout 并尝试恢复 PTY，
再 release，回收整个 control 进程组。Herdr 已退出或 layout 不可用时无法保证恢复，
审计 `restored=false`，仍会终止进程，不能留下失联 owner。清理期间持续排空 stdout，
避免 Herdr 因输出堵塞无法读取恢复命令。`restored=true` 表示恢复／release 完整写入且
control 正常退出；协议无 PTY 尺寸确认消息，真实应用的 SIGWINCH 仍须端到端验证。

下行队列双限：64 帧、8 MiB（含正在写的一帧），超限丢弃增量并关闭。
Ping 周期、I/O 和子进程监管复用已有 `[serve] request_timeout`（默认 15 秒），
未新增未经环境实测的超时默认值。上线前由编排方在目标环境测量延迟后决定是否调参。
慢客户端或物理断网时最多等已有 I/O 超时，再清理。

审计事件 terminal.connect/disconnect/takeover/resize，字段白名单只允许 pane、设备、
尺寸、固定原因、输入／产生的输出字节数、丢弃帧数与恢复结果；无终端字节／Base64／原始错误。
审计 SSE 受 session_access 过滤。现有 terminal SSE、screen 与 HTTP input 契约继续保留。

合同样例：`contracts/v1/sessions-terminal-ws.json`。App 仓未改动。
