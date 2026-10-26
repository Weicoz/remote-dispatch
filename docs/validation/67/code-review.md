# 票 67 代码审查

按 `~/.agents/skills/code-review/SKILL.md` 执行两路独立只读审查。
基线：`4db8cf3f980baf4e4f7d26f090c8edfbbf4ab95f`；实现尚未提交时，
审查命令为 `git diff --cached 4db8cf3`，基线至 HEAD 的提交列表为空。
规格来源为本票 `.rd-context/67-serve-list-cache.md` 与诊断；
仓库没有 `docs/agents/issue-tracker.md`，本票已有明确本地规格，不引入外部 tracker。
审查代理只读取当前工作区，未运行测试或修改文件；执行证据由主实现会话提供。

## Standards

初审发现：

- P1：最初将 Unix 并发槽绑定在连接生命周期，32 条空闲连接或 SSE 可占满槽位，
  阻塞后续列表请求。改为完整列表 GET 的处理槽，SSE 和部分 HTTP 请求不占槽；
  accept 队列独立保留 128。新增 32 条部分请求连接再读取列表的回归，先超时后通过。
- 建议（Duplicated Code）：`records()` 与列表视图重复加载、过滤、合并记录。
  已提取 `local_view()`，保持当前用户过滤一致。

复审结论：两项已处理，未发现新增阻断问题；没有发现违反项目开发约定或目录边界的硬违规。

## Spec

初审发现：

- P1：“状态变化后立即可见”缺少同轮状态事件覆盖旧快照。
  已独立保留同 job、round、host 的状态事件，后续 progress 不会覆盖它；
  较新的状态事件覆盖列表状态。随后补充文件 mtime 优先级，避免没有 `captured_at`
  的新回执被旧 running 事件覆盖，并保留已有已知退出码。
- P2：普通 `/v1/jobs` 新增对全部历史文件的失败依赖。
  当前视图已仅加载当前轮的 receipt、ack、review；仅 results 历史、同目录旧轮次和
  dispatch.json 不再阻断当前派单列表。项目汇总与历史列表保留历史读取语义。

复审进一步发现同目录旧轮次的隔离缺口，已补充坏 r1、正常 r2 的红灯回归并修复。
最终复审确认 P1、P2 均已解决，LocalFiles 成员／轮次与版本键未发现错误命中路径，
未发现新的明确阻断问题。

两轴汇总：Standards 初审 1 项可靠性问题、1 项改进建议，均处理；
Spec 初审 2 项问题及其复审边界，均处理。最终两轴无未解决阻断项。

测试对应：`tdd-review-red.log`、`tdd-old-round-red.log`、`tdd-receipt-order-red.log`
保存失败复现，`review-final-tests.log` 保存修复后的 7 项通过结果。
