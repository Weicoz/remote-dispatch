# 票 68：code-review 双轴审查

基线：`4a445edae9886db424cdb33ea7ebaae591d9790e`（本轮开始时的 HEAD）。
审查工作区命令：`git diff 4a445edae9886db424cdb33ea7ebaae591d9790e --`，
另读新增合同 `contracts/v1/sessions-input-raw-text.json`。审查开始时新提交列表为空。
规格：`.rd-context/68-terminal-raw-io.md`，背景为同目录的 `68-app-terminal-findings.md`。
规范来源：本票用户指令、仓库现有 Python unittest/HTTP/SSE/假 Herdr 接缝与合同；
仓内没有适用的 CODING_STANDARDS/CONTRIBUTING。未读取示例目录的 AGENTS。
本轮已完整读取 `/Users/user/.agents/skills/code-review/SKILL.md`，
按其明确要求并行运行两个只读审查子代理；测试始终由主代理串行执行。

## Standards

审查代理：`/root/review_standards`。原始结论：

硬性规范：未发现违反本票目录、分支、依赖边界或测试组织约定的改动；未跟踪合同
`sessions-input-raw-text.json` 与接口一致。

判断项／健壮性：

- **P2：layout 异常结构导致请求直接断开。** 合法 JSON `null` 或 `[]` 会在
  `result.get(...)` 触发未捕获的 `AttributeError`。`/screen` 路径及 HTTP 异常处理均不捕获
  此异常，无法返回预期的服务不可用响应。建议验证 layout 对象形状，统一转为
  `OSError("herdr_viewport_unavailable")`，并通过假 Herdr 的 HTTP 接缝补充回归覆盖。

已逐项检查所列 12 个 Fowler 气味，未发现值得要求重构的具体问题。尺寸字段在接缝与
合同中重复属于协议要求；当前规模不足以引入领域类型或额外转发层。

处理：先添加 `test_invalid_herdr_layout_returns_service_unavailable_for_screen`，
`review-layout-red.log` 复现非对象 JSON 的 HTTP 断连；随后在调用 `.get` 前验证顶层对象，
其余错误结构继续由原有异常转换统一抛出 OSError。`review-layout-green.log` 覆盖
`null/[]/result:null/layout:null/panes:[null]`，均返回完整 503 响应。
同一审查代理只读复核最终差异后确认：**该 P2 已关闭**。

## Spec

审查代理：`/root/review_spec`。结论：未发现可确认的实现缺陷或越界改动。

- 尺寸／光标／resize：读取目标 pane rect、传 observe 尺寸、解析同帧光标、尺寸变化重建
  observer；初始屏幕提取完整帧。假 Herdr 覆盖不同尺寸和静默 resize。
- 默认带 Enter：仅显式 `submit:false` 跳过 Enter，严格限制 JSON 布尔值及请求结构。
- 按键：14 键与规格一致，不支持键仍拒绝，合同已说明。
- 脱敏／幂等：帧继续先解码脱敏再编码，元数据扫描保留；沿用输入幂等记录、结果重放和
  每次请求所需的 Idempotency-Key。
- 合同与文档已列出新增字段和旧请求兼容行为；改动限定本仓后端。

审查时待补的报告内容为滚动历史／alt-screen 方案及真实 Herdr/App 端到端步骤，
已在 `../68-terminal-raw-io.md` 补齐。审查代理未运行测试，执行证据由主代理保存。

Standards：1 项 P2，已修复并复核关闭；Spec：0 项实现缺陷，报告待补内容已补齐。
