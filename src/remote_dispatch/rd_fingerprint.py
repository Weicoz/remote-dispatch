"""派单前环境指纹核对。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import shlex
import subprocess
import sys
import time
from . import rd_native as native
from . import rd_warm as warm
from . import rd_artifacts as artifacts
from . import cli

__all__ = [
    "FingerprintMismatch",
    "check_fingerprint",
    "compare_fingerprint",
    "drop_attempt",
    "fingerprint_settings",
    "host_fingerprint_command",
    "reject_fingerprint",
    "remaining_timeout",
    "run_local_fingerprint",
    "run_remote_fingerprint",
    "win_ps",
]


class FingerprintMismatch(Exception):
    """目标机环境指纹与本机不一致，或指纹/setup 步骤失败；attempt 记录本台的结果。"""
    def __init__(self, attempt):
        super().__init__(attempt.get("error") or "环境指纹不一致")
        self.attempt = attempt


def fingerprint_settings(proj):
    """返回 (指纹命令或 None, 超时秒数)；超时配置不合法直接退出 4。"""
    command = (proj.get("fingerprint") or "").strip() or None
    raw = (proj.get("fingerprint_timeout") or "").strip()
    try:
        timeout = int(raw) if raw else artifacts.DEFAULT_FINGERPRINT_TIMEOUT
    except ValueError:
        cli.die(f"fingerprint_timeout 必须是正整数秒：{raw}", 4)
    if timeout <= 0:
        cli.die(f"fingerprint_timeout 必须是正整数秒：{raw}", 4)
    return command, timeout


def host_fingerprint_command(proj, host, command):
    """Windows 目标优先用 fingerprint_windows（PowerShell 语法）；未配置时沿用 fingerprint。"""
    if command and host["platform"] == "windows":
        return (proj.get("fingerprint_windows") or "").strip() or command
    return command


def win_ps(host, script, timeout):
    """经 SSH 在 Windows 目标机跑 PowerShell 脚本（EncodedCommand，无 shell 插值）；超时返回 None。
    脚本里 $root 是展开后的工作根目录。"""
    prefix = "$root=[Environment]::ExpandEnvironmentVariables(" + native.ps_quote(host["workdir"]) + ").Replace('~',$env:USERPROFILE); "
    if host["path"]:
        prefix += "$env:PATH=[Environment]::ExpandEnvironmentVariables(" + native.ps_quote(host["path"]) + ")+';'+$env:PATH; "
    cmd = ["ssh", *shlex.split(host["ssh_opts"]), host["ssh"], " ".join(native.powershell(prefix + script))]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return p


def run_local_fingerprint(command, cwd, timeout):
    """本机仓库目录执行指纹命令，返回 stdout；失败或超时在派单前退出 4。"""
    try:
        p = subprocess.run(["sh", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        cli.die(f"本机指纹命令超过 {timeout} 秒，未派单（可调 fingerprint_timeout）。", 4)
    except OSError as error:
        cli.die(f"本机指纹命令无法执行：{error}", 4)
    if p.returncode:
        cli.die(f"本机指纹命令失败（退出码 {p.returncode}），未派单：{(p.stderr or '').strip()[:300]}", 4)
    return p.stdout


def run_remote_fingerprint(host, command, timeout, cwd='"$ROOT"', job=None):
    """目标机上执行指纹命令；返回 (stdout, 错误说明或 None)。"""
    if host["platform"] == "windows":
        where = f"Join-Path $root {native.ps_quote('wt/' + job)}" if job else "$root"
        inner = native.powershell(command)[-1]
        script = (f"$w={where}; if(-not (Test-Path $w)){{ $w=$HOME }}; Set-Location $w; $env:RD_WT=(Get-Location).Path; "
                  f"& powershell.exe -NoProfile -NonInteractive -EncodedCommand {inner}; exit $LASTEXITCODE")
        r = cli.win_ps(host, script, timeout)
    else:
        script = f'cd {cwd} || exit 96\nexport RD_WT="$PWD"\nsh -c {shlex.quote(command)}'
        r = cli.ssh(host, script, check=False, timeout=timeout)
    if r is None:
        return "", f"指纹命令超过 {timeout} 秒"
    if r.returncode:
        return r.stdout or "", f"指纹命令退出码 {r.returncode}：{(r.stderr or '').strip()[:300]}"
    return r.stdout, None


def compare_fingerprint(local, remote, host_name):
    return artifacts.fingerprint_diff(local.rstrip(), remote.rstrip(), host_name)


def check_fingerprint(host, rec, command, local, timeout, setup_timeout):
    """同步完成后先在目标机跑 setup（落 setup.done，启动脚本随后跳过），再执行指纹并与本机比较。
    返回 setup 这一步实际耗时（秒）：setup 与主体共用同一超时预算，调用方要从 launch 的 timeout 里扣掉。"""
    job = rec["job"]
    # host["timeout"] 来自 config.ini，是字符串；ssh/win_ps 需要数值
    setup_timeout = int(setup_timeout) if setup_timeout not in (None, "") else None
    if host["platform"] == "windows":
        setup = (f"$d=Join-Path $root {native.ps_quote('jobs/' + job)}; $w=Join-Path $root {native.ps_quote('wt/' + job)}; "
                 "if((Test-Path (Join-Path $d 'setup.ps1')) -and -not (Test-Path (Join-Path $d 'setup.done'))){ "
                 "Set-Location $w; $env:RD_JOB=" + native.ps_quote(job) + "; $env:RD_WT=$w; $env:RD_JOB_DIR=$d; "
                 "$s=Get-Content -Raw (Join-Path $d 'setup.ps1'); "
                 "& powershell.exe -NoProfile -NonInteractive -Command $s *> (Join-Path $d 'setup.log'); "
                 "if($LASTEXITCODE -ne 0){ exit $LASTEXITCODE }; New-Item -ItemType File (Join-Path $d 'setup.done') -Force | Out-Null }")
        began = time.monotonic()
        r = cli.win_ps(host, setup, setup_timeout)
    else:
        setup = (f'D="$ROOT/jobs/{job}"; [ -f "$D/setup.sh" ] || exit 0; [ -f "$D/setup.done" ] && exit 0\n'
                 f'cd {warm.worktree_sh(rec)} || exit 96\n'
                 f'export RD_JOB={shlex.quote(job)} RD_WT="$PWD" RD_JOB_DIR="$D"\n'
                 f'sh "$D/setup.sh" > "$D/setup.log" 2>&1 && touch "$D/setup.done"')
        began = time.monotonic()
        r = cli.ssh(host, setup, check=False, timeout=setup_timeout)
    setup_spent = time.monotonic() - began
    if r is None or r.returncode:
        reason = "setup 超时" if r is None else f"setup 失败（退出码 {r.returncode}，见 setup.log）"
        raise cli.FingerprintMismatch(dict(host=host["name"], remote="", diff="", error=reason + "，未能核对环境指纹"))
    remote, error = cli.run_remote_fingerprint(host, command, timeout, warm.worktree_sh(rec), job=job)
    diff = "" if error else cli.compare_fingerprint(local, remote, host["name"])
    if error or diff:
        raise cli.FingerprintMismatch(dict(host=host["name"], remote=artifacts.trim(remote), diff=diff, error=error))
    rec["fingerprint"] = dict(command=command, match=True, host=host["name"], local=artifacts.trim(local),
                              remote=artifacts.trim(remote), digest=artifacts.fingerprint_digest(local))
    return setup_spent


def remaining_timeout(total, spent):
    """总超时预算扣掉已用掉的 setup 时间，至少 1 秒。"""
    return max(1, int(float(total)) - int(spent or 0))


def drop_attempt(host, rec):
    """换机前删掉本台已建的 worktree 与任务目录（尽力而为）。"""
    job, bare = rec["job"], rec["bare"]
    if host["platform"] == "windows":
        cli.win_ps(host, f"$w=Join-Path $root {native.ps_quote('wt/' + job)}; $b=Join-Path $root {native.ps_quote(bare)}; "
                     f"git -C $b worktree remove --force $w 2>$null; if(Test-Path $w){{ Remove-Item -Recurse -Force $w }}; "
                     f"git -C $b worktree prune 2>$null; Remove-Item -Recurse -Force (Join-Path $root {native.ps_quote('jobs/' + job)}) -ErrorAction SilentlyContinue", 300)
        return
    if warm.is_warm(rec):
        # 热槽位只释放领用，保留 worktree 与缓存给后续任务。
        cli.ssh(host, warm.release_script(rec) + f'rm -rf "$ROOT/jobs/{job}"', check=False)
        return
    wt = f'"$ROOT/wt/{job}"'
    cli.ssh(host, f'git -C "$ROOT/{bare}" worktree remove --force {wt} 2>/dev/null || rm -rf {wt}; '
              f'git -C "$ROOT/{bare}" worktree prune 2>/dev/null; rm -rf "$ROOT/jobs/{job}"', check=False)


def reject_fingerprint(host, rec, command, local, tried):
    """全部候选都不一致：任务不执行主体，目标机落退出码 95，回执记两边指纹，打印逐行差异后退出 5。"""
    last = tried[-1]
    lines = []
    for item in tried:
        lines.append(f"[{item['host']}] " + (item["error"] or "环境指纹不一致"))
        if item["diff"]:
            lines.append(item["diff"])
    text = "\n".join(lines)
    rec["fingerprint"] = dict(command=command, match=False, host=last["host"], local=artifacts.trim(local),
                              remote=last["remote"], diff=last["diff"], error=last["error"],
                              digest=artifacts.fingerprint_digest(local),
                              tried=[dict(host=i["host"], remote=i["remote"], diff=i["diff"], error=i["error"]) for i in tried])
    rec["dispatch_state"] = "fingerprint_rejected"
    cli.save_job(rec)
    job = rec["job"]
    if host["platform"] == "windows":
        cli.win_ps(host, f"$d=Join-Path $root {native.ps_quote('jobs/' + job)}; New-Item -ItemType Directory -Force $d | Out-Null; "
                     f"[IO.File]::WriteAllText((Join-Path $d 'stdout.r1.log'), {native.ps_quote(text + chr(10))}); "
                     "'' | Set-Content (Join-Path $d 'stderr.r1.log'); '95' | Set-Content (Join-Path $d 'exit_code')", 60)
    else:
        cli.ssh(host, f'D="$ROOT/jobs/{job}"; mkdir -p "$D"; printf %s {shlex.quote(text + chr(10))} > "$D/stdout.r1.log"; '
                  f': > "$D/stderr.r1.log"; : > "$D/run.r1.sh"; echo 95 > "$D/exit_code"', check=False)
    print(text, file=sys.stderr)
    cli.die(f"环境指纹核对未通过，任务 {job} 未执行主体（退出码 95）。修复目标机环境，或显式 --skip-fingerprint。", 5)
