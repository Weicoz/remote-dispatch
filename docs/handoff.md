# 会话交付协议

## 绑定与交付

发起方掌握任务范围和验收。接收方只执行当前指令，完成后等待发起方明确续派；等待表示任务进程已结束而原会话可恢复，不要求代理持续消耗模型调用。

```bash
rd run --agent codex --host mini --initiator-session <当前会话ID> --prompt-file task.md
rd status <job> --details --initiator-session <当前会话ID>
rd watch <job> --keep-on-disconnect
rd result <job> --summary --initiator-session <当前会话ID>
rd ack <job> --round 1 --receipt-id <result返回的receipt_id> --initiator-session <当前会话ID>
# 发起方读完结果并决定修复或复检后，继续原接收会话。
rd resume <job> --prompt-file followup.md --initiator-session <当前会话ID>
```

参数优先于 `RD_INITIATOR_SESSION`，该变量优先于宿主实际提供的 `CODEX_THREAD_ID`。未配置且宿主未提供时新任务未绑定；既有任务不自动迁移或推断所属会话。绑定ID是路由元数据，不是鉴权凭据。同员工的其他会话必须显式使用原ID才能接管结果或续派，CLI不据此提供操作系统级隔离。

`result` 缓存每个 job/round 的独立回执；`inbox` 只列当前员工、当前发起会话尚未 ack 的结果。缓存回执在连接中断时仍可读取；远端尚未收取的结果保留在任务目录，连接恢复后读取。`ack` 必须匹配轮次和具体 receipt_id，可以重复确认同一回执，不会启动任务。接收会话已识别后，`resume --session` 不接受不同线程ID。

CLI以JSON/后台watch事件向调用它的宿主返回数据，未内置跨会话聊天发送器。宿主支持后台唤醒时，由原发起会话消费事件；不支持时保留pending结果，下次该会话主动读取inbox。不能将“写入回执”“返回元数据”“发起方收取”“用户已看到”混成一个状态。

## 执行方回执

新任务的 `RD_REPORTER` 指向任务目录内的Python脚本，报告写入本任务 `reports/`，不会创建下一轮。workspace隔离仅增加该报告目录写权限，凭据目录保持原规则。

POSIX示例：

```bash
python3 "$RD_REPORTER" progress --phase testing --summary '正在运行相关检查'
python3 "$RD_REPORTER" result --outcome partial --checks failed \
  --summary '一项检查失败，等待发起方决定' --artifact private/evidence/check.json
```

Windows使用该机器当前Python与 `$env:RD_REPORTER`。无Python的POSIX普通命令保持基础执行能力；发起方仍可读取退出/提交等元数据，结构化执行方报告需要Python3。

回执为UTF-8 JSON，最大64KiB，原子写入、权限0600；包含job、round、phase、summary、artifacts、updated_at。结果另外包含 outcome (`completed/partial/blocked/failed`)、checks (`passed/failed/partial/not_run/not_reported`)。只引用本任务证据，摘要不含密码、私钥或敏感正文。

runner收尾补 executor_exit、finished_at。代理未写结构化结果时，从Codex最后回复生成有界摘要，checks仍为not_reported；普通命令的stdout保留为日志引用。非零退出不能被执行方completed声明掩盖。

## 证据索引与有界读取

用`rd-report`对应的Python入口增加`--source-fingerprint <SHA256>`、`--artifact <本任务路径>`或`--evidence-file <JSON>`。产物文件实际读取散列与大小；缺失标`missing`，符号链接、越界或读取中变化标`unverified`。目录仅记录存在，不冒充目录内容已哈希。原`artifacts`路径列表保持兼容。

`evidence`只接受`source_fingerprint`、`checks`、`artifacts`和布尔`cleanup_confirmed`。分项检查为`{name,status,artifact?}`，status沿用检查状态；产物为`{path,state,sha256?,bytes?,kind?}`，state为`present/missing/unverified`。索引最大64KiB；大原件留私有目录，不把完整源码散列表和日志反复打印进上下文。

`rd result --summary`最多返回600字摘要、来源指纹、分项状态计数、缺失／未核产物数量、模型故障分类和本机完整回执路径。默认完整JSON接口不变。所有执行方证据仍待发起方验收，不能把`present`或回收自述等同独立核验。

发起方结果另含 initiator_session、receiver_session、receipt_id、captured_at、head/dirty 和 needs_initiator_review。检查状态来自执行方报告，不是调度器独立验收。`delivery_state=pending/received` 只表示收取，原始执行/检查失败仍保留。

## 状态与周期

`status --details` 返回当前轮进度与可取得结果，未报告的阶段不填成已完成。运行中的Git状态由当前任务worktree实际读取，head_source=live；收尾记录或无法实读时标recorded，并附observed_at。

默认15分钟周期及配置优先级不变。`watch --keep-on-disconnect` 在本次有界等待内继续只读查询，不顺延截止时间、不重派、不续跑。到期仍有不可达任务时，progress_due携带unreachable状态；发起方报告未确认事实。默认watch保持原遇连接错误提前返回的行为，兼容已有监控。

新任务默认clean前要求本轮结果已收取确认；发起方先保存需要的产物，再按原Git安全检查清理。旧任务的run/status/watch/resume/fetch等调用保持兼容；本协议不要求迁移正在执行的任务或复制全机配置。
