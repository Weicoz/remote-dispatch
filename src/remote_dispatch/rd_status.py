"""执行状态与查看：remote_state、job_states、status/wait/watch/logs/timings。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from . import rd_native as native
from . import rd_messages as messages
from . import rd_warm as warm
from . import rd_artifacts as artifacts
from . import rd_timings as timings
from . import rd_health as health
from . import rd_line as workline
from . import cli

__all__ = [
    "cmd_logs",
    "cmd_status",
    "cmd_timings",
    "cmd_wait",
    "cmd_watch",
    "describe",
    "job_states",
    "merge_check",
    "network_stalls",
    "remote_state",
    "scoped_jobs",
]


def remote_state(host, jobs):
    if host["platform"] == "windows":
        try:
            return native.request(host, "state", {"jobs": jobs}, timeout=30)
        except (ValueError, subprocess.TimeoutExpired):
            return {j: {"state": "unreachable"} for j in jobs}
    sample_code = Path(cli.HERE, "rd_health.py").read_text(encoding="utf-8") + "\nimport sys\nprint(json.dumps(log_sample(sys.argv[1], sys.argv[2])))"
    script = "sample_code=" + shlex.quote(sample_code) + "\nfor J in " + " ".join(map(shlex.quote, jobs)) + r'''; do
  D="$ROOT/jobs/$J"; st=missing
  if [ -d "$D" ]; then
    st=starting
    if [ -f "$D/exit_code" ]; then st=done
    elif [ -f "$D/pid" ] && kill -0 "$(cat "$D/pid")" 2>/dev/null; then st=running
    elif [ -f "$D/pid" ]; then st=dead; fi
  fi
  n=$(ls "$D"/run.r*.sh 2>/dev/null | sed 's/.*run.r//;s/\.sh$//' | sort -n | tail -n 1)
  n=${n:-0}
  last=$(tail -n 5 "$D/stdout.r$n.log" 2>/dev/null | awk 'NF{l=$0}END{print l}' | tr '\t' ' ' | cut -c1-200)
  head=$(cat "$D/head" 2>/dev/null); dirty=$(cat "$D/dirty" 2>/dev/null); head_source=recorded
  W="$ROOT/wt/$J"; [ -f "$D/worktree" ] && W=$(cat "$D/worktree")
  if [ "$st" = running ] && [ -d "$W" ]; then
    current_head=$(GIT_OPTIONAL_LOCKS=0 git -C "$W" rev-parse HEAD 2>/dev/null)
    if [ -n "$current_head" ]; then
      head=$current_head; head_source=live
      dirty=$(GIT_OPTIONAL_LOCKS=0 git -C "$W" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    fi
  fi
  # 热槽位被下一任务接手前写下的交接快照优先于记录值（槽位现场已不属于本任务）。
  if [ "$st" != running ] && [ -f "$D/final-head" ]; then head=$(cat "$D/final-head"); dirty=$(cat "$D/final-dirty" 2>/dev/null); head_source=recorded; fi
  setup=; [ -f "$D/setup.sh" ] && { [ -f "$D/setup.done" ] && setup=done || setup=pending; }
  sample=$(python3 -c "$sample_code" "$D" "$n" 2>/dev/null)
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$J" "$st" "$(cat "$D/exit_code" 2>/dev/null)" \
    "$([ -f "$D/timed_out" ] && echo yes)" "$(cat "$D/push_rc" 2>/dev/null)" "$head" \
    "$dirty" "$n" "$setup" "$last" "$head_source" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sample" "$(cat "$D/started.r$n" 2>/dev/null)"
done'''
    r = cli.ssh(host, script, check=False, timeout=30)
    out = {}
    if not r or r.returncode != 0:
        return {j: {"state": "unreachable"} for j in jobs}
    keys = ["job", "state", "exit", "timed_out", "push_rc", "head", "dirty", "round", "setup", "last", "head_source", "observed_at", "network_sample", "started_at"]
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == len(keys):
            out[parts[0]] = dict(zip(keys, parts))
            try:
                out[parts[0]]["network_sample"] = json.loads(parts[-2])
            except ValueError:
                out[parts[0]].pop("network_sample")
    return out


def job_states(cfg, recs):
    by_host = {}
    for r in recs:
        key = (r["host"], r.get("user_id"), r.get("worker_root"))
        if key not in by_host:
            by_host[key] = (cli.job_host(cfg, r), [])
        by_host[key][1].append(r["job"])
    states = {}
    for host, jobs in by_host.values():
        states.update(cli.remote_state(host, jobs))
    for rec in recs:
        state = states.get(rec["job"], {})
        if rec.get("dispatch_state") == "preparation_failed" and state.get("state") in ("starting", "missing"):
            state["state"] = "dead"
    return states


def describe(rec, st):
    s = st.get("state", "?")
    bits = [f"{rec['job']}  [{rec['host']}]  {s}"]
    if rec.get("user_id"):
        bits.append("ID=" + rec["user_id"])
    if rec.get("dispatch_state") == "preparation_failed":
        bits.append("preparation failed; registered for rdispatch clean")
    if s == "done":
        code = st.get("exit")
        label = {"0": "ok", "124": "timed out", "97": "setup failed", "95": "environment fingerprint mismatch", "98": "profile/auth setup failed", "96": "worktree missing", "143": "stopped"}.get(code, "failed")
        bits.append(f"exit {code} ({label})")
        if rec.get("handoff_version"):
            bits.append("等待发起方收取与判断；检查状态以结果回执为准")
    if rec.get("artifact_paths"):
        bits.append("产物 " + ("待拉回" if artifacts.artifacts_pending(rec) else "已处理") + f"（{len(rec['artifact_paths'])} 项）")
        if artifacts.artifacts_pending(rec) and warm.is_warm(rec):
            bits.append(warm.ARTIFACT_PIN_TEXT)
    if st.get("setup") == "pending" and s == "running":
        bits.append("running setup")
    if st.get("head"):
        bits.append("no new commits" if st["head"] == rec["base"] else f"new commits (head {st['head'][:10]})")
    if st.get("dirty") not in (None, "", "0"):
        bits.append(f"{st['dirty']} uncommitted file(s) left in worktree")
    if rec["sync"] == "origin" and st.get("push_rc") not in (None, "", "0"):
        bits.append("PUSH FAILED (see: rdispatch logs JOB --file push.log)")
    line = "  ".join(bits)
    if s == "running" and st.get("last"):
        line += f"\n    last output: {st['last']}"
    return line


def scoped_jobs(a, cfg=None, *, include_legacy=False, by_project=False):
    """跨发起会话查当前员工；项目显式指定，否则在当前仓库内筛选。

    by_project：不指定 --project 时取当前仓库的项目 ID 选择派单（同项目其他 worktree 派出的派单也在内）。"""
    uid = cli.require_user()
    recs = [cli.load_job(a.job)] if getattr(a, "job", None) else cli.all_jobs()
    recs = [r for r in recs if r.get("user_id") == uid or (include_legacy and not r.get("user_id"))]
    project = getattr(a, "project", None)
    if project:
        inferred = cli.queries(cfg).jobs(recs, {}, user_id=uid)
        recs = [rec for rec, row in zip(recs, inferred) if row["project"] == project]
    directory = cli.get_dir(a)
    if directory or not project:
        probe = cli.run(["git", "-C", directory or os.getcwd(), "rev-parse", "--show-toplevel"], check=False)
        if probe.returncode == 0:
            repo = Path(probe.stdout.strip()).resolve()
            if by_project:
                if not project:
                    name = cli.project_name(str(repo))[0]
                    inferred = cli.queries(cfg).jobs(recs, {}, user_id=uid)
                    recs = [rec for rec, row in zip(recs, inferred) if row["project"] == name]
            else:
                recs = [r for r in recs if Path(r["repo"]).resolve() == repo]
        elif directory:
            cli.die("指定目录不是 Git 仓库。", 4)
    return recs


def merge_check(rec, head, into):
    """只检查本地对象与祖先关系；不隐式 fetch，也不猜合入目标。"""
    ref = into or rec.get("source_branch")
    reasons = []
    target = None
    blocker = workline.merge_blocker(rec)
    if blocker:
        reasons.append("未合入：" + blocker)
    if not ref:
        reasons.append("需指定 --into")
    else:
        reference = ref if into else "refs/heads/" + ref
        resolved = cli.git(["rev-parse", "-q", "--verify", "--end-of-options", reference + "^{commit}"], rec["repo"], check=False)
        if resolved.returncode:
            reasons.append(f"REF 不存在或不是提交：{ref}")
        else:
            target = resolved.stdout.strip()
    if not head:
        reasons.append("无法核验远端 head")
    elif head != rec["base"]:
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head) or cli.git(["cat-file", "-e", head + "^{commit}"], rec["repo"], check=False).returncode:
            reasons.append("未取回")
        elif target:
            compared = cli.git(["merge-base", "--is-ancestor", head, target], rec["repo"], check=False)
            if compared.returncode:
                reasons.append("未合入 " + ref if compared.returncode == 1 else "无法核验合入关系")
    return reasons


def cmd_timings(a, cfg):
    """按项目与目标机列出已保存回执里的 setup 与主体耗时（只读，供门禁选机读取）。"""
    uid = cli.require_user()
    records = [rec for rec in cli.all_jobs() if rec.get("user_id") in (None, "", uid)]
    rows = timings.history(cli.STATE, records, project=a.project, host=a.host, kind=a.kind)
    print(json.dumps(dict(rows=rows, median_body_seconds_by_host=timings.median_body_by_host(rows, success_only=True)),
                     ensure_ascii=False, indent=2))


def cmd_status(a, cfg):
    unmerged = getattr(a, "unmerged", False)
    if getattr(a, "into", None) and not unmerged:
        cli.die("--into 需要 --unmerged。", 4)
    filtered = unmerged or getattr(a, "project", None) or cli.get_dir(a)
    recs = cli.scoped_jobs(a, cfg, include_legacy=True) if filtered else ([cli.load_job(a.job)] if a.job else [r for r in cli.all_jobs() if not r.get("user_id") or r["user_id"] == cli.require_user()])
    if not recs:
        print("no jobs.")
        return
    states = cli.job_states(cfg, recs)
    if unmerged:
        selected = []
        for rec in recs:
            snapshot = states.get(rec["job"], {})
            head = snapshot.get("head")
            if head and head != rec["base"]:
                reasons = cli.merge_check(rec, head, getattr(a, "into", None))
                if reasons:
                    states[rec["job"]] = dict(snapshot, merge_status="；".join(reasons))
                    selected.append(rec)
        recs = selected
    if getattr(a, "details", False):
        output = []
        for rec in recs:
            cli.require_initiator(rec, a)
            snapshot = states.get(rec["job"], {})
            output.append(cli.queries(cfg).details(rec, snapshot, user_id=cli.require_user()))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return
    if a.json:
        print(json.dumps(cli.queries(cfg).jobs(recs, states, user_id=cli.require_user()), indent=2, ensure_ascii=False))
        return
    for r in recs:
        snapshot = states.get(r["job"], {})
        print(cli.describe(r, snapshot) + ("  " + snapshot["merge_status"] if "merge_status" in snapshot else ""))


def cmd_wait(a, cfg):
    rec = cli.load_job(a.job)
    deadline = time.time() + a.timeout if a.timeout else None
    while True:
        st = cli.job_states(cfg, [rec]).get(rec["job"], {})
        if st.get("state") in ("done", "dead", "missing"):
            print(cli.describe(rec, st))
            pulled = cli.auto_pull_artifacts(cfg, rec, st)
            if pulled is not None:
                print("\n".join(["产物取回："] + artifacts.artifact_lines(pulled)))
            sys.exit(0 if st.get("exit") == "0" else 1)
        if deadline and time.time() > deadline:
            print(cli.describe(rec, st))
            sys.exit(2)
        time.sleep(a.interval)


def network_stalls(recs, states, threshold):
    """跨 watch 唤醒保留计时；新进展、日志轮次变化/截断会重置。"""
    stalled = []
    now = time.time()
    for rec in recs:
        state = states.get(rec["job"], {})
        sample = state.get("network_sample")
        if state.get("state") != "running" or not sample:
            continue
        key = hashlib.sha256(json.dumps([rec.get(k) for k in ("host", "user_id", "worker_root", "job")]).encode()).hexdigest()
        path = Path(cli.STATE).resolve() / "network-watch" / (key + ".json")
        old = messages.read_json(path) or {}
        previous = old.get("sample", {})
        reset = old.get("round") != state.get("round") or not previous
        for name, stream in sample.items():
            prior_size = previous.get(name, {}).get("size", 0)
            # 窗口外的新数据可能包含进展，宁可重新计时，不据丢失的正文报警。
            reset = reset or stream["size"] < prior_size or stream["base"] > prior_size or stream["progress"] > prior_size
        reset = reset or any(old.get(key) != state.get(key) for key in ("head", "dirty"))
        reconnect = any(stream["reconnect"] > stream["progress"] for stream in sample.values())
        new_reconnect = any(stream["reconnect"] > previous.get(name, {}).get("size", 0) for name, stream in sample.items())
        since = old.get("since")
        if reset or since is None:
            since = now if reconnect and new_reconnect else None
        elif not reconnect:
            since = None
        else:
            since = min(now, since)
        messages.atomic_json(path, dict(sample=sample, round=state.get("round"), since=since,
                                        head=state.get("head"), dirty=state.get("dirty")))
        if since is not None and now - since >= threshold:
            stalled.append(rec["job"])
    return stalled


def cmd_watch(a, cfg):
    """Wake the initiating harness for one shared progress check or a completion."""
    timeout = a.timeout if a.timeout is not None else cfg.report_interval()
    if timeout <= 0:
        cli.die("watch timeout must be positive", 4)
    uid = cli.require_user()
    recs = [cli.load_job(job) for job in dict.fromkeys(a.jobs)]
    if any(rec.get("user_id") != uid for rec in recs):
        cli.die("watch only accepts jobs belonging to RD_USER_ID", 4)
    stall_seconds = cfg.network_stall_seconds() if cfg is not None and hasattr(cfg, "cp") else health.positive_seconds(
        os.environ.get("RD_NETWORK_STALL_SECONDS") or 600, "RD_NETWORK_STALL_SECONDS")
    deadline = time.monotonic() + timeout
    while True:
        states = cli.job_states(cfg, recs)
        finished = [rec["job"] for rec in recs if states.get(rec["job"], {}).get("state") in ("done", "dead", "missing")]
        unavailable = any(states.get(rec["job"], {}).get("state", "unreachable") == "unreachable" for rec in recs)
        stalled = cli.network_stalls(recs, states, stall_seconds)
        now = time.monotonic()
        keep = getattr(a, "keep_on_disconnect", False)
        event = "monitor_error" if stalled else "task_finished" if finished else "monitor_error" if unavailable and not keep else "progress_due" if now >= deadline else None
        if event:
            jobs = [{key: rec[key] for key in ("job", "host", "project", "branch") if key in rec} for rec in recs]
            for rec, snapshot in zip(recs, jobs):
                state = states.get(rec["job"], {})
                snapshot.update({key: state[key] for key in ("state", "exit", "round", "timed_out", "push_rc") if key in state})
                snapshot.setdefault("state", "unreachable")
                if rec["job"] in stalled:
                    snapshot.update(monitor_reason="network_reconnect_stalled", network_stall_seconds=stall_seconds,
                                    action="请检查远端外网、代理及模型服务连接；任务仍在运行，未自动停止。")
                if rec.get("handoff_version"):
                    snapshot.update(initiator_session=rec.get("initiator_session"),
                                    result_ready=state.get("state") == "done", needs_initiator_review=True)
                    try:
                        number = int(state.get("round") or rec["rounds"])
                        reader = cli.queries(cfg)
                        progress = reader.report(rec, number, "progress") if state.get("state") != "unreachable" else None
                        receipt = reader.result(rec, number, state)
                        snapshot.update(progress={key: progress[key] for key in ("phase", "summary", "updated_at")
                                                  if key in progress} if progress else None,
                                        result_summary=messages.result_summary(receipt) if receipt else None)
                    except (ValueError, OSError, SystemExit, subprocess.SubprocessError):
                        snapshot.update(progress=None, result_summary=None, summary_unavailable=True)
            print(json.dumps({"event": event, "finished": finished, "jobs": jobs}, ensure_ascii=False), flush=True)
            failed = bool(stalled) or (unavailable and not keep) or any(states[job].get("exit") != "0" for job in finished)
            sys.exit(1 if failed else 0 if finished else 2)
        time.sleep(min(30, deadline - now))


def cmd_logs(a, cfg):
    rec = cli.load_job(a.job)
    host = cli.job_host(cfg, rec)
    rnd = a.round or rec["rounds"]
    name = a.file or {"out": f"stdout.r{rnd}.log", "err": f"stderr.r{rnd}.log", "setup": "setup.log",
                      "last": f"last.r{rnd}.txt"}[a.stream]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        cli.die("bad file name")
    if host["platform"] == "windows":
        print(native.request(host, "logs", {"job": rec["job"], "file": name, "lines": int(a.lines)}))
        return
    r = cli.ssh(host, f'tail -n {int(a.lines)} "$ROOT/jobs/{rec["job"]}/{name}"', check=False)
    sys.stdout.write(r.stdout if r.returncode == 0 else f"(no {name} yet)\n")
