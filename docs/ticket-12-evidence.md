# 12-lock-and-pure-query 交付证据

执行环境：远端 rdispatch 任务 worktree，macOS，Python 3.12.13。
仓库：remote-dispatch；分支：`ticket/12-lock-and-pure-query`。
实施及审查基点：`6b3d3aedbd02f5118a56986a5ab390e86dc8f096`。
本地没有 `refs/heads/integration/desktop-board-v3`，无法核验相对该引用的落后情况；
未切换分支、合并或 push，未遇冲突。提交号见交付回报及本分支 `git log -1`。

## 逐条验收

| 验收项 | 证据 |
| --- | --- |
| 并发续跑只产生一轮，另一次报轮次进行中 | `tests/test_rd_locking.py::test_concurrent_resumes_only_launch_one_new_round`：两个真实 CLI 进程，在目标机 RPC 启动边界暂停首个请求；第二个退出 4，错误含 `round in progress`；目标机只记录一次 r2 启动，`status --json` 读回 `rounds=2` |
| 三个读命令不修改状态目录 | `tests/test_rd_query.py::test_read_commands_return_finished_result_without_changing_state`：比较全部目录及文件的路径、模式、mtime_ns、文件字节；覆盖 `status --json`、`status --details`、`result` 和 `result --summary`，同时验证接收会话及回执仍返回 |
| 运行中及异常状态查询仍只读 | `test_running_dead_missing_and_unreachable_queries_do_not_write`：覆盖 running / starting / dead / missing / unreachable，含结果不可用时的退出码 2 |
| 签收、停止、续跑互斥 | `test_resume_excludes_ack_and_stop_before_reading_target_state`、`test_ack_excludes_resume_and_stop`、`test_stop_excludes_resume_and_ack`：真实 CLI 跨进程覆盖六个方向；竞争者均退出 4 |
| 现有测试全部通过 | 唯一一次完整套件：202 个测试全部通过，79.620 秒；原有 187 个，加本票 15 个 |

补充行为证据：

- `test_running_round_rejects_a_later_resume_without_incrementing`：锁已释放、目标机仍运行时仍拒绝重复续跑。
- `test_target_failure_releases_lock_for_a_subsequent_resume`、`test_launch_failure_releases_lock_for_stop`、`test_process_exit_releases_lock_for_another_command`：探测失败、启动失败和进程被终止后可继续操作。
- `test_different_jobs_do_not_contend`：不同 job 独立加锁。
- `test_result_can_be_acknowledged_later_and_read_after_disconnect`：跨查询时间签收、持久化后离线读取，状态仍只读。
- `test_existing_historical_receipt_keeps_id_without_connecting`：旧历史回执 ID 兼容，读取不连接目标机。
- `test_wrong_receipt_id_does_not_materialize_an_uncached_receipt`：错误 ID 不落盘回执。
- `test_inbox_collecting_an_old_result_cannot_rewind_a_resumed_job`：并发 inbox 不回退已续跑的轮次。

## TDD 与检查记录

约定接缝：CLI 输出／退出码、持久化后查询读回的状态、状态目录文件快照。
测试使用 unittest、临时 RD_STATE、已有 patch 风格及文件支持的假目标机 RPC；
没有连接真实设备、服务或模型。

红绿记录：纯查询测试先因新增 results/ 和改写 job 失败；改用查询层后通过。
查询后签收测试先因无本地缓存退出 4；签收路径显式保存后通过。
并发续跑测试先出现第二次成功启动 r3；加锁后第二次退出 4、仅启动 r2。
审查新增的 inbox 回归先读回 `rounds=1` 而失败；移除旧 job 回写后读回 2。

实际检查命令与结果：

| 命令 | 结果 |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p test_rd_query.py -v` | 最终针对性运行 5 个，全部通过 |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p test_rd_locking.py -v` | 当时已有 9 个，全部通过；随后补充 inbox 回归见下一行 |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p test_rd_locking.py -k inbox -v` | 1 个，先失败再修复通过 |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p test_rd_messages.py -v` | 12 个，全部通过 |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p test_rd_workflow.py -v` | 11 个，全部通过 |
| `PYTHONPATH=src python3 -m unittest tests.test_rd_locking.JobLockingTests.test_inbox_collecting_an_old_result_cannot_rewind_a_resumed_job tests.test_rd_query -v` | Spec 审查复核运行，6 个全部通过 |
| `PYTHONPATH=src python3 -m compileall -q src tests` | 通过；仅语法检查，不冒充类型检查 |
| `git diff --check` | 通过 |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` | **202 个全部通过，79.620 秒，退出 0；完整套件实际执行 1 次** |

本仓库无提交 hook，hook 实际执行 0 次；按派单方约定手动完整检查一次，正常提交，
未绕过或修改 hook。仓库无类型检查配置，当前 Python 环境未安装 mypy／pyright；
未执行独立静态类型检查。无需 build。

## Code review

已完整读取 implement、tdd、code-review 技能；按 code-review 要求由两个独立子代理
分别审查 Standards 和 Spec，基点为实施前 HEAD，上下文采用本地票据与规格。

- Standards：0 项规范违规；复核接口文档、Python 3.9 语法兼容性和测试风格后通过。
- Spec：1 项已修复。`inbox -> collect_result -> save_job` 可将旧记录覆盖到已启动的新轮次；
  收取回执现仅保存回执，不再回写 job。续跑自身仍在锁内识别并保存接收会话。
  新增跨进程红绿回归并复核通过，未关闭发现 0 项。

## 改动文件与复用接口

- `.gitignore`：排除随单只读 `.rd-context/`；提交前 `git status` 无该目录，未修改或提交上下文。
- `src/remote_dispatch/rd_lock.py`：`JobLock(state, job)`、`acquire()`、`release()`、上下文管理和 `JobBusy`。
- `src/remote_dispatch/rd_query.py`：`Query.jobs / details / result / delivery`、`result_path`。
- `src/remote_dispatch/cli.py`：锁接入、查询适配 `queries(cfg)`、签收显式保存、删除收取结果时旧 job 回写。
- `tests/test_rd_locking.py`、`tests/test_rd_query.py`：10 个并发行为测试和 5 个纯查询行为测试。
- `docs/handoff.md`、`docs/locking-and-queries.md`：更新查询语义，记录调用方式、回调和完整返回结构。
- `docs/ticket-12-evidence.md`：本验收记录。

调用方式及返回结构详见 [接口说明](locking-and-queries.md)。后续写路径应在读取前
`with JobLock(state, job):`，一直持有到启动结束；不可重入，内部业务函数不重复加锁。
纯查询可复用 `queries(cfg)` 或注入三个只读回调构造 `Query`；身份筛选由调用方承担，
CLI 保留原发起会话规则，serve 可按用户规则调用。

## 待定稿文案

以下键为回报中的建议标识，代码未新增文案系统：

| 建议文案键 | 本票新增／修改的实际提示 |
| --- | --- |
| `job_lock.busy` | `round in progress; another operation holds the job lock; retry after it finishes`（退出 4） |
| `resume.round_in_progress` | `round in progress; rdispatch stop it first or wait`（退出 4） |
| `job_lock.invalid_path` | `任务锁路径不得是符号链接。`（ValueError，CLI 退出 4） |

## 未做／未验证与编排方步骤

本票不涉及 serve、监听端口、外部服务、真实设备或测试栈部署，无必须交由编排方
运行的端到端检查。测试覆盖假 Windows 目标机 RPC；**Windows 控制端原生
msvcrt 锁后端未实机验证**，如需 Windows 交付，由编排方在该平台复跑锁测试。
没有改变外部服务超时或限流数值。

编排方审查本分支提交后，合入 integration 并按其流程安装更新的 Python 包即可；
本会话未部署、写测试栈目录或重启服务。未改 CHANGELOG、版本号或 skills。
未核验缺失的 integration 引用新旧关系，未执行独立静态类型检查。
