"""回收：统一回收判定、clean／keep／slim。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tarfile
import time
from . import rd_native as native
from . import rd_messages as messages
from . import rd_slim as slim
from . import rd_warm as warm
from . import rd_include_guard as include_guard
from . import rd_artifacts as artifacts
from . import rd_plan as plans
from . import rd_line as workline
from . import cli

__all__ = [
    "backup_reports",
    "blocking_entries",
    "cmd_clean",
    "cmd_clean_batch",
    "cmd_clean_merged",
    "cmd_keep",
    "cmd_slim",
    "disk_low",
    "expiry_verdict",
    "include_changed",
    "last_activity",
    "line_lapsed",
    "merge_block_code",
    "pending_results",
    "plan_referrers",
    "reclaim_check",
    "remove_job",
    "retention_verdict",
    "slim_block",
    "slim_summary",
    "slim_worktree",
    "source_missing_report",
    "worktree_changes",
]


def backup_reports(host, rec):
    root = Path(os.environ.get("RD_REPORTS_DIR") or "~/.local/share/remote-dispatch/reports").expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    job = messages.job_id(rec["job"])
    destination = root / job
    if destination.exists():
        destination = root / (job + "-" + str(time.time_ns()))
    with tempfile.TemporaryDirectory(prefix=".reports-", dir=root) as temporary:
        staged = Path(temporary) / "reports"
        staged.mkdir()
        with tempfile.TemporaryFile() as archive:
            if host["platform"] == "windows":
                native.request(host, "reports-bundle", {"job": job}, output=archive)
            else:
                cli.ssh(host, f'D="$ROOT/jobs/{job}/reports"; '
                          'if [ -L "$D" ]; then echo "reports is a symlink" >&2; exit 1; '
                          'elif [ -d "$D" ]; then tar -cf - -C "$D" .; '
                          'elif [ -e "$D" ]; then exit 1; else tar -cf - -T /dev/null; fi', stdout=archive)
            archive.seek(0)
            native.extract(archive, staged)
        staged.rename(destination)
    print(f"reports saved: {destination}")
    return str(destination)


def worktree_changes(host, rec):
    """实时读取 head 和完整状态条目 [(XY, 路径)]，NUL 分隔保留空格、换行及重命名两端。"""
    if host["platform"] == "windows":
        info = native.request(host, "clean-check", {"job": rec["job"], "detailed": True})
        head, status = info["head"], info["status"]
    else:
        messages.job_id(rec["job"])
        # 热槽位已被下一任务接手时读交接快照，不读槽位现场。
        reply = cli.ssh(host, warm.resolve_sh(rec) +
                    'if [ -z "$WT" ]; then [ -f "$FSD/final-head" ] && [ -f "$FSD/final-status.z" ] || exit 1; '
                    'cat "$FSD/final-head" "$FSD/final-status.z"; exit 0; fi\n'
                    'head=$(git -C "$WT" rev-parse HEAD) || exit 1; '
                    'printf "%s\\n" "$head"; '
                    'GIT_OPTIONAL_LOCKS=0 git -C "$WT" status --porcelain=v1 -z --untracked-files=all', check=False)
        if not reply or reply.returncode:
            raise ValueError("无法核验 worktree 的 head 与未提交内容")
        head, separator, status = reply.stdout.partition("\n")
        if not separator:
            raise ValueError("worktree 检查响应无效")
    paths = []
    entries = iter(status.split("\0"))
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise ValueError("worktree 路径状态无效")
        paths.append((entry[:2], entry[3:]))
        if "R" in entry[:2] or "C" in entry[:2]:
            original = next(entries, "")
            if not original:
                raise ValueError("worktree 重命名路径状态无效")
            paths.append((entry[:2], original))
    return head, paths


def blocking_entries(rec, entries):
    """去掉已声明为可再生的未跟踪路径；已跟踪文件的改动和未声明的未跟踪文件仍然算未提交内容。"""
    declared = rec.get("regenerable_paths") or []
    return [path for code, path in entries
            if not (code == "??" and any(path == item or path.startswith(item + "/") for item in declared))]


def pending_results(cfg, rec, snapshot):
    unlaunched = rec.get("dispatch_state") in ("preparing", "preparation_failed")
    if rec.get("handoff_version") and not unlaunched:
        receipt = cli.collect_result(cfg, rec, snapshot)
        if not receipt or cli.delivery(rec, receipt) != "received":
            return True
    for path in cli.result_path(rec, 1).parent.glob("r*.json"):
        if re.fullmatch(r"r[0-9]+\.json", path.name):
            receipt = messages.read_json(path)
            if receipt and cli.delivery(rec, receipt) != "received":
                return True
    return False


def last_activity(rec):
    """最后一次回执确认时间；旧任务没有回执时退回任务登记的更新时间。"""
    acks = [path.stat().st_mtime for path in cli.result_path(rec, 1).parent.glob("r*.ack.json")]
    return max(acks) if acks else os.path.getmtime(cli.job_path(rec["job"]))


def disk_low(host):
    """目标机可用磁盘低于 min_free_disk_gb 时收紧保留期；无法读取时按不紧张处理。"""
    if host["platform"] == "windows":
        return False
    reply = cli.ssh(host, 'd="$ROOT"; [ -d "$d" ] || d="$HOME"; df -Pk "$d" | awk \'NR==2{printf "%d",$4/1048576}\'', check=False, timeout=30)
    free = cli.num(reply.stdout.strip()) if reply and reply.returncode == 0 else None
    return free is not None and free < float(host["min_free_disk_gb"])


def merge_block_code(reason):
    """把 merge_check 的原因文本归入枚举值。"""
    for prefix, code in (("未取回", cli.BLOCK_NOT_FETCHED), ("无法核验", cli.BLOCK_UNVERIFIABLE), ("未合入", cli.BLOCK_NOT_MERGED),
                         ("需指定 --into", cli.BLOCK_INTO_REQUIRED), ("REF 不存在", cli.BLOCK_REF_INVALID)):
        if reason.startswith(prefix):
            return code
    return cli.BLOCK_UNVERIFIABLE


def source_missing_report(host, rec):
    """源仓库已删除时，用目标机 bare 仓库核对 worktree 提交是否仍可取回或已合入；返回给人看的核对结果。"""
    prefix = f"源仓库不存在：{rec['repo']}；"
    if host["platform"] == "windows":
        return prefix + "Windows 目标机无法核对 bare 仓库，请人工确认提交已取回或不再需要。"
    j, b = rec["job"], rec["branch"]
    bare = f'"$ROOT/{rec.get("bare", "repos/" + rec["project"] + ".git")}"'
    messages.job_id(j)
    try:
        r = cli.ssh(host, warm.resolve_sh(rec) +
                      'if [ -n "$WT" ]; then head=$(git -C "$WT" rev-parse HEAD 2>/dev/null); '
                      'else head=$(cat "$FSD/final-head" 2>/dev/null); fi; echo "head=$head"; '
                      f'if git -C {bare} rev-parse --git-dir >/dev/null 2>&1; then echo bare=yes; '
                      f'echo "tip=$(git -C {bare} rev-parse -q --verify {shlex.quote("refs/heads/" + b)} 2>/dev/null)"; '
                      f'if [ -n "$head" ] && git -C {bare} cat-file -e "$head^{{commit}}" 2>/dev/null; then echo present=yes; '
                      f'echo "merged=$(git -C {bare} for-each-ref --contains "$head" --format="%(refname:short)" refs/heads '
                      f'| grep -vxF {shlex.quote(b)} | head -3 | tr "\\n" " ")"; fi; fi', check=False)
    except (OSError, ValueError, SystemExit, subprocess.SubprocessError) as error:
        return prefix + f"bare 仓库核对失败：{error}"
    fields = dict(line.partition("=")[::2] for line in (r.stdout or "").splitlines()) if r and not r.returncode else {}
    head, tip = fields.get("head", ""), fields.get("tip", "")
    if fields.get("bare") != "yes":
        return prefix + "目标机上的 bare 仓库不存在或无法读取，无法核对。"
    if not head:
        return prefix + f"worktree 已不可读，无法核对；bare 仓库分支 {b} " + (f"位于 {tip[:12]}。" if tip else "不存在。")
    if head == rec["base"]:
        return prefix + "目标机 bare 仓库核对：worktree 没有新提交，删除不丢提交。"
    merged = fields.get("merged", "").strip()
    if fields.get("present") != "yes":
        return prefix + f"目标机 bare 仓库核对：缺少 worktree head {head[:12]} 的提交，删除后无法恢复。"
    if head == tip:
        return prefix + f"目标机 bare 仓库核对：分支 {b} 已含 worktree head {head[:12]}，提交可从 bare 取回。"
    where = f"，已合入 bare 分支 {merged}" if merged else "，但不在任何分支上"
    return prefix + f"目标机 bare 仓库核对：有 worktree head {head[:12]} 的提交{where}；分支 {b} 位于 {tip[:12] or '无'}。"


def reclaim_check(cfg, rec, snapshot, host, *, single=False, force=False, a=None, into=None, mode="merged", confirmed=False):
    """回收安全条件的唯一判定点，单个 clean 与批量清理共用。

    返回 (code, detail, head, reasons)：code 为 None 表示安全条件全部满足。
    single 对应 `rdispatch clean JOB`（可被 force 跳过部分检查）；批量按 mode 区分 merged / expired。
    reasons 是 merge_check 的原文，供保留期分级使用。"""
    state = snapshot.get("state")
    unlaunched = rec.get("dispatch_state") in ("preparing", "preparation_failed")
    if rec.get("project") and not (single and force):
        referrers = cli.plan_referrers(rec)
        if referrers and mode == "expired" and not single and cli.line_lapsed(cfg, rec, referrers, time.time()):
            referrers = []
        if referrers:
            return cli.BLOCK_PLAN_REFERENCED, "待派登记中后续工作项仍引用该派单：" + "、".join(referrers), None, []
    if state in ("done", "dead") and artifacts.artifacts_pending(rec) and not unlaunched and not (single and force):
        text = "有声明的产物尚未拉回；先 rdispatch fetch JOB --artifacts（--force 可放弃）"
        if warm.is_warm(rec):
            text += "。" + warm.ARTIFACT_PIN_TEXT
        return cli.BLOCK_ARTIFACTS_PENDING, text, None, []
    if single:
        if state == "running" or (state == "starting" and not unlaunched):
            return cli.BLOCK_RUNNING, "the job is still running; rdispatch stop it first", None, []
        if state == "unreachable":
            return cli.BLOCK_UNREACHABLE, f"{host['name']} is unreachable; nothing was removed", None, []
        if rec.get("handoff_version") and not force and not unlaunched:
            cli.require_initiator(rec, a)
            receipt = cli.collect_result(cfg, rec, snapshot)
            if not receipt or cli.delivery(rec, receipt) != "received":
                return cli.BLOCK_RECEIPT_PENDING, "先用 rdispatch result 收取结果，再用 rdispatch ack 确认；未交付回执不清理。", None, []
        if not Path(rec["repo"]).is_dir() and not (force or confirmed):
            return cli.BLOCK_NEEDS_CONFIRM, cli.source_missing_report(host, rec), None, []
        if state == "missing" or force:
            return None, "", None, []
        j, b = rec["job"], rec["branch"]
        if host["platform"] == "windows":
            info = native.request(host, "clean-check", {"job": j})
            orphan, dirty, head = False, str(info["dirty"]), info["head"]
        else:
            messages.job_id(j)
            w = '"$WT"'
            r = cli.ssh(host, warm.resolve_sh(rec) +
                          # 热槽位已被接手：读交接快照；快照也没有则按无法核验处理。
                          'if [ -z "$WT" ]; then if [ -f "$FSD/final-head" ]; then '
                          'echo "dirty=$(cat "$FSD/final-dirty" 2>/dev/null || echo 0)"; echo "head=$(cat "$FSD/final-head")"; '
                          'else echo orphan; fi; '
                          f'elif [ ! -e {w} ] || [ "$(git -C {w} rev-parse --show-toplevel 2>/dev/null)" = "$(cd {w} && pwd -P)" ]; then '
                          f'echo "dirty=$(git -C {w} status --porcelain 2>/dev/null | wc -l | tr -d " ")"; '
                          f'echo "head=$(git -C {w} rev-parse HEAD 2>/dev/null)"; else echo orphan; fi', check=False)
            fields = dict(line.partition("=")[::2] for line in (r.stdout or "").splitlines())
            orphan = "orphan" in fields or r.stdout.strip() == "orphan"
            dirty, head = fields.get("dirty", "0"), fields.get("head", "")
        if orphan:
            return cli.BLOCK_ORPHAN, (f"{j} 的 worktree 目录已不在 git worktree 登记中或无法读取，无法核验其中是否有未提交内容。"
                                  "确认可以丢弃后用 --force。"), head, []
        if dirty.strip() != "0" and rec.get("regenerable_paths"):
            try:
                dirty = str(len(cli.blocking_entries(rec, cli.worktree_changes(host, rec)[1])))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                return cli.BLOCK_UNVERIFIABLE, f"核验未提交内容失败，保留未删除内容与登记：{error}", head, []
        if dirty.strip() != "0":
            return cli.BLOCK_DIRTY, f"{dirty.strip()} uncommitted file(s) in the remote worktree would be lost. Check them, or --force.", head, []
        if confirmed:
            return None, "", head, []
        have = cli.git(["rev-parse", "-q", "--verify", f"refs/remotes/rd/{b}"], rec["repo"], check=False).stdout.strip()
        if head and head != rec["base"] and head != have:
            return cli.BLOCK_NOT_FETCHED, "the job has commits you have not fetched. Run rdispatch fetch first, or --force.", head, []
        return None, "", head, []
    if state == "running":
        return cli.BLOCK_RUNNING, "任务运行中", None, []
    if state == "starting" or rec.get("dispatch_state") == "preparing":
        return cli.BLOCK_PREPARING, "任务准备中或启动结果待核实", None, []
    if state not in ("done", "dead"):
        if state == "unreachable":
            return cli.BLOCK_UNREACHABLE, "连接不可达", None, []
        return cli.BLOCK_NOT_FINISHED, "未确认任务已结束", None, []
    if not Path(rec["repo"]).is_dir():
        return cli.BLOCK_NEEDS_CONFIRM, cli.source_missing_report(host, rec) + "批量清理不代为确认；改用 rdispatch clean JOB。", None, []
    try:
        head, entries = cli.worktree_changes(host, rec)
        paths = cli.blocking_entries(rec, entries)
        reasons = cli.merge_check(rec, head, into)
    except (OSError, ValueError, SystemExit, subprocess.SubprocessError) as error:
        return cli.BLOCK_UNVERIFIABLE, f"核验或清理失败，保留未删除内容与登记：{error}", None, []
    unsafe = [r for r in reasons if r.startswith(("未取回", "无法核验"))]
    if unsafe or (reasons and mode == "merged"):
        chosen = unsafe or reasons
        return cli.merge_block_code(chosen[0]), "；".join(chosen), head, reasons
    try:
        if cli.pending_results(cfg, rec, snapshot):
            return cli.BLOCK_RECEIPT_PENDING, "结果回执待收取；先用 rdispatch result / rdispatch ack", head, reasons
    except (OSError, ValueError, SystemExit, subprocess.SubprocessError) as error:
        return cli.BLOCK_UNVERIFIABLE, f"核验或清理失败，保留未删除内容与登记：{error}", head, reasons
    included = rec.get("include_paths", [])
    unexpected = [p for p in paths if not include_guard.covers(included, p)]
    inside = [p for p in paths if include_guard.covers(included, p)]
    if inside and rec.get("include_hashes") is not None and host["platform"] != "windows":
        # --include 目录里只豁免内容与派单时一致的原输入；新增或改动的文件是执行方的成果，按未提交内容阻塞。
        try:
            unexpected += cli.include_changed(host, rec, inside)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return cli.BLOCK_UNVERIFIABLE, f"核验 --include 输入内容失败，保留未删除内容与登记：{error}", head, reasons
    if unexpected:
        return cli.BLOCK_DIRTY, f"存在非 --include 路径的未提交内容：{unexpected!r}", head, reasons
    return None, "", head, reasons


def include_changed(host, rec, paths):
    """目标机上逐个散列 --include 目录里的未提交文件，返回内容与派单记录不一致的路径。
    热槽位已被接手（现场不在）时无内容可核对，也没有可再丢失的内容，沿用目录豁免。"""
    reply = cli.ssh(host, warm.resolve_sh(rec) + 'if [ -z "$WT" ]; then echo @@gone; exit 0; fi\ncd "$WT" || exit 1\n'
                + include_guard.remote_hash_script(paths), check=False, timeout=120)
    if not reply or reply.returncode:
        raise ValueError("无法读取目标机上的 --include 文件")
    lines = reply.stdout.splitlines()
    if lines[:1] == ["@@gone"]:
        return []
    return include_guard.changed_paths(paths, rec["include_hashes"], lines)


def plan_referrers(rec):
    return plans.PlanStore(cli.STATE, rec.get("user_id") or cli.require_user(), rec["project"]).referrers(rec["job"])


def line_lapsed(cfg, rec, referrers, now):
    """工作线停滞后继续无人处理：不再因待派工作项保留，按「已取回未合入」的保留期处理。"""
    try:
        return workline.abandoned(rec, referrers, cli.last_activity(rec), now, cfg.line_stale_days())
    except OSError:
        return False


def retention_verdict(rec, head, reasons, activity, hours, now, lapsed=False):
    """返回 (可回收, 说明, 阻塞码)。只判断复用可能性；未取回、未提交等安全条件由 reclaim_check 先行拦截。"""
    if rec.get("keep_until", 0) > now:
        return False, "已用 rdispatch keep 钉住，到 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["keep_until"])), cli.BLOCK_PINNED
    if not rec.get("agent"):
        kind, label = "command", "命令任务不能续跑"
    elif lapsed:
        kind, label = "unmerged", "工作线停滞后无人处理，按已取回未合入处理"
    elif head == rec["base"] or not reasons:
        kind, label = "accepted", "已合入或无新提交"
    else:
        kind, label = "unmerged", "已取回未合入（" + "；".join(reasons) + "）"
    age = (now - activity) / 3600
    if age < hours[kind]:
        return False, f"{label}，保留期 {hours[kind]:g} 小时，还剩 {hours[kind] - age:.1f} 小时", cli.BLOCK_RETENTION
    return True, f"{label}，最后确认于 {age:.1f} 小时前，超过保留期 {hours[kind]:g} 小时", None


def expiry_verdict(cfg, rec, head, reasons, host, now, pressure=None):
    """clean --expired 与 serve 定时回收共用的保留期判定：含工作线停滞与磁盘紧张收紧。
    pressure 是 {(host, worker_root): bool} 缓存，避免同一目标机重复探测磁盘。返回 (可回收, 说明, 阻塞码)。"""
    pressure = {} if pressure is None else pressure
    hours = cfg.retention_hours()
    key = (rec["host"], rec.get("worker_root"))
    if key not in pressure:
        pressure[key] = cli.disk_low(host)
    limits = {k: min(v, cli.RETENTION_LOW_DISK_HOURS[k]) for k, v in hours.items()} if pressure[key] else hours
    lapsed = bool(rec.get("project")) and cli.line_lapsed(cfg, rec, cli.plan_referrers(rec), now)
    expired, note, code = cli.retention_verdict(rec, head, reasons, cli.last_activity(rec), limits, now, lapsed)
    if pressure[key]:
        note += "；目标机磁盘不足，保留期已收紧"
    return expired, note, code


def cmd_clean_batch(a, cfg, mode):
    """--merged 只看合入；--expired 另按续跑可能性分级保留，过期才回收。两者共用 reclaim_check 的安全条件。"""
    if a.job or a.force or a.delete_branch or getattr(a, "discard_reports", False):
        cli.die(f"--{mode} 不与 job、--force、--delete-branch 或 --discard-reports 混用。", 4)
    as_json = getattr(a, "json", False)
    if as_json and not getattr(a, "dry_run", False):
        cli.die("--json 只能与 --dry-run 一起使用。", 4)
    recs = cli.scoped_jobs(a, cfg, by_project=True)
    if not recs:
        print("[]" if as_json else "no jobs.")
        return
    states = cli.job_states(cfg, recs)
    pressure = {}
    now = time.time()
    rows = []

    def report(j, code, text, line):
        rows.append(dict(job=j, reclaimable=code == cli.RECLAIM_OK, code=code, reason=text))
        if not as_json:
            print(line)

    for rec in recs:
        j = rec["job"]
        snapshot = states.get(j, {})
        try:
            host = cli.job_host(cfg, rec)
            code, detail, head, reasons = cli.reclaim_check(cfg, rec, snapshot, host, a=a, into=getattr(a, "into", None), mode=mode)
            if code:
                report(j, code, detail, f"skip {j} [{code}]: {detail}")
                continue
            note = "reports 将保存，分支保留"
            if mode == "expired":
                expired, note, code = cli.expiry_verdict(cfg, rec, head, reasons, host, now, pressure)
                if not expired:
                    report(j, code, note, f"keep {j} [{code}]: {note}")
                    continue
            if getattr(a, "dry_run", False):
                report(j, cli.RECLAIM_OK, note, f"would clean {j} [{cli.RECLAIM_OK}] ({note})")
            else:
                print(f"clean {j}: {note}")
                cli.remove_job(a, rec, host)
        except (OSError, ValueError, SystemExit, subprocess.SubprocessError) as error:
            text = f"核验或清理失败，保留未删除内容与登记：{error}"
            report(j, cli.BLOCK_UNVERIFIABLE, text, f"skip {j} [{cli.BLOCK_UNVERIFIABLE}]: {text}")
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_clean_merged(a, cfg):
    return cli.cmd_clean_batch(a, cfg, "merged")


@cli.locked_job
def cmd_keep(a, cfg):
    rec = cli.load_job(a.job)
    if rec.get("user_id") and rec["user_id"] != cli.require_user():
        cli.die("只能钉住当前 RD_USER_ID 的任务。", 4)
    if a.hours < 0:
        cli.die("--hours 必须是非负数；0 表示取消钉住。", 4)
    if a.hours == 0:
        rec.pop("keep_until", None)
        print(f"{rec['job']}: 已取消钉住，按默认保留期判断。")
    else:
        rec["keep_until"] = time.time() + a.hours * 3600
        print(f"{rec['job']}: 钉住到 {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['keep_until']))}，rdispatch clean --expired 不回收。")
    cli.save_job(rec)
    if warm.is_warm(rec):
        # 热槽位：把钉住状态同步到目标机，其他控制端的领用判定也要尊重它。
        host = cli.job_host(cfg, rec)
        if host["platform"] != "windows":
            r = cli.ssh(host, warm.pin_script(rec, until=rec.get("keep_until", 0)), check=False, timeout=30)
            if r is None or r.returncode:
                print("警告：未能把钉住状态同步到目标机，其他控制端可能仍会接手该热槽位。", file=sys.stderr)


def slim_block(rec, snapshot):
    """瘦身的前置条件：任务已结束、目标机可达、worktree 在。返回 (阻塞码, 说明) 或 (None, "")。"""
    state = snapshot.get("state")
    if state == "running" or state == "starting" or rec.get("dispatch_state") in ("preparing", "launching"):
        return cli.BLOCK_RUNNING, "任务运行中或准备中，不能瘦身"
    if state == "unreachable":
        return cli.BLOCK_UNREACHABLE, "连接不可达"
    if state == "missing":
        return cli.BLOCK_NOT_FINISHED, "目标机上没有该任务"
    if warm.is_warm(rec):
        return cli.BLOCK_NOT_FINISHED, "热工作树槽位常驻构建缓存，不瘦身（槽位闲置超期由定时回收处理）"
    if not rec.get("regenerable_paths"):
        return cli.BLOCK_NOT_FINISHED, "该任务没有声明 regenerable_paths（项目配置里未声明，或任务早于该功能）"
    return None, ""


def slim_worktree(host, rec, dry_run):
    """在目标机上执行瘦身；返回 (已删/将删 [{path, kb}], 跳过 [{path, reason}])。"""
    j, paths = rec["job"], slim.parse_paths(";".join(rec.get("regenerable_paths") or []))
    if host["platform"] == "windows":
        reply = native.request(host, "slim", {"job": j, "paths": paths, "dry_run": dry_run})
        return reply["removed"], reply["skipped"]
    messages.job_id(j)
    wt, job_dir = warm.worktree_sh(rec), f'"$ROOT/jobs/{j}"'
    drop = ":" if dry_run else f'rm -f {job_dir}/setup.done'
    body = [f'WT={wt}; [ -d "$WT" ] || {{ echo "worktree 不存在" >&2; exit 1; }}; WR=$(cd "$WT" && pwd -P)',
            'skip() { printf "skipped\t%s\t%s\n" "$1" "$2"; }',
            'slim_one() { rel=$1; p="$WT/$rel"; [ -e "$p" ] || [ -L "$p" ] || return 0',
            '  par=$(cd "$(dirname "$p")" 2>/dev/null && pwd -P) || { skip "$rel" "父目录不可读"; return 0; }',
            '  case "$par" in "$WR"|"$WR"/*) ;; *) skip "$rel" "路径越出 worktree"; return 0;; esac',
            '  if [ -n "$(git -C "$WT" ls-files -- "$rel" 2>/dev/null | head -n 1)" ]; then skip "$rel" "含已跟踪文件"; return 0; fi',
            '  kb=$(du -sk "$p" 2>/dev/null | cut -f1)']
    if dry_run:
        body.append('  printf "removed\t%s\t%s\n" "$rel" "${kb:-0}"; }')
    else:
        body += ['  [ -L "$p" ] || chmod -R u+rwx "$p" 2>/dev/null',
                 '  if rm -rf "$p"; then printf "removed\t%s\t%s\n" "$rel" "${kb:-0}"; else skip "$rel" "删除失败"; fi }']
    body += [f'slim_one {shlex.quote(p)}' for p in paths] + [drop]
    reply = cli.ssh(host, "\n".join(body))
    removed, skipped = [], []
    for line in (reply.stdout or "").splitlines():
        verb, _, rest = line.partition("\t")
        rel, _, detail = rest.partition("\t")
        if verb == "removed":
            removed.append(dict(path=rel, kb=int(detail) if detail.isdigit() else 0))
        elif verb == "skipped":
            skipped.append(dict(path=rel, reason=detail))
    return removed, skipped


def slim_summary(removed, skipped):
    text = "、".join(f"{r['path']} {r['kb']} KB" for r in removed) or "没有可删的声明路径"
    if skipped:
        text += "；跳过：" + "、".join(f"{r['path']}（{r['reason']}）" for r in skipped)
    return text


def cmd_slim(a, cfg):
    """clean --slim：只删声明的可再生子路径和 setup 完成标记，保留 worktree、代码与其余文件；下次续跑重新 setup。"""
    if a.merged or getattr(a, "expired", False) or a.force or a.delete_branch or a.discard_reports or getattr(a, "into", None):
        cli.die("--slim 不与 --merged、--expired、--force、--delete-branch、--discard-reports 或 --into 混用。", 4)
    as_json = getattr(a, "json", False)
    dry_run = getattr(a, "dry_run", False)
    if as_json and not dry_run:
        cli.die("--json 只能与 --dry-run 一起使用。", 4)
    if a.job:
        rec = cli.load_job(a.job)
        if rec.get("user_id") and rec["user_id"] != cli.require_user():
            cli.die("只能瘦身当前 RD_USER_ID 的任务。", 4)
        recs = [rec]
    else:
        recs = cli.scoped_jobs(a, cfg)
    if not recs:
        print("[]" if as_json else "no jobs.")
        return
    states = cli.job_states(cfg, recs)
    rows = []
    for rec in recs:
        j = rec["job"]
        row = dict(job=j, slimmable=False, reason="", removed=[], skipped=[])
        code, detail = cli.slim_block(rec, states.get(j, {}))
        if code:
            row["reason"] = detail
            if a.job:
                cli.die(detail, 1)
        else:
            try:
                row["removed"], row["skipped"] = cli.slim_worktree(cli.job_host(cfg, rec), rec, dry_run)
                row["slimmable"] = True
                row["reason"] = cli.slim_summary(row["removed"], row["skipped"])
            except (OSError, ValueError, SystemExit, subprocess.SubprocessError) as error:
                row["reason"] = f"瘦身失败，保留未删除内容：{error}"
                if a.job:
                    cli.die(row["reason"], 1)
        rows.append(row)
        if not as_json:
            verb = "would slim" if dry_run and row["slimmable"] else "slimmed" if row["slimmable"] else "skip"
            print(f"{verb} {j}: {row['reason']}")
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_clean(a, cfg):
    if getattr(a, "slim", False):
        return cli.cmd_slim(a, cfg)
    if getattr(a, "merged", False) and getattr(a, "expired", False):
        cli.die("--merged 与 --expired 只能选一个。", 4)
    if getattr(a, "merged", False):
        return cli.cmd_clean_merged(a, cfg)
    if getattr(a, "expired", False):
        return cli.cmd_clean_batch(a, cfg, "expired")
    if not a.job or getattr(a, "into", None) or getattr(a, "dry_run", False) or getattr(a, "project", None):
        cli.die("请指定 job；--into、--dry-run 和范围筛选需要 --merged 或 --expired。", 4)
    rec = cli.load_job(a.job)
    host = cli.job_host(cfg, rec)
    j = rec["job"]
    st = cli.remote_state(host, [j]).get(j, {})
    code, detail, _, _ = cli.reclaim_check(cfg, rec, st, host, single=True, force=a.force, a=a)
    if code == cli.BLOCK_NEEDS_CONFIRM:
        if not sys.stdin.isatty():
            cli.die(detail + "确认删除请加 --force。", 4)
        print(detail)
        if input("确认删除此派单的目标机 worktree？[y/N] ").strip().lower() not in ("y", "yes"):
            cli.die("未确认，保留 worktree 与登记。", 1)
        code, detail, _, _ = cli.reclaim_check(cfg, rec, st, host, single=True, a=a, confirmed=True)
    elif a.force and not Path(rec["repo"]).is_dir():
        print(cli.source_missing_report(host, rec))
    if code:
        cli.die(detail, 4 if code == cli.BLOCK_RECEIPT_PENDING else 1)
    cli.remove_job(a, rec, host)


def remove_job(a, rec, host):
    """共用报告保存与删除过程，调用方负责各自的清理前置条件。"""
    j, b = rec["job"], rec["branch"]
    bare = rec.get("bare", f"repos/{rec['project']}.git")
    if not getattr(a, "discard_reports", False):
        try:
            rec["reports_backup"] = cli.backup_reports(host, rec)
            cli.save_job(rec)
        except (OSError, ValueError, SystemExit, tarfile.TarError) as error:
            cli.die(f"reports 取回失败，远端与 job 登记均保留：{error}；重试或显式 --discard-reports。")
    delete_worker = a.delete_branch
    delete_origin = a.delete_branch
    if rec.get("dispatch_state"):
        delete_origin = a.delete_branch and rec.get("owns_origin_branch", False)
        if host["platform"] == "windows":
            owned = native.request(host, "branch-owned", {"job": j})
        else:
            owned = cli.ssh(host, f'test -f "$ROOT/jobs/{j}/branch-created"', check=False).returncode == 0
        delete_worker = a.delete_branch and (rec.get("owns_worker_branch", False) or owned)
    if host["platform"] == "windows":
        native.request(host, "clean", {"job": j, "bare": bare, "branch": b, "delete_branch": delete_worker})
    else:
        # 分支被空闲热槽位检出时 git 拒绝删除：先把槽位 detach 再删分支。
        drop = (warm.detach_script(bare, b) +
                f'if git -C "$ROOT/{bare}" show-ref --verify --quiet {shlex.quote("refs/heads/" + b)}; then '
                f'git -C "$ROOT/{bare}" branch -q -D {shlex.quote(b)} || exit 1; fi; ') if delete_worker else ""
        messages.job_id(j)
        if warm.is_warm(rec):
            # 热槽位只释放领用并删日志，不删 worktree。
            cli.ssh(host, warm.release_script(rec) + f'{drop}rm -rf "$ROOT/jobs/{j}"')
        else:
            wt = warm.worktree_sh(rec)
            # 登记已丢失或目录不可读写时 worktree remove 会失败：恢复权限，仍失败则删目录并 prune。
            cli.ssh(host, f'if [ -e {wt} ]; then '
                  f'git -C "$ROOT/{bare}" worktree remove --force {wt} 2>/dev/null || {{ '
                  f'chmod -R u+rwx {wt} 2>/dev/null; '
                  f'git -C "$ROOT/{bare}" worktree remove --force {wt} 2>/dev/null || {{ '
                  f'rm -rf {wt} && [ ! -e {wt} ] && git -C "$ROOT/{bare}" worktree prune; }}; }} || exit 1; fi; '
                  f'{drop}rm -rf "$ROOT/jobs/{j}"')
    if delete_origin and rec["sync"] == "origin":
        cli.git(["push", "origin", "--delete", b], rec["repo"])
    os.remove(cli.job_path(j))
    print(f"cleaned {j}" + (" and deleted branch " + b if delete_worker else f" (branch {b} kept; ownership not confirmed or deletion not requested)"))
