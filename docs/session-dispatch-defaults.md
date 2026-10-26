# 会话派单默认值

默认值只存在控制端 `RD_STATE/session-defaults/<用户>.json`；不同用户、发起会话互相隔离。

```sh
rdispatch session-default <发起会话ID> --host m4 --harness codex
rdispatch session-default <发起会话ID>                # 读取
rdispatch session-default <发起会话ID> --clear-host   # 只清除目标机
rdispatch session-default <发起会话ID> --clear-harness
rdispatch session-default <发起会话ID> --clear        # 两项都改读配置
rdispatch session-default <Herdr窗格ID> --pane --host m4
```

每项独立按命令行参数 > 会话默认值 > `[defaults]` 配置取值。
`run --agent` 与 `run --harness` 等价；设置默认 harness 后可只传 `--prompt-file`。
`--cmd`、`--test` 始终执行命令，不套用默认 harness。

```ini
[defaults]
host = auto
harness = codex
```

没有配置目标机时仍为 `auto`；没有配置 harness 时仍须显式指定 `--agent`。
显式 `--host auto` 覆盖会话目标机，沿用自动选择规则（不选本机目标）。
明确目标机不存在、离线或无法接单时返回中文错误，不静默切换机器。
原有 `--force-host` 跳过负载检查的语义保持。

发起会话识别沿用 `--initiator-session`、`RD_INITIATOR_SESSION`、父进程链识别的 harness
会话变量。ID 缺失时按 `HERDR_PANE_ID`（经 serve 转交时使用原始来源窗格）匹配。
找不到唯一匹配时读配置。具备会话 ID 时不跨会话套用同窗格的旧记录。

## serve 契约

- `GET /v1/sessions/{Herdr窗格ID}/dispatch-defaults` 返回 `{host, harness, config: {host, harness}}`。
  顶层两项仍是会话覆盖值，`null` 表示读配置；`config` 每次只读当前本地配置的
  `[defaults] host/harness`，独立于会话覆盖。未配置目标机（或空值）为 `auto`，
  未配置 harness（或空值）为 `null`，与 CLI 没有隐含默认 harness 的行为一致。
  配置中的其他字段不返回。服务启动后修改默认值，下次 GET 即可读到；查询不写状态。
  返回值按本次配置的敏感值脱敏；配置格式损坏时返回 503 `read_unavailable` 与中文错误信息。
- `PUT` 相同路径，要求 JSON 对象和 `Idempotency-Key`；完整替换两项，缺项或 `null`
  表示读配置。CLI 设置只更新指定项。
- 会话 ID 优先；Herdr 只能给出推测匹配时按窗格保存，避免误用其他会话的默认值。
- 相同调用方、键、方法、路径和规范 JSON 请求体重放首次结果；同键异请求返回
  422 `idempotency_key_reused`。结果持久保存，服务重启后仍有效。
- 实际变化才发 `session.changed`，携带 `user_id / pane_id / session_id / dispatch_defaults`。
  客户端收到此变体后重新读取默认值；原有会话状态事件不变。
- 沿用会话访问总开关和设备权限；未开启时读写均返回 403 `session_access_disabled`。
- 写入和 CLI 共用按用户的文件锁；serve 运行时 CLI 转交 Unix socket。
  默认值、幂等结果、事件作为一次可回滚写入提交。

后台成功发现会话时更新存活状态。确认会话消失后开始计时，保留满 30 天再清理；
会话重开会取消关闭计时。发现失败和 serve 停机都不推测关闭时间，保留期从首次成功
确认关闭开始，所以可能延后回收。关闭会话访问时仍清理已有关闭时间的到期记录，
但不据此判定新关闭。清理在 serve 的后台采集中进行，无需查询触发写入。

## 部署与复测（编排方执行）

实现会话只提交代码。编排方按现有方式更新测试栈 Python 包并重启 serve，再在真实
Herdr 会话设置默认值、用省略目标机和 harness 的 `run --prompt-file` 验证派单；
分别显式覆盖两项并核对派单记录。App 使用上述 GET/PUT 和事件刷新，不另存状态。
验证已配对设备权限与真实离线目标机错误；无需 Swift/App 构建。
票 59 还需核对配置 `host/harness` 与 GET 的 `config` 相符：清除会话覆盖后
显示配置值；设置会话覆盖后括号中的配置当前值仍保持独立。App 文案由编排方定稿。
