"""run：派单范围解析、未发送改动检查、建树与 cmd_run。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import argparse
import contextlib
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from . import rd_native as native
from . import rd_messages as messages
from . import rd_slim as slim
from . import rd_warm as warm
from . import rd_include_guard as include_guard
from . import rd_artifacts as artifacts
from . import rd_agents as agents
from . import rd_projects as projects
from . import rd_plan as plans
from . import rd_initiator as initiators
from .rd_session_defaults import SessionDefaults, resolve_mode
from .rd_host_config import parse_cache_dirs
from . import cli

__all__ = [
    "cmd_run",
    "include_list",
    "place_worktree",
    "release_unstarted_slot",
    "run_scope",
    "unsent_changes",
]


def run_scope(a, cfg):
    """run 与 suggest 共用的前置解析：用户、发起会话、派单默认值、项目与待派工作项。不生成派单 ID，不探测目标机。"""
    uid = cli.require_user()
    forwarded_origin = getattr(a, "_forwarded_origin", None)
    origin_harness = forwarded_origin["initiator_harness"] if forwarded_origin else initiators.harness()
    owner = cli.initiator_session(a, origin_harness)
    if not owner:
        print("警告：没有发起会话，结果和续派无法归属到某个会话；用 --initiator-session 或 RD_INITIATOR_SESSION 指定。", file=sys.stderr)
    pane = ((forwarded_origin.get("initiator_herdr") or {}).get("pane_id") if forwarded_origin
            else os.environ.get("HERDR_PANE_ID"))
    defaults = SessionDefaults(cli.STATE, uid).get(owner, pane) if owner or pane else {}
    configured = dict(cfg.cp["defaults"]) if cfg.cp.has_section("defaults") else {}
    a.dispatch_mode = resolve_mode(getattr(a, "dispatch_mode", None),
                                   SessionDefaults(cli.STATE, uid).get_mode(owner, pane) if owner or pane else None, cfg.cp)
    a.host = a.host if a.host is not None else defaults.get("host") or configured.get("host") or "auto"
    # A command/test dispatch remains a command even when the session has a harness default.
    test_full = getattr(a, "test_full", False)
    if not a.agent and not a.cmd and not a.test and not test_full:
        a.agent = defaults.get("harness") or configured.get("harness")
    if a.host != "auto" and not cfg.cp.has_section("host:" + a.host):
        cli.die(f"目标机不存在：{a.host}；请修改会话派单默认值或显式指定 --host。", 4)
    repo = cli.local_repo(cli.get_dir(a))
    project, origin_url = cli.project_name(repo)
    proj = cfg.project(a.project or project)
    project = a.project or project
    if not re.fullmatch(r"[A-Za-z0-9._-]+", project) or project in (".", ".."):
        cli.die("项目名必须是英文、数字、点、下划线或连字符，不得包含路径。", 4)
    plan_item, after = getattr(a, "plan_item", None), getattr(a, "after", None) or []
    if after and not plan_item:
        cli.die("--after 需要同时指定 --plan-item。", 4)
    plan_store = None
    if plan_item:
        plan_store = plans.PlanStore(cli.STATE, uid, project)
        try:
            plan_store.get(plan_store.read(), plan_item)
        except ValueError as error:
            cli.die(str(error), 4)
        for earlier in after:
            cli.load_job(messages.job_id(earlier))
    return argparse.Namespace(uid=uid, forwarded_origin=forwarded_origin, origin_harness=origin_harness, owner=owner,
                              repo=repo, project=project, origin_url=origin_url, proj=proj, plan_item=plan_item,
                              after=after, plan_store=plan_store)


def unsent_changes(porcelain, include_paths, wip=False):
    """`git status --porcelain -z -uall` 的输出里，去掉已被 --include 覆盖的路径，返回仍不会发送的条目。
    wip=True 时已跟踪的改动由 --wip 发送，只检查未跟踪文件。输出不得 strip：首行 " M" 的前导空格是状态位。"""
    rows = []
    tokens = porcelain.split("\0")
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # 重命名／复制后跟一个原路径条目
        if wip and status != "??":
            continue
        if not any(path == inc or path.startswith(inc.rstrip("/") + "/") for inc in include_paths):
            rows.append(entry)
    return rows


def include_list(a, repo):
    """--include 规范化成仓库内相对路径；缺失或越界直接退出 4。"""
    include_paths = []
    missing = []
    for value in a.include:
        path = Path(os.path.abspath(os.path.join(repo, value)))
        if not path.exists():
            missing.append(f"{value} -> {path}")
        try:
            include_paths.append(path.relative_to(repo).as_posix())
        except ValueError:
            cli.die(f"--include 路径必须位于仓库内：{value}（仓库根目录：{repo}）", 4)
    if missing:
        cli.die("--include 缺少以下路径（相对仓库根目录解析）：\n  " + "\n  ".join(missing), 4)
    return include_paths


def release_unstarted_slot(host, rec):
    """准备或启动失败时释放热槽位领用：目标机上没有 pid 也没有 run 脚本（任务肯定没启动）才释放，
    释放后心跳随之停止；有则保留（启动结果待核实）。释放失败不掩盖原错误，控制端崩溃由心跳上限兜底。"""
    if warm.is_warm(rec) and host["platform"] != "windows":
        with contextlib.suppress(Exception, SystemExit):
            cli.ssh(host, warm.release_unstarted_script(rec), check=False, timeout=30)


def place_worktree(cfg, host, rec, a, proj, repo_key, prefix):
    """POSIX 目标建任务 worktree：命令任务先试热槽位（一次 ssh 完成准备、领用、交接快照与精确快照），
    无空闲槽位且达上限、关闭热模式或领用结果缺失时建冷 worktree。结果写进 rec["worktree"] 并落盘。"""
    job, bare, branch = rec["job"], rec["bare"], rec["branch"]
    cold = f'git -C "$ROOT/{bare}" worktree add -q "$ROOT/wt/{job}" {shlex.quote(branch)} || exit 92'
    fields, prepared = None, False
    try:
        use_warm = warm.eligible(a, proj, host, cfg.warm_worktree())
        slots = warm.slot_limit(proj) if use_warm else 0
    except ValueError as error:
        cli.die(str(error), 4)
    if use_warm:
        pinned = warm.pinned_jobs([r for r in cli.all_jobs() if r.get("host") == host["name"]
                                   and r.get("user_id") == rec.get("user_id") and r["job"] != job], time.time())
        script = warm.claim_script(prefix, bare, job, branch, f"{warm.slot_name_part(rec['project'])}-{repo_key}", slots, pinned,
                                   heartbeat_max=warm.heartbeat_limit(getattr(a, "timeout", None) or host["timeout"],
                                                                      cli.fingerprint_settings(proj)[1]))
        fields, prepared = warm.parse_result(cli.ssh(host, script).stdout), True
        if fields and fields["mode"] == warm.COLD:
            print(f"热工作树：{fields.get('reason', '无空闲槽位')}，改用冷 worktree。")
        elif fields:
            print(f"热工作树：槽位 {fields['slot']}（" + ("复用" if fields.get("reused") == "1" else "新建") + "）")
    if not prepared:
        cli.ssh(host, prefix + cold)
    elif fields is None:
        cli.ssh(host, cold)
    warm.apply_result(rec, fields, job)
    cli.save_job(rec)
    if warm.is_warm(rec) and rec.get("artifact_paths"):
        # 产物在槽位里：拉回前其他控制端的领用也不能接手。
        cli.ssh(host, warm.pin_script(rec, artifacts=True))


def cmd_run(a, cfg):
    scope = cli.run_scope(a, cfg)
    uid, forwarded_origin, origin_harness, owner = scope.uid, scope.forwarded_origin, scope.origin_harness, scope.owner
    repo, project, origin_url, proj = scope.repo, scope.project, scope.origin_url, scope.proj
    plan_item, after, plan_store = scope.plan_item, scope.after, scope.plan_store
    test_full = getattr(a, "test_full", False)
    if a.test or test_full:
        # 旧 test 视同 test_full；--test 优先快测，没配快测时回落到全量。
        full = proj.get("test_full") or proj.get("test")
        a.cmd = (full if test_full else proj.get("test_quick") or full) or cli.die(
            f"--test-full needs 'test_full = <command>' (or 'test')" if test_full
            else f"--test needs 'test_quick', 'test_full' or 'test = <command>'"
            f" under [project:{project}] in {cli.CONFIG}")
        a.heavy = a.heavy or test_full or proj.get("test_heavy", "true").lower() in ("1", "true", "yes")
    if bool(a.agent) == bool(a.cmd):
        cli.die("give exactly one of --agent (with --prompt-file) or --cmd / --test")
    cfg.probe_agent = a.agent or "command"
    prompt = None
    permission = getattr(a, "permission_mode", None)
    if a.agent:
        try:
            spec = agents.spec(a.agent)
        except ValueError as error:
            cli.die(str(error), 4)
        if not a.prompt_file:
            cli.die("--agent needs --prompt-file")
        agent = cfg.agent(a.agent)
        if any("{model_args}" not in agent.get(k, "") for k in ("start", "resume")):
            cli.die(f"{a.agent} 启动与续跑模板必须保留 {{model_args}}，以执行员工配置隔离与参数。", 4)
        # 参数错误在推分支、建 worktree 之前暴露。
        cli.agent_command(agent, a.model, a.effort, permission=permission)
        prompt = sys.stdin.read() if a.prompt_file == "-" else Path(a.prompt_file).read_text(encoding="utf-8")
    elif permission:
        cli.die("--permission-mode 只用于代理派单。", 4)
    if getattr(a, "context_from", None):
        if not a.agent:
            cli.die("--context-from 只用于代理派单。", 4)
        prompt = cli.context_pack(cfg, uid, a.context_from) + "\n" + prompt

    include_paths = cli.include_list(a, repo)
    try:
        artifact_paths = artifacts.parse_artifacts(getattr(a, "artifact", None))
    except ValueError as error:
        cli.die(str(error), 4)
    for artifact_path in artifact_paths:
        hits = artifacts.tracked_conflicts(cli.tracked_files(repo, artifact_path), artifact_path)
        if hits:
            cli.die(f"--artifact 不得指向已被 Git 跟踪的路径（会覆盖本地源码）：{artifact_path}（含 {hits[0]}）。"
                "已跟踪文件的改动走分支取回。", 4)
    line, no_line, line_hint = getattr(a, "line", None), getattr(a, "no_line", False), None
    if line and no_line:
        cli.die("--line 与 --no-line 不能同时使用。", 4)
    if (getattr(a, "accept", None) or getattr(a, "accept_chain", False)) and not line:
        cli.die("--accept、--accept-chain 需要同时指定 --line。", 4)
    if getattr(a, "accept_chain", False) and not getattr(a, "accept", None):
        cli.die("--accept-chain 需要同时指定 --accept。", 4)
    if no_line:
        if not plan_item:
            cli.die("--no-line 需要同时指定 --plan-item。", 4)
    elif line:
        if cli.line_accept(a, cfg, scope, include_paths, prompt):
            return
    elif a.dispatch_mode == "line":
        if not plan_item:
            print("工作线未接线：line 模式需要 --plan-item；按默认方式派出。")
        elif cli.line_wire(a, cfg, uid, owner, plan_store, plan_item, after, include_paths, prompt):
            return
    elif a.dispatch_mode == "suggest" and plan_item:
        wired, line_hint = cli.line_ask(a, cfg, scope, include_paths, prompt)
        if wired:
            return
    sha = cli.git(["rev-parse", "--verify", a.ref + "^{commit}"], repo).stdout.strip()
    source = cli.git(["symbolic-ref", "--quiet", "HEAD"], repo, check=False)
    source_branch = source.stdout.strip()[len("refs/heads/"):] if source.returncode == 0 else None
    wip = b""
    if a.wip:
        diff = subprocess.run(["git", "-C", repo, "diff", "--no-ext-diff", "--no-textconv",
                               "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/", sha, "--"], capture_output=True)
        if diff.returncode:
            cli.die("--wip 无法生成工作树补丁：" + diff.stderr.decode(errors="replace"))
        wip = diff.stdout
        if not wip:
            cli.die(f"--wip：已跟踪文件相对 {a.ref} 没有改动；未跟踪文件请用 --include。未派单。", 4)
        print(f"wip: sending tracked working-tree changes relative to {a.ref}; applied to remote index")
    dirty = cli.git(["status", "--porcelain", "-z", "-uall"], repo).stdout
    if dirty:
        unsent = cli.unsent_changes(dirty, include_paths, wip=bool(a.wip))
        if unsent and (a.test or test_full) and not getattr(a, "allow_dirty", False):
            cli.die("测试类派单（--test / --test-full）拒绝派出：本地有未发送的改动，远端会测到旧代码。\n  "
                + "\n  ".join(unsent[:5]) + ("\n  ..." if len(unsent) > 5 else "")
                + "\n先提交，或加 --wip（已跟踪文件改动）/ --include 路径（未跟踪文件）一并发送；"
                "确认要测 HEAD 时用 --allow-dirty 跳过。未派单。", 4)
        if unsent:
            print("警告：本地未提交的改动不会发送，远端运行的是 " + a.ref + " 的已提交代码"
                  "（先提交，或用 --wip / --include）。", file=sys.stderr)

    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000000:09d}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", a.name or a.agent or "cmd").strip("-")
    job = f"{uid}-{project}-{name}-{stamp}"
    branch = a.branch or f"rd/{uid}/{name}-{stamp}"
    cli.git(["check-ref-format", "--branch", branch], repo)
    fp_command, fp_timeout = cli.fingerprint_settings(proj)
    skip_fp = bool(getattr(a, "skip_fingerprint", False))
    local_fp = None
    if fp_command and skip_fp:
        print("警告：已用 --skip-fingerprint 跳过环境指纹核对；本机与目标机环境可能不一致。", file=sys.stderr)
    elif fp_command:
        local_fp = cli.run_local_fingerprint(fp_command, repo, fp_timeout)

    with contextlib.ExitStack() as stack:
        included = None
        if include_paths:
            included = stack.enter_context(tempfile.TemporaryFile())
            packed = subprocess.run(["tar", "-cf", "-", "-C", repo, "--", *include_paths],
                                    stdout=included, stderr=subprocess.PIPE,
                                    env=dict(os.environ, COPYFILE_DISABLE="1"))
            if packed.returncode:
                cli.die("--include 打包失败，未派单：\n" + packed.stderr.decode(errors="replace"))
            included.seek(0)
            # 实际写入目标机的文件清单（任何 include 形式，含 "."），热槽位下次领用前据此清理。
            listing = subprocess.run(["tar", "-tf", "-"], stdin=included, capture_output=True)
            unpacked = warm.unpacked_files(listing.stdout.decode(errors="replace")) if listing.returncode == 0 else []
            included.seek(0)
        explicit = a.force_host and a.host not in (None, "auto")
        if explicit:
            candidates = [cfg.host(a.host, check_disabled=True)]
        elif fp_command and not skip_fp and a.host in (None, "auto"):
            candidates = cli.pick_hosts(cfg, a.host, a.tag)
        else:
            candidates = [cli.pick_host(cfg, a.host, a.tag)]
        remaining = candidates[1:]

        def bind_host(chosen):
            """目标机作用域、控制端检查与员工配置预检；首选与换机共用。"""
            chosen = cli.scoped_host(chosen, uid)
            if getattr(a, "workdir", None):
                custom_root = a.workdir.rstrip("/\\")
                chosen["workdir"] = custom_root + ("/users/" + uid if uid else "")
                chosen["shared_workdir"] = custom_root
                if cli.is_local(chosen) and not custom_root.startswith(("/", "~", "$HOME")):
                    cli.die("本机目标的 --workdir 必须是绝对路径或 ~ 开头。", 4)
                try:
                    parse_cache_dirs(chosen.get("cache_dirs", ""), custom_root)
                except ValueError as error:
                    cli.die(f"[host:{chosen['name']}] cache_dirs {error}", 4)
            cli.check_controller(chosen, uid)
            if chosen["platform"] == "windows" and (a.test or test_full):
                a.cmd = proj.get("test_windows", a.cmd)
            return chosen, cli.agent_preflight(chosen, uid, a.agent, agent) if a.agent else None

        def host_fields(chosen, chosen_profile):
            return {"host": chosen["name"], "sync": chosen["sync"], "worker_root": chosen["workdir"],
                    "shared_root": chosen["shared_workdir"], "profile": chosen_profile,
                    "auth_file": chosen[spec["auth_key"]] if a.agent else chosen["codex_auth_file"],
                    "worker_platform": chosen["platform"], "worker_python": chosen["python"]}

        host, profile = bind_host(candidates[0])
        rec = {"job": job, "project": project, "origin": origin_url, "repo": repo, "branch": branch,
               "base": sha, "source_branch": source_branch, "include_paths": include_paths,
               "agent": a.agent, "cmd": a.cmd, "rounds": 1,
               "created": time.strftime("%Y-%m-%d %H:%M:%S"), "user_id": uid,
               "model": a.model, "effort": a.effort, "agent_config": agent if a.agent else None,
               "agent_kind": a.agent, "isolated": bool(a.agent), "permission_mode": permission,
               "heavy": a.heavy,
               "handoff_version": 1, "initiator_session": owner, "test": bool(a.test or test_full),
               "test_tier": ("full" if test_full or not proj.get("test_quick") else "quick") if (a.test or test_full) else None,
               "regenerable_paths": slim.parse_paths(proj.get("regenerable_paths", ""))}
        rec["worktree"] = warm.cold_field(job)
        if include_paths:
            hashes = include_guard.hash_includes(repo, include_paths)
            if hashes is not None:
                rec["include_hashes"] = hashes
        rec.update(host_fields(host, profile))
        if artifact_paths:
            rec["artifact_paths"] = artifact_paths
        if fp_command and skip_fp:
            rec["fingerprint"] = {"command": fp_command, "skipped": True}
        if getattr(a, "gate", None):
            rec["gate"] = a.gate
        if plan_item:
            rec["plan_item"] = plan_item
        if line_hint:
            rec["line_hint"] = line_hint
        rec["round_prompts"] = {"1": prompt}
        source = dict(forwarded_origin) if forwarded_origin else initiators.origin(origin_harness, rec["created"])
        source["initiator_herdr"] = dict(source["initiator_herdr"], captured_at=rec["created"])
        rec.update(source)
        print(f"dispatching {job} -> {host['name']} (branch {branch}, base {sha[:10]}, sync {host['sync']})")

        repo_key = hashlib.sha256((origin_url or repo).encode()).hexdigest()[:12]
        rec["project_id"] = projects.project_id(project, origin_url)
        bare = f"repos/{project}-{repo_key}.git"
        rec["bare"] = bare
        rec["dispatch_state"] = "preparing"
        # 登记到 started 之间持有任务锁：并发的收取、清理等写操作拿不到锁，不会在半成品状态上落盘。
        stack.enter_context(cli.wait_job_lock(job))
        projects.ProjectIndex(cli.STATE, uid).register(rec)
        if plan_item:
            plan_store.attach_job(plan_item, job, after)
        tried = []
        while True:
            try:
                if host["platform"] == "windows":
                    rec["worktree"] = warm.cold_field(job)
                    cli.native_init(host, rec, repo, sha, proj.get("origin_url") or origin_url)
                    if a.include:
                        native.request(host, "upload", {"relative": "wt/" + job, "archive": True}, payload=included)
                    if wip:
                        native.request(host, "apply-wip", {"job": job}, payload=wip)
                    setup = proj.get("setup_windows", proj.get("setup"))
                    if setup:
                        cli.put_file(host, f"jobs/{job}/setup.ps1", setup)
                    spent = 0
                    if fp_command and not skip_fp:
                        spent = cli.check_fingerprint(host, rec, cli.host_fingerprint_command(proj, host, fp_command), local_fp,
                                                  fp_timeout, a.timeout or host["timeout"])
                    rec["dispatch_state"] = "launching"
                    cli.save_job(rec)
                    cli.launch(host, rec, 1, "", prompt, cli.remaining_timeout(a.timeout or host["timeout"], spent))
                    rec["dispatch_state"] = "started"
                    cli.save_job(rec)
                    cli.record_decline(a, scope)
                    print(f"started. job id: {job}")
                    return
                if host["sync"] == "origin":
                    worker_url = proj.get("origin_url") or origin_url or cli.die("this repository has no 'origin' remote")
                    if not rec.get("owns_origin_branch"):
                        cli.push_task_branch(repo, "origin", branch, sha)
                        rec["owns_origin_branch"] = True
                        cli.save_job(rec)
                    prep = (f'if [ -d "$ROOT/{bare}" ]; then\n'
                            f'  if git -C "$ROOT/{bare}" show-ref --verify --quiet {shlex.quote("refs/heads/" + branch)}; then '
                            f'echo "task branch already exists; use another --branch" >&2; exit 91; fi\n'
                            f'else GIT_TERMINAL_PROMPT=0 git clone --bare {shlex.quote(worker_url)} "$ROOT/{bare}" || exit 90; fi\n'
                            f'GIT_TERMINAL_PROMPT=0 git -C "$ROOT/{bare}" fetch origin '
                            f'{shlex.quote("refs/heads/" + branch + ":refs/heads/" + branch)} || exit 91\n'
                            f'touch "$ROOT/jobs/{job}/branch-created" || exit 91\n')
                elif host["sync"] == "direct":
                    cli.ssh(host, f'mkdir -p "$ROOT/repos" && {{ [ -d "$ROOT/{bare}" ] || git init -q --bare "$ROOT/{bare}"; }}')
                    cli.push_task_branch(repo, cli.scp_path(host, bare), branch, sha, env=cli.git_ssh_env(host))
                    rec["owns_worker_branch"] = True
                    cli.save_job(rec)
                    prep = ""
                else:
                    cli.die(f"[host:{host['name']}] sync must be origin or direct")
                cli.place_worktree(cfg, host, rec, a, proj, repo_key,
                               f'mkdir -p "$ROOT/jobs/{job}" "$ROOT/repos" "$ROOT/wt" "$SHARED_ROOT/locks" "$ROOT/bin"\n{prep}')

                if a.include:
                    cli.ssh(host, f'tar -xf - -C {warm.worktree_sh(rec)}', stdin=included)
                    if warm.is_warm(rec) and unpacked:
                        # 记下解包路径：下次领用前连被 .gitignore 忽略的文件一起清掉。
                        cli.ssh(host, warm.record_unpacked_script(rec), stdin_data="\n".join(unpacked) + "\n")
                if wip:
                    with tempfile.TemporaryFile() as patch_file:
                        patch_file.write(wip)
                        patch_file.seek(0)
                        cli.ssh(host, f'git -C {warm.worktree_sh(rec)} apply --index --binary -', stdin=patch_file)
                if proj.get("setup"):
                    cli.put_file(host, f"jobs/{job}/setup.sh", proj["setup"] + "\n")
                spent = 0
                if fp_command and not skip_fp:
                    spent = cli.check_fingerprint(host, rec, fp_command, local_fp, fp_timeout, a.timeout or host["timeout"])

                if a.agent:
                    cmd_text = cli.agent_command(agent, a.model, a.effort, permission=permission)
                else:
                    cmd_text = ("rd-heavy sh -c " + shlex.quote(a.cmd)) if a.heavy else a.cmd
                rec["dispatch_state"] = "launching"
                cli.save_job(rec)
                cli.launch(host, rec, 1, cmd_text, prompt, cli.remaining_timeout(a.timeout or host["timeout"], spent))
                rec["dispatch_state"] = "started"
                cli.save_job(rec)
                cli.record_decline(a, scope)
                print(f"started. job id: {job}")
                print(f"  rdispatch status {job}    rdispatch logs {job}    rdispatch wait {job}    rdispatch fetch {job}")
                break
            except cli.FingerprintMismatch as mismatch:
                tried.append(mismatch.attempt)
                if not remaining:
                    cli.reject_fingerprint(host, rec, fp_command, local_fp, tried)
                print(f"{host['name']} 环境指纹不一致，换下一台候选。", file=sys.stderr)
                cli.drop_attempt(host, rec)
                host = remaining.pop(0)
                try:
                    host, profile = bind_host(host)
                except SystemExit:
                    rec["dispatch_state"] = "preparation_failed"
                    cli.save_job(rec)
                    raise
                rec.update(host_fields(host, profile))
                rec["dispatch_state"] = "preparing"
                cli.save_job(rec)
                print(f"dispatching {job} -> {host['name']} (branch {branch}, base {sha[:10]}, sync {host['sync']})")
                if included is not None:
                    included.seek(0)
                continue
            except (SystemExit, Exception, KeyboardInterrupt):
                if rec["dispatch_state"] == "fingerprint_rejected":
                    raise
                if rec["dispatch_state"] == "preparing":
                    rec["dispatch_state"] = "preparation_failed"
                    cli.save_job(rec)
                    cli.release_unstarted_slot(host, rec)
                    print(f"rdispatch: 准备失败，job 已登记：{job}；核对后用 rdispatch clean {job} --force --delete-branch 清理本任务。", file=sys.stderr)
                else:
                    cli.release_unstarted_slot(host, rec)
                    print(f"rdispatch: 启动结果待核实，job 已登记：{job}；先用 rdispatch status {job} 检查。", file=sys.stderr)
                raise
