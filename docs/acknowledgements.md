# 签收、代签与主动停止

签收仅表示收到了回执，交付内容仍需验收。签收和停止从读取 job 到落盘完成，
始终持有同一把 job 锁，见 [锁与查询接口](locking-and-queries.md)。

```sh
rdispatch ack JOB --round 1 --receipt-id RECEIPT_ID
rdispatch ack JOB --round 1 --receipt-id RECEIPT_ID --acked-by 'App MacBook' --via serve
rdispatch stop JOB --stopped-by 'App MacBook' --via serve
```

`--acked-by` / `--stopped-by` 接受会话 ID 或设备名。默认使用当前会话
（沿用 `--initiator-session`、`RD_INITIATOR_SESSION` 与 harness 会话识别），
无会话时记为 `cli`。`--via` 只接受 `cli` / `serve`，默认 `cli`。
这些参数记录操作来源；后续 serve 接入时应由服务端填写设备身份。
同一用户可以跨会话签收和停止，查询详情、读取结果和续跑仍沿用原会话限制。

签收文件仍为 `results/JOB/rN.ack.json`，包含
`receipt_id, received_at, acked_by, via`。`receipt_id` 必须与轮次回执匹配，
错误 ID 不会生成回执或签收文件。旧的无签收方记录仍视为已签收，不推断其代签方。
保留期仍从最后一次签收文件的修改时间起算；代签与普通签收完全相同。

`result`、`result --summary` 和 `status --details` 新增只读的
`acknowledgement`（匹配的签收记录或 `null`）。纯查询不会补写回执或签收记录。
查询接口新增 `Query.acknowledgement(rec, receipt)`，保持旧回执 ID 不变。

`inbox` 的 `results` 保留待签收回执，并列出本会话所有被代签的已缓存回执，
包括历史轮次和断连后的缓存。代签条目包含 `delivery_state=received`、
`delegated=true`、`acknowledgement`（签收方、时间、渠道）及至多 600 字符的
`summary`。签收方与发起会话相同的记录不作为代签展示。
代签条目持续展示到 job 被清理，不因运行 `inbox` 而被删除或改变签收时间。

主动停止保留退出码 143，并在本地持久化 `report.outcome=failed` 的回执，
自动签收方为停止者。回执附带 `stopped_by`、`stopped_at`、
`suppress_push=true`、`review_state=pending` 和 `needs_initiator_review=true`。
后续推送渠道必须遵守 `suppress_push`；本票未新增推送发送器。
已有执行报告的检查和证据保留，无报告时 `checks=not_reported`。

停止前在 job 的逐轮 `stop_requests` 中保存停止者、渠道和请求时间。
若目标机已停止、随后确认快照／读取报告／签收落盘失败，再次运行同一 job 的
`stop` 会补齐回执和签收，保留原停止者。已经完成自动签收的重复停止不会
改写回执和签收时间。目标机不可达时返回退出码 4，核对状态后可重试；
自行结束的轮次保留原回执，不冒记为主动停止。

本票不涉及服务、端口或构建。编排方合入后按现有安装流程更新 Python 包，
可用临时 `RD_STATE` 运行 `tests/test_rd_ack.py`、`tests/test_rd_query.py`、
`tests/test_rd_messages.py`、`tests/test_rd_locking.py` 与
`tests/test_rd_dispatch.py` 复测。serve／App 的真实签收与推送行为由后续票接入验证。
