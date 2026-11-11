# 票 64：目标机配置 CRUD 实现与验证记录

范围仅 remote-dispatch；没有改 App、部署、重启服务、push、merge 或切换分支。
接口合同为 `docs/serve.md` 的“目标机配置（票 64）”节。
实现使用本宿主 `~/.agents/skills/implement/SKILL.md`、`tdd/SKILL.md`、
`code-review/SKILL.md`；指定 `/Users/user/.agents/skills/weicoz-agent-implement/SKILL.md`
不存在，已读取当前宿主 `~/.agents/skills/weicoz-agent-implement/SKILL.md` 全文，
按其“检查与验收”节及本票约定执行。未读取其他个人规则、记忆或历史。

## 逐条验收证据

测试源码：`tests/test_rd_serve_host_config.py`。测试使用真实 HTTP、Unix socket、
本地配置文件与隔离目录；TLS 测试绑定回环临时端口，不接触测试栈或真实目标机。

| 验收项 | 实现及证据 |
| --- | --- |
| 列出全部目标机配置，不回显密钥 | GET `/v1/config/hosts` 每次读文件，包含禁用项与继承默认值。`test_lists_disabled_hosts_effective_fields_and_only_credential_presence` 核对名称、ssh、sync、max_heavy、platform、enabled，认证字段只有字段名及 is_set。其余普通字段包含 workdir、资源限制等，未知扩展字段也返回字段名及设置状态。`test_new_credentials_are_redacted_from_other_public_responses` 验证新凭据同时遮蔽在既有 jobs 响应中。 |
| 新增、修改、删除；写前备份、保留注释及无关段落 | POST/PUT/DELETE；`test_create_is_backed_up_atomic_preserves_comments_and_replays` 比对备份原字节及 0600 权限；`test_update_keeps_comments_other_sections_and_credentials_and_refreshes_reads` 核对保留的凭据、注释与 project 段；`test_update_preserves_crlf_and_comments_in_multiline_options` 核对 CRLF 和多行注释；删除测试核对保留注释及无关段落。备份或原子 replace 失败测试确认原配置未变，临时文件清理。 |
| 名称唯一、必填项、sync 合法；失败原因可读且不写半截 | `test_validation_rejects_duplicate_required_sync_and_ini_injection_without_writing` 覆盖重复名、缺 ssh、错误 sync/platform/max_heavy/workdir/百分比、换行注入及非法名称，核对错误 reason/field、原字节不变、未建备份。整数语法及 401 位数字的独立回归核对 HTTP 400。 |
| 有进行中派单时拒绝删除 | `test_delete_rejects_in_progress_dispatch_then_succeeds_after_completion` 从真实本地 worker 的 running 到 done，先 409 再成功删除；`test_delete_protects_other_user_unknown_dispatch_and_preserves_unrelated_content` 覆盖其他用户状态未知的任务，错误不泄露其任务 ID。 |
| 修改后无需重启，下次读取生效 | 同一 serve 实例完成写入、GET 读回和 `service.config.host()` 检查；替换配置对象保留在途读取使用的旧 parser，写后唤醒原后台轮询。指标接口仍沿用后台快照语义。 |
| 合同及测试同步 | `contracts/v1/host-config-{list,create,update,delete,invalid,active}.json` 六份样例及 README、serve 文档；HTTP 测试按样例递归验证状态、必需字段和 JSON 类型。App 副本留给编排方同步。 |

TDD 接缝为本票要求的 serve HTTP 接口与可观察的配置文件／备份效果。
读取先 404 失败再通过；新增、校验、编辑、删除及审查回归均记录过先失败再通过。
没有为本票调用远端 SSH、真实模型或真实设备。

## Standards 审查

依 code-review 技能，独立只读子代理审查起始 HEAD
`c74af379ef7d07769374d654c05d14481d2e8e4c` 至本票工作区的修改和新文件。
仓库没有 CODING_STANDARDS、CONTRIBUTING 或 `docs/agents/issue-tracker.md`；
使用本票已给的规格、用户约束、相邻实现、测试风格和 CI；未执行额外 setup。

累计发现 3 项，均已修复并增加回归覆盖：

1. 整数校验接受 `1.0`、`1e3`，但既有派单代码使用 `int(原字符串)`。增加相同的整数语法校验。
2. 配置编辑器把多行字段内形似配置项／段头的续行当成独立项，可能修改凭据并遗漏实际更新。
   改为按 ConfigParser 的缩进规则先识别续行；回归保留凭据里的 `sync=` 与 `[host:fake]`。
3. 修正整数语法后，超大整数传入 `math.isfinite()` 引发 OverflowError。
   改为先对原字符串做浮点有限性检查，整数项额外核对 int 语法；401 位 timeout 回归返回 400。

最终只读复审确认三个问题闭合，剩余明确问题 0，未新增值得单独提出的 smell。
子代理不修改代码、不运行检查；修复及测试由主实现会话完成。

## Spec 审查

独立只读子代理发现同样 3 项：续行问题违反保留未修改配置与写后生效要求；
整数格式问题影响既有派单消费者；超大整数断连违反失败原因可读要求。
最终复审确认全部关闭，未发现新的明确票据缺口或范围外行为。
本记录按两轴分别保留结论；两轴共涉及同三处问题，没有将其算成六个独立缺陷。

## 检查与环境

环境为远端 rdispatch 管理的当前任务 worktree，Darwin arm64、Python 3.12.13。
实现会话内检查串行执行，真实 socket 检查设置 `RD_RUN_SOCKET_TESTS=1`。
没有禁用或修改 hook；`core.hooksPath` 未设置，实际 hooks 目录仅有 `.sample`，
没有生效的 pre-commit 或其他提交 hook，因此 hook 实际执行次数为 **0**。
全量套件由手动运行一次提供，未声称由 hook 覆盖。

| 命令 | 结果 |
| --- | --- |
| `PYTHONPATH=src RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_host_config -v` | 审查修复后 13 项通过；后续超大整数回归并入下一条重检。 |
| `PYTHONPATH=src RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve tests.test_rd_serve_writes tests.test_rd_serve_network tests.test_rd_serve_host_last_seen tests.test_rd_session_defaults -v` | 81 项已执行测试通过，另 1 个模块加载错误：项目现有测试使用 `from test_rd import ...`，缺少 tests 导入路径。下一条使用正确 PYTHONPATH 复检该模块及本票测试。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_host_config tests.test_rd_session_defaults -v` | 32 项通过，23.276 秒；包含最终的整数溢出回归。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest discover -s tests -p 'test_*.py' -v` | 已运行一次：540 项，539 通过、1 失败，305.430 秒。唯一失败在未修改基线上复现，详见下文；不记为全绿。 |
| `python3 -m compileall -q src tests` | 通过，仅语法编译，不是静态类型检查。 |
| `git diff --check` | 通过。 |
| `python3 scripts/check_skill_links.py skills/weicoz-remote-dispatch/SKILL.md` | 通过。 |
| `python3 -m mypy --version` / `python3 -m pyright --version` | 均为 No module named；本仓没有类型检查配置或命令，本次未执行静态类型检查。没有将 compileall 当作类型检查。 |

原始检查日志位于本任务 `.rd-context/64-evidence/`（上下文目录不纳入实现提交）：
`host-config-tests.log`、`related-tests.log`、`host-config-and-defaults-tests.log`、`full-tests.log`、
`projects-stats-recheck.log`、`baseline-projects-stats.log`。
本表保留自包含的最终结果，编排方可进一步核对原日志。

## 全量中的既有时序失败

全量唯一失败为
`test_rd_serve_projects_stats.ProjectsStatsTests.test_live_round_elapsed_time_uses_real_target_marker_without_query_writes`，
断言位于原测试第 422 行：对查询前后状态目录做快照比较，期间后台 Reports 线程新建
`reports/fixture.json`。本票没有修改项目统计、汇报线程或该测试。

- 当前代码定向复测：`PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_projects_stats.ProjectsStatsTests.test_live_round_elapsed_time_uses_real_target_marker_without_query_writes -v`，1 项通过，1.308 秒。
- 为核对基线，使用 `git archive HEAD src tests contracts` 在本任务忽略目录
  `.rd-context/64-evidence/baseline/` 解包起始提交；未切换分支，未创建或修改其他 worktree。
  在该基线目录下以其独立 src/tests 作为 PYTHONPATH，用 `python3 -m unittest <上述同一测试名重复五次> -v`
  串行执行五次：4 项通过、1 项同断言失败，6.660 秒，同样新增 `reports/fixture.json`。
- 原始日志为 `projects-stats-recheck.log` 与 `baseline-projects-stats.log`。
  证据证明该失败在未修改基线上存在；没有通过修改汇报行为、修改既有测试或重复全套掩盖失败。
  全量实际结果保持 539/540，建议编排方将该既有异步快照竞态另票处理。

## 改动文件与 Git

- `src/remote_dispatch/rd_serve_host_config.py`：配置读写、验证、私密备份、原子替换、删除保护及脱敏。
- `src/remote_dispatch/rd_serve.py`：GET/POST/PUT/DELETE 接线、配置对象和初始敏感值登记。
- `src/remote_dispatch/rd_serve_network.py`：让已鉴权的 HTTPS DELETE 到达目标机配置路由。
- `tests/test_rd_serve_host_config.py`：13 项行为及合同测试。
- `docs/serve.md`、`contracts/v1/README.md` 与六份 `host-config-*.json`：接口合同、错误码、备份及操作语义。
- 本验证记录。

唯一仓库 remote-dispatch；分支 `rd/weicoz/v4-64-20261026-182242-268461000`。
起始提交 `c74af379ef7d07769374d654c05d14481d2e8e4c`，工作区初始干净。
提交后具体 SHA 通过会话最终结果及 reporter 回传，避免在提交正文内产生自指 SHA。
本地不存在远端跟踪引用，未 fetch，不能判断与集成分支的领先／落后；本轮未遇到冲突。

## 待编排方定稿的文案

无 App 改动。文案键及建议占位文字为 docs/serve.md 错误表中的：
`idempotency_key_required`、`invalid_host_request`、`host_field_required`、
`invalid_host_field`、`not_found`、`method_not_allowed`、`host_exists`、
`host_has_active_dispatches`、`host_config_busy`、`idempotency_key_reused`、
`host_config_unavailable`、`host_config_outcome_unknown`。字段名通过 error.field 参数提供。

## 需编排方／端到端执行

1. 由编排方按测试栈既有流程部署本提交；实现会话没有写部署目录或重启服务。
2. App 同步六份合同样例及新增字段／错误码，再定稿目标机设置页文案。
3. 在测试栈用配对后的 App 新增一台停用的测试目标机，编辑连接与认证文件配置，
   验证秘密不回显；启用后等待正常后台采集，确认下一次配置读取即时更新。
4. 使用专门的测试派单验证运行中删除 409；完成后再删除并核对私密备份、
   原有注释／项目段、服务进程持续运行。不要以真实生产任务进行删除试验。
5. 按编排方的构建安排执行打包／部署及跨设备复测；本轮未运行 build、真实 SSH、
   真实模型、真实 App 或跨设备端到端链路。

已执行真实本机 Unix socket 与 TLS 回环测试，未将这些检查移交为失败项。
未验证项为上列部署／跨设备链路和未配置的静态类型检查；全量存在上述已在基线复现的时序失败。
实现已完成，但检查状态按 partial 回传，最终验收由发起方基于证据判断。
