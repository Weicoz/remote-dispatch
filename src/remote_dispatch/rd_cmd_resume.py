"""续跑、迁机、取回与停止：resume、takeover、fetch、stop。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import contextlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from . import rd_native as native
from . import rd_messages as messages
from . import rd_artifacts as artifacts
from . import rd_agents as agents
from . import rd_query as query
from . import rd_review as reviews
from . import rd_events as events
from . import rd_line as workline
from . import cli

__all__ = [
    "SessionLookupError",
    "check_recovery",
    "cmd_fetch",
    "cmd_resume",
    "cmd_stop",
    "cmd_takeover",
    "fetch_job_branch",
    "find_session",
    "handover_pack",
    "migrate_job",
    "record_line_residue",
    "resume_job",
    "rollback_launch",
]


class SessionLookupError(ValueError):
    """读取目标机会话日志超时或失败；调用方下一轮重试，不把它当作「没有会话」。"""


def find_session(host, rec, agent):
    if host["platform"] == "windows":
        try:
            output = native.request(host, "sessions", {"job": rec["job"], "round": rec.get("rounds", 1)},
                                    timeout=cli.REMOTE_READ_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise cli.SessionLookupError("会话查找超时") from None
        matched = re.search(agent["session_regex"], output)
        return matched.group(1) if matched else None
    number = messages.round_id(rec.get("rounds", 1))
    r = cli.ssh(host, f'head -c 200000 "$ROOT/jobs/{rec["job"]}/stdout.r{number}.log" 2>/dev/null', check=False,
            timeout=cli.REMOTE_READ_TIMEOUT)
    if r is None:
        raise cli.SessionLookupError("会话查找超时")
    m = re.search(agent["session_regex"], r.stdout or "")
    return m.group(1) if m else None


def check_recovery(rec, receipt, args, cfg):
    failure = ((receipt or {}).get("report") or {}).get("runtime_failure")
    new_context = getattr(args, "new_context", False)
    repair = getattr(args, "retry_after_repair", None)
    if repair is not None:
        messages.identifier(repair, "恢复证据说明")
    if new_context:
        if not failure or failure.get("kind") != "context":
            cli.die("resume_recovery_not_allowed: 仅已识别的上下文故障可以 --new-context；路由、认证或网络故障须先修复。", 4)
        history = [h for h in rec.get("context_history", []) if h.get("reason") != "policy"]
        if (history and history[-1].get("from_round") == rec["rounds"] - 1
                and failure.get("work_observed") is False and not repair):
            cli.die("resume_recovery_not_allowed: 新上下文也未开始工作；保存回执并先修复启动条件，不重复新建。", 4)
        return
    if not failure or failure.get("work_observed") is not False or repair:
        return
    limit = cfg.recovery_limit() if cfg is not None else 1
    count = 1
    for number in range(rec["rounds"] - 1, 0, -1):
        previous = messages.read_json(cli.result_path(rec, number))
        old = ((previous or {}).get("report") or {}).get("runtime_failure")
        if not old or old.get("kind") != failure.get("kind") or old.get("work_observed") is not False:
            break
        count += 1
        if count > limit:
            break
    if count > limit:
        cli.die("resume_recovery_not_allowed: 重复模型启动故障，已停止续跑；用 rdispatch result --summary 查看分类。取得修复证据后才用 --retry-after-repair，纯上下文故障可以显式 --new-context。", 4)


def record_line_residue(host, rec, item):
    """工作项开始前记录 worktree 里的未跟踪文件清单；只记录不删除，读取失败不阻止派单。"""
    try:
        paths = [path for code, path in cli.worktree_changes(host, rec)[1] if code == "??"]
    except (OSError, ValueError, SystemExit, subprocess.SubprocessError) as error:
        print(f"note: 无法记录工作项 {item} 开始前的未跟踪文件清单：{error}")
        return
    workline.record_residue(rec, item, paths)
    if paths:
        print(f"note: worktree 里有 {len(paths)} 个未跟踪文件，已记入回执与看板，未删除。")


def handover_pack(cfg, rec):
    """换机交接包：复用上下文包拼装；没有回执等取不到来源时返回空串，不阻止换机。"""
    try:
        return cli.context_pack(cfg, rec["user_id"], [rec["job"]]) + "\n"
    except (Exception, SystemExit):
        return ""


@cli.locked_job
def cmd_takeover(a, cfg):
    """同一用户 ID 的其他发起会话接手工作线，写入交接记录；跨用户 ID 与非工作线一律拒绝。"""
    rec = cli.load_job(a.job)
    session = cli.initiator_session(a)
    if not session:
        cli.die("接手需要 --initiator-session 或 RD_INITIATOR_SESSION。", 4)
    try:
        changed = workline.takeover(rec, cli.require_user(), session)
    except ValueError as error:
        cli.die(str(error), 4)
    if changed:
        cli.save_job(rec)
    print(f"{rec['job']}: " + (f"发起会话已由 {rec['line']['handovers'][-1]['from_session'] or '无'} 交接给 {session}。"
                               if changed else f"当前会话 {session} 已是发起会话。"))


@cli.locked_job
def cmd_resume(a, cfg):
    rec = cli.load_job(a.job)
    cli.require_initiator(rec, a)
    cli.resume_job(a, cfg, rec)


def resume_job(a, cfg, rec, *, prompt=None, before_launch=None, rollback_stack=None, line_item=None):
    if getattr(a, "agent", None) and a.agent != rec.get("agent"):
        cli.die("harness_change_not_allowed: 续跑不能换 harness；请另派一单。", 4)
    cli.require_user()
    if not rec.get("user_id") or not (rec.get("profile") or rec.get("isolated")):
        cli.die("resume_not_allowed: 旧任务未固定用户配置；查看、取回仍可用，续跑必须重新派单。", 4)
    if not rec.get("agent"):
        cli.die("resume_not_allowed: only agent jobs can be resumed; start a new job for commands")
    cfg.host(rec["host"])  # 续跑是新一轮工作：和派新单一样严格校验目标机配置
    host = cli.job_host(cfg, rec)
    host["probe_agent"] = rec["agent"]
    host["probe_profile_fingerprint"] = (rec.get("profile") or {}).get("fingerprint")
    st = cli.remote_state(host, [rec["job"]]).get(rec["job"], {})
    if st.get("state") in ("running", "starting"):
        cli.die("round_in_progress: round in progress; rdispatch stop it first or wait", 4)
    if st.get("state") == "unreachable":
        cli.die(f"resume_host_unavailable: job is unreachable on {host['name']}", 4)
    source_host = host
    moving = bool(getattr(a, "host", None) and a.host != rec["host"])
    if moving:
        host = cli.scoped_host(cfg.host(a.host), rec["user_id"])
        host["probe_agent"] = rec["agent"]
    info = cli.probe(host)
    why = cli.assess(host, info)
    print(cli.fmt_probe(host, info, why))
    if why and not a.force_host:
        reason = "resume_host_unavailable" if not info["reachable"] else "resume_resource_check_failed"
        cli.die(f"{reason}: {host['name']} cannot take the session now ({'; '.join(why)}).", 3)
    agent = rec.get("agent_config") or cfg.agent(rec["agent"])
    session = a.session or rec.get("receiver_session") or cli.find_session(source_host, rec, agent)
    if not session:
        cli.die("resume_session_not_found: could not find the agent's session id in the logs; pass --session ID")
    known = rec.get("receiver_session") or (cli.find_session(source_host, rec, agent) if rec.get("handoff_version") else None)
    if known and session != known:
        cli.die("resume_session_mismatch: 续派必须复用原接收会话；指定 --session 与原会话不匹配。", 4)
    receipt = cli.collect_result(cfg, rec, st) if rec.get("handoff_version") else None
    cli.check_recovery(rec, receipt, a, cfg)
    if prompt is None:
        prompt = sys.stdin.read() if a.prompt_file == "-" else Path(a.prompt_file).read_text(encoding="utf-8")
    round_prompt = prompt
    if getattr(a, "new_context", False):
        rec.setdefault("context_history", []).append(dict(receiver_session=session, from_round=rec["rounds"],
                                                          receipt_id=receipt["receipt_id"], reason="context", at=messages.utc_now()))
        session = None
        prompt = ("原接收会话因上下文故障退役；沿用当前工作树、模型、员工配置和原授权，仅完成本轮剩余项。"
                  "旧失败和回执保留，不重复已完成工作。\n\n" + prompt)
    if line_item and line_item.get("rotate"):
        rec.setdefault("context_history", []).append(dict(receiver_session=session, from_round=rec["rounds"],
                                                          receipt_id=receipt["receipt_id"], reason="policy", at=messages.utc_now()))
        session = None
        prompt = ("工作线按策略换新会话；沿用当前工作树、模型和授权，以下交接包为前序工作摘要。\n\n"
                  + (line_item["handoff"] + "\n" if line_item["handoff"] else "") + prompt)
    if getattr(a, "retry_after_repair", None):
        rec.setdefault("recovery_repairs", []).append(dict(from_round=rec["rounds"],
                                                           evidence=a.retry_after_repair, at=messages.utc_now()))
    if moving:
        print("note: 换机导出包含整个工作树；体积大时先 rdispatch clean JOB --slim 瘦身再换机。")
        target_profile = cli.agent_preflight(host, rec["user_id"], rec["agent"], agent, auto_sync=False)
        if target_profile != rec.get("profile"):
            cli.die("resume_profile_mismatch: 目标机用户配置与原派单不一致；先同步原配置。", 4)
        rec.setdefault("context_history", []).append(dict(receiver_session=session, from_round=rec["rounds"],
                                                          reason="host_changed", at=messages.utc_now()))
        session = None
        prompt = ("续跑已更换目标机并换新会话；沿用迁移工作树、原 harness 和授权，仅完成附言要求。"
                  "以下交接包由 rdispatch 从前序回执拼装。\n\n" + (cli.handover_pack(cfg, rec) if rec.get("handoff_version") else "")
                  + "\n" + prompt)
    # 续跑沿用原权限档位；显式 --permission-mode 才改变并记入任务。
    permission = getattr(a, "permission_mode", None) or rec.get("permission_mode")
    command = cli.agent_command(agent, a.model or rec.get("model"), a.effort or rec.get("effort"), session, permission)
    rec["permission_mode"] = permission
    rec["receiver_session"] = session
    rec["rounds"] += 1
    if getattr(a, "refresh_auth", False):
        rec["refresh_auth_round"] = rec["rounds"]  # 只对本轮生效：重新复制认证文件，不动配置副本、会话与 worktree
    if line_item:
        workline.start_item(rec, line_item["item"], rec["rounds"], line_item["base"], line_item.get("sync"))
        cli.record_line_residue(host, rec, line_item["item"])
    else:
        workline.note_round(rec, rec["rounds"])
    rec["session"] = session
    rec["model"] = a.model or rec.get("model")
    rec["effort"] = a.effort or rec.get("effort")
    cli.check_controller(host, rec["user_id"])
    with contextlib.ExitStack() as operation:
        if moving:
            (rollback_stack if rollback_stack is not None else operation).enter_context(cli.migrate_job(rec, source_host, host))
        checkpoint = native.request(host, "resume-checkpoint", dict(job=rec["job"]))
        if before_launch:
            before_launch()
        guard = cli.rollback_launch(host, rec, checkpoint)
        if rollback_stack is not None:
            rollback_stack.enter_context(guard)
            guard = contextlib.nullcontext()
        with reviews.rollback_files([cli.job_path(rec["job"])]), guard, events.transaction():
            rec["round_prompts"] = dict(rec.get("round_prompts", {}), **{str(rec["rounds"]): round_prompt})
            cli.save_job(rec)
            cli.launch(host, rec, rec["rounds"], command, prompt, a.timeout or host["timeout"])
    print(f"resumed {rec['job']} (round {rec['rounds']}, session {session or 'pending new context'})")


@contextlib.contextmanager
def rollback_launch(host, rec, checkpoint):
    """Keep the target checkpoint alive until the caller publishes its events."""
    try:
        yield
    except (Exception, SystemExit) as error:
        if checkpoint is not None:
            try:
                native.request(host, "resume-rollback", dict(job=rec["job"], round=rec["rounds"], checkpoint=checkpoint))
            except (Exception, SystemExit) as rollback_error:
                raise ValueError("resume_launch_failed: 启动失败且目标机回滚未确认；请核对目标机状态。") from rollback_error
        if isinstance(error, ValueError) and str(error).startswith("event_write_failed:"):
            raise
        raise ValueError("resume_launch_failed: 目标机启动失败；请核对目标机状态后重试。") from error


@contextlib.contextmanager
def migrate_job(rec, source, target):
    """Transfer only this job's worktree; retain the source for recovery."""
    data = dict(job=rec["job"], bare=rec["bare"], branch=rec["branch"])
    try:
        with tempfile.TemporaryFile() as archive:
            native.request(source, "resume-export", data, output=archive)
            archive.seek(0)
            imported = native.request(target, "resume-import", data, payload=archive)
    except (Exception, SystemExit) as error:
        raise ValueError("resume_transfer_failed: 无法迁移派单工作树；原目标机与轮次保留。") from error
    rec.setdefault("worker_history", []).append({key: rec[key] for key in
        ("host", "worker_root", "shared_root", "worker_platform", "worker_python", "sync") if key in rec})
    target["workdir"] = imported["root"]
    rec.update(host=target["name"], worker_root=target["workdir"], shared_root=target["shared_workdir"],
               worker_platform=target["platform"], worker_python=target["python"], sync="direct",
               auth_file=target[agents.SPECS[rec["agent"]]["auth_key"]], profile_script_uploaded=False)
    try:
        yield
    except (Exception, SystemExit, KeyboardInterrupt):
        # Only the worktree created by this import is eligible for rollback.
        native.request(target, "clean", dict(data, delete_branch=True))
        raise


def cmd_fetch(a, cfg):
    rec = cli.load_job(a.job)
    if getattr(a, "artifacts", False):
        if not rec.get("artifact_paths"):
            cli.die("该派单没有声明 --artifact 产物。", 4)
        results = cli.pull_artifacts(cfg, rec)
        print("\n".join(["产物取回："] + artifacts.artifact_lines(results)))
        if any(item["status"] == "error" for item in results):
            sys.exit(1)
        return
    cli.fetch_job_branch(cfg, rec)
    repo, b = rec["repo"], rec["branch"]
    log = cli.git(["log", "--oneline", f"{rec['base']}..rd/{b}"], repo).stdout.strip()
    print(f"fetched into rd/{b}")
    print(log if log else "(no commits on top of the dispatched base)")
    if log:
        print(f"review: git diff {rec['base'][:10]}..rd/{b}     take it: git merge --ff-only rd/{b}")


def fetch_job_branch(cfg, rec):
    """把派单分支取回控制端 refs/remotes/rd/<分支>；fetch 与 suggest 的同步预检共用。"""
    host = cli.job_host(cfg, rec)
    repo, b = rec["repo"], rec["branch"]
    spec = f"+refs/heads/{b}:refs/remotes/rd/{b}"
    if rec["sync"] == "origin":
        cli.git(["fetch", "origin", spec], repo)
    elif host["platform"] == "windows":
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "result.bundle"
            with bundle.open("wb") as stream:
                native.request(host, "fetch-bundle", {"bare": rec["bare"], "branch": b}, output=stream)
            cli.git(["fetch", str(bundle), spec], repo)
    else:
        cli.git(["fetch", cli.scp_path(host, rec.get("bare", f"repos/{rec['project']}.git")), spec], repo, env=cli.git_ssh_env(host))


@cli.locked_job
def cmd_stop(a, cfg):
    rec = cli.load_job(a.job)
    host = cli.job_host(cfg, rec)
    stopper = messages.identifier(getattr(a, "stopped_by", None) or cli.initiator_session(a) or "cli", "停止者")
    before = cli.job_states(cfg, [rec]).get(rec["job"], {})
    request = rec.get("stop_requests", {}).get(str(rec["rounds"]))
    if request is None and before.get("state") in ("running", "starting"):
        request = dict(stopped_by=stopper, via=getattr(a, "via", "cli"), requested_at=messages.utc_now())
        rec.setdefault("stop_requests", {})[str(rec["rounds"])] = request
        cli.save_job(rec)
    # Keep the original actor across failures after the target has been killed.
    already_stopped = request and before.get("state") == "done" and str(before.get("exit")) == "143"
    if already_stopped:
        snapshot = before
        print("stopped or already finished")
    else:
        if before.get("state") == "unreachable":
            cli.die("停止结果尚未确认；请核对 rdispatch status 后重试。", 4)
        if host["platform"] == "windows":
            native.request(host, "stop", {"job": rec["job"]})
            print("stopped or already finished")
        else:
            r = cli.ssh(host, cli.KILLTREE + f'; D="$ROOT/jobs/{rec["job"]}"; p=$(cat "$D/pid" 2>/dev/null); '
                    'if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then killtree "$p"; echo 143 > "$D/exit_code"; '
                    'echo stopped; else echo "not running"; fi')
            print(r.stdout.strip())
        snapshot = cli.job_states(cfg, [rec]).get(rec["job"], {}) if request else before
    if not request:
        return
    if snapshot.get("state") == "done" and str(snapshot.get("exit")) != "143":
        # The round finished on its own while stop was reaching the target.
        rec["stop_requests"].pop(str(rec["rounds"]), None)
        cli.save_job(rec)
        return
    if snapshot.get("state") != "done" or str(snapshot.get("exit")) != "143":
        cli.die("停止结果尚未确认；请核对 rdispatch status 后重试。", 4)
    receipt = cli.queries(cfg).result(rec, rec["rounds"], snapshot)
    if receipt.get("stopped_by") and cli.queries(cfg).acknowledgement(rec, receipt):
        return
    stopper, stopped_at = request["stopped_by"], request["requested_at"]
    events.emit(cli.STATE, "job.state_changed", events.job_data(rec, execution_state="done", executor_exit=143,
                stopped_by=stopper, stopped_at=stopped_at, suppress_push=True))
    receipt.update(executor_exit=143, stopped_by=stopper, stopped_at=stopped_at,
                   suppress_push=True, review_state="pending", needs_initiator_review=True)
    report = dict(receipt.get("report") or dict(schema=1, kind="result", job=rec["job"], round=rec["rounds"],
                                               checks="not_reported", summary="派单已主动停止，仍需验收。"))
    report.update(outcome="failed", updated_at=stopped_at)
    receipt["report"] = report
    receipt["receipt_id"] = query.receipt_id(receipt)
    cli.persist_receipt(rec, receipt)
    cli.acknowledge_receipt(rec, receipt, stopper, request["via"])
