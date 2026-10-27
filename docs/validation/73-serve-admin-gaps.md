# 票 73 serve 设置接口补缺：验收证据

仅修改当前 remote-dispatch worktree，未改 App、未切分支、未 push、未部署或重启外部服务。
执行环境：macOS arm64，Python 3.12.13，10 核；检查串行，真实 loopback TLS / Unix socket 已可运行。
基点：3385ece；分支：`rd/weicoz/v4-73-20261027-225655-383823000`。

## 验收逐项映射

| 验收项 | 实现及证据 |
| --- | --- |
| 全局 session_access 读写与合同 | GET/PUT `/v1/config/session-access`，默认 true，立即改变运行时行为；复用 HostConfigRoutes 的配置锁、备份与原子替换。`test_global_session_access_is_persisted_and_takes_effect_without_restart`、`test_session_access_rejects_bad_requests_and_preserves_config_on_replace_failure`、`test_setting_contract_examples_match_responses`；`tdd-session-access-red/green.log`。合同 session-access-get/put/invalid.json |
| 关闭配对窗口 | DELETE `/v1/pairing-window`，旧码立即 403，已配对设备仍可鉴权，旧关闭请求重放不关闭新窗口；`test_closing_pairing_window_invalidates_code_and_preserves_paired_device`、`tdd-close-window-red/green.log`；合同 network-pairing-window-close.json |
| 设备 paired_at/last_seen_at | 成功配对记录 UTC ISO 时间；成功鉴权更新时间，失败不更新，旧记录未知值 null；前台内存合并、后台60秒批量持久化、停服补刷。`test_device_times_track_only_authenticated_requests_and_survive_restart`、`test_legacy_device_timestamps_are_unknown_until_successful_authentication`、三方并发与停服鉴权测试；`tdd-device-times-*`、`review-flush-lock-shutdown-*`；合同 network-devices.json |
| 监听地址 kind | 按 VPN 优先、LAN 次之、other 回退判断 IPv4/IPv6；含禁用配置项和实际候选、新事件。`test_network_classifies_selected_addresses_including_disabled_rows` 固定 13 个地址期望；`tdd-network-kind-red/green.log`，既有地址事件与合同检查同步 |
| 删除阻止数量和 ID | 409 原 reason 保留；error.running_count 全量、error.running_job_ids 排序最多20个，含其他用户及未知执行状态。`test_delete_reports_total_and_caps_active_ids_across_users` 25项夹具，既有进行中→完成删除测试；`tdd-delete-details-red/green.log`；合同 host-config-active.json |
| max_heavy ≥1、无8上限 | 99 成功；0/-1/小数/NaN/非数字/布尔值拒绝并带可读原因。`test_max_heavy_accepts_above_eight_and_explains_invalid_values`、`tdd-max-heavy-red/green.log`；合同 host-config-max-heavy-invalid.json |
| 测试、文档和现有语义 | docs/serve.md、contracts/v1/README.md 更新；14个新增 HTTP/鉴权/终端测试；正常设备权限、幂等、防泄露和读缓存回归保留；检查结果见下 |

## 检查命令及结果

所有命令从本票 worktree 执行；日志相对于 `docs/validation/73/`。

- 各 TDD 切片：`PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_admin.<Class>.<test> -v`。
  原始 red/green 日志保留；每个功能先失败后最小实现通过。
- 首次相关回归：`python3 -m unittest tests.test_rd_serve_admin tests.test_rd_serve_host_config tests.test_rd_serve_network -v`，
  同上环境；36项，2失败，`related-tests.log`。一项为 kind 事件白名单遗漏，一项为原删除测试缓存时序；均已处理。
- 相关回归：加入 `tests.test_rd_events`，50项全部通过，`related-final-tests.log`。
  该次之后审查增加的停服、三方并发与流权限修复另有定向 green，并由最终全套覆盖。
- 性能：`PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_list_cache tests.test_rd_serve_read_latency -v`，
  16项通过，`performance-regression.log/.exit`。含200当前派单、311历史结果、1.5万事件、10客户端及SSE夹具。
- 三方并发慢刷盘：`review-flush-lock-shutdown-green.log`，10次 HTTPS 前台约0.89–1.26ms、设备查询约0.19ms；
  后台刷盘与等待中的设备写未阻塞鉴权／查询，且新时间、权限写与重启恢复均通过。
- 全量：`PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest discover -s tests -p 'test_*.py' -v`；
  595项全部通过，354.559秒，退出码0；`full-suite.log/.exit`。完整套件实际执行1次。
- 静态检查：compileall、Python3.9 AST 兼容性、合同JSON解析、git diff --check，均通过，见 `static-checks.log`；未运行类型检查（无项目入口或已安装工具）。

正常 hook 保留。`core.hooksPath` 未配置，默认 pre-commit 文件不存在，因此 hook 实际执行次数0；
完整套件由实现会话直接执行，不存在可复用的 hook 全量结果，未禁用或绕过 hook。
本仓无配置的类型检查入口，mypy/pyright 均未安装；语法检查不冒称类型检查。

## 审查与改动文件

[双轴 code-review 与处理](73/code-review.md) 包含两路发现、定向复现和复核关闭结果。
生产代码：rd_serve_host_config.py、rd_serve_network.py、rd_serve_sessions.py、rd_events.py。
测试：test_rd_serve_admin.py（新增）、test_rd_serve_host_config.py、test_rd_serve_network.py、
test_rd_serve_sessions.py（假 Herdr gate）、test_rd_session_defaults.py、test_rd_terminal_ws.py。
文档：docs/serve.md、contracts/v1/README.md；新增6个合同、更新4个既有合同与本验证目录。

## 文案与未验证范围

待编排方定稿：`error.invalid_host_field.max_heavy`，占位“并发上限必须是大于等于 1 的整数。”
没有新增 App 文案或改动 App。新接口和字段的 App 接线、真实设备与真实服务部署尚未验证。
未访问远端更新基线，不能据此声明已与远端最新主线同步；本地未出现合并冲突。
分钟周期采用票据要求的60秒；测试直接驱动同一刷盘接缝，不等待真实60秒时钟。
异常强退最多丢失未落盘周期的最新访问时间；磁盘不可用时保留内存值并在下一周期重试。

## 需编排方／端到端执行

1. 编排方审查本票提交后，将代码部署到测试栈并按现有流程重启 serve；本会话不写测试栈部署目录。
2. App 对接 GET/PUT `/v1/config/session-access` 与 DELETE `/v1/pairing-window`，复制新增和更新的合同。
3. 在测试栈读写总开关，确认配置备份、注释保留以及设备权限独立；观察已开启 WS/SSE 与新的会话请求权限。
4. 真机开启窗口后提前关闭，确认旧码失败而已有设备仍可用；验证 paired_at/last_seen_at 显示。
5. 用真实流量跨越60秒，核对设备时间批量落盘与服务重启恢复；在实际历史量下确认列表延迟。
6. 核对实际 LAN/Tailscale/EasyTier 地址分类、跨用户进行中派单删除阻止详情、max_heavy>8 保存和非法值文案。

提交号由最终回报给出；完整日志均随本票提交。
