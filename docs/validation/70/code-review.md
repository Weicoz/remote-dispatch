# 票 70 code-review

范围：当前任务 WIP 相对 `987b6448ebb4d596b4daf8cae7025e1f2e94a6ff`。
首轮命令 `git diff --cached 987b6448ebb4d596b4daf8cae7025e1f2e94a6ff`；
修复复核命令 `git diff HEAD -- src tests docs/terminal-websocket.md`。
技能：按当前宿主 `/Users/user/.agents/skills/code-review/SKILL.md`，规范与规格两轴独立子代理只读审查。
本轮提交尚未创建，基线为进入任务时 HEAD；未读取其他目录个人规则或历史。
规格使用用户提供的 `.rd-context/70-terminal-ws-bridge.md`，无外部 issue tracker 依赖。

## Standards

首轮发现 2 项：

1. P1：输入线程与清理线程可并发写 stdin。大 input 部分写入后 done 截断剩余内容，
   resize/release 可能追加到半条 NDJSON，无法执行恢复。
2. P2：带引号的 JSON 凭据字段名（如 `{"token":"…"}`）无法识别，未配置的值可能漏过。

处理：TerminalControl 增加写锁；检查 stop 只在命令开始前，已开始命令完整写完才释放写锁；
传输失败标记 write_broken，后续禁止继续向损坏的 NDJSON 追加。
凭据标签支持可选引号，并保留跨帧状态、转义引号与长空白处理。

复核结论：原 2 项硬问题已修复，未发现新增阻断项；标准库、Python 3.9 语法与审计白名单符合约束。
复核只读，未自行运行测试或修改文件。

## Spec

首轮发现 2 项 P1，对应票据“先 resize 回 pane layout 再 release”：

1. 部分 NDJSON 输入与清理命令交错，不能保证恢复。
2. done 后停止排空 stdout；Herdr 若被输出管道堵住，虽然命令进入 stdin，也不能读取执行，
   清理最终 kill，旧实现仍可能把 restored 记为 true。

处理：同上串行完整写入；output_loop 结束后进入无解析／无日志的 stdout drain，直到 control 被回收。
启动失败时也启动 drainer。resize/release 完整写入且进程自然正常退出才记录 restored=true。
背压夹具改为有限大输出后继续读取 stdin，测试必须观测到恢复命令、release 与成功恢复状态。

复核结论：原 2 项 P1 已关闭，未发现新增阻断项。协议无尺寸确认消息，真实 PTY/SIGWINCH
仍需编排方端到端验证，接口文档已说明。

## 反例证据

`review-red.log` / `review-red.exit`：3 项反例失败；假大输入的 Base64 长行已截断，不含真实终端正文。
`review-green.log` / `review-green.exit`：同 3 项通过。

- 部分 1 MiB input 被夹具读走 4096 字节后暂停，serve 停止后放开；断言完整 input 一次送达，
  后面才是当前 layout resize 与 release。
- 慢 WS 客户端遇到 512 帧 burst，背压关闭；断言 stdout 仍被排空、恢复 resize/release 被夹具读到、
  进程回收且审计 restored=true。
- JSON 凭据字段名与值跨帧，敏感值不出现在下行。

首轮计数：Standards 2（最严重 P1）；Spec 2（最严重 P1）；部分为同一问题的不同审查轴。
最终两轴未解决阻断项均为 0。
