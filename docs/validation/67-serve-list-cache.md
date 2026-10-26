# 票 67：serve 列表缓存与并发交付证据

范围：仅 remote-dispatch 当前任务 worktree；未修改 App、部署目录或其他分支，未读取用户私有状态。
设备为本次远端 rdispatch 的 macOS arm64；测试使用临时目录、合成回执与事件、隔离 Herdr 可执行夹具。
初始工作区干净，基线 `4db8cf3f980baf4e4f7d26f090c8edfbbf4ab95f`。
分支：`rd/weicoz/v4-67-20261026-235115-795015000`；提交号见会话最终回执。
未 fetch，因此不声称已验证与上游的领先／落后关系；本轮无合并或冲突。

## 逐项验收证据

| 验收项 | 实现与可查证据 |
| --- | --- |
| A：列表载荷代际缓存、单飞重建、后台预热 | `ProjectRoutes.list_view()` 以各文件组版本、事件 seq、执行快照版本、会话版本、配置和本地日期为键；锁保护单次重建并在完整成功后发布。poll 完成采集及统计后预热 current/history 视图，再标记 Reports 就绪。 |
| B：批量回执／ack／review、历史缓存、避免重复 resolve | `LocalFiles` 每次读取检查 inode/size/mtime/ctime，只重新解析变化 JSON；Query 使用一次解析的绝对 state 路径和批量 documents；历史记录及卡片随列表视图缓存。current 只加载当前轮三类文件，不依赖无关历史。 |
| 列表移除 `read_after(0)` | 项目汇总使用 EventCursor 和按身份保留的最新事件，单独保留状态事件；后续 progress 不覆盖状态事件。 |
| SSE、Reports 增量 | SSE 改为 EventCursor，保留序号及段内字节偏移；Reports 继续使用已有 EventCursor，配合 Service.records 的文件代际缓存，0.25 秒空轮询不再重新解析 200 个 job。事件轮转／重连由现有事件与 serve 契约测试覆盖。 |
| C：Unix 并发／排队，无 connect refused | Unix accept 队列 128，完整列表 GET 同时处理上限 32；SSE／未完成请求不占列表槽。10 客户端每接口全部返回 200；另测 32 条部分 HTTP 请求仍可读取列表。 |
| 真实形态与性能 | 200 个当前 job、311 个 results-only 历史 job，各 3 轮 receipt/ack/review，约 1.5 万条事件，加一条真实 Unix HTTP SSE 和后台 Reports；`full-suite.log` 中 projects 暖态 28.1 ms、10 并发 308.7 ms，jobs 暖态 15.1 ms、10 并发 152.3 ms，分别低于 300 ms／1 s。 |
| 状态变化后下一次读取立即可见 | `test_warm_lists_show_next_round_receipt_ack_review_event_and_deletion_immediately` 依次写轮次、回执、ack、review、新事件、原地编辑与删除，直接断言下一 GET；没有等待刷新窗口。规模测试也断言新事件和新回执可见。 |
| 同轮事件与新回执的先后 | `test_same_round_state_events_are_visible_before_the_receipt_arrives` 覆盖 running→done、状态事件后 progress、旧 running 事件后无 captured_at 的新 done 回执。状态事件须匹配 job/round/host 且比已有快照和文件新；不可达不覆盖已有终态回执，未知退出码不抹掉已知值。 |
| 合同兼容 | 新增 700 项大项目索引及无关用户坏文件测试；新增无关 results-only 历史和同目录坏旧轮次不阻断 current 的回归；现有 HTTP/SSE 契约与历史分页测试保留。响应字段未新增或删除。 |
| 全量测试 | 550 项全部通过，312.777 秒，退出 0，无跳过；见 `67/full-suite.log`／`full-suite.exit`。全量实际执行 1 次。 |

## 技能与 TDD

完整读取 `~/.agents/skills/implement/SKILL.md`、`tdd/SKILL.md` 及其 tests/mocking 参考。
用户指定的 `/Users/weicoz/.agents/skills/weicoz-agent-implement/SKILL.md` 在本机不存在，
已找到并完整读取 `~/.agents/skills/weicoz-agent-implement/SKILL.md`，按其检查约定执行。
接缝沿用票据已指定的 HTTP 列表、SSE 和状态写入后的立即读取；测试不依赖内部缓存结构。

红→绿记录：

- `67/tdd-performance-red.log`：原实现 projects 暖态 1.505 秒，超 300 ms；
  修复后的规模测试与最终性能见 `67/review-final-tests.log`。
- `67/tdd-index-red.log`：项目索引扫描误依赖其他用户坏文件导致 400；修复后的大索引测试通过。
- `67/tdd-review-red.log`：32 条部分请求导致超时、无关历史导致 400、同轮状态仍旧，均复现后修复。
- `67/tdd-old-round-red.log`：坏旧轮次仍阻断 current，复现后改为当前轮精确加载。
- `67/tdd-receipt-order-red.log`：旧 running 事件覆盖无 captured_at 新 done 回执，复现后补文件时间优先级。
- `67/review-final-tests.log`：上述修复后的 7 项检查全部通过，退出 0。

保留开发中间失败日志（`performance-intermediate.log`、`review-intermediate.log`、
`review-related-tests.log`、`profile-run.log`），不将这些失败结果标为通过。
其中 `review-related-tests.log` 的 2 项合同失败由未知事件退出码覆盖已知 receipt 退出码引起，
已修复并由最终合同定向复测覆盖。

## 检查记录

所有测试命令使用 `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3`，按模块串行执行。
性能测试内部的 10 个客户端是被测并发负载，不是并行测试任务。
开发期 cProfile 探索用同一规模测试，结果不用于验收延迟。

| 命令（省略上述环境前缀） | 结果／日志 |
| --- | --- |
| `-m unittest tests.test_rd_serve_list_cache tests.test_rd_query tests.test_rd_events tests.test_rd_reports tests.test_rd_serve_projects_stats tests.test_rd_serve_read_latency -v` | 中期相关检查 63 项通过，50.604 秒；`67/related-tests.log`。 |
| `-m unittest tests.test_rd_serve_list_cache tests.test_rd_serve_projects_stats tests.test_rd_serve_read_latency -v` | 审查返修途中 36 项、2 项合同失败，已处理；`67/review-related-tests.log`。 |
| `-m unittest tests.test_rd_serve_list_cache tests.test_rd_serve_projects_stats.ProjectsStatsTests.test_new_http_and_sse_contract_examples_match_requests_responses_and_events -v` | 最后定向检查 7 项通过，8.732 秒，退出 0；`67/review-final-tests.log`。 |
| `-m unittest discover -s tests -p 'test_*.py' -v` | 550 项全部通过，312.777 秒，退出 0，无跳过；`67/full-suite.log`／`full-suite.exit`。全量共执行 1 次。 |
| `python3 -m compileall -q src tests`、`git diff --check`、`git diff --cached --check` | 最终均退出 0；`67/static-checks.log`。 |

Git `core.hooksPath` 未设置，实际 hooks 目录只有 `.sample`，没有生效的提交 hook。
正常提交保留原 hook 配置，hook 实际执行次数 **0**，全量测试由手动一次执行提供，未声称 hook 覆盖。
静态类型检查：仓库没有 mypy／pyright 配置或 CI 入口，环境两者均未安装。
`compileall` 只作为语法检查，不冒充静态类型检查；最终记录另见 `67/static-checks.log`。

## code-review

已完整读取 `~/.agents/skills/code-review/SKILL.md`，按其明确要求运行 Standards、Spec 两路独立只读审查，
并对发现逐项修复后复审。详见 [审查记录](67/code-review.md)。最终两轴均无未解决阻断项。

## 改动文件

- `src/remote_dispatch/rd_serve_cache.py`：文件代际批量读视图。
- `src/remote_dispatch/rd_query.py`：单次 state resolve、可注入批量 JSON 读视图。
- `src/remote_dispatch/rd_serve_history.py`：从批量文档重建历史记录。
- `src/remote_dispatch/rd_serve_projects.py`：current/history 分开的列表缓存、单飞、预热、增量事件和状态优先级。
- `src/remote_dispatch/rd_serve.py`：增量 SSE、缓存 records、执行快照代际、Unix 队列与列表处理槽。
- `src/remote_dispatch/rd_serve_sessions.py`：会话快照版本。
- `tests/test_rd_serve_list_cache.py`：6 项合成规模／缓存正确性／合同／连接回归。
- `docs/serve.md`、本文件与 `docs/validation/67/`：运行行为与验收证据。

## 文案、部署与未验证项

本票没有新增界面或用户文案键，无待定稿占位文字。
没有部署、写测试栈目录、重启服务、push、切换分支、合并，未连接真实生产 socket、目标机或用户 Herdr。
没有新增依赖外部服务的超时／限流数值；32 个列表处理槽复用已有 HTTPS 默认连接上限的量级，
128 backlog 与 10 并发及 32 条部分请求在本机真实 Unix socket 回归中验证。

**需编排方／端到端执行**：审查提交后，按现有测试栈流程安装本提交并由编排方重启 serve；
在测试栈以 App 左栏轮询加 SSE 复测 `/v1/projects`、`/v1/jobs`，核对暖态与 10 并发预算，
在真实派单状态变化、回执、ack、review 后立即重读确认无陈旧状态，同时检查历史分页与 SSE 断线续接。
本机会话已执行合成数据的真实 Unix socket 与全量套件，不把测试栈／App 未执行项算为本票自动检查失败。
未做独立 build、Swift/App 构建、其他 Python 版本或其他设备的性能复测。
最终验收由发起方结合本证据判断，会话退出码与 DONE 不代替验收。
