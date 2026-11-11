# 72：会话发现与派单刷新解耦交付证据

实现范围为当前 remote-dispatch worktree；起始基线 `bfc76c6a2815ec4a79e317b8ad051ac2b178a39b`，
分支 `rd/weicoz/v4-72-20261027-104926-774223000`。未切换分支、未 push、未合入其他分支，
未修改 App、未部署／重启测试栈。提交号由本票最终回执提供。
规格与现场依据来自 `.rd-context/72-session-poll-isolation.md`、`.rd-context/72-e2e-findings.md`。

## 验收项与证据

| 票据项 | 实现与可查证据 |
| --- | --- |
| 会话发现独立节拍，默认 15 秒 | `Service.poll_sessions` 独立线程；保留原默认和本地写唤醒；`tdd-isolation-{red,green}.log`，200 条历史派单、180 条指向会挂起 30 秒的可取消假目标机，HTTP 断言新 pane 在两个 0.5 秒测试周期内出现且目标仍阻塞。测试缩短的是刷新周期，没有调整生产 SSH 超时。 |
| 按 host 批量、离线退避熔断、恢复重试 | 复用 `cli.job_states` 按 host／user／worker_root 分组；网络查询在 job 锁外，发布前在锁内重读并复核 generation；`tdd-batch-{red,green}.log` 验证 12 条同 host 一次 RPC 且 RPC 期间可取得 job 锁；`tdd-backoff-{red,green}.log` 验证连续失败后本地唤醒不绕过等待、恢复同轮查询并解除退避。 |
| 已终态且已收取回执不再探测 | 同 generation 的终态快照、终态回执、匹配 ack 后跳过；`tdd-terminal-{red,green}.log` 验证重启后无状态查询，新轮次后重新查询。未签收继续刷新，沿用现有收取语义。 |
| collect 失败可观测、成功空列表可区分 | 新增顶层 `collection`，失败保留旧列表并记录类别／连续次数；连续 3 次失败标记过期，成功重置；`tdd-stale-{red,green}.log` 从有效列表经过三次失败到成功空列表。日志不输出异常消息／终端正文／凭据。 |
| messages 隔离 KeyError／TypeError | 保留已有 `unsupported_format`，清除部分转换缓存；`tdd-conversion-{red,green}.log` 对两种异常注入验证两 pane 均出现、HTTP 支持状态正确且不泄漏错误正文。此项为转换接缝的故障注入，不断言内部调用形状。 |
| 同轮缓存转写扫描 | `discover` 局部按 harness 缓存，`locate` 接受本轮扫描结果；`tdd-scan-{red,green}.log` 在文件系统边界验证同轮两个 pane 扫描一次、下轮能看到新文件。 |
| 测试与现有语义 | 新增 9 项 HTTP／RPC／文件系统接缝回归；审查额外覆盖禁用 host 既有派单、批量响应缺项；定向最终 11 项通过，含两项需随新增字段／锁边界更新的既有回归。最终全量 581 项全部通过，无跳过，见 `72/full-suite-final.log`。 |
| 合同／文档与真实环境步骤 | `contracts/v1/sessions-list.json` 新增 `collection`，新增 `sessions-stale.json`，合同索引和 `docs/serve.md` 同步更新；本报告列出部署及复测边界。 |

全部日志位于同目录下的 `72/`。TDD 首次红与绿均保留；不是只记录最终通过。
TDD 接缝以票据指定的 `/v1/sessions` 与假目标机验收为准；扫描在文件系统边界计数，
坏转换在转换接缝注入指定异常。既有测试样式在 HTTP 接缝使用可执行假 Herdr，未访问真实个人会话。

## 检查命令、次数与覆盖

技能路径差异：指定的 `/Users/user/.agents/skills/weicoz-agent-implement/SKILL.md` 在本机不存在。
已完整读取本机 `~/.agents/skills/weicoz-agent-implement/SKILL.md`，该版本没有名为“实现会话检查约定”的节；
因此按本票明示的串行、保留 hook、相关与全量单元测试、socket 执行边界要求运行，未声称读到缺失章节。

命令均串行。测试统一前缀 `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest`。

| 命令／阶段 | 结果与日志 |
| --- | --- |
| 单个 `tests.test_rd_session_poll_isolation.SessionPollIsolationTests.<test>` | 九个功能／审查切片分别先红后绿；日志见上述对应名称。 |
| `tests.test_rd_session_poll_isolation tests.test_rd_serve_sessions tests.test_rd_serve_host_last_seen tests.test_rd_serve_writes tests.test_rd_serve_review_fixes -v` | 首次相关检查 100 项，2 失败：新增 collection 打破旧精确响应断言、锁外批量改变旧 mock／排队前提。已修复测试以保持原行为目标；`72/related-tests.log`、退出 1。 |
| `tests.test_rd_session_poll_isolation tests.test_rd_serve_sessions.SessionTests.test_persistent_projects_survive_herdr_outage_and_never_expose_origin_credentials tests.test_rd_serve_review_fixes.WriteFixTests.test_slow_job_probe_does_not_block_another_jobs_ack -v` | 修复及审查回归 11 项通过，11.337 秒；`72/review-final-tests.log`、退出 0。 |
| `discover -s tests -p 'test_*.py' -v` | 首轮 581 项，11 个失败记录（含子用例），350.606 秒，退出 1；见 `72/full-suite.log`。修复后第二次 581 项全部通过，337.342 秒，无跳过，退出 0；见 `72/full-suite-final.log`。 |
| `python3 -m compileall -q src tests`；`ast.parse(..., feature_version=(3,9))` | 源码／测试编译及改动文件 Python 3.9 语法检查通过；`72/static-checks.log`。这些不是类型检查。 |
| `git diff --check` | 通过。 |

本仓 pyproject.toml、Makefile、CI 没有静态类型检查入口，本机 mypy／pyright 均未安装；
未新增依赖，静态类型检查未运行。无独立 build 入口。
正常提交 hook 已检查：core.hooksPath 未配置，实际 pre-commit 不存在；hook 实际执行 0 次，
没有跳过／修改 hook。完整测试单独执行 2 次：首次发现问题后修复，必须重检，未把失败结果当作通过。
环境为 macOS arm64、Python 3.12.13、10 核；开始时 load 约 1.84、磁盘可用约 49.6 GB，
系统内存空闲百分比 38%；读取总内存／swap 的 sysctl 被沙箱拒绝。测试无并发执行。
真实 Unix／TCP／TLS socket 测试已在本机运行；最终套件 581 项无跳过，详见全量日志。

首轮全量的 11 个失败记录归因与处理：网络测试两处精确顶层字段断言改为保留状态码／会话内容断言；
进度只读测试原先把启动后台写入算成 HTTP 写入，现以传输边界阻塞启动刷新，并等待会话采集完成后
比较文件快照；ProjectsStats 使用未配置 host 的既有 job_states 模拟，新增调度不应先强制解析 host，
现仅已配置非启用 host 才检查连接配置与退避，未知 host 仍交既有 job_states 校验；
200 派单慢查询测试原先依赖 probe 失败后继续 state 查询，已将 probe 改为成功后阻塞 state，保留读延迟目标。
以上没有降低原业务断言；33 项相关复测通过，45.803 秒，退出 0，见 `72/full-fixes-tests.log`，
完整命令见 `72/check-commands.md`。两个审查轴对这些返修均已只读复核，无新增问题。

## code-review 发现与处理

见 `72/code-review.md`：规范轴 1 项文档矛盾已修复；规格轴 2 项兼容性问题已按 TDD 修复；
两个只读代理复核均确认关闭。未留下明确审查问题。
审查比较本票起始基线与工作区差异；没有外部 issue-tracker 配置，本票本地规格足以完成审查。

## 改动文件

- `src/remote_dispatch/rd_serve.py`：独立会话线程、双刷新唤醒、批量状态查询、host 退避、终态跳过。
- `src/remote_dispatch/rd_serve_sessions.py`：采集状态／脱敏日志、坏转换隔离、同轮扫描缓存。
- `tests/test_rd_session_poll_isolation.py`：九项本票回归。
- `tests/test_rd_serve_sessions.py`：适配新增顶层字段，保留旧列表断言。
- `tests/test_rd_serve_review_fixes.py`：改在 SSH 边界阻塞并保留其他派单可签收断言。
- `tests/test_rd_serve_network.py`：保留会话权限与内容断言，兼容新增顶层字段。
- `tests/test_rd_serve_read_latency.py`：探针成功后阻塞批量查询，维持 200 派单读取延迟断言。
- `tests/test_rd_serve.py`：隔离启动后台写入与 HTTP 只读断言。
- `contracts/v1/sessions-list.json`、`contracts/v1/sessions-stale.json`、`contracts/v1/README.md`：新增采集状态合同。
- `docs/serve.md`、本报告与 `docs/validation/72/`：接口语义、部署复测、检查和审查证据。

## 需编排方／端到端执行

1. 由编排方将提交部署到测试栈并按现有流程重启 serve；本实现会话未写测试栈目录或重启服务。
2. 真实环境保留约 200 条历史派单、多数受控不可达，记录新建／关闭 pane 与 `/v1/sessions`
   更新时序；默认 15 秒下两个刷新周期内可见。测量不可达传输、恢复探测、CPU／内存，
   生产周期和超时需要目标环境数据支撑，本票没有凭估计修改传输超时。
3. 观察按 host SSH 批量、连续失败退避、熔断到期探测与恢复同轮重试；
   验证终态且签收后停止远程刷新，resume／迁机后重新刷新。
4. 受控制造 Herdr 失败三次，验证旧列表保留、collection.stale、计数、脱敏日志；
   恢复成空列表后 stale 重置。真实 Herdr／SSH 和真实 App 未在实现会话中操作。
5. App 合同同步、新增字段兼容及展示；待定稿文案键 `sessions.list_stale`，
   占位文字“会话列表已过期，正在重试”。无其他新增用户文案。

未验证：生产默认节拍下的真实耗时／负载／恢复、真实设备与 App 展示、部署结果、静态类型。
未 fetch 或比较远端，不能声称远端基线最新；起始 worktree 干净，本地无冲突。
退出码／回执只表示会话执行结果，最终验收由编排方判断。
