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

## 派单模式

`dispatch_mode` 取值 `suggest`（默认）、`line`、`ticket`，优先级为命令行 `--dispatch-mode` >
会话默认值 > `[defaults] dispatch_mode`。取值为 `line` 时的接线行为见下节；`suggest`、`ticket` 本版不接线。

### 工作线接线（`line` 模式）

`run --plan-item <新工作项>` 在 `line` 模式下判断能否接入前序派单所在的线（前序派单取 `--after`、待派登记里的 `after`
与依赖工作项最后一个派单，必须唯一）。全部满足才接线，新工作项作为同一派单、worktree、分支的新轮次追加：

- 前序派单有交接协议版本、属于同一用户 ID 与发起会话、worktree 在且不在运行，最新一轮已验收通过且回执有 `head`；
- harness、模型、权限档位、`--include` 与线一致；不带 `--wip`；不指定 `--branch`，或等于线分支；`--host` 不指定或等于线所在目标机；
- `[features] line` 未设为 false（已有线不受此项限制）。

任一不满足，按默认方式派单并输出「工作线未接线：原因」。接线时打印线名、分支、权限档位和基线。
同一线同一时间只有一个运行中的轮次，沿用派单级文件锁；锁被占用时退出码 4。
派单记录的 `line` 字段含 `items`（工作项到轮次列表）、`bases`（工作项开始时的基线）与 `current`；
`RD_BASE` 在线上取所属工作项的基线（上一个工作项验收轮回执的 `head`），第一个工作项仍取派单基线。
打回只续跑当前工作项，新轮次记入同一工作项。本版会话沿用当前会话。
判定集中在 `rd_line.decide`，由 `cli.line_wiring` 收集事实后调用，后续 `suggest` 复用。

### `suggest` 与建议令牌

`rdispatch suggest`（参数同 `run`，另有 `--json`）只读判断本工作项能否接线，退出码恒为 0。判断在生成派单 ID 和代理预检之前，
条件为：严格接线条件（`rd_line.decide`，经 `cli.line_wiring`，不另写一份）、本链未拒绝过建议、前序派单所在目标机在线且可接单
（只读探测）、integration 同步预检无冲突（见下）。不满足时输出原因，不生成令牌。

满足时输出工作项顺序（已完成、本项、后续待派，按依赖拓扑排序）、线名、目标机、分支、权限档位、基线、不接受时的派法和令牌。
令牌绑定建议内容指纹、用户 ID 和发起会话，30 分钟内单次有效，存于控制端 `state/suggest/<用户ID>/`。
`cli.build_suggestion(cfg, args)` 返回 `suggest --json` 的结构，serve 可直接调用。

- 接受：`run --line <线名> --accept <令牌>`；加 `--accept-chain` 同时记「本链后续工作项都接受」。接受时重算指纹，
  过期、已用、内容变化、用户或会话不符一律退出码 4，不消耗令牌以外的状态。
- 常驻同意：该线有常驻同意时，`run --line <线名>` 免令牌；每次仍校验严格接线条件，不满足就按默认方式派出并写明原因。
  `--line` 既无有效令牌又无常驻同意，退出码 4。
- 拒绝：`run --no-line` 按默认方式派出，只在待派登记里记「本链不再建议」和拒绝次数。
- integration 同步：integration 新头取控制端解析的 `--ref`。它已是前序验收提交的祖先就不同步。否则控制端先取回前序提交，
  再用 `git merge-tree --write-tree` 预检二者能否无冲突合并，结果写入建议的 `sync`（`needed`、`integration`、`from_head`、
  `precheck`）并计入指纹；预检有冲突不给建议并列出冲突文件。不在目标机上试合并。接受即授权一次写操作：把新头推到目标机 bare
  的 `refs/rd-sync/<派单>`，在 worktree 里合并（只在 worktree 停在验收提交且无跟踪文件改动时执行），新工作项基线取合并后的 head。
  实际合并冲突时 `merge --abort`，worktree 回到合并前，按默认方式派出并写明原因。同步记录写入派单 `line.syncs`、
  回执 `integration_sync` 和看板。Windows 目标机本版不同步。
- 管理：`rdispatch plan chain <线名>` 查看；`--clear` 清除拒绝设置并恢复建议；`--revoke-accept` 撤销常驻同意。

```sh
rdispatch session-default <会话> --dispatch-mode line
rdispatch session-default <会话> --clear-dispatch-mode
```

`--clear` 只清 host、harness，不清派单模式。非法取值退出码 4。serve 在线时 CLI 写入按
ADR 0003 转交 serve（单写入者）。

## serve 契约

- `GET /v1/sessions/{Herdr窗格ID}/dispatch-defaults` 返回 `{host, harness, config: {host, harness}}`。
  顶层两项仍是会话覆盖值，`null` 表示读配置；`config` 每次只读当前本地配置的
  `[defaults] host/harness`，独立于会话覆盖。未配置目标机（或空值）为 `auto`，
  未配置 harness（或空值）为 `null`，与 CLI 没有隐含默认 harness 的行为一致。
  配置中的其他字段不返回。服务启动后修改默认值，下次 GET 即可读到；查询不写状态。
  返回值按本次配置的敏感值脱敏；配置格式损坏时返回 503 `read_unavailable` 与中文错误信息。
- `GET/PUT /v1/sessions/{Herdr窗格ID}/dispatch-mode`：会话级派单模式的独立接口，
  体为 `{dispatch_mode}`（`null` 清除），响应 `{dispatch_mode, config: {dispatch_mode}}`（GET）。
  `dispatch-defaults` 的 `config` 只读追加 `dispatch_mode`；其 PUT 只替换 host/harness，
  不接受 `dispatch_mode`，也不会清掉已存的值。非法取值返回 400。
- `PUT` 相同路径，要求 JSON 对象和 `Idempotency-Key`；完整替换两项，缺项或 `null`
  表示读配置。CLI 设置只更新指定项。
  PUT 在写入前重新发现窗格；窗格不存在或已关闭时返回 404 `session_closed`，
  不写默认值、幂等记录或事件。GET 仍允许读取保留期内的历史默认值。
- 会话 ID 优先；Herdr 只能给出推测匹配时按窗格保存，避免误用其他会话的默认值。
- 相同调用方、键、方法、路径和规范 JSON 请求体重放首次结果；同键异请求返回
  422 `idempotency_key_reused`。结果持久保存，服务重启后仍有效。
  窗格已关闭时，已有成功请求仍重放首次结果，不写记录；新请求返回上述 404。
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
