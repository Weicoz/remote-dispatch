# 票据 72 代码审查

基点：`bfc76c6a2815ec4a79e317b8ad051ac2b178a39b`（本票起始 HEAD）。
审查范围：`git diff bfc76c6 --`，加本票新增测试、合同与证据文件。
按 `code-review` 技能分别运行两个只读审查代理；未运行测试、未编辑代码。
规格来源 `.rd-context/72-session-poll-isolation.md`，背景 `.rd-context/72-e2e-findings.md`。
本仓没有独立 CONTRIBUTING／CODING_STANDARDS 或 issue-tracker 配置；本票本地规格
已完整提供，不引入外部配置。规范依据用户约束、README、serve 文档、Python 配置与技能的 smell baseline。

## Standards

初审 1 项 P2：`docs/serve.md` 旧段仍称会话在 host 探测后顺序采集，状态查询在 job 锁内，
与独立线程／锁外批量查询矛盾。已同步原段和新增段，补充合同索引。
审查没有发现既有接口字段含义改变或需要重构的 smell。
复核：原文档问题关闭，两处旧测试更新符合新增响应字段及 SSH 系统边界测试约定；无新增规范问题。

## Spec

初审 2 项：

1. P1：只将启用 host 加入刷新调度，会让禁用 host 的既有运行任务永久停在旧状态。
   修复：禁用 host 不加入 `/v1/hosts`，但既有派单仍批量查询且沿用退避。
   验证：`review-disabled-red.log` 先失败、`review-disabled-green.log` 修复后通过。
2. P2：批量响应缺某 job 时会启动退避却跳过该任务，静默保留旧 running。
   修复：缺项补为 `unreachable`，保持原单条查询的失败含义。
   验证：`review-partial-red.log` 先失败、`review-partial-green.log` 修复后通过。

复核：两项均关闭，核读四份 red／green 日志，未发现新增或剩余明确规格问题。
真实环境时延、恢复和 App 兼容性仍需编排方验证。

审查结论：Standards 初审 1 项、剩余 0；Spec 初审 2 项、剩余 0。

## 首轮完整测试返修后的再次复核

执行方首轮完整测试 581 项、11 个失败记录后，修复并运行相关 33 项通过。
两个原审查代理均只读复核新增差异，未跑测试、未编辑，均未发现新增问题：

- Network 两处断言保留状态码及原 sessions 内容，允许新增 collection。
- 200 job 用例探针成功后阻塞状态 RPC，仍覆盖慢派单期间读取及时且不额外访问目标。
- progress 只读用例在传输边界暂停启动刷新并等待会话采集，消除后台落盘竞态。
- 仅已配置非启用 host 检查连接／退避；未知 host 保留 job_states 的原校验路径。

最终结论仍为 Standards 剩余 0 项、Spec 剩余 0 项；检查结果以执行方日志为准。
