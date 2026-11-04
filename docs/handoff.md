# 会话交付协议

## 绑定与交付

发起方掌握任务范围和验收。接收方只执行当前指令，完成后等待发起方明确续派；等待表示任务进程已结束而原会话可恢复，不要求代理持续消耗模型调用。

```bash
rdispatch run --agent codex --host mini --initiator-session <当前会话ID> --prompt-file task.md
rdispatch status <job> --details --initiator-session <当前会话ID>
rdispatch watch <job> --keep-on-disconnect
rdispatch result <job> --summary --initiator-session <当前会话ID>
rdispatch ack <job> --round 1 --receipt-id <result返回的receipt_id> --initiator-session <当前会话ID>
# 发起方读完结果并决定修复或复检后，继续原接收会话。
rdispatch resume <job> --prompt-file followup.md --initiator-session <当前会话ID>
```

参数优先于 `RD_INITIATOR_SESSION`，再读取父进程链中最近的 harness 对应的会话变量：Claude Code 用 `CLAUDE_CODE_SESSION_ID`、Codex 用 `CODEX_THREAD_ID`、DSH 用 `DSH_SESSION_ID`、AGY 用 `ANTIGRAVITY_CONVERSATION_ID`，Kimi 不自动读取会话。识别不了 harness 时记为 `unknown`，不会因其他 harness 的环境变量存在而绑定会话。App 可以显式传入 `app:<device_id>` 或所选会话 ID。未配置且宿主未提供时新任务未绑定；既有任务不自动迁移或推断所属会话。绑定ID是路由元数据，不是鉴权凭据。同用户的其他会话必须显式使用原ID才能接管结果或续派，CLI不据此提供操作系统级隔离。

`run` 同时保存 `initiator_harness`、控制端机器名 `initiator_host` 和 `initiator_herdr`。后者的 `session / workspace_id / pane_id` 分别取自 `HERDR_SESSION / HERDR_WORKSPACE_ID / HERDR_PANE_ID`，缺值为 `null`；`captured_at` 与派单的 `created` 相同，标明这些是派单时继承的值，移动 pane 后不随之更新。`status --json` 可读回来源；缺这些字段的旧记录仍可正常查询。

`status --json`、`status --details` 和 `result` 只读，不创建缓存或回写接收会话。已有缓存回执在连接中断时仍可读取；未缓存的结果按目标机当前状态即时生成，断线时无法取得。`ack` 在持有 job 锁时核对轮次和具体 receipt_id，并保存此前未缓存的回执；`inbox`、续跑和清理也保留原有的显式收取路径。`inbox` 只列当前用户、当前发起会话尚未 ack 的结果。`ack` 可以重复确认同一回执，不会启动任务。接收会话已识别后，`resume --session` 不接受不同线程ID。

CLI以JSON/后台watch事件向调用它的宿主返回数据，未内置跨会话聊天发送器。宿主支持后台唤醒时，由原发起会话消费事件；不支持时保留pending结果，下次该会话主动读取inbox。不能将“写入回执”“返回元数据”“发起方收取”“用户已看到”混成一个状态。

## 执行方回执

新任务的 `RD_REPORTER` 指向任务目录内的Python脚本，报告写入本任务 `reports/`，不会创建下一轮。workspace隔离仅增加该报告目录写权限，凭据目录保持原规则。

POSIX示例：

```bash
python3 "$RD_REPORTER" progress --phase testing --summary '正在运行相关检查'
python3 "$RD_REPORTER" result --outcome partial --checks failed \
  --summary '一项检查失败，等待发起方决定' --artifact private/evidence/check.json \
  --decision '用递归下降解析' --unfinished '补错误位置信息'
```

`result` 可重复 `--decision <文本>`、`--unfinished <文本>`，写入回执 `report.decisions`、`report.unfinished`（字符串列表，最多 50 条、每条不超过 2000 字，仍受 64KiB 总上限约束）。`progress` 不接受这两项。`rdispatch run --context-from` 把它们拼进上下文包。

Windows使用该机器当前Python与 `$env:RD_REPORTER`。无Python的POSIX普通命令保持基础执行能力；发起方仍可读取退出/提交等元数据，结构化执行方报告需要Python3。

回执为UTF-8 JSON，最大64KiB，原子写入、权限0600；包含job、round、phase、summary、artifacts、updated_at。结果另外包含 outcome (`completed/partial/blocked/failed`)、checks (`passed/failed/partial/not_run/not_reported`)。只引用本任务证据，摘要不含密码、私钥或敏感正文。

runner收尾补 executor_exit、finished_at。setup 与主体共用同一超时预算（`--timeout`）：setup 超时或主体超时都是退出码124，回执`timeout_phase`为`setup`或`body`；主体只拿 setup 用剩的时间（至少1秒）。代理未写结构化结果时，从Codex最后回复生成有界摘要，checks仍为not_reported；普通命令的stdout保留为日志引用。`run --test` / `--test-full` 的命令任务例外：兜底回执按退出码写checks=passed（0）或failed（非0），并在`test_command`记录被执行的命令（最多1000字符）；任务自己写过结果回执时以其为准。测试类派单遇本地未发送的改动（既无`--wip`也未被`--include`覆盖）退出码4拒绝，防止测到旧代码；`--allow-dirty`显式跳过，非测试派单只在stderr警告。非零退出不能被执行方completed声明掩盖。

## 证据索引与有界读取

用`rd-report`对应的Python入口增加`--source-fingerprint <SHA256>`、`--artifact <本任务路径>`或`--evidence-file <JSON>`。产物文件实际读取散列与大小；缺失标`missing`，符号链接、越界或读取中变化标`unverified`。目录仅记录存在，不冒充目录内容已哈希。原`artifacts`路径列表保持兼容。

`evidence`只接受`source_fingerprint`、`checks`、`artifacts`和布尔`cleanup_confirmed`。分项检查为`{name,status,artifact?}`，status沿用检查状态；产物为`{path,state,sha256?,bytes?,kind?}`，state为`present/missing/unverified`。索引最大64KiB；大原件留私有目录，不把完整源码散列表和日志反复打印进上下文。

`rdispatch result --summary`最多返回600字摘要、来源指纹、分项状态计数、缺失／未核产物数量、模型故障分类和本机完整回执路径（`receipt_path` 是签收／显式收取后的保存位置，纯查询不保证该文件已存在）。默认完整JSON接口不变。所有执行方证据仍待发起方验收，不能把`present`或回收自述等同独立核验。

发起方结果另含 initiator_session、receiver_session、receipt_id、captured_at、head/dirty 和 needs_initiator_review。检查状态来自执行方报告，不是调度器独立验收。`delivery_state=pending/received` 只表示收取，原始执行/检查失败仍保留。

## 状态与周期

`status --details` 返回当前轮进度与可取得结果，未报告的阶段不填成已完成。运行中的Git状态由当前任务worktree实际读取，head_source=live；收尾记录或无法实读时标recorded，并附observed_at。

默认15分钟周期及配置优先级不变。`watch --keep-on-disconnect` 在本次有界等待内继续只读查询，不顺延截止时间、不重派、不续跑。到期仍有不可达任务时，progress_due携带unreachable状态；发起方报告未确认事实。默认watch保持原遇连接错误提前返回的行为，兼容已有监控。

新任务默认clean前要求本轮结果已收取确认；发起方先保存需要的产物，再按原Git安全检查清理。旧任务的status/watch/fetch/stop/clean调用保持兼容；没有员工配置快照的旧任务不能resume，须重新派单；本协议不要求迁移正在执行的任务或复制全机配置。
