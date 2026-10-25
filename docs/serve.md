# 本机 Unix socket 服务

```sh
rdispatch serve
rdispatch serve --socket /private/tmp/rdispatch.sock --poll-interval 15
# 使用自定义路径时，CLI 与 App 连接同一个路径：
RD_SERVE_SOCKET=/private/tmp/rdispatch.sock rdispatch ack JOB --round 1 --receipt-id ID
curl --unix-socket /private/tmp/rdispatch.sock http://localhost/v1/jobs
curl -N --unix-socket /private/tmp/rdispatch.sock -H 'Last-Event-ID: 12' http://localhost/v1/events
```

默认路径是 `$RD_SERVE_SOCKET`，否则 `$RD_STATE/serve.sock`。
服务只绑定 AF_UNIX，不监听 TCP。socket 权限 0600，新建目录权限 0700。
每个 RD_STATE 仅一个服务，独占 `locks/serve/service.lock`，不与名为 serve 的 job 锁混用。
拒绝覆盖普通文件、符号链接或仍在监听的 socket；取得服务锁后可回收拒绝连接的旧 socket。
SIGINT/SIGTERM 会停止采集、等待已接受的写命令和采集结束，再释放服务锁并删除本次 socket。
服务读取启动时的配置和用户；修改配置后由编排方重启服务。

只读接口、字段和 SSE 帧以 [contracts/v1](../contracts/v1/README.md) 为准。
serve 可查看当前用户所有发起会话的详情、日志、回执；其他用户的 job 返回 404。
纯查询使用 `Query`、已有项目推断、签收和验收叠加，不写状态、不探测目标机、不收取回执。
日志端点按请求只读目标机日志。后台启动即采集，之后默认每 15 秒采集，可用
`--poll-interval` 设置正的有限秒数；周期是每轮采集完成后的等待时间。
采集先在共享 job 锁内重读记录、查询状态和进度并收取回执；竞争时跳过本轮。
目标机使用现有探针和资源阈值。采集变化通过 `EventLog.observe_job/observe_host`
与 `collect_result` 发布；显示字段不变不追加事件。单个目标机失败不会中断其他采集。
查询在首次采集前可能返回启动中／无回执，之后读取缓存观察和持久回执。
SSE 仅读取事件日志，补读包括轮转段；15 秒发一次注释心跳，断线后用最后收到的 ID 重连。

## CLI 转交协议

本地 CLI 的 `run/resume/ack/review/stop/project`，以及同样会写 RD_STATE 的
`inbox/keep/clean`，先尝试连接同一 socket。`project rebuild` 也走转交。
没有 socket 或连接被拒绝时才直接执行现有路径。
连接成功后发生请求、读取或解析错误，CLI 返回 4 和 `cli_forward_failed`，
**不自动重试，也不改走直接写入**；应先核对状态，避免重复派单或续跑。
这条私有通道没有跨进程崩溃幂等保证；App 的公开写接口与幂等键留给票 20。

请求是 `POST /_cli/v1`，Content-Type 为 application/json，必须有 Content-Length，
最多 8 MiB，不支持分块请求。字段：

| 字段 | 含义 |
| --- | --- |
| `argv` | 原始 CLI 参数数组，不经 shell 解释 |
| `cwd` | 调用方的绝对工作目录，保留 `-C`、相对提示词与 include 路径语义 |
| `config` / `user_id` | 必须匹配服务的配置路径和用户，否则拒绝，不转为直接写入 |
| `initiator_session` | 调用方已按原有优先级解析的会话 |
| `origin` | 调用方识别的 harness、控制端机器、Herdr 来源；派单时间由实际写入时补齐 |
| `stdin` | `--prompt-file -` 的正文；其他命令为空字符串 |

serve 串行执行接受的写命令，使用自己的子进程隔离各命令的 cwd、stdin、stdout、stderr
和来源上下文；不修改服务进程的全局环境或输出流。子进程仍调用原 CLI 命令实现，
签收／验收／停止渠道强制为 `serve`，签收方等显式参数仍保留。
子进程不再转交。响应 HTTP 200，JSON 为 `exit_code/stdout/stderr`，CLI 复现输出与退出码；
HTTP 400 表示请求无效或身份／配置不匹配，503 表示执行通道不可用。
所有 job 写操作和后台采集仍使用原来的 `JobLock`；项目写使用原索引锁，事件锁始终最内层。

## 检查与编排方复测

```sh
PYTHONPATH=src python3 -m unittest tests.test_rd_serve -v
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

测试使用临时 RD_STATE、Unix socket、真实隔离本机 shell／Git 夹具和资源探针替身，
覆盖 HTTP、用户隔离、纯查询、SSE 新事件与轮转补读、CLI 转交／直接写入、共享锁、
连接中断不重试、进程退出清理、敏感字段与样例结构。无构建步骤。
编排方按现有流程更新 Python 包后，在临时 RD_STATE 中启动服务，用上述 curl 读取，
运行签收／停止／验收并断开重连 SSE，再终止服务核对 socket 删除。
真实 SSH 目标机的探测、报告收取与日志读取，以及 App 消费样例的集成由编排方端到端复测；
实现会话不部署测试栈或重启现有服务。
