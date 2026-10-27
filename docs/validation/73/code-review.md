# 票 73 code-review

基点：3385ece（本票会话开始时 HEAD）；审查对象是当前工作区新增与修改文件。
规格：`.rd-context/73-serve-admin-gaps.md`、`.rd-context/73-app-measurements.md`。
当前仓库没有 `docs/agents/issue-tracker.md`；本轮使用用户给定的本地票据作为权威来源，未配置外部票据工具。
按 `~/.agents/skills/code-review/SKILL.md` 完整读取后启动两路只读子代理；没有让代理修改或运行检查。

## Standards

审查代理：`standards_review`。规范来源为本轮用户指令、Python >=3.9 元数据、现有实现惯例，
并附加技能列出的 12 项代码气味基线。发现 2 项正确性问题：

1. P1：后台持 NetworkLock 慢刷盘，写请求先持 self.lock 再等文件锁，间接阻塞鉴权和查询。
   已统一 start/write 的锁顺序为 NetworkLock → self.lock。新增“刷盘 + 等待中的设备写 + 读”三方测试，
   `review-flush-lock-shutdown-red.log` 复现前台超时；green 通过，前台实测约 0.19–1.26 ms。
2. P2：关闭监听前最终刷盘，随后成功鉴权可能遗漏时间。已在鉴权锁内检查 stop，
   关闭监听和连接并经过前台锁屏障后最终刷盘。`review-shutdown-red.log` / `review-flush-lock-shutdown-green.log`
   覆盖已建立 TLS 连接在停服后发请求，从原 200 改为 503 serve_stopping。

同一代理已复核两项均关闭。无额外 Python 3.9 兼容性硬问题；配置写入复用既有机制。
HostConfigRoutes.write 分支增加属于维护性判断，无需为风格扩大重构。

## Spec

审查代理：`spec_review`。发现 2 项：

1. P1：已有终端 SSE 和等待首帧的 screen 仅在请求开始检查权限；总开关关闭后仍可输出。
   已在 screen 发 JSON 前和 SSE 转发每帧前复查动态权限。
   `review-live-terminal-red.log` 中 screen 原返回 200、SSE 原输出终端帧；green 中分别为 403 和空流。
   测试在假 Herdr 可执行文件边界用文件 gate 控制首帧，未接触真实会话。
2. P2：慢刷盘通过等待的写请求阻塞读取，与 Standards P1 相同，已修复并补三方并发测试。

同一代理复核两项均关闭，未发现新增明确问题或范围扩张。

## 实现者补充审查

- URL 编码路径 `/v1/config/%73ession-access` 原可绕过 local_only；先解码再判断，
  `review-encoded-path-red/green.log` 覆盖 GET 与 PUT。
- 新 kind 原被事件白名单滤除；仅允许 lan/tailscale/easytier/other，保留旧无 kind 事件。
  地址事件合同与既有事件测试同步。
- 原删除测试在显示列表为 done 时，执行状态查询缓存可能尚未刷新；改为等待实际删除守卫成功，
  仍验证进行中拒绝、配置不变和完成后成功，不放宽业务断言。
- 两个既有测试直接赋值 sessions.enabled，已改为通过新增 PUT HTTP 接口关闭，验证同一公共行为。

审查结果：Standards 2 项已关闭；Spec 2 项已关闭（其中 1 项重复）；未留已知未处理发现。
