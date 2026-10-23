# 合票后的缓存保留与回收

## worktree 过期回收（rdispatch clean --expired）

rdispatch 的 worktree 只被同一任务的 `rdispatch resume` 复用；新派单总是新建 worktree。所以“留不留”判断的是这个任务还会不会续跑：删早了，续跑要重新下载依赖和构建；一直不删，磁盘被旧任务占满。`rdispatch clean --expired` 按续跑可能性分级保留，只回收过期且安全的 worktree。

安全条件与 `--merged` 相同，任一不满足就永不自动回收：任务已结束；没有未取回的提交、合入关系可核验；全部轮次回执已收取并 `rdispatch ack`；worktree 除 `--include` 路径外没有未提交内容。回收前保存 reports，任务分支保留在目标机仓库，之后仍可 `rdispatch fetch` 或以该分支重新派单，但不能再 `rdispatch resume`。

保留期从最后一次回执确认（`rdispatch ack`）算起：

| 任务 | 默认保留 | 理由 |
| --- | --- | --- |
| 普通命令（`--cmd` / `--test`） | 0 小时 | 不能续跑，确认后即回收 |
| 代理任务，提交已合入目标分支或没有新提交 | 24 小时 | 已验收，留一天给追加修改 |
| 代理任务，已取回但未合入 | 7 天 | 还在审或部分完成，最可能续跑 |

目标机可用磁盘低于 `min_free_disk_gb` 时收紧：已合入的立即回收，未合入的上限 24 小时。

调整保留期写在 `[retention]`（`command_hours`、`accepted_hours`、`unmerged_hours`），环境变量 `RD_RETAIN_<KIND>_HOURS` 优先。明确还要续跑的任务用 `rdispatch keep <job> --hours N` 钉住，`--hours 0` 取消。

```bash
rdispatch clean --expired --dry-run    # 先看计划：每个任务给出 keep / would clean 与理由
rdispatch clean --expired              # 只回收过期且安全的 worktree
```

## 合票后的缓存回收

merger实际接受、合入后，在当前授权内执行；本工具不会赋予删除旧目录、工作树或备份的授权。只操作本任务`RD_USER_ID`／`RD_JOB`／`RD_WT`，可以选择其`private/worktrees/`内的票据工作树。

使用随新job提供的`rd_cache.py`，先计划，再按同一组明确路径加`--apply`。以下路径是相对票据工作树的示例：

```bash
python3 "$RD_JOB_DIR/rd_cache.py" \
  --worktree "$RD_WT" --merged-into <已接受的integration引用或提交> \
  --closed-receipt <本轮真实关闭回执JSON> \
  --cache private/DerivedData/Build/Intermediates.noindex \
  --retain private/DerivedData/Build/Products \
  --retain private/evidence \
  --output "$RD_WT/private/cache-retention.json"
# 核对计划和当前授权后，使用相同参数加 --apply。
```

关闭回执要求：`head`等于待清工作树HEAD，或仅落后于后继Markdown收口提交；`all_owned_resources_closed=true`、`source_freeze_ended=true`，且`observed_current=[]`或`current_matching_births=[]`；有`job`时也必须相同。工作树干净，HEAD必须是`--merged-into`的祖先。integration在另一台机器时，先把已接受提交同步为目标库可验证的引用，不伪造合入关系。

只接受已声明的DerivedData中间缓存、Rust target的incremental和node_modules/.cache。源码、Products、最终FFI、原日志／截图等证据、依赖、备份和worktree本身都不属于清理目标；用`--retain`列出当前产物与证据，工具会核前后字节／权限。未知或仍有打开文件的目录保留；缺少lsof或安全目录删除支持时只能保留计划，不能推断已闲置。

工具拒绝跨员工、越界／符号链接、跟踪文件、未合入来源、未关闭过程、重叠路径；每次删除都更新0600回执，失败也保存已删清单。输出回执位于待删缓存之外。没有可用关闭证据时标未清理，继续产品交付，不为回收缓存重跑测试。

POSIX文件与Git夹具可直接验证；Windows不能凭本地夹具声称实际清理通过。此入口不改共享锁、代理、系统配置，也不取代`rdispatch clean`删除worktree所需的现有确认流程。
