# 项目索引

控制端在 `RD_STATE/projects/<用户ID>.json` 保存项目历史，用户 ID 复用默认用户逻辑。
`run` 登记新派单时更新索引；续跑不增加累计派单数。已登记但准备失败的派单也计数。
`clean` 不删除索引、隐藏状态或控制端的 `results/` 历史。

```sh
rdispatch status --json                  # 每条派单包含 project、origin、project_id
rdispatch project hide <project_id>
rdispatch project show <project_id>
rdispatch project rebuild                # 输出重建后的完整项目列表，含隐藏项目
```

三个项目子命令成功返回 JSON，退出码为 0。未知项目或索引正被其他写入占用时，
退出码为 4；写入冲突时重试命令。`hide` / `show` 使用不透明 ID，避免同名仓库歧义。
新派单自动恢复已隐藏的项目。

索引格式为 `{"schema": 1, "projects": {"<ID>": {…}}}`。每项字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 不透明项目 ID |
| `name` / `origin` | 项目名与派单时本地 origin URL；没有 origin 时为空字符串 |
| `first_dispatch_at` / `last_dispatch_at` | 首次／最近派单时间，沿用 job 的 `created` 格式 |
| `dispatch_count` | 累计派单数，按 job ID 去重，不按轮次数 |
| `hidden` | 是否隐藏 |

ID 算法：将 `[项目名, origin]` 以 UTF-8 编码的紧凑 JSON 序列化
（`ensure_ascii=False, separators=(",", ":")`），取 SHA-256 的前 20 位十六进制字符
（80 位）。不使用仓库路径，因此同一 origin 的多个 worktree 归同一项。
不同 origin 原文视为不同身份，不归一化 SSH／HTTPS 等 URL 拼写。

新增派单还在 `results/<job>/dispatch.json` 持久化精简登记信息：
`job / user_id / project / origin / created`。它不含提示词或凭据；
即使尚未生成回执或 `clean --discard-reports`，也能重建历史。
`rebuild` 合并 `jobs/*.json`、`results/*/dispatch.json`、`results/*/rN.json`，
job 记录优先于登记文件，登记文件优先于回执；签收／验收文件不参与计数。
重建保留仍可读取索引中的隐藏标记。索引内容损坏时可重建，但无法恢复损坏的隐藏标记。

旧记录补项目只发生在查询和重建内存中，不回写 job 或旧回执：
先取 `project`，否则去掉当前用户的 `<用户ID>-` 前缀，在配置、索引及已知派单
项目名中做最长匹配，要求名称后是 `-`；仍未命中归为“未归属”。
重建优先使用 `created`；缺失时从 job ID 的时间后缀恢复；均不可得时返回 `null`，
不把回执收取时间冒充派单时间。旧记录缺少 origin 时使用空字符串，不猜测仓库地址。
缺少 `user_id` 的旧记录按兼容规则纳入当前用户；历史信息不足时无法可靠确定原用户。
索引、派单和回执均已删除的旧历史无法恢复。

项目写入使用 `locks/projects/<用户ID>.lock`，与 `locks/<job>.lock` 独立。
锁文件不删除或替换；采用现有操作系统文件锁后端，竞争时非阻塞返回。
初始 job 登记、精简登记和增量索引更新在同一项目锁内执行；写入中断后可重建。

纯查询 `Query.projects(user_id)` 返回项目列表（包括隐藏项），不创建目录、不取写锁、
不写缓存。`Query.jobs(..., user_id=...)` 及 `details(..., user_id=...)` 返回推断的
`project` 和 `project_id`；调用端负责用户筛选。无 `user_id` 的旧记录需要调用端传入
当前用户。`CLI queries(cfg)` 提供配置中的已知项目名。

本票没有服务或部署目录改动。编排方合入后按现有方式安装 Python 包，
在隔离的 `RD_STATE` 下执行 run → hide → run → clean → rebuild 复测即可；
无需构建、监听端口、重启服务或真实设备。serve API 由票 22 接入。
