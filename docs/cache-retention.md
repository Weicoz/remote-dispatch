# 合票后的缓存保留与回收

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

POSIX文件与Git夹具可直接验证；Windows不能凭本地夹具声称实际清理通过。此入口不改共享锁、代理、系统配置，也不取代`rd clean`删除worktree所需的现有确认流程。
