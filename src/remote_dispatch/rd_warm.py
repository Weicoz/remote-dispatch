"""命令任务热工作树：目标机按「用户 + 项目 + 仓库 key」保留固定数量的常驻 worktree，后续命令任务复用。

路径固定为 $ROOT/wt/_warm/<项目>-<仓库key>-<n>，Xcode DerivedData、CMake、venv 这类按绝对路径的缓存才能命中。
任务与槽位分离：派单记录的 worktree 字段是 `cold:<相对路径>` 或 `warm:<槽位名>#<代次>`，
所有读取 worktree 位置的地方统一走 job_worktree / resolve_sh；旧记录没有该字段，按 wt/<job> 冷处理。

领用状态都在目标机上：
- $ROOT/warm/.mutex            领用互斥（mkdir 原子；只在领用与回收脚本里短暂持有）
- $ROOT/warm/<槽位>.claim/     当前占用者：job、at（领用时间）、pid、released（运行结束标记）
- $ROOT/warm/<槽位>.gen        代次计数，每次领用加 1
- $ROOT/jobs/<job>/worktree    该任务实际使用的 worktree 绝对路径（启动脚本与状态查询读取）
- $ROOT/jobs/<job>/final-*     槽位被下一任务接手（或闲置回收）前写下的交接快照

本模块只生成 POSIX sh 脚本与解析结果，不直接连目标机；Windows 目标一律冷。
"""
import os
import re
import shlex

from . import rd_artifacts as artifacts

COLD = "cold"
WARM = "warm"
DEFAULT_SLOTS = 2
DEFAULT_IDLE_DAYS = 7.0
# 交接快照里的补丁上限；超过只存统计。
PATCH_LIMIT = 20 * 1024 * 1024
# 准备期靠目标机上的心跳进程刷新 claim/at：任务启动脚本写下 pid、领用被释放或超过上限时心跳停止；
# at 超过 HEARTBEAT_STALE_SECONDS 没刷新才判定准备者已不在。任何读不出的状态一律按占用处理。
HEARTBEAT_SECONDS = 20
HEARTBEAT_STALE_SECONDS = 180
HEARTBEAT_MAX_SECONDS = 6 * 3600
ARTIFACT_PIN_TEXT = ("热槽位因产物未拉回被占用，其他命令任务不会复用它；"
                     "解除：rdispatch fetch JOB --artifacts，或 rdispatch clean JOB --force 放弃产物")
OFF_VALUES =("off", "false", "0", "no", "disable", "disabled")
RESULT_PREFIX = "rdwarm:"


def cold_field(job):
    return f"{COLD}:wt/{job}"


def warm_field(slot, generation):
    return f"{WARM}:{slot}#{generation}"


def parse_field(rec):
    """返回 (模式, 相对路径或槽位名, 代次或 None)。没有该字段的旧记录按 wt/<job> 冷处理。"""
    value = rec.get("worktree") or ""
    mode, _, rest = value.partition(":")
    if mode == WARM and rest:
        slot, _, generation = rest.partition("#")
        return WARM, slot, generation or None
    if mode == COLD and rest:
        return COLD, rest, None
    return COLD, f"wt/{rec['job']}", None


def is_warm(rec):
    return parse_field(rec)[0] == WARM


def job_worktree(rec):
    """任务 worktree 相对 $ROOT 的路径。热任务返回槽位路径，不检查槽位是否已被后来者接手（见 resolve_sh）。"""
    mode, name, _ = parse_field(rec)
    return f"wt/_warm/{name}" if mode == WARM else name


def worktree_sh(rec):
    """job_worktree 的 shell 双引号形式，放在已定义 $ROOT 的 ssh 脚本里。"""
    return f'"$ROOT/{job_worktree(rec)}"'


def resolve_sh(rec):
    """读取类脚本的前置片段：设置 WT（现场路径）与 FSD（交接快照目录）。

    冷任务只设 WT。热任务在槽位仍属于本任务（代次一致且目录在）时设 WT，否则设 FSD，读 final-* 文件。"""
    mode, name, generation = parse_field(rec)
    if mode == COLD:
        return f'WT="$ROOT/{name}"; FSD=""\n'
    return (f'WT=""; FSD=""\n'
            f'if [ "$(cat "$ROOT/warm/{name}.gen" 2>/dev/null)" = {shlex.quote(str(generation or ""))} ] '
            f'&& [ -d "$ROOT/wt/_warm/{name}" ]; then WT="$ROOT/wt/_warm/{name}"; '
            f'else FSD="$ROOT/jobs/{rec["job"]}"; fi\n')


def slot_name_part(project):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", project or "x").strip("-") or "x"


def feature_enabled(cp):
    """[features] warm_worktree：显式写 false 才整体关闭。"""
    return cp.get("features", "warm_worktree", fallback="true").strip().lower() not in OFF_VALUES


def slot_limit(proj):
    """[project:x] warm_slots；默认 2。非整数或负数抛 ValueError。"""
    text = str(proj.get("warm_slots", "")).strip()
    if not text:
        return DEFAULT_SLOTS
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"[project] warm_slots 必须是非负整数：{text}") from None
    if value < 0:
        raise ValueError(f"[project] warm_slots 必须是非负整数：{text}")
    return value


def eligible(args, proj, host, features_on=True, *, agent=None):
    """命令任务默认热；代理任务、gate 全量门禁、--fresh、warm = off、Windows 目标一律冷。"""
    if agent or getattr(args, "agent", None):
        return False
    if getattr(args, "gate", None) or getattr(args, "fresh", False):
        return False
    if not features_on or host["platform"] == "windows":
        return False
    if str(proj.get("warm", "on")).strip().lower() in OFF_VALUES:
        return False
    return slot_limit(proj) > 0


def idle_days(cp):
    """闲置多少天后可回收槽位：环境变量 RD_RETAIN_WARM_IDLE_DAYS 优先，再读 [retention] warm_idle_days。"""
    value = os.environ.get("RD_RETAIN_WARM_IDLE_DAYS") or cp.get("retention", "warm_idle_days", fallback=str(DEFAULT_IDLE_DAYS))
    try:
        days = float(value)
    except (TypeError, ValueError):
        raise ValueError("RD_RETAIN_WARM_IDLE_DAYS / [retention] warm_idle_days 必须是非负天数") from None
    if not days >= 0:
        raise ValueError("RD_RETAIN_WARM_IDLE_DAYS / [retention] warm_idle_days 必须是非负天数")
    return days


def pinned_jobs(recs, now):
    """不能被接手的热任务：用 keep 钉住的，或声明的产物还没拉回的（产物在槽位里）。"""
    return sorted(rec["job"] for rec in recs
                  if is_warm(rec) and (rec.get("keep_until", 0) > now or artifacts.artifacts_pending(rec)))


# ---------------------------------------------------------------- shell 片段

# 互斥：mkdir 原子；持有者 pid 已不存在才清理，清理本身再用 .reap 互斥并重新核对 pid，
# 两个等待者不会互删对方刚拿到的锁；等不到就交给调用方退回冷路径。
MUTEX_SH = r"""
WARM="$ROOT/warm"; MUT="$WARM/.mutex"
mkdir -p "$WARM" "$ROOT/wt/_warm" || exit 92
rd_unlock() { [ "$(cat "$MUT/pid" 2>/dev/null)" = "$$" ] && rm -rf "$MUT"; }
rd_stale() {
  rd_sp=$(cat "$MUT/pid" 2>/dev/null)
  if [ -n "$rd_sp" ]; then ! kill -0 "$rd_sp" 2>/dev/null; else [ -n "$(find "$MUT" -prune -mmin +1 2>/dev/null)" ]; fi
}
rd_lock() {
  rd_i=0
  until mkdir "$MUT" 2>/dev/null; do
    if rd_stale; then
      if mkdir "$MUT.reap" 2>/dev/null; then
        if rd_stale; then rm -rf "$MUT"; fi
        rmdir "$MUT.reap" 2>/dev/null
      elif [ -n "$(find "$MUT.reap" -prune -mmin +1 2>/dev/null)" ]; then rmdir "$MUT.reap" 2>/dev/null
      fi
      continue
    fi
    rd_i=$((rd_i + 1)); [ "$rd_i" -gt 60 ] && return 1
    sleep 1
  done
  echo $$ > "$MUT/pid"
  trap rd_unlock EXIT; trap 'rd_unlock; exit 1' HUP INT TERM
}
"""

# 占用判定：成功（0）表示仍被占用。PINNED 是控制端传入的被钉住或产物未拉回的任务名（空格分隔）；
# 目标机上的 pinned 标记（产物待拉）与 pinned-until（keep 到期时刻）让其他控制端的钉住也生效。
# 准备期（还没有 pid）看心跳：claim/at 在 STALE 秒内被刷新才算占用，读不出一律按占用。
BUSY_TEMPLATE = r"""
rd_busy() {
  rd_oj=$(cat "$1/job" 2>/dev/null); [ -n "$rd_oj" ] || return 1
  case " $PINNED " in *" $rd_oj "*) return 0;; esac
  rd_d="$ROOT/jobs/$rd_oj"
  [ -d "$rd_d" ] || return 1
  [ -f "$rd_d/pinned" ] && return 0
  rd_u=$(cat "$rd_d/pinned-until" 2>/dev/null)
  if [ -n "$rd_u" ] && [ "$rd_u" -gt "$(date +%s)" ] 2>/dev/null; then return 0; fi
  [ -f "$rd_d/exit_code" ] && return 1
  [ -f "$1/released" ] && return 1
  if [ -f "$rd_d/pid" ]; then kill -0 "$(cat "$rd_d/pid")" 2>/dev/null && return 0; return 1; fi
  # 心跳进程还活着（休眠唤醒后 at 可能暂时过期）就仍是占用；同时核对命令行含领用目录，防 pid 复用。
  rd_hp=$(cat "$1/hb.pid" 2>/dev/null)
  if [ -n "$rd_hp" ] && kill -0 "$rd_hp" 2>/dev/null; then
    case "$(ps -p "$rd_hp" -o command= 2>/dev/null)" in *"$1"*) return 0;; esac
  fi
  rd_at=$(cat "$1/at" 2>/dev/null)
  case "$rd_at" in ''|*[!0-9]*) return 0;; esac
  [ $(( $(date +%s) - rd_at )) -lt @@STALE@@ ] && return 0
  return 1
}
"""


def busy_sh():
    return BUSY_TEMPLATE.replace("@@STALE@@", str(HEARTBEAT_STALE_SECONDS))


# 交接快照：$1 槽位 worktree，$2 上一任务名，$3 原因。每个任务只写一次；任何一步写失败返回 1，
# 并清掉不完整的 final-*，调用方据此放弃接手（槽位保持原状）。
SNAPSHOT_SH = r"""
rd_snapshot() {
  rd_sd="$ROOT/jobs/$2"; [ -d "$rd_sd" ] || return 0; [ -f "$rd_sd/final-state.json" ] && return 0
  rd_snap_fail() { rm -f "$rd_sd"/final-status.z "$rd_sd"/final-untracked.txt "$rd_sd"/final-patch.tmp "$rd_sd"/final.patch "$rd_sd"/final-head "$rd_sd"/final-dirty "$rd_sd"/final-state.json.tmp "$rd_sd"/final-state.json; return 1; }
  rd_h=$(git -C "$1" rev-parse HEAD 2>/dev/null) || rd_h=
  git -C "$1" status --porcelain=v1 -z --untracked-files=all > "$rd_sd/final-status.z" 2>/dev/null || { rd_snap_fail; return 1; }
  rd_n=$(git -C "$1" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  git -C "$1" ls-files -o --exclude-standard 2>/dev/null | head -n 20000 > "$rd_sd/final-untracked.txt" || { rd_snap_fail; return 1; }
  rd_u=$(wc -l < "$rd_sd/final-untracked.txt" | tr -d ' ')
  rd_t=$(git -C "$1" diff --numstat HEAD 2>/dev/null | wc -l | tr -d ' ')
  git -C "$1" diff --binary --full-index HEAD 2>/dev/null | head -c @@LIMIT1@@ > "$rd_sd/final-patch.tmp" || { rd_snap_fail; return 1; }
  rd_sz=$(wc -c < "$rd_sd/final-patch.tmp" | tr -d ' ')
  rd_saved=false; rd_over=false
  if [ "$rd_sz" -gt @@LIMIT@@ ]; then rm -f "$rd_sd/final-patch.tmp"; rd_over=true
  elif [ "$rd_sz" -gt 0 ]; then mv "$rd_sd/final-patch.tmp" "$rd_sd/final.patch" || { rd_snap_fail; return 1; }; rd_saved=true
  else rm -f "$rd_sd/final-patch.tmp"; fi
  printf '{"schema":1,"job":"%s","reason":"%s","captured_at":"%s","head":"%s","dirty":%s,"tracked_changed":%s,"untracked_count":%s,"patch_saved":%s,"patch_over_limit":%s,"patch_bytes":%s,"patch_file":%s,"status_file":"final-status.z","untracked_file":"final-untracked.txt"}\n' \
    "$2" "$3" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rd_h" "${rd_n:-0}" "${rd_t:-0}" "${rd_u:-0}" "$rd_saved" "$rd_over" \
    "$([ "$rd_saved" = true ] && echo "$rd_sz" || echo null)" "$([ "$rd_saved" = true ] && echo '"final.patch"' || echo null)" \
    > "$rd_sd/final-state.json.tmp" || { rd_snap_fail; return 1; }
  { printf '%s\n' "$rd_h" > "$rd_sd/final-head" && printf '%s\n' "$rd_n" > "$rd_sd/final-dirty"; } || { rd_snap_fail; return 1; }
  mv "$rd_sd/final-state.json.tmp" "$rd_sd/final-state.json" || { rd_snap_fail; return 1; }
  if [ ! -s "$rd_sd/final-state.json" ] || [ ! -f "$rd_sd/final-status.z" ] || [ ! -s "$rd_sd/final-head" ] \
     || { [ "$rd_saved" = true ] && [ ! -s "$rd_sd/final.patch" ]; }; then rd_snap_fail; return 1; fi
  return 0
}
""".replace("@@LIMIT1@@", str(PATCH_LIMIT + 1)).replace("@@LIMIT@@", str(PATCH_LIMIT))


def _assign(**values):
    return "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in values.items())


def unpacked_path(slot):
    """本任务解包进槽位的 --include 路径清单；下一次领用前先按它清掉（含被 .gitignore 忽略的文件）。"""
    return f"warm/{slot}.unpacked"


def heartbeat_limit(*timeouts):
    """心跳存活上限（秒）：该次派单的准备超时（setup、指纹、host timeout 取最大）加 10 分钟。"""
    values = [int(float(t)) for t in timeouts if t not in (None, "")]
    return max(values or [0]) + 600


def unpacked_files(listing):
    """从 `tar -tf` 输出得到实际写入槽位的文件相对路径（任何 --include 形式，含 "."）；
    目录、.git、绝对路径与含 .. 的条目丢弃。"""
    out = []
    for line in listing.splitlines():
        path = line[2:] if line.startswith("./") else line
        parts = path.split("/")
        if not path or path.endswith("/") or path.startswith("/") or ".." in parts or parts[0] == ".git":
            continue
        out.append(path)
    return out


def claim_script(prefix, bare, job, branch, slot_prefix, slots, pinned, heartbeat_max=None):
    """一次 ssh 完成：准备（prefix）→ 互斥内领用空闲槽位或在上限内新建 → 交接快照 → 精确快照 → 否则退回冷 worktree。

    快照写不全就不接手（槽位保持原状，退冷）；接手后中途失败先废弃槽位（快照已保障）再退冷，
    不会留下指向被改过的槽位的上一任务状态。领用成功后在目标机起心跳进程，准备期靠它判定占用。

    输出 rdwarm:key=value 行：mode=warm|cold、slot、gen、reused、prev_commit、prev_job、path，冷路径带 reason。"""
    body = prefix + f'BARE="$ROOT/{bare}"\n' + _assign(JOB=job, BRANCH=branch, PREFIX=slot_prefix, MAX=slots,
                                                      PINNED=" ".join(pinned),
                                                      HB_STEP=HEARTBEAT_SECONDS,
                                                      HB_LOOPS=max(1, int(heartbeat_max or HEARTBEAT_MAX_SECONDS)
                                                                   // max(1, HEARTBEAT_SECONDS)))
    body += MUTEX_SH + busy_sh() + SNAPSHOT_SH + r'''
rd_cold() {
  rm -f "$ROOT/jobs/$JOB/worktree" "$ROOT/jobs/$JOB/warm-claim"
  git -C "$BARE" worktree add -q "$ROOT/wt/$JOB" "$BRANCH" || exit 92
  echo "rdwarm:mode=cold"; echo "rdwarm:reason=$1"
  exit 0
}
# 只对 git clean 报错的未跟踪路径放开权限（路径本身及其下内容；上级目录只加目录权限，不递归），
# 不碰跟踪文件，避免把 100644 变成 100755。$1 是 clean 的 stderr 文件。
rd_fix_clean() {
  sed -n -e "s/^warning: could not open directory '\(.*\)': .*/\1/p" -e 's/^warning: failed to remove \(.*\): .*/\1/p' "$1" | while IFS= read -r fp; do
    fp=${fp%/}
    case "$fp" in ''|/*|..|../*|*/../*|*/..) continue;; esac
    d=$fp
    while [ -n "$d" ] && [ "$d" != "." ]; do
      chmod u+rwx "$W/$d" 2>/dev/null
      case "$d" in */*) d=${d%/*};; *) d=.;; esac
    done
    if [ -d "$W/$fp" ]; then chmod -R u+rwx "$W/$fp" 2>/dev/null; fi
  done
}
# 废弃已接手但中途失败的槽位：上一任务的状态已写进交接快照，槽位删除后它读快照。
rd_discard() {
  chmod -R u+rwx "$W" 2>/dev/null
  git -C "$BARE" worktree remove --force "$W" >/dev/null 2>&1 || { rm -rf "$W"; git -C "$BARE" worktree prune >/dev/null 2>&1; }
  [ ! -e "$W" ] && rm -rf "$C"
}
rd_lock || rd_cold mutex_timeout
pick=
for pass in 1 2; do
  n=1
  while [ "$n" -le "$MAX" ]; do
    S="$PREFIX-$n"; n=$((n + 1))
    C="$WARM/$S.claim"
    if [ -d "$C" ] && rd_busy "$C"; then continue; fi
    if [ "$pass" = 1 ] && [ ! -d "$ROOT/wt/_warm/$S" ]; then continue; fi
    pick=$S; break
  done
  [ -n "$pick" ] && break
done
[ -n "$pick" ] || rd_cold slots_full
S=$pick; C="$WARM/$S.claim"; W="$ROOT/wt/_warm/$S"
prev_job=$(cat "$C/job" 2>/dev/null); prev=; reused=0
if [ -d "$W" ] && [ "$(git -C "$W" rev-parse --show-toplevel 2>/dev/null)" = "$(cd "$W" && pwd -P)" ]; then
  reused=1
  prev=$(git -C "$W" rev-parse HEAD 2>/dev/null)
  if [ -n "$prev_job" ]; then rd_snapshot "$W" "$prev_job" handoff || rd_cold snapshot_failed; fi
  gd=$(git -C "$W" rev-parse --absolute-git-dir 2>/dev/null)
  if [ -n "$gd" ] && [ -e "$gd/index.lock" ]; then
    # 占用者已终态；锁超过 1 分钟且确认没有 git 进程引用该目录才删。探测不到 pgrep 视为不确定，不删。
    if command -v pgrep >/dev/null 2>&1 && [ -n "$(find "$gd/index.lock" -mmin +1 2>/dev/null)" ] && ! pgrep -f "git.*$W" >/dev/null 2>&1; then rm -f "$gd/index.lock"; fi
    [ -e "$gd/index.lock" ] && rd_cold index_locked
  fi
  # 上一任务解包的 --include 路径（含被忽略的文件）先清掉，git clean -fd 不碰被忽略的文件。
  if [ -f "$WARM/$S.unpacked" ]; then
    tr '\n' '\0' < "$WARM/$S.unpacked" | xargs -0 git -C "$W" clean -ffdxq -- >/dev/null 2>&1
    rm -f "$WARM/$S.unpacked"
  fi
  git -C "$W" checkout -q -f "$BRANCH" >/dev/null 2>&1 || { rd_discard; rd_cold checkout_failed; }
  rd_tries=0
  while ! git -C "$W" clean -ffdq >/dev/null 2>"$WARM/.clean.$$"; do
    rd_tries=$((rd_tries + 1))
    [ "$rd_tries" -gt 3 ] && { rm -f "$WARM/.clean.$$"; rd_discard; rd_cold clean_failed; }
    rd_fix_clean "$WARM/.clean.$$"
  done
  rm -f "$WARM/.clean.$$"
  if [ "$rd_tries" -gt 0 ]; then
    # 清理期间放开过权限：再检出一次，确保跟踪文件内容与权限都恢复成任务提交。
    git -C "$W" checkout -q -f "$BRANCH" >/dev/null 2>&1 || { rd_discard; rd_cold checkout_failed; }
  fi
  # 硬校验：领用后（--include/--wip 之前）工作树必须与任务提交完全一致，否则废弃槽位退冷。
  [ -z "$(git -C "$W" status --porcelain 2>/dev/null)" ] || { rd_discard; rd_cold dirty_after_claim; }
elif [ -d "$W" ] && [ -n "$prev_job" ] && [ -d "$ROOT/jobs/$prev_job" ] && [ ! -f "$ROOT/jobs/$prev_job/final-state.json" ]; then
  # 槽位目录异常且上一任务还没有交接快照：不删，退回冷路径。
  rd_cold slot_invalid
else
  rm -rf "$W"; git -C "$BARE" worktree prune >/dev/null 2>&1
  git -C "$BARE" worktree add -q "$W" "$BRANCH" >/dev/null 2>&1 || rd_cold create_failed
fi
rm -rf "$C"; mkdir "$C" || rd_cold claim_failed
printf '%s\n' "$JOB" > "$C/job"; echo $$ > "$C/pid"; date +%s > "$C/at"
gen=$(( $(cat "$WARM/$S.gen" 2>/dev/null || echo 0) + 1 )); echo "$gen" > "$WARM/$S.gen"
printf '%s\n' "$W" > "$ROOT/jobs/$JOB/worktree"; printf '%s\n' "$C" > "$ROOT/jobs/$JOB/warm-claim"
# 准备期心跳：任务启动脚本写下 pid、领用被释放或到达上限时结束。
nohup sh -c 'echo $$ > "$3/hb.pid"; i=0; while [ "$i" -lt "$1" ] && [ "$(cat "$3/job" 2>/dev/null)" = "$4" ] && [ ! -f "$5/pid" ]; do date +%s > "$3/at.tmp" && mv -f "$3/at.tmp" "$3/at"; sleep "$2"; i=$((i + 1)); done' sh "$HB_LOOPS" "$HB_STEP" "$C" "$JOB" "$ROOT/jobs/$JOB" >/dev/null 2>&1 </dev/null &
echo "rdwarm:mode=warm"; echo "rdwarm:slot=$S"; echo "rdwarm:gen=$gen"; echo "rdwarm:reused=$reused"
echo "rdwarm:prev_commit=$prev"; echo "rdwarm:prev_job=$prev_job"; echo "rdwarm:path=$W"
'''
    return body


def parse_result(stdout):
    """解析 claim_script 输出；没有 mode 行（脚本被替换、输出缺失）返回 None，调用方按冷处理。"""
    fields = {}
    for line in (stdout or "").splitlines():
        if line.startswith(RESULT_PREFIX):
            key, _, value = line[len(RESULT_PREFIX):].partition("=")
            fields[key] = value.strip()
    return fields if fields.get("mode") in (WARM, COLD) else None


def apply_result(rec, fields, job):
    """把领用结果写进派单记录；fields 为 None 或冷时记 cold:wt/<job>。"""
    for key in ("warm_reused", "warm_prev_commit", "warm_prev_job", "warm_cold_reason"):
        rec.pop(key, None)
    if fields and fields.get("mode") == WARM:
        rec["worktree"] = warm_field(fields["slot"], fields.get("gen", "0"))
        rec["warm_reused"] = fields.get("reused") == "1"
        if fields.get("prev_commit"):
            rec["warm_prev_commit"] = fields["prev_commit"]
        if fields.get("prev_job"):
            rec["warm_prev_job"] = fields["prev_job"]
    else:
        rec["worktree"] = cold_field(job)
        if fields and fields.get("reason"):
            rec["warm_cold_reason"] = fields["reason"]


def receipt_fields(rec):
    """回执附加字段；没有 worktree 字段的旧记录不附加，保持原回执不变。"""
    if not rec.get("worktree"):
        return {}
    if not is_warm(rec):
        return {"worktree_mode": COLD}
    out = {"worktree_mode": WARM, "warm_reused": bool(rec.get("warm_reused"))}
    if rec.get("warm_prev_commit"):
        out["warm_prev_commit"] = rec["warm_prev_commit"]
    return out


def release_script(rec):
    """释放该任务的领用（仍是它占用才删）；槽位里的 worktree 与缓存保留。"""
    mode, name, _ = parse_field(rec)
    if mode != WARM:
        return ""
    return (f'C="$ROOT/warm/{name}.claim"; '
            f'if [ "$(cat "$C/job" 2>/dev/null)" = {shlex.quote(rec["job"])} ]; then rm -rf "$C"; fi\n')


def release_unstarted_script(rec):
    """准备失败时释放：任务启动脚本已写下 pid（可能已在运行）则不动，避免放掉运行中的槽位。"""
    if not is_warm(rec):
        return ""
    d = f'"$ROOT/jobs/{rec["job"]}"'
    return f'[ -f {d}/pid ] || ls {d}/run.r*.sh >/dev/null 2>&1 || {{ {release_script(rec)} }}\n'


def pin_script(rec, *, until=None, artifacts=None):
    """把钉住状态写到目标机，让其他控制端的领用判定也能看到。
    until：keep 到期时刻（epoch 秒），0 清除；artifacts：True 标记产物待拉，False 清除；None 不动。"""
    if not is_warm(rec):
        return ""
    parts = [f'D="$ROOT/jobs/{rec["job"]}"; [ -d "$D" ] || exit 0']
    if until is not None:
        parts.append(f'echo {int(until)} > "$D/pinned-until"' if until else 'rm -f "$D/pinned-until"')
    if artifacts is not None:
        parts.append(': > "$D/pinned"' if artifacts else 'rm -f "$D/pinned"')
    return "\n".join(parts) + "\n"


def record_unpacked_script(rec):
    """把本任务解包的路径清单（stdin，每行一个）写成槽位的 .unpacked，供下次领用前清理。"""
    mode, name, _ = parse_field(rec)
    if mode != WARM:
        return ""
    return f'mkdir -p "$ROOT/warm" && cat >> "$ROOT/{unpacked_path(name)}"\n'


def detach_script(bare, branch):
    """删分支前把检出该分支的热槽位切到 detached HEAD，否则 git 拒绝删除被检出的分支。"""
    return (f'for W in "$ROOT"/wt/_warm/*; do [ -d "$W" ] || continue; '
            f'[ "$(git -C "$W" symbolic-ref -q --short HEAD 2>/dev/null)" = {shlex.quote(branch)} ] || continue; '
            f'git -C "$W" checkout -q --detach || exit 1; done\n')


def list_script(pinned, idle_days):
    """列出本用户的热槽位：槽位、大小 KB、占用者、状态（busy/free）、是否闲置超期。"""
    minutes = int(float(idle_days) * 1440)
    return _assign(PINNED=" ".join(pinned)) + busy_sh() + (
        r'''for W in "$ROOT"/wt/_warm/*; do [ -d "$W" ] || continue
  S=${W##*/}; C="$ROOT/warm/$S.claim"; oj=$(cat "$C/job" 2>/dev/null)
  st=free; [ -d "$C" ] && rd_busy "$C" && st=busy
  ref="$C/released"; [ -e "$ref" ] || ref="$ROOT/jobs/$oj/exit_code"; [ -e "$ref" ] || ref="$C"; [ -e "$ref" ] || ref="$W"
  idle=0; [ "$st" = free ] && [ -n "$(find "$ref" -prune -mmin +@@MIN@@ 2>/dev/null)" ] && idle=1
  printf '%s\t%s\t%s\t%s\t%s\n' "$S" "$(du -sk "$W" 2>/dev/null | cut -f1)" "$oj" "$st" "$idle"
done
'''.replace("@@MIN@@", str(minutes)))


def reap_script(slot, pinned, idle_days):
    """闲置槽位回收：互斥内重新判定，先写交接快照（写不全就不删）再删 worktree 与领用目录；输出 reaped 或 kept:<原因>。"""
    minutes = int(float(idle_days) * 1440)
    return (_assign(S=slot, PINNED=" ".join(pinned)) + MUTEX_SH + busy_sh() + SNAPSHOT_SH + r'''
rd_lock || { echo "kept:busy"; exit 0; }
W="$ROOT/wt/_warm/$S"; C="$ROOT/warm/$S.claim"; oj=$(cat "$C/job" 2>/dev/null)
[ -d "$W" ] || { echo "kept:missing"; exit 0; }
if [ -d "$C" ] && rd_busy "$C"; then echo "kept:busy"; exit 0; fi
ref="$C/released"; [ -e "$ref" ] || ref="$ROOT/jobs/$oj/exit_code"; [ -e "$ref" ] || ref="$C"; [ -e "$ref" ] || ref="$W"
[ -n "$(find "$ref" -prune -mmin +@@MIN@@ 2>/dev/null)" ] || { echo "kept:not_idle"; exit 0; }
if [ -n "$oj" ]; then rd_snapshot "$W" "$oj" idle_reclaim || { echo "kept:snapshot_failed"; exit 0; }; fi
BARE=$(cd "$W" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd -P)
git -C "${BARE:-/nonexistent}" worktree remove --force "$W" >/dev/null 2>&1 || { chmod -R u+rwx "$W" 2>/dev/null; rm -rf "$W"; [ -n "$BARE" ] && git -C "$BARE" worktree prune >/dev/null 2>&1; }
[ ! -e "$W" ] || { echo "kept:remove_failed"; exit 1; }
rm -rf "$C"
echo reaped
'''.replace("@@MIN@@", str(minutes)))
