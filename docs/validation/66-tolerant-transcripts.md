# 66：聊天转写对未知记录前向兼容

本轮只修改 remote-dispatch，未修改 App、测试栈或其他工作区。
票据和诊断来自派单输入 `.rd-context/66-tolerant-transcripts.md`、`.rd-context/66-diagnosis.md`。
基线为 `c793c830aac940c2dad6bed339d538751aa4b7f5`。

## 逐条验收证据

| 验收项 | 实现及证据 |
| --- | --- |
| 按诊断 §3.1 最小修复 | `rd_transcripts.py` 跳过未知顶层记录、Claude 无字符串 content 的 system、Codex 未知 response_item 子类型；`test_claude_lifecycle_system_records_do_not_hide_chat` 覆盖 stop_hook_summary / turn_duration；`test_codex_new_metadata_and_response_types_preserve_chat` 覆盖 additional_tools、agent_message（含 encrypted_content）、inter_agent_communication_metadata、compacted，已识别文字完整保留，跨代理及压缩历史不进入聊天。 |
| ID 未命中回退 cwd + mtime 并标 probable | `rd_serve_sessions.py::locate` 在 ID 未命中时选择同 cwd 最新文件；`test_missing_session_id_falls_back_to_latest_cwd_with_probable_label` 通过真实 Unix socket 验证 Claude/Codex × 保存/实时身份四种组合，排除更新的其他 cwd 文件，并验证 `probable=true`、`identity_text="可能是这个会话"` 和消息正文。 |
| future_record / new-format 与损坏分开 | `test_unknown_top_level_records_and_fields_preserve_recognized_messages` 验证未知记录和扩展字段跳过；`test_unknown_transcript_record_preserves_chat_and_invalid_input_never_writes` 验证启动读取、增量追加和 after 游标。`test_changed_or_malformed_formats_return_unsupported_without_partial_content` 对有效正文后的损坏 JSON、非对象、缺失/非法 type、已识别消息缺必需字段及非法 payload 验证无部分正文；现有嵌套内容块损坏测试保留。HTTP unsupported 合同改用 `{broken`，Codex unsupported 夹具改用缺 content 的已知 message。 |
| 真实形态的脱敏最小样例 | 全部由代码手工合成，只保留诊断中的类型/字段形态；未访问真实转写文件，未提交真实对话、凭据或敏感路径。 |
| 合同说明 | `docs/serve.md` 和 `tests/fixtures/transcripts/README.md` 明确未知记录跳过、损坏整次 unsupported，以及回退条件。`contracts/v1/sessions-unsupported.json` 响应形状不变，合同测试仍核对该响应。 |
| agy/kimi 保持 unsupported | 未新增读取器；`test_changed_or_malformed_formats_return_unsupported_without_partial_content` 加入两个 harness 的公开转换断言，serve 无读取器的分支未改。 |

## TDD 和检查

已完整读取当前宿主 `~/.agents/skills/implement/SKILL.md`、`tdd/SKILL.md`
及其 tests/mocking 参考、`code-review/SKILL.md`。
用户指定的 `/Users/weicoz/.agents/skills/weicoz-agent-implement/SKILL.md` 不存在，实际完整读取
`~/.agents/skills/weicoz-agent-implement/SKILL.md`；该版本无同名“实现会话检查约定”标题，
按其“检查与验收”章节及本轮用户明确约定执行。
测试接缝采用票据指定的公开转写接口与会话 HTTP 接口。

所有测试串行执行，未禁用、修改或绕过 hook。
`core.hooksPath` 未配置，Git 实际 hooks 目录只有 `.sample`，无生效提交 hook；
因此 hook 实际执行次数为 **0**。首轮全量发现另一处旧断言，经修正后手动串行复跑全量，
完整套件实际执行 **2 次**；复跑由首轮失败触发，不重复声称 hook 覆盖。
环境为远端受管 macOS arm64、Python 3.12.13；CPU 负载、内存与磁盘已检查，保持单测试进程。

以下命令均从本任务 worktree 执行；日志保留在 `.rd-context/evidence/`。

| 命令 | 结果与日志 |
| --- | --- |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_transcripts.TranscriptTests.test_unknown_top_level_records_and_fields_preserve_recognized_messages -v` | 红：两个 harness 均失败；最小修改后绿。`tdd-1-red.log` / `tdd-1-green.log`。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_transcripts.TranscriptTests.test_claude_lifecycle_system_records_do_not_hide_chat -v` | 红：6 个生命周期子用例失败；最小修改后绿。`tdd-2-red.log` / `tdd-2-green.log`。 |
| `PYTHONPATH=src:tests python3 -m unittest tests.test_rd_transcripts.TranscriptTests.test_codex_new_metadata_and_response_types_preserve_chat -v` | 红：3 个 response 子类型用例失败；最小修改后绿。`tdd-3-red.log` / `tdd-3-green.log`。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_sessions.SessionTests.test_missing_session_id_falls_back_to_latest_cwd_with_probable_label -v` | 红：4 种身份组合失败；最小修改后绿。`tdd-4-red.log` / `tdd-4-green.log`。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_transcripts tests.test_rd_serve_sessions -v` | 43 项通过，31.574 秒；`related-tests.log`。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_transcripts -v` | 补充损坏结构及 agy/kimi 断言后，11 项通过；`transcripts-final.log`。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest discover -s tests -p 'test_*.py' -v`（首轮） | 544 项，543 通过、1 失败，305.794 秒；失败为 `test_rd_serve_review_fixes.SessionFixTests.test_transcript_parses_only_appended_lines_with_stable_ids_and_resets_on_replace` 的旧“未知=损坏”断言。见 `full-tests.log` / `full-tests.exit`（1）。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest tests.test_rd_serve_review_fixes -v` | 同步上述旧断言后 13 项通过，12.384 秒；`review-fixes-tests.log`。 |
| `PYTHONPATH=src:tests RD_RUN_SOCKET_TESTS=1 python3 -m unittest discover -s tests -p 'test_*.py' -v`（修复后复跑） | 544 项全部通过，306.433 秒，退出码 0，无跳过；见 `full-tests-final.log` / `full-tests-final.exit`。 |
| `python3 -m compileall -q src tests` | 实现后及最后测试修正后均通过。仅语法编译，不是静态类型检查；`compileall.log`、`compileall-final.log`。 |
| `git diff --check` | 通过。 |

静态类型检查缺口：pyproject.toml、Makefile、CI 均未配置此入口，环境没有 mypy/pyright
可执行文件或模块，未临时引入工具或更改项目检查配置。环境证据见 `check-environment.log`。

## code-review

按技能要求，对初始 HEAD 至工作区的差异分别启动 Standards 与 Spec 两个只读审查代理。
固定点 `c793c830aac940c2dad6bed339d538751aa4b7f5`；
命令 `git diff c793c830aac940c2dad6bed339d538751aa4b7f5`，该阶段新增提交为 0。
仓库没有 issue-tracker 配置，直接使用用户指定的本地票据及诊断作为规格来源。

- Standards：代码无硬性违反或需处理的 Fowler 启发式发现；未见越界或非脱敏内容。
  补审发现交付文档漏列缓存测试文件、全量结果和次数尚待同步，已补齐文件并如实记录首轮失败与复跑。
- Spec：初审及补审均无缺失、错误或越界；损坏校验、回退范围与 probable、agy/kimi 和缓存边界均符合票据。

全量测试额外发现 `test_rd_serve_review_fixes.py` 第三处旧“未知=损坏”断言，已改为未知记录空成功，
随后用真正损坏 JSON 验证 unsupported；保留重写后恢复的既有测试。两路审查均补审该差异，
确认不涉及 §3.3 的追加恢复问题。
原始两轴报告及审查差异快照见 `code-review.md`、`review.diff`、`review-final.diff`。
主代理另核对最终改动与检查结果，未将独立审查当作测试结果。

## 改动文件

- `src/remote_dispatch/rd_transcripts.py`：容忍未知记录，保留已知结构校验。
- `src/remote_dispatch/rd_serve_sessions.py`：ID 未命中回退。
- `tests/test_rd_transcripts.py`：兼容形态与损坏边界。
- `tests/test_rd_serve_sessions.py`：HTTP 回退、追加、游标和 unsupported 合同。
- `tests/test_rd_serve_review_fixes.py`：缓存替换测试同步未知/损坏断言，保留既有重写恢复行为。
- `tests/fixtures/transcripts/codex-unsupported.jsonl`：真正损坏的必需结构。
- `tests/fixtures/transcripts/README.md`：夹具与转换契约。
- `docs/serve.md`：接口契约。
- `docs/validation/66-tolerant-transcripts.md`：本交付证据。

## 分支、文案和未验证项

仅仓库 remote-dispatch，分支 `rd/weicoz/v4-66-20261026-223406-954990000`。
本轮按远端 rdispatch 流程在该分支提交，提交号由最终回报给出；不 push、不切换分支、不 merge。
没有本地 main/origin/main 引用，未联网 fetch，无法独立判断上游是否已前进；当前工作区无冲突。

未新增或修改用户文案。沿用字段 `identity_text` 的“可能是这个会话”和 unsupported 响应
`text` 的“不支持”；若需文案定稿，由编排方决定，未改 App。

缓存中 unsupported 一旦置位后追加有效行仍永久禁用聊天的既有问题（诊断 §3.3）本票明确不改。
显式路径未命中不回退；ID 未命中且无同 cwd 候选时保留原身份，消息仍 unsupported。
未用真实私人会话验证，不声称实际 10/10 会话或 App 已恢复；未更改外部依赖的超时或限流参数。

需编排方／端到端执行：

1. 按既有测试栈流程部署本提交、重启 serve；实现会话未写部署目录或重启服务。
2. 用目标环境的实际 Claude/Codex 会话核对 GET sessions 与 messages，确认已识别消息显示，
   ID 回退显示 probable 提示，压缩历史和跨代理消息不混入正文。
3. 在 App 验证聊天与终端、after 增量游标、agy/kimi 不支持态及文案；真实设备与配对链路未验证。
4. 若要求跨 Python 版本或构建安装，复用项目 CI 的 3.11/3.12/3.13 矩阵执行；本轮未构建安装包。

这些编排／端到端项不计为本票实现检查失败；最终验收由发起方结合证据判断。
