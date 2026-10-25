# job 锁与纯查询接口

`remote_dispatch.rd_lock.JobLock(state, job)` 是非阻塞、不可重入的文件锁。
签收、停止、续跑从读取 job 前到操作完成持有同一把锁，包含目标机启动。
竞争失败抛出 `JobBusy`，CLI 返回退出码 4 和 `round in progress`。
不同 job 使用独立锁。异常、进程退出均释放锁；锁文件保留，不能删除或替换。

```python
from remote_dispatch.rd_lock import JobLock

with JobLock(state, job):
    rec = load_job(job)
    # 检查、写入及启动都在此作用域内完成。
```

也可 `lock = JobLock(state, job); lock.acquire()`，在 `finally` 中调用
`lock.release()`。释放可重复调用。未来验收或 serve 写路径应在最外层持锁，
内部调用不再重复加锁；CLI 的 `locked_job` 包装器采用此接口。
POSIX 用 `flock`，Windows 用单字节 `msvcrt.locking`；均由操作系统管理生命周期。

`remote_dispatch.rd_query.Query(state, *, states, report, receiver)` 提供纯查询。
三个回调均须只读：

| 回调 | 参数 | 返回 |
| --- | --- | --- |
| `states` | job 记录列表 | `{job_id: execution_snapshot}` |
| `report` | `(rec, round_number, kind)` | 目标机报告字典或 `None`，kind 为 `progress` / `result` |
| `receiver` | `rec` | 接收会话 ID 或 `None` |

CLI 的 `remote_dispatch.cli.queries(cfg)` 已接好现有目标机读取适配器，供复用。

| 方法 | 返回结构 |
| --- | --- |
| `jobs(records, snapshots=None)` | `list[dict]`，每条为 job 记录叠加 execution 快照字段；可复用已读快照 |
| `details(rec, snapshot=None)` | `{job, initiator_session, execution, progress, result, acknowledgement, delivery_state}` |
| `result(rec, number=None, snapshot=None)` | 已有或即时回执字典；未结束／失联且无缓存时为 `None` |
| `delivery(rec, receipt)` | `pending` / `received` |
| `acknowledgement(rec, receipt)` | 匹配回执 ID 的签收记录或 `None`，旧签收记录保持原字段 |

回执字段：`schema, job, round, host, initiator_session, execution_state,
executor_exit, receiver_session, report, head, dirty, needs_initiator_review,
receipt_id, captured_at`。旧缓存保留原 ID 和字段。未缓存历史轮次不借用当前轮次
重建，抛出 `ValueError`。详情无回执时 `delivery_state=not_available`。
新 ID 按回执内容计算，不含查询时间 `captured_at`；再次查询相同结果可用于签收。
签收先验证 ID 再落盘，错误 ID 不生成回执文件。

查询不修改调用方的记录、不写缓存、不创建目录、不取写锁。
`collect_result` 是保留的显式持久化路径，查询不调用它。
身份与输出策略由调用端负责：CLI 详情／结果仍检查原发起会话；未来 serve
按当前用户筛选记录后可直接调用查询接口，响应字段按 serve 契约筛选。

本票不改变部署方式。编排方合入后安装当前 Python 包即可；没有服务重启、端口、
真实设备或测试栈部署步骤。macOS 上的跨进程测试使用假目标机 RPC，Windows
目标机分支也经同一 RPC 边界覆盖；Windows 控制端的原生锁后端需在 Windows 复测。

签收方、代签展示与停止自动签收字段见 [签收、代签与主动停止](acknowledgements.md)。
