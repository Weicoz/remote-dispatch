"""工作线、待派登记、门禁与建议：session-default、plan、gate、line、suggest。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import argparse
import json
import shlex
import sys
import time
from . import rd_messages as messages
from . import rd_timings as timings
from . import rd_review as reviews
from . import rd_context_pack as context_packs
from . import rd_plan as plans
from . import rd_line as workline
from . import rd_suggest as suggestions
from .rd_lock import JobBusy
from .rd_session_defaults import SessionDefaults, validate_mode
from . import cli

__all__ = [
    "build_suggestion",
    "chain_name",
    "cmd_gate",
    "cmd_line",
    "cmd_plan",
    "cmd_session_default",
    "cmd_suggest",
    "context_pack",
    "context_source",
    "gate_choose",
    "line_accept",
    "line_ask",
    "line_decline",
    "line_facts",
    "line_sync_apply",
    "line_sync_plan",
    "line_wire",
    "line_wiring",
    "record_decline",
]


def cmd_session_default(a, cfg):
    values = {}
    for key in ("host", "harness"):
        if getattr(a, "clear_" + key) or a.clear:
            values[key] = None
        elif getattr(a, key) is not None:
            values[key] = getattr(a, key)
    store = SessionDefaults(cli.STATE, cli.require_user())
    session, pane = (None, a.session) if a.pane else (a.session, None)
    mode_touched = a.clear_dispatch_mode or a.dispatch_mode is not None
    if mode_touched:
        validate_mode(a.dispatch_mode)  # 非法取值经 main 的 ValueError 处理，退出码 4
    if values:
        _, result = store.update(values, session=session, pane=pane)
    else:
        result = store.get(session=session, pane=pane)
    if mode_touched:
        store.update_mode(None if a.clear_dispatch_mode else a.dispatch_mode, session=session, pane=pane)
    mode = store.get_mode(session=session, pane=pane)
    if mode_touched or mode is not None:
        result = dict(result, dispatch_mode=mode)
    print(json.dumps(result, ensure_ascii=False))


def context_source(cfg, uid, job):
    """整理一个前序派单的上下文包来源；跨用户或没有回执时退出码 4。"""
    try:
        rec = cli.load_job(messages.job_id(job))
    except ValueError as error:
        cli.die(str(error), 4)
    if not rec.get("user_id") or rec["user_id"] != uid:
        cli.die(f"--context-from {job} 不属于当前用户 ID，拒绝作为上下文来源。", 4)
    receipt, review = None, {}
    for number in range(rec["rounds"], 0, -1):
        found = messages.read_json(cli.result_path(rec, number))
        if found is None and number == rec["rounds"]:
            try:
                found = cli.queries(cfg).result(rec, number, cli.job_states(cfg, [rec]).get(job, {}))
            except (Exception, SystemExit):
                found = None
        if found is None:
            continue
        state = reviews.read(cli.STATE, rec, number)
        if receipt is None or state["review_state"] == "accepted":
            receipt, review = found, state
        if state["review_state"] == "accepted":
            break
    if receipt is None:
        cli.die(f"--context-from {job} 没有回执，拒绝作为上下文来源；先用 rdispatch result 收取。", 4)
    report = receipt.get("report") or {}
    repo, ref = rec.get("repo"), "refs/remotes/rd/" + rec["branch"]
    commits, files = [], []
    if repo and cli.git(["rev-parse", "--verify", "--quiet", ref], repo, check=False).returncode == 0:
        commits = cli.git(["log", "--oneline", f"{rec['base']}..{ref}"], repo, check=False).stdout.splitlines()
        files = cli.git(["diff", "--name-only", f"{rec['base']}..{ref}"], repo, check=False).stdout.splitlines()
    changed = report.get("changed_files", receipt.get("changed_files"))
    if isinstance(changed, list):
        files = [x["path"] if isinstance(x, dict) else str(x) for x in changed] or files
    return dict(job=job, round=receipt.get("round"), review_state=review.get("review_state"),
                outcome=report.get("outcome"), checks=report.get("checks"), head=receipt.get("head"),
                summary=report.get("summary", ""), commits=commits, files=files,
                decisions=report.get("decisions") or [], unfinished=report.get("unfinished") or [])


def context_pack(cfg, uid, jobs):
    return context_packs.build([cli.context_source(cfg, uid, job) for job in dict.fromkeys(jobs)])


def cmd_plan(a, cfg):
    uid = cli.require_user()
    project = a.project
    if not project:
        project = cli.project_name(cli.local_repo(cli.get_dir(a)))[0]
    store = plans.PlanStore(cli.STATE, uid, project)
    action = a.action
    if action == "chain":
        if a.clear or a.revoke_accept:
            def reset(setting):
                if a.clear:
                    setting.update(no_suggest=False, rejections=0)
                if a.revoke_accept:
                    setting.update(accept_all=False)
            setting = store.change_chain(a.line, reset)
        else:
            setting = store.chain(a.line)
        if a.json:
            print(json.dumps(dict(project=project, line=a.line, **setting), ensure_ascii=False, indent=2))
        else:
            print(f"线 {a.line}：本链不再建议={'是' if setting['no_suggest'] else '否'}，拒绝 {setting['rejections']} 次，"
                  f"常驻同意={'是' if setting['accept_all'] else '否'}")
        return
    if action == "add":
        store.add(a.item, a.title, a.depends, a.after)
    elif action == "set":
        if a.title is None and a.depends is None and a.after is None:
            cli.die("plan set 至少需要 --title、--depends 或 --after 之一；清空依赖传空串。", 4)
        store.update(a.item, a.title, [d for d in a.depends if d] if a.depends is not None else None,
                     [j for j in a.after if j] if a.after is not None else None)
    elif action in ("done", "cancel"):
        store.set_status(a.item, plans.DONE if action == "done" else plans.CANCELLED)
    elif action == "remove":
        store.remove(a.item)
    if action != "list":
        print(f"plan {action} {a.item}")
        return
    rows = store.rows()
    if a.json:
        print(json.dumps(dict(project=project, summary=store.summary(), items=rows), ensure_ascii=False, indent=2))
        return
    if not rows:
        print("no plan items.")
    for row in rows:
        extra = f" 等待 {'、'.join(row['blocked_by'])}" if row["blocked_by"] else ""
        jobs = f" 派单 {'、'.join(row['jobs'])}" if row["jobs"] else ""
        print(f"{row['id']} [{row['state']}] {row['title']}{extra}{jobs}")


def gate_choose(a, cfg, project, uid):
    """返回 (目标机名, 选择理由)。只用全量测试（test_tier=full）的成功历史，挑最快且有空闲重命令名额的机器。"""
    if a.host:
        return a.host, f"--host 指定 {a.host}"
    records = [rec for rec in cli.all_jobs() if rec.get("user_id") in (None, "", uid) and rec.get("test_tier") == "full"]
    rows = timings.history(cli.STATE, records, project=project, kind="test")
    medians = timings.median_body_by_host(rows, success_only=True)
    hosts = {h["name"]: h for h in cfg.hosts()}
    ranked = sorted((seconds, name) for name, seconds in medians.items() if name in hosts)
    if not ranked:
        default = cfg.cp["defaults"].get("host") if cfg.cp.has_section("defaults") else None
        if not default or default == "auto":
            cli.die("没有全量测试历史，且未配置默认目标机；请用 --host 指定，或在 [defaults] 设置 host。", 4)
        return default, "无数据，用默认目标机"
    skipped = []
    for seconds, name in ranked:
        info = cli.probe(hosts[name])
        used = cli.num(info.get("heavy")) or 0
        if not cli.assess(hosts[name], info) and used < int(hosts[name]["max_heavy"]):
            reason = f"历史全量测试中位耗时 {seconds:g}s，最快且有空闲重命令名额"
            if skipped:
                reason = f"历史全量测试中位耗时 {seconds:g}s；更快的 {'、'.join(skipped)} 满载或不可用"
            return name, reason
        skipped.append(f"{name}({seconds:g}s)")
    cli.die(f"有历史的目标机均无空闲重命令名额或不可用（{'、'.join(skipped)}）；未派出。", 3)


def cmd_gate(a, cfg):
    """合入前门禁：对指定提交在选定目标机跑一次全量测试，复用 run 的派单、排队与回执路径。"""
    uid = cli.require_user()
    repo = cli.local_repo(cli.get_dir(a))
    project = a.project or cli.project_name(repo)[0]
    sha = cli.git(["rev-parse", "--verify", a.ref + "^{commit}"], repo).stdout.strip()
    host, reason = cli.gate_choose(a, cfg, project, uid)
    print(f"gate: 目标机 {host}（{reason}）；被测提交 {sha[:10]}")
    values = dict(force_host=False, tag=[], include=[], branch=None, wip=False, timeout=None, initiator_session=None)
    values.update(vars(a))
    values.update(test=False, test_full=True, agent=None, cmd=None, prompt_file=None, heavy=True,
                  model=None, effort=None, gate={"ref": a.ref, "commit": sha})
    run_args = argparse.Namespace(**values)
    run_args.host = host
    run_args.name = a.name or "gate"
    cli.cmd_run(run_args, cfg)


@cli.locked_job
def cmd_line(a, cfg):
    """工作线收尾：prefix 在验收轮的 head 建前缀分支；close 关闭线，之后派单才按已合入处理。"""
    rec = cli.load_job(a.job)
    cli.require_initiator(rec, a)
    if a.line_action == "close":
        state = cli.job_states(cfg, [rec]).get(rec["job"], {}).get("state")
        reason = workline.close_blocker(rec, state)
        if reason:
            cli.die(reason + "。", 4)
        rec["line"]["closed"] = True
        cli.save_job(rec)
        print(f"工作线 {rec['line']['name']} 已关闭；派单 {rec['job']} 之后按合入状态进入回收。")
        return
    items, reason = workline.prefix_items(rec, a.items)
    if reason:
        cli.die(reason + "。", 4)
    heads = {}
    for item in items:
        number = workline.accepted_round(rec, item, lambda n: reviews.read(cli.STATE, rec, n)["review_state"])
        if number is None:
            cli.die(f"工作项 {item} 尚未验收通过，不能合入。", 4)
        receipt = messages.read_json(cli.result_path(rec, number)) or cli.queries(cfg).result(rec, number)
        heads[item] = (receipt or {}).get("head")
    head = heads[items[-1]]
    if not head:
        cli.die(f"工作项 {items[-1]} 的验收轮回执缺少 head。", 4)
    if cli.git(["cat-file", "-e", head + "^{commit}"], rec["repo"], check=False).returncode:
        cli.die(f"提交 {head[:10]} 尚未取回；先用 rdispatch fetch {rec['job']}。", 4)
    name = a.branch or f"{rec['branch']}-prefix-{len(items)}"
    existing = cli.git(["rev-parse", "-q", "--verify", "refs/heads/" + name], rec["repo"], check=False).stdout.strip()
    if existing and existing != head:
        cli.die(f"分支 {name} 已存在且指向 {existing[:10]}；不改写，换个 --branch。", 4)
    if not existing:
        cli.git(["branch", name, head], rec["repo"])
    print(f"前缀分支 {name} -> {head[:10]}（工作项 {'、'.join(items)}）；线未关闭，派单仍不算已合入。")
    print(f"门禁：rdispatch gate {name}")


def line_facts(cfg, rec):
    """接线判定要用的目标机与验收事实；全部只读，读不到按缺失处理。"""
    facts = dict(state=None, review_state=None, session=None, head=None)
    try:
        snapshot = cli.job_states(cfg, [rec]).get(rec["job"], {})
        facts["state"] = snapshot.get("state")
        facts["review_state"] = reviews.read(cli.STATE, rec, rec["rounds"])["review_state"]
        if facts["review_state"] == "accepted":
            receipt = messages.read_json(cli.result_path(rec, rec["rounds"])) or cli.queries(cfg).result(rec, rec["rounds"])
            facts["head"] = (receipt or {}).get("head")
        host = cli.job_host(cfg, rec)
        facts["session"] = rec.get("receiver_session") or cli.find_session(
            host, rec, rec.get("agent_config") or cfg.agent(rec["agent"]))
    except (Exception, SystemExit):
        pass
    return facts


def line_wiring(cfg, a, uid, owner, rec, item, include_paths):
    """判定本次派单能否作为 rec 所在线的新工作项：(可接线, 原因, 基线)。只读，供 run 自动接线与后续 suggest 复用。"""
    facts = cli.line_facts(cfg, rec)
    request = dict(uid=uid, owner=owner, agent=a.agent, model=a.model, permission=getattr(a, "permission_mode", None),
                   include=include_paths, wip=a.wip, branch=a.branch, host=a.host, item=item,
                   enabled=cfg.line_enabled())
    ok, reason = workline.decide(rec, request, facts)
    return ok, reason, facts.get("head")


def line_sync_plan(cfg, rec, repo, ref, head):
    """接线前的 integration 同步预检，只在控制端做：返回 dict(needed, ...)。

    integration 新头 = 控制端解析的 ref。它已是验收提交 head 的祖先就不需要同步；
    否则先取回前序提交，再用 git merge-tree 预检 head 与新头能否无冲突合并，precheck 为 clean/conflict/error。
    不在目标机上试合并，也不写目标机。
    """
    resolved = cli.git(["rev-parse", "--verify", (ref or "HEAD") + "^{commit}"], repo, check=False)
    integration = resolved.stdout.strip()
    if resolved.returncode or not integration:
        return dict(needed=False)
    if cli.git(["cat-file", "-e", head + "^{commit}"], repo, check=False).returncode:
        try:
            cli.fetch_job_branch(cfg, dict(rec, repo=repo))
        except (Exception, SystemExit) as error:
            return dict(needed=True, integration=integration, from_head=head, precheck="error",
                        reason=f"无法取回前序验收提交 {head[:10]}：{error}")
        if cli.git(["cat-file", "-e", head + "^{commit}"], repo, check=False).returncode:
            return dict(needed=True, integration=integration, from_head=head, precheck="error",
                        reason=f"取回后仍缺少前序验收提交 {head[:10]}")
    if cli.git(["merge-base", "--is-ancestor", integration, head], repo, check=False).returncode == 0:
        return dict(needed=False)
    plan = dict(needed=True, integration=integration, from_head=head)
    if cli.job_host(cfg, rec).get("platform") == "windows":
        return dict(plan, precheck="error", reason="Windows 目标机暂不支持同步 integration 新头")
    merged = cli.git(["merge-tree", "--write-tree", "--name-only", "--no-messages", head, integration], repo, check=False)
    if merged.returncode == 0:
        return dict(plan, precheck="clean")
    if merged.returncode == 1:
        files = [name for name in merged.stdout.splitlines()[1:] if name.strip()]
        return dict(plan, precheck="conflict", files=files,
                    reason=f"integration 新头 {integration[:10]} 与前序验收提交 {head[:10]} 预检有冲突"
                           + (f"（{'、'.join(files)}）" if files else ""))
    return dict(plan, precheck="error", reason="合并预检失败：" + (merged.stderr or "").strip())


def line_sync_apply(cfg, rec, repo, plan):
    """接受建议才执行的写操作：推 integration 新头到目标机 bare，在 worktree 里合并。

    返回 (成功, 原因, 新 head)。合并冲突时中止并确认 worktree 回到合并前；任何失败都不改派单记录。
    """
    host = cli.job_host(cfg, rec)
    bare = rec.get("bare", f"repos/{rec['project']}.git")
    pushed = cli.git(["push", "--porcelain", "--no-follow-tags", "--force", cli.scp_path(host, bare),
                  f"{plan['integration']}:refs/rd-sync/{rec['job']}"], repo, check=False, env=cli.git_ssh_env(host))
    if pushed.returncode:
        return False, "推送 integration 新头到目标机失败：" + (pushed.stderr or "").strip(), None
    script = (f'cd "$ROOT/wt/{rec["job"]}" 2>/dev/null || {{ echo status=missing; exit 0; }}\n'
              f'pre=$(git rev-parse HEAD)\n'
              f'if [ "$pre" != {shlex.quote(plan["from_head"])} ]; then\n'
              # 上次同步已成功、之后 resume 失败：worktree 已含验收提交与 integration 新头，重试按已同步处理。
              f'  if git merge-base --is-ancestor {shlex.quote(plan["from_head"])} "$pre" 2>/dev/null && '
              f'git merge-base --is-ancestor {shlex.quote(plan["integration"])} "$pre" 2>/dev/null; '
              f'then echo status=merged; echo head=$pre; else echo status=moved; fi\n'
              f'  exit 0\nfi\n'
              f'[ -z "$(git status --porcelain --untracked-files=no)" ] || {{ echo status=dirty; exit 0; }}\n'
              f'if git -c user.name=rdispatch -c user.email=rdispatch@localhost merge --no-edit -q '
              f'{shlex.quote(plan["integration"])} >/dev/null 2>&1; then echo status=merged\n'
              f'else git merge --abort >/dev/null 2>&1 || git reset -q --hard "$pre"; echo status=conflict; fi\n'
              f'echo head=$(git rev-parse HEAD)\n')
    try:
        out = cli.ssh(host, script, check=False, timeout=120)
    except (Exception, SystemExit) as error:
        return False, f"目标机合并未完成：{error}", None
    fields = dict(row.partition("=")[::2] for row in out.stdout.splitlines()) if out and out.returncode == 0 else {}
    status, head = fields.get("status"), fields.get("head")
    if status == "merged" and head:
        return True, None, head
    reasons = dict(conflict="实际合并有冲突，已中止并恢复 worktree 到合并前状态", missing="前序 worktree 已不存在",
                   moved="worktree 已偏离验收提交，不同步", dirty="worktree 有未提交的跟踪文件改动，不同步")
    return False, reasons.get(status) or "目标机合并结果无法确认：" + ((out.stderr if out else "") or "").strip(), None


def line_wire(a, cfg, uid, owner, plan_store, item, after, include_paths, prompt):
    """line 模式：满足严格接线条件就把本工作项作为前序派单的新轮次追加，返回 True；否则打印原因返回 False。"""
    previous, reason = workline.predecessor(plan_store.read(), item, after)
    if previous is None:
        print(f"工作线未接线：{reason}；按默认方式派出。")
        return False
    try:
        with cli.wait_job_lock(previous):
            rec = cli.load_job(previous)
            ok, reason, base = cli.line_wiring(cfg, a, uid, owner, rec, item, include_paths)
            if not ok:
                print(f"工作线未接线：{reason}；按默认方式派出。")
                return False
            receipt = messages.read_json(cli.result_path(rec, rec["rounds"]))
            action, why = workline.session_policy(
                receipt, workline.reuse_limits(cfg.cp, rec["agent"]), time.time() - cli.last_activity(rec))
            if action == "refuse":
                print(f"工作线未接线：{why}，不做策略轮换；按默认方式派出。")
                return False
            # 只读判定都通过后才同步 integration（写操作），同步后的 head 作为本工作项基线。
            sync = None
            repo = cli.local_repo(cli.get_dir(a))
            plan = cli.line_sync_plan(cfg, rec, repo, getattr(a, "ref", None), base)
            if plan["needed"]:
                if plan["precheck"] != "clean":
                    print(f"工作线未接线：{plan['reason']}；按默认方式派出。")
                    return False
                synced, why, merged = cli.line_sync_apply(cfg, rec, repo, plan)
                if not synced:
                    print(f"工作线未接线：同步 integration 新头失败，{why}；按默认方式派出。")
                    return False
                sync = dict(integration=plan["integration"], from_head=base, head=merged, at=messages.utc_now())
                print(f"已同步 integration {plan['integration'][:10]} 到 worktree（{base[:10]} -> {merged[:10]}）。")
                base = merged
            line_item = dict(item=item, base=base, sync=sync)
            if action == "rotate":
                # --context-from 已带前序派单的上下文包时不再重复拼一份。
                given = {messages.job_id(job) for job in getattr(a, "context_from", None) or []}
                line_item.update(rotate=True, handoff="" if previous in given else cli.context_pack(cfg, uid, [previous]))
            resume_args = argparse.Namespace(
                job=previous, agent=None, session=None, host=None, model=a.model, effort=a.effort,
                timeout=a.timeout, force_host=a.force_host, permission_mode=getattr(a, "permission_mode", None),
                prompt_file=None, new_context=False, retry_after_repair=None, initiator_session=owner)
            cli.resume_job(resume_args, cfg, rec, prompt=prompt, line_item=line_item,
                       before_launch=lambda: plan_store.attach_job(item, previous, after))
    except JobBusy:
        cli.die("round_in_progress: round in progress; another operation holds the job lock; retry after it finishes", 4)
    line = rec["line"]
    print(f"工作线 {line['name']}：工作项 {item} 接入 {previous}（分支 {rec['branch']}，权限档位 "
          f"{rec.get('permission_mode') or '默认'}，基线 {base[:10]}）")
    print(workline.wired_marker(previous, line["name"]))
    return True


def chain_name(rec):
    """本链（线）的名字：已成线取线名，否则取第一个工作项。"""
    return rec["line"]["name"] if rec.get("line") else workline.first_item(rec)


def build_suggestion(cfg, a, scope=None, issue=True):
    """suggest 的核心，全部只读：返回 suggest --json 的结构（serve 也调用它）。

    不建议时 suggest=False 并给出 reason，不生成令牌；全部条件满足时带建议内容、指纹与令牌。
    issue=False 只重算内容与指纹（接受令牌时用），不发新令牌。严格接线条件复用 line_wiring。
    """
    scope = scope or cli.run_scope(a, cfg)

    def refuse(reason, **extra):
        return dict(suggest=False, reason=reason, **extra)
    if not scope.plan_item:
        return refuse("建议需要 --plan-item")
    store = scope.plan_store
    items = store.read()
    previous, reason = workline.predecessor(items, scope.plan_item, scope.after)
    if previous is None:
        return refuse(reason)
    try:
        rec = cli.load_job(previous)
    except SystemExit:
        return refuse(f"前序派单 {previous} 不存在", previous=previous)
    include = cli.include_list(a, scope.repo)
    ok, reason, base = cli.line_wiring(cfg, a, scope.uid, scope.owner, rec, scope.plan_item, include)
    chain = cli.chain_name(rec)
    if not ok:
        return refuse(reason, previous=previous, line=chain)
    if store.chain(chain)["no_suggest"]:
        return refuse(f"本链 {chain} 已拒绝过建议（本链不再建议）；用 rdispatch plan chain {chain} --clear 恢复",
                      previous=previous, line=chain)
    try:
        host = cli.job_host(cfg, rec)
        info = cli.probe(host)
        busy = cli.assess(host, info)
    except (Exception, SystemExit) as error:
        return refuse(f"前序目标机 {rec['host']} 探测失败：{error}", previous=previous, line=chain)
    if busy:
        return refuse(f"前序目标机 {rec['host']} 离线或暂时无法接单（{'; '.join(busy)}）", previous=previous, line=chain)
    plan = cli.line_sync_plan(cfg, rec, scope.repo, getattr(a, "ref", None), base)
    if plan["needed"] and plan["precheck"] != "clean":
        return refuse(plan["reason"], previous=previous, line=chain)
    sync = {key: plan[key] for key in ("needed", "integration", "from_head", "precheck") if key in plan}
    done = list(rec["line"]["items"]) if rec.get("line") else [workline.first_item(rec)]
    content = dict(sync=sync, line=chain, previous=previous, host=rec["host"], branch=rec["branch"], harness=rec.get("agent"),
                   model=rec.get("model"), permission_mode=rec.get("permission_mode"), include=sorted(include),
                   base=base, items=dict(completed=done, current=scope.plan_item,
                                         pending=suggestions.descendants(items, scope.plan_item)))
    result = dict(suggest=True, reason=None, fingerprint=suggestions.fingerprint(content), **content,
                  accept=f"rdispatch run --line {chain} --accept <令牌> --plan-item {scope.plan_item}（其余参数同本次）",
                  decline="rdispatch run --no-line --plan-item " + scope.plan_item + "（其余参数同本次；按默认方式派出并记「本链不再建议」）")
    if issue:
        token, expires = suggestions.TokenStore(cli.STATE, scope.uid).issue(result["fingerprint"], scope.owner)
        store.change_chain(chain, lambda setting: setting.update(suggested=setting["suggested"] + 1))
        result.update(token=token, expires_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires)))
    return result


def cmd_suggest(a, cfg):
    result = cli.build_suggestion(cfg, a)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not result["suggest"]:
        print("不建议接入工作线：" + result["reason"])
        return
    steps = result["items"]
    print(f"建议接入工作线 {result['line']}（前序派单 {result['previous']}）")
    print(f"  工作项顺序：已完成 {'、'.join(steps['completed'])}；本项 {steps['current']}；"
          f"后续待派 {'、'.join(steps['pending']) or '无'}")
    sync = result["sync"]
    print("  同步 integration：" + (f"需要（新头 {sync['integration'][:10]}，预检无冲突；接受后推送并在 worktree 合并）"
                                 if sync["needed"] else "不需要"))
    print(f"  目标机 {result['host']}，分支 {result['branch']}，权限档位 {result['permission_mode'] or '默认'}，基线 {result['base'][:10]}")
    print(f"  接受：rdispatch run --line {result['line']} --accept {result['token']}（可加 --accept-chain 常驻同意；其余参数同本次）")
    print(f"  不接受：rdispatch run --no-line（其余参数同本次）")
    print(f"  令牌 {result['expires_at']} 前单次有效")


def line_accept(a, cfg, scope, include_paths, prompt):
    """run --line：校验令牌或常驻同意后接线；接线条件在接线时再校验一次，不满足就返回 False（按默认方式派出）。"""
    if not scope.plan_item:
        cli.die("--line 需要同时指定 --plan-item。", 4)
    previous, reason = workline.predecessor(scope.plan_store.read(), scope.plan_item, scope.after)
    if previous is None:
        cli.die(f"不能接线：{reason}", 4)
    chain = cli.chain_name(cli.load_job(previous))
    if a.line != chain:
        cli.die(f"线名不符：前序派单 {previous} 所在的线是 {chain}，不是 {a.line}。", 4)
    token = getattr(a, "accept", None)
    if token:
        suggestion = cli.build_suggestion(cfg, a, scope, issue=False)
        if not suggestion["suggest"]:
            cli.die("建议已失效，拒绝接线：" + suggestion["reason"], 4)
        try:
            suggestions.TokenStore(cli.STATE, scope.uid).consume(token, suggestion["fingerprint"], scope.owner)
        except suggestions.TokenRejected as error:
            cli.die(str(error), 4)
        if getattr(a, "accept_chain", False):
            scope.plan_store.change_chain(chain, lambda setting: setting.update(accept_all=True))
            print(f"已记录常驻同意：本链 {chain} 后续工作项可用 --line {chain} 直接接线（每次仍校验严格接线条件）。")
    elif not scope.plan_store.chain(chain)["accept_all"]:
        cli.die(f"--line 需要有效的建议令牌（--accept）；先运行 rdispatch suggest。本链 {chain} 没有常驻同意。", 4)
    wired = cli.line_wire(a, cfg, scope.uid, scope.owner, scope.plan_store, scope.plan_item, scope.after, include_paths, prompt)
    if wired:
        scope.plan_store.change_chain(chain, lambda setting: setting.update(accepted=setting["accepted"] + 1))
    return wired


def line_ask(a, cfg, scope, include_paths, prompt):
    """run 没带 --line／--no-line 且可以接线：返回 (是否已接线, 「本可接线」提示或 None)。

    stdin 和 stdout 都是终端才询问：y 等同 --accept，n 等同 --no-line。
    非终端不阻塞：按默认方式派出，提示写进派单记录（看板可见）。
    """
    suggestion = cli.build_suggestion(cfg, a, scope, issue=False)
    if not suggestion["suggest"]:
        return False, None
    chain, items = suggestion["line"], suggestion["items"]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        scope.plan_store.change_chain(chain, lambda setting: setting.update(suggested=setting["suggested"] + 1))
        hint = dict(line=chain, previous=suggestion["previous"], branch=suggestion["branch"],
                    message=f"本可接入工作线 {chain}（前序派单 {suggestion['previous']}）；"
                            f"用 rdispatch suggest 取令牌后以 --line {chain} --accept 接线，或 --no-line 拒绝")
        print("note: " + hint["message"])
        return False, hint
    print(f"可以接入工作线 {chain}（前序派单 {suggestion['previous']}，分支 {suggestion['branch']}）；"
          f"已完成 {'、'.join(items['completed'])}，本项 {items['current']}，后续待派 {'、'.join(items['pending']) or '无'}")
    while True:
        try:
            answer = input("接入这条工作线？[y/n] ").strip().lower()
        except EOFError:
            return False, None
        if answer in ("y", "yes"):
            issued = cli.build_suggestion(cfg, a, scope)
            if not issued["suggest"]:
                print("建议已失效：" + issued["reason"] + "；按默认方式派出。")
                return False, None
            a.line, a.accept = chain, issued["token"]
            return cli.line_accept(a, cfg, scope, include_paths, prompt), None
        if answer in ("n", "no"):
            a.no_line = True
            return False, None


def record_decline(a, scope):
    """拒绝建议只在派单确实启动后记一次；目标机忙、预检或员工配置检查失败的派单不计。"""
    if getattr(a, "no_line", False):
        cli.line_decline(scope)


def line_decline(scope):
    """run --no-line：只在待派登记里记「本链不再建议」和拒绝次数，不创建任何派单状态。"""
    previous, _ = workline.predecessor(scope.plan_store.read(), scope.plan_item, scope.after)
    try:
        chain = cli.chain_name(cli.load_job(previous)) if previous else None
    except SystemExit:
        chain = None
    if chain is None:
        print("--no-line：没有可接线的前序派单，无需记录；按默认方式派出。")
        return
    setting = scope.plan_store.change_chain(
        chain, lambda s: s.update(no_suggest=True, rejections=s["rejections"] + 1))
    print(f"已拒绝建议：本链 {chain} 不再建议（累计拒绝 {setting['rejections']} 次）；"
          f"用 rdispatch plan chain {chain} --clear 恢复。按默认方式派出。")
