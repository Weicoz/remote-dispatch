# 代码结构

`cli.py` 保留常量、配置（`Config`）、目标机连接原语（`ssh`、`probe`、`assess`、`pick_hosts`）、任务记录与任务锁（`save_job`、`load_job`、`wait_job_lock`）。命令实现按职责拆在下面的模块里，`cli` 末尾用 `from .<模块> import *` 重新导出它们的全部名字，所以外部的 `cli.<名字>` 调用保持兼容。

| 模块 | 职责 |
| --- | --- |
| `rd_cli_parser` | `main`、`legacy_main`：参数解析与分发 |
| `rd_cmd_run` | `run`：范围解析、未发送改动检查、建树、`cmd_run` |
| `rd_launch` | 运行脚本生成、启动文件打包上传、缓存目录环境、员工配置预检 |
| `rd_fingerprint` | 派单前环境指纹核对 |
| `rd_artifact_transfer` | 非 Git 产物取回的传输（路径校验在 `rd_artifacts`） |
| `rd_cmd_line` | 工作线、待派登记、门禁、建议、会话默认值 |
| `rd_status` | `remote_state`、`job_states`、status／wait／watch／logs／timings |
| `rd_cmd_results` | 回执收取与持久化、inbox、看板、签收与验收 |
| `rd_cmd_resume` | resume、takeover、fetch、stop |
| `rd_cmd_clean` | 统一回收判定、clean／keep／slim |
| `rd_cmd_admin` | 目标机配置、员工配置包、体检、serve／service／device |

## 引用约定

拆出去的模块用 `from . import cli`，凡是引用 `cli` 里定义的名字（函数、常量、`STATE`、`CONFIG`、`ssh`、`save_job` 等）一律写成 `cli.<名字>`，包括拆出去的模块之间互相调用。测试和调用方对 `cli.<名字>` 的补丁因此对所有模块一致生效；直接写裸名字会让补丁静默失效，测试仍通过却不再测到真实路径。新增命令时照此写。

这些模块要在 `cli` 之后导入（`cli` 末尾会导入它们）；单独先导入其中一个模块会得到缺少名字的半成品 `cli`。
