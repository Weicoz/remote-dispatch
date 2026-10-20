# Employee Profiles

## 同步确认

每次首次登记或主动同步，代理先确认员工自选 ID；已有 `RD_USER_ID` 时展示该值，没有时询问。再准备选定配置包，运行 `rd profile check <机器>`，展示机器、员工目录、文件清单、变化、版本指纹和源更新时间，询问是否同步。收到明确答复后才执行：

```bash
rd profile sync mini --confirm-user-id zhangsan --confirm-version <profile-check-输出的完整指纹>
```

两个确认参数不是员工日常固定配置：它们记录本次同步已确认的 ID 和版本。清单变化后必须重新检查和确认。ID 缺失、确认 ID 错误、确认版本过期、远端同步竞争或本地比远端旧时停止同步。提醒不会自动更新，拒绝更新时使用旧配置；首次没有远端配置则不派任务。

## 配置包

来源是派单端员工选定的配置。`RD_PROFILE_DIR` 可指定包目录；默认 `~/.config/remote-dispatch/profiles/<ID>/`。不是整个 `~/.codex`，不是目标机机器主人的配置。机器之间共用 ID，但不自动上传新电脑的环境。

```text
<本地配置包>/
├── config.toml         必需：模型、接口、权限、MCP、代理定义
├── AGENTS.md           必需：公司规范＋员工允许的差异
├── git.json            可选：{"name":"员工名","email":"员工邮箱"}
├── skills/<name>/      可选：SKILL.md 及配套资源
├── agents/             可选：config_file 指向的相对路径
├── rules/              可选：Codex 执行规则
└── tools/              可选：配置引用的配套脚本
```

使用 `assets/profile.example` 起步，将审阅后的个人差异合入。复制明确选定的 skills 实体文件；包内软链接被拒绝，避免指向派单端个人路径。脚本拒绝常见凭据文件、隐藏文件、个人运行状态和未支持的配置字段。支持字段以 `src/remote_dispatch/rd_profiles.py` 的 `CONFIG_KEYS` 为准。人工还必须检查技能正文和资源，脚本不能证明任意资源不含敏感内容。

检查目标机工具和 MCP 服务是否可用；派单端的 `localhost` 不代表目标机上的同一个服务。服务地址、环境变量和技能内部脚本须按目标平台准备，不自动猜测或改写业务接口。

当前配置包不传 hooks、plugins、登录文件、个人历史或记忆。员工 skills 的清单附到独立 `AGENTS.md`，代理按需读取各自的 `SKILL.md`；不依赖共享账号的个人 skill 目录。员工之间的配置更新不改全局文件。

## 目标机路径

`workdir` 是机器级根目录，脚本追加员工命名空间：

```text
<workdir>/
├── locks/                              整机重命令排队
└── users/<RD_USER_ID>/
    ├── profiles/current.json           当前配置指针
    ├── profiles/<内容指纹>/             保留的配置版本
    ├── repos/<仓库名>-<仓库标识>.git    员工裸仓库
    ├── wt/<job>/                       任务 worktree
    ├── jobs/<job>/                     提示词、日志、退出码
    │   └── codex/                      此任务的 CODEX_HOME、会话和配置
    └── bin/rd-heavy                    使用整机锁的排队入口
```

配置中的 `/Users/员工/...`、`~/...` 或 Windows 绝对路径被拒绝。代理 `config_file` 写包内相对路径，例如 `agents/reviewer.toml`。需要绝对资源路径时，写 `@RD_PROFILE@/tools/tool.py`；启动时替换为此任务的目标机配置目录。AGENTS 和 skill 正文也必须使用资源相对路径，不保留个人机器绝对路径。

共享账号的个人 skills 以明确 `SKILL.md` 路径禁用；hooks、plugins、memories 和外部记忆导入由启动参数关闭。项目自己的规则和 skills 仍属于项目。配置路径与会话隔离不等于共享系统账号的访问权限隔离。

Windows 的目录由目标机 Python `Path` 处理，不沿用 Mac/Linux 的 HOME 或分隔符。配置文件、代理定义和日志显式使用 UTF-8；环境保留 Windows 必需的 `SYSTEMROOT`、`PATHEXT` 等基础变量。ID 与资源文件名拒绝 Windows 保留名及非法字符，避免跨平台创建目录失败。平台差异见 [Platforms](platforms.md)。

## 更新判断与续跑

指纹包含文件内容、相对路径和可执行权限，能识别新增、修改和删除。配置内容相同则不提醒。内容不同且本地配置源更新时间领先远端超过 604800 秒时提醒；恰好七天不提醒。与当前日期、文件解压时间或同步时间无关。

首次检查取包内文件的最新修改时间；后续指纹变化（包括删除）记录检测时间，未变化就沿用时间。跨机器复制可能改变首次时间，因此指纹优先用于确认内容是否相同，时间差是更新提示，不是身份或版本顺序的证明。

同步保存新版本并原子切换当前指针，不删除旧配置。任务首次运行时复制选定版本到自己的 `CODEX_HOME`；续跑不读新指针、不重写配置。更改机器清单的工作根目录也不会改变已记录任务的目标路径。

## 认证与兼容

本机和目标机配置操作要求 Python 3.11+。后台配置明确设置 `approval_policy = "never"`，权限按配置包的 `sandbox_mode` 执行。没有身份认证时，ID 只负责配置归属；不要将 ID 称为 token。

Codex 还必须支持命名 permission profiles。默认 `workspace-write` 将 worktree 指向的 Git 元数据保护为只读，直接提交会失败。运行脚本将这种模式编译为继承 `:workspace` 的任务权限，开放当前员工 `repos/` 内的 Git 公共目录、本任务 Git 目录及机器级 `locks/` 写入，供提交与 `rd-heavy` 排队使用；工作区其余文件保护保留。任务网络统一开启，用于依赖下载及连接测试，源配置的 `sandbox_workspace_write.network_access = false` 不再生成禁网权限。用户当次明确要求离线或平台强制限制仍按该限制处理。模型、规则和源配置版本不变；`read-only` 与 `danger-full-access` 不自动转换。任务目录不在员工命名空间或 Git 元数据越出当前员工 `repos/` 时停止启动。既有任务权限调整需有对应授权和恢复副本，不自动覆盖续跑快照。

认证只从目标机提供：主机 `codex_auth_file` 指向已授权的认证文件，或 `env_file` 导出配置引用的 API 环境变量。认证文件只复制到当前任务配置，权限 0600；不进入同步包、Git 或日志。不自动读共享账号默认 `auth.json` 或钥匙串。首次登录、验证码和扫码由用户接管。目标机环境只向代理传递基础变量、RD 变量及配置明确引用的 API/MCP 变量。

当前只完成 Codex 员工配置适配，其他代理阻止派单。旧版任务保留原路径，可查看、取回、停止和清理；旧任务无员工配置快照，不能直接续跑。`rd status` 默认展示当前 ID 的任务及旧版任务，指定新任务 ID 时校验归属。ID 是操作防串用检查，不是安全授权边界。

权限依据：[OpenAI permission profiles](https://learn.chatgpt.com/docs/permissions) 和 [worktree Git 路径保护](https://learn.chatgpt.com/docs/agent-approvals-security#protected-paths-in-writable-roots)。
