# 检查命令记录

工作目录：本票 remote-dispatch worktree。所有测试串行执行。

```sh
# 每个功能切片（先红，最小实现后绿），<test> 为下列对应方法
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_session_poll_isolation.SessionPollIsolationTests.<test> -v

# 首次相关检查：100 项，2 失败，随后修复
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_session_poll_isolation tests.test_rd_serve_sessions tests.test_rd_serve_host_last_seen tests.test_rd_serve_writes tests.test_rd_serve_review_fixes -v

# 第一轮审查修复后的 11 项检查
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_session_poll_isolation tests.test_rd_serve_sessions.SessionTests.test_persistent_projects_survive_herdr_outage_and_never_expose_origin_credentials tests.test_rd_serve_review_fixes.WriteFixTests.test_slow_job_probe_does_not_block_another_jobs_ack -v

# 首轮完整测试，581 项、11 个失败记录，full-suite.log
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest discover -s tests -p 'test_*.py' -v

# 完整测试发现问题后的 33 项相关复测，全部通过
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve.ServeTests.test_progress_legacy_latest_and_no_progress_are_read_without_remote_queries tests.test_rd_serve_network.NetworkTests.test_candidates_are_sent_in_events_and_session_access_can_only_be_enabled_locally tests.test_rd_serve_network.NetworkTests.test_device_permission_applies_to_projects_resolution_events_and_session_input tests.test_rd_serve_projects_stats tests.test_rd_serve_read_latency.ReadLatencyTests.test_two_hundred_jobs_and_thirty_projects_return_while_target_is_slow tests.test_rd_session_poll_isolation -v

# 修复后的第二次完整测试，full-suite-final.log
PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest discover -s tests -p 'test_*.py' -v

# 语法与差异检查（不是类型检查）
python3 -m compileall -q src tests
git diff --check
```

| red／green 日志前缀 | test 方法 |
| --- | --- |
| tdd-isolation | test_new_pane_appears_within_two_cycles_while_200_jobs_target_hangs |
| tdd-stale | test_collection_failure_is_visible_and_successful_empty_list_recovers |
| tdd-batch | test_jobs_are_batched_by_target_without_holding_job_lock_during_rpc |
| tdd-backoff | test_offline_host_backs_off_and_recovery_refreshes_jobs_in_same_cycle |
| tdd-terminal | test_received_terminal_job_is_not_reprobed_but_new_round_is |
| tdd-scan | test_each_discovery_scans_a_harness_once_for_multiple_panes |
| tdd-conversion | test_bad_transcript_conversion_does_not_freeze_other_panes_or_leak_errors |
| review-disabled | test_disabled_host_existing_jobs_continue_refreshing |
| review-partial | test_partial_host_response_marks_missing_job_unreachable |

退出码文件对应完整／相关检查。单条 TDD red 日志有预期失败摘要，green 日志均有 OK。
静态类型检查入口与工具均缺失，未运行；static-checks.log 记录环境及 Python 3.9 语法检查。
pre-commit 不存在，未禁用或修改 hook，hook 实际执行次数为 0。
