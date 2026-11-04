"""命令行入口：参数解析与分发。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import argparse
import configparser
import os
import sys
from . import __version__
from . import rd_agents as agents
from . import cli

__all__ = [
    "legacy_main",
    "main",
]


def main(argv=None, *, forwarded=None):
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        if hasattr(sys.stdin, "reconfigure"):
            try:
                sys.stdin.reconfigure(encoding="utf-8")
            except Exception:
                pass
    ap = argparse.ArgumentParser(prog="rdispatch", description=cli.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-C", "--dir", dest="top_dir", help="本地仓库目录（默认为当前工作目录）")
    ap.add_argument("--version", action="version", version=f"rdispatch {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    repo_parser = argparse.ArgumentParser(add_help=False)
    repo_parser.add_argument("-C", "--dir", dest="dir", help="本地仓库目录（默认为当前工作目录）")

    sub.add_parser("hosts", help="probe every configured machine").set_defaults(fn=cli.cmd_hosts)

    p = sub.add_parser("device", help="列出或吊销已配对设备（需要 serve）")
    actions = p.add_subparsers(dest="action", required=True)
    command = actions.add_parser("list", help="列出已配对设备及其配对时间、最近在线时间")
    command.add_argument("--json", action="store_true", help="输出 serve 返回的 JSON")
    command.set_defaults(fn=cli.cmd_device)
    command = actions.add_parser("revoke", help="吊销一台设备，立即切断它的访问")
    command.add_argument("device_id", help="设备 ID，来自 rdispatch device list")
    command.add_argument("--json", action="store_true", help="输出 serve 返回的 JSON")
    command.set_defaults(fn=cli.cmd_device)

    p = sub.add_parser("pair", help="打开或关闭配对窗口（需要 serve）")
    actions = p.add_subparsers(dest="action", required=True)
    command = actions.add_parser("open", help="打开配对窗口，返回配对码与过期时间")
    command.add_argument("--json", action="store_true", help="输出 serve 返回的 JSON")
    command.set_defaults(fn=cli.cmd_pair)
    command = actions.add_parser("close", help="立即关闭配对窗口，不影响已配对设备")
    command.add_argument("--json", action="store_true", help="输出 serve 返回的 JSON")
    command.set_defaults(fn=cli.cmd_pair)

    p = sub.add_parser("session-access", help="查看或开关会话访问总开关")
    p.add_argument("action", nargs="?", choices=("show", "on", "off"), default="show")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cli.cmd_session_access)

    p = sub.add_parser("host", help="查看或增删改目标机配置；hosts 负责探测状态")
    actions = p.add_subparsers(dest="action", required=True)
    command = actions.add_parser("list", help="列出配置中的目标机，含禁用项")
    command.add_argument("--json", action="store_true")
    command.set_defaults(fn=cli.cmd_host)
    for action in ("add", "update"):
        command = actions.add_parser(action, help=("新增" if action == "add" else "修改") + "目标机")
        command.add_argument("name")
        command.add_argument("--ssh", help="user@address，或 local 表示本机")
        command.add_argument("--workdir", help="目标机上的工作根目录")
        command.add_argument("--sync", choices=("origin", "direct"))
        command.add_argument("--platform", choices=("posix", "macos", "linux", "windows"))
        command.add_argument("--enabled", help="1/0、true/false 或 yes/no")
        command.add_argument("--launcher", choices=("nohup", "tmux"))
        command.add_argument("--tags", help="逗号分隔的标签")
        command.add_argument("--set", dest="set_fields", action="append", metavar="FIELD=VALUE",
                             help="设置任意配置字段，可重复")
        if action == "update":
            command.add_argument("--unset", dest="unset_fields", action="append", metavar="FIELD",
                                 help="移除本段覆盖，恢复继承，可重复")
        command.add_argument("--json", action="store_true")
        command.set_defaults(fn=cli.cmd_host)
    command = actions.add_parser("delete", help="删除目标机；有运行中派单时拒绝")
    command.add_argument("name")
    command.add_argument("--json", action="store_true")
    command.set_defaults(fn=cli.cmd_host)

    p = sub.add_parser("serve", help="serve local HTTP and events over a Unix socket")
    p.add_argument("--socket", help="Unix socket path (default RD_SERVE_SOCKET or RD_STATE/serve.sock)")
    p.add_argument("--poll-interval", type=float, default=15, help="background poll interval in seconds; config reload checks every min(1, interval) seconds (default 15)")
    p.set_defaults(fn=cli.cmd_serve)

    p = sub.add_parser("service", help="把 serve 装成常驻 LaunchAgent（只支持 macOS）")
    actions = p.add_subparsers(dest="action", required=True)
    actions.add_parser("install", help="生成并加载 LaunchAgent plist；启动开机自启与崩溃拉起").set_defaults(fn=cli.cmd_service)
    actions.add_parser("restart", help="重新加载 plist，用于升级 rdispatch 后生效").set_defaults(fn=cli.cmd_service)
    command = actions.add_parser("status", help="显示加载状态、进程、socket 与 serve 版本")
    command.add_argument("--json", action="store_true", help="输出机器可读状态")
    command.set_defaults(fn=cli.cmd_service)
    actions.add_parser("uninstall", help="卸载并删除 plist；保留状态目录与日志").set_defaults(fn=cli.cmd_service)

    p = sub.add_parser("project", help="manage persistent project history")
    actions = p.add_subparsers(dest="action", required=True)
    for action in ("hide", "show"):
        command = actions.add_parser(action, help=f"{action} a project")
        command.add_argument("project_id", help="opaque project ID from status --json")
        command.set_defaults(fn=cli.cmd_project)
    actions.add_parser("rebuild", help="rebuild projects from jobs and receipts; keep hidden flags").set_defaults(fn=cli.cmd_project)

    p = sub.add_parser("profile", help="检查或确认同步用户 Codex 配置（不自动同步）")
    p.add_argument("action", choices=["check", "sync"])
    p.add_argument("host")
    p.add_argument("--confirm-user-id", help="用户明确确认后的用户 ID，必须等于当前采用的用户 ID")
    p.add_argument("--confirm-version", help="用户审阅的本地完整配置指纹，来自 profile check")
    p.set_defaults(fn=cli.cmd_profile)

    p = sub.add_parser("doctor", parents=[repo_parser], help="check that a machine has what a job needs")
    p.add_argument("host")
    p.add_argument("--agent", action="append", help="require this agent (repeatable)")
    p.set_defaults(fn=cli.cmd_doctor)

    def add_run_options(p):
        """run 与 suggest 共用的参数。"""
        p.add_argument("--agent", "--harness", help=f"agent to run ({', '.join(agents.SPECS)}); [agent:NAME] in the config only overrides their templates")
        p.add_argument("--prompt-file", help="prompt for the agent ('-' reads stdin)")
        p.add_argument("--cmd", help="shell command to run instead of an agent")
        p.add_argument("--test", action="store_true", help="run the project's quick test command (falls back to test_full / test)")
        p.add_argument("--test-full", action="store_true", help="run the project's full test command (queued as heavy)")
        p.add_argument("--heavy", action="store_true", help="hold a heavy slot while the command runs")
        p.add_argument("--fresh", action="store_true", help="命令任务不用热工作树，新建冷 worktree（默认复用目标机上的常驻槽位）")
        p.add_argument("--host", help="目标机，或 auto；默认按会话派单默认值、[defaults] host、auto 取值")
        p.add_argument("--force-host", action="store_true", help="skip the load check for --host")
        p.add_argument("--tag", action="append", default=[], help="only machines with this tag (repeatable)")
        p.add_argument("--ref", default="HEAD", help="commit to send (default HEAD)")
        p.add_argument("--wip", action="store_true", help="apply tracked working-tree differences from --ref to the remote index; fail if empty")
        p.add_argument("--allow-dirty", action="store_true", help="测试类派单也允许本地有未发送的改动（远端测的是已提交的 --ref）")
        p.add_argument("--branch", help="branch the job works on (default rd/<RD_USER_ID>/<name>-<time>)")
        p.add_argument("--name", help="short label used in the job id")
        p.add_argument("--project", help="project name (default: name of the origin repository)")
        p.add_argument("--workdir", help="临时覆盖目标机的工作根目录（默认读取 host 配置的 workdir）")
        p.add_argument("--include", action="append", default=[], help="extra path inside repository to copy, relative to repository root (repeatable)")
        p.add_argument("--artifact", action="append", default=[], metavar="PATH",
                       help="任务结束后用 rsync 拉回的仓库内相对路径（文件或目录，可重复）；不得是绝对路径或含 ..")
        p.add_argument("--skip-fingerprint", action="store_true", help="跳过 [project] fingerprint 环境指纹核对（打印警告）")
        p.add_argument("--context-from", action="append", default=[], metavar="JOB",
                       help="把前序派单的上下文包放在提示词前面（可重复；须是本用户 ID 且已有回执）")
        p.add_argument("--model")
        p.add_argument("--effort")
        p.add_argument("--permission-mode", choices=list(agents.AGY_PERMISSIONS),
                       help="AGY 必填：plan/accept-edits/auto/skip；无人值守只有 skip 能执行命令和 git 提交")
        p.add_argument("--timeout", type=int, help="seconds before the job is killed")
        p.add_argument("--initiator-session", help="发起方会话ID；默认 RD_INITIATOR_SESSION，再读取父进程链识别的 harness 会话变量")
        p.add_argument("--dispatch-mode", help="派单模式：suggest、line、ticket；默认读会话默认值，再读 [defaults] dispatch_mode")
        p.add_argument("--plan-item", help="把本次派单关联到待派登记（rdispatch plan）里的工作项")
        p.add_argument("--after", action="append", default=[], help="本工作项依赖的前序派单 ID（可重复）；需要 --plan-item")

    p = sub.add_parser("run", parents=[repo_parser], help="dispatch an agent session or a command")
    add_run_options(p)
    p.add_argument("--line", help="接入工作线（线名）；需要 --accept 令牌，或该线已有常驻同意")
    p.add_argument("--accept", metavar="令牌", help="接受 rdispatch suggest 给出的建议令牌（需要 --line）")
    p.add_argument("--accept-chain", action="store_true", help="接受时同时设为「本链后续工作项都接受」（常驻同意，每次仍校验严格接线条件）")
    p.add_argument("--no-line", action="store_true", help="拒绝建议：按默认方式派出，并在待派登记里记「本链不再建议」")
    p.set_defaults(fn=cli.cmd_run)

    p = sub.add_parser("suggest", parents=[repo_parser], help="只读：判断本工作项能否接入工作线，给出建议与令牌（参数同 run）")
    add_run_options(p)
    p.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p.set_defaults(fn=cli.cmd_suggest)

    p = sub.add_parser("plan", parents=[repo_parser], help="待派登记：登记还没派出的工作项及其依赖")
    p.add_argument("--project", help="项目名；默认当前仓库的 origin 仓库名")
    actions = p.add_subparsers(dest="action", required=True)
    q = actions.add_parser("list", help="列出工作项及已派、待派、阻塞状态")
    q.add_argument("--json", action="store_true")
    for name, text in (("add", "登记工作项"), ("set", "修改工作项（只改传入的字段）")):
        q = actions.add_parser(name, help=text)
        q.add_argument("item")
        q.add_argument("--title")
        q.add_argument("--depends", action="append", default=None if name == "set" else [], help="依赖的工作项（可重复）；set 时传空串清空")
        q.add_argument("--after", action="append", default=None if name == "set" else [], help="依赖的前序派单 ID（可重复）；set 时传空串清空")
    for name, text in (("done", "标记完成"), ("cancel", "标记取消"), ("remove", "删除（不得被其他工作项依赖）")):
        actions.add_parser(name, help=text).add_argument("item")
    q = actions.add_parser("chain", help="查看或清除某条链（线）的建议设置：本链不再建议、拒绝次数、常驻同意")
    q.add_argument("line", help="线名")
    q.add_argument("--clear", action="store_true", help="清除「本链不再建议」和拒绝次数，恢复建议")
    q.add_argument("--revoke-accept", action="store_true", help="撤销「本链后续工作项都接受」的常驻同意")
    q.add_argument("--json", action="store_true")
    p.set_defaults(fn=cli.cmd_plan)

    p = sub.add_parser("session-default", help="读取、设置或清除会话派单默认值")
    p.add_argument("session", help="发起会话 ID；--pane 时为 Herdr 窗格 ID")
    p.add_argument("--pane", action="store_true", help="按 Herdr 窗格保存默认值")
    for field in ("host", "harness"):
        group = p.add_mutually_exclusive_group()
        group.add_argument("--" + field)
        group.add_argument("--clear-" + field, action="store_true")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--dispatch-mode", help="派单模式：suggest（默认）、line、ticket")
    group.add_argument("--clear-dispatch-mode", action="store_true")
    p.add_argument("--clear", action="store_true", help="清除目标机和 harness 默认值，改读配置")
    p.set_defaults(fn=cli.cmd_session_default)

    p = sub.add_parser("status", parents=[repo_parser], help="show jobs")
    p.add_argument("job", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--details", action="store_true", help="返回本轮进度/结果，要求原发起会话")
    p.add_argument("--initiator-session")
    p.add_argument("--unmerged", action="store_true", help="跨会话列出当前员工未合入的提交")
    p.add_argument("--into", help="合入目标 REF；默认各 job 的 source_branch")
    p.add_argument("--project", help="限定项目；未指定时 --unmerged 在当前仓库内筛选")
    p.set_defaults(fn=cli.cmd_status)

    p = sub.add_parser("gate", parents=[repo_parser], help="合入前门禁：对指定提交跑一次全量测试（按历史耗时自动选目标机）")
    p.add_argument("ref", help="被测提交")
    p.add_argument("--host", help="指定目标机，覆盖自动选择")
    p.add_argument("--project", help="project name (default: name of the origin repository)")
    p.add_argument("--timeout", type=int, help="seconds before the job is killed")
    p.set_defaults(fn=cli.cmd_gate)

    p = sub.add_parser("line", help="工作线收尾：prefix 建前缀分支，close 关闭线")
    p.add_argument("line_action", choices=("prefix", "close"))
    p.add_argument("job")
    p.add_argument("items", nargs="*", help="prefix：工作项，必须是线上顺序的前 k 个")
    p.add_argument("--branch", help="prefix：前缀分支名；默认 <线分支>-prefix-<k>")
    p.set_defaults(fn=cli.cmd_line)

    p = sub.add_parser("timings", help="按项目与目标机查询各轮次 setup 与主体耗时（读本地已保存回执）")
    p.add_argument("--project")
    p.add_argument("--host")
    p.add_argument("--kind", choices=["test", "command", "agent"])
    p.set_defaults(fn=cli.cmd_timings)

    p = sub.add_parser("wait", help="block until a job finishes (exit 0 ok, 1 failed, 2 still running)")
    p.add_argument("job")
    p.add_argument("--timeout", type=int)
    p.add_argument("--interval", type=int, default=30)
    p.set_defaults(fn=cli.cmd_wait)

    p = sub.add_parser("watch", help="initiator-side shared progress wake-up (0 completed, 1 alert, 2 progress due)")
    p.add_argument("jobs", nargs="+", help="explicit jobs monitored by this initiator")
    p.add_argument("--timeout", type=int, help="override this wait in seconds; otherwise use RD_REPORT_INTERVAL_SECONDS, [monitor] report_interval_seconds, or 900")
    p.add_argument("--keep-on-disconnect", action="store_true", help="短期不可达时保留本次巡检截止时间，继续只读观察")
    p.set_defaults(fn=cli.cmd_watch)

    p = sub.add_parser("logs", help="show the tail of a job's output")
    p.add_argument("job")
    p.add_argument("stream", nargs="?", default="out", choices=["out", "err", "setup", "last"])
    p.add_argument("-n", "--lines", default=40)
    p.add_argument("--round", type=int)
    p.add_argument("--file", help="any file in the job directory, e.g. push.log")
    p.set_defaults(fn=cli.cmd_logs)

    p = sub.add_parser("result", help="收取本轮结果；执行退出不等于验收通过")
    p.add_argument("job")
    p.add_argument("--round", type=int)
    p.add_argument("--initiator-session")
    p.add_argument("--summary", action="store_true", help="仅返回有界摘要及完整回执路径；保留原完整JSON接口")
    p.set_defaults(fn=cli.cmd_result)

    p = sub.add_parser("board", help="看板：派单数量、目标机、模型、时长、进度、待派与阻塞")
    p.add_argument("--all", action="store_true", help="该用户 ID 下全部派单，含没有发起会话的")
    p.add_argument("--json", action="store_true")
    p.add_argument("--initiator-session")
    p.set_defaults(fn=cli.cmd_board)

    p = sub.add_parser("inbox", help="列出当前发起会话待收取的结果")
    p.add_argument("--initiator-session")
    p.add_argument("--orphans", action="store_true", help="改列当前用户 ID 下没有发起会话的派单，供集中签收")
    p.add_argument("--json", action="store_true", help="与 --orphans 或 --wait 合用：输出 JSON（inbox 默认即 JSON）")
    p.add_argument("--wait", action="store_true", help="只读等待本会话新汇报：有新汇报退出 0，超时退出 2；不签收、不写回执")
    p.add_argument("--timeout", type=float, help="与 --wait 合用：最长等待秒数；省略则一直等")
    p.add_argument("--interval", type=float, default=5, help=argparse.SUPPRESS)
    p.set_defaults(fn=cli.cmd_inbox)

    p = sub.add_parser("ack", help="确认已收取特定回执，不自动验收或续派")
    p.add_argument("job")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--receipt-id", required=True)
    p.add_argument("--initiator-session")
    p.add_argument("--acked-by", help="签收方会话 ID 或设备名；默认当前会话，无会话时为 cli")
    p.add_argument("--via", choices=["cli", "serve"], default="cli", help="签收渠道（默认 cli）")
    p.set_defaults(fn=cli.cmd_ack)

    p = sub.add_parser("review", help="验收指定轮次；打回附言并立即续跑")
    p.add_argument("job")
    p.add_argument("--round", type=int, required=True)
    actions = p.add_mutually_exclusive_group(required=True)
    actions.add_argument("--accept", dest="review_action", action="store_const", const="accepted", help="通过")
    actions.add_argument("--reject", metavar="附言", help="打回并用附言续跑")
    actions.add_argument("--void", dest="review_action", action="store_const", const="voided", help="作废")
    p.add_argument("--initiator-session")
    p.add_argument("--reviewed-by", help="验收方会话 ID 或设备名；默认当前会话，无会话时为 cli")
    p.add_argument("--via", choices=["cli", "serve"], default="cli", help="签收渠道（默认 cli）")
    p.add_argument("--host", help="续跑目标机；默认沿用原目标机")
    p.add_argument("--agent", "--harness", help="必须与原派单 harness 一致；换 harness 请另派一单")
    p.add_argument("--session", help="接收会话 ID")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--timeout", type=int)
    p.set_defaults(fn=cli.cmd_review, force_host=False)

    p = sub.add_parser("resume", help="continue an agent job in the same session")
    p.add_argument("job")
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--host", help="续跑目标机；默认沿用原目标机")
    p.add_argument("--agent", "--harness", help="必须与原派单 harness 一致；换 harness 请另派一单")
    p.add_argument("--session", help="session id, if it cannot be found in the logs")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--permission-mode", choices=list(agents.AGY_PERMISSIONS),
                   help="改变原任务权限档位：plan/accept-edits/auto/skip；无人值守只有 skip 能执行命令和 git 提交")
    p.add_argument("--timeout", type=int)
    p.add_argument("--force-host", action="store_true")
    p.add_argument("--initiator-session")
    p.add_argument("--new-context", action="store_true", help="仅上下文故障时显式接替原会话，保留任务与配置")
    p.add_argument("--retry-after-repair", help="同类故障修复后续跑的证据说明；不绕过资源或身份检查")
    p.add_argument("--refresh-auth", action="store_true",
                   help="本轮重新复制目标机认证文件覆盖任务内副本；默认续跑沿用首轮凭据")
    p.set_defaults(fn=cli.cmd_resume)

    p = sub.add_parser("takeover", help="同一用户 ID 的其他发起会话接手工作线，写入交接记录")
    p.add_argument("job")
    p.add_argument("--initiator-session")
    p.set_defaults(fn=cli.cmd_takeover)

    p = sub.add_parser("fetch", help="bring the job's commits back as rd/<branch>")
    p.add_argument("job")
    p.add_argument("--artifacts", action="store_true", help="只重新拉取 run --artifact 声明的非 Git 产物")
    p.set_defaults(fn=cli.cmd_fetch)

    p = sub.add_parser("stop", help="kill a running job")
    p.add_argument("job")
    p.add_argument("--initiator-session")
    p.add_argument("--stopped-by", help="停止者会话 ID 或设备名；默认当前会话，无会话时为 cli")
    p.add_argument("--via", choices=["cli", "serve"], default="cli", help="停止及自动签收渠道（默认 cli）")
    p.set_defaults(fn=cli.cmd_stop)

    p = sub.add_parser("keep", help="钉住任务 worktree，rdispatch clean --expired 不回收；--hours 0 取消")
    p.add_argument("job")
    p.add_argument("--hours", type=float, required=True, help="从现在起保留的小时数；0 取消钉住")
    p.set_defaults(fn=cli.cmd_keep)

    p = sub.add_parser("clean", parents=[repo_parser], help="remove a finished job's worktree and logs from the worker")
    p.add_argument("job", nargs="?")
    p.add_argument("--slim", action="store_true", help="只删项目声明的可再生子路径（regenerable_paths）和 setup 完成标记，保留 worktree 与代码；省略 job 时处理当前范围内已结束的 job，可配 --dry-run / --json / --project")
    p.add_argument("--merged", action="store_true", help="按合入、回执及未提交路径条件批量清理当前员工已结束的 job")
    p.add_argument("--expired", action="store_true", help="同 --merged 的安全条件，再按续跑可能性分级保留：命令任务确认即回收、已合入 24 小时、未合入 7 天；见 [retention]")
    p.add_argument("--into", help="合入目标 REF；默认各 job 的 source_branch")
    p.add_argument("--dry-run", action="store_true", help="只列出 --merged / --expired 计划，不删除")
    p.add_argument("--json", action="store_true", help="与 --dry-run 合用：输出每个派单的 job、reclaimable、code、reason")
    p.add_argument("--project", help="限定项目；未指定时 --merged / --expired 取当前仓库的项目 ID")
    p.add_argument("--delete-branch", action="store_true", help="also delete the job branch (worker and origin)")
    p.add_argument("--force", action="store_true", help="discard uncommitted or unfetched work")
    p.add_argument("--discard-reports", action="store_true", help="skip local reports backup before deleting the remote job")
    p.add_argument("--initiator-session")
    p.set_defaults(fn=cli.cmd_clean)

    a = ap.parse_args(argv)
    try:
        from . import rd_serve
        if forwarded is not None:
            if a.command not in rd_serve.WRITE_COMMANDS and a.command not in rd_serve.FORWARD_ONLY:
                cli.die("invalid_cli_command: command cannot be forwarded", 4)
            a._forwarded_origin = forwarded["origin"]
            if hasattr(a, "initiator_session"):
                a.initiator_session = forwarded.get("initiator_session")
            if hasattr(a, "via"):
                a.via = "serve"
        elif a.command in rd_serve.CACHED_READS and (code := rd_serve.cached_read(a)) is not None:
            raise SystemExit(code)
        elif a.command in rd_serve.WRITE_COMMANDS and not getattr(a, "wait", False):
            code = rd_serve.forward(a, list(sys.argv[1:] if argv is None else argv))
            if code is not None:
                raise SystemExit(code)
        # service/device/pair 不读配置：配置写坏时仍要能查看服务状态和修复设置。
        a.fn(a, None if a.command in ("service", "device", "pair") else cli.Config())
    except configparser.Error as error:
        line = getattr(error, "lineno", None)
        where = f"第 {line} 行" if line else type(error).__name__
        cli.die(f"config_invalid: {cli.CONFIG} 格式错误（{where}）；修好后重试。", 4)
    except ValueError as error:
        cli.die(str(error), 4)


def legacy_main():
    """旧入口 rd：与 oh-my-zsh、PowerShell、CMD 的 rd 别名冲突，过渡一个版本后移除。"""
    print("rdispatch: 命令已从 rd 改名为 rdispatch；rd 入口将在下个版本移除。", file=sys.stderr)
    cli.main()
