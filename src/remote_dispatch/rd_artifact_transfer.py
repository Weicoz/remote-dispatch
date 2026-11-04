"""非 Git 产物取回的传输（rsync／Windows 原生通道）。纯路径校验在 rd_artifacts。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import tarfile
import time
from . import rd_native as native
from . import rd_warm as warm
from . import rd_artifacts as artifacts
from . import cli

__all__ = [
    "auto_pull_artifacts",
    "local_artifact_guard",
    "native_artifact",
    "pull_artifacts",
    "rsync_artifact",
    "rsync_supports_protect_args",
    "tracked_files",
]


def tracked_files(repo, path):
    """仓库里与 path 相关的已跟踪文件：path 本身、其下全部文件，以及各级上级路径上的已跟踪文件。"""
    specs = [path] + [path.rsplit("/", i)[0] for i in range(1, path.count("/") + 1)]
    p = cli.git(["ls-files", "-z", "--", *[":(literal)" + spec for spec in specs]], repo, check=False)
    return [name for name in p.stdout.split("\0") if name] if p.returncode == 0 else []


def local_artifact_guard(repo, path, is_dir):
    """拉回前的本地保护；返回 (状态, 说明)，None 表示可写。跟踪冲突跳过，符号链接/创建目录失败为错误。"""
    hits = artifacts.tracked_conflicts(cli.tracked_files(repo, path), path)
    if hits:
        return "skipped", f"本地目标已被 Git 跟踪（{hits[0]}），为免覆盖源码已跳过"
    dest = os.path.join(repo, *path.split("/"))
    existing = dest
    while not os.path.lexists(existing):
        existing = os.path.dirname(existing)
    if os.path.commonpath([repo, os.path.realpath(existing)]) != repo or (os.path.islink(dest) and is_dir):
        return "error", "本地目标路径经符号链接指向仓库外，已拒绝"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except OSError as error:
        return "error", str(error)
    return None


def pull_artifacts(cfg, rec, timeout=cli.ARTIFACT_FETCH_TIMEOUT):
    """把远端 worktree 里声明的产物拉回本地仓库同一相对路径；缺失单独报告，不算整体失败。
    POSIX 用 rsync；Windows 经原生 worker 的 base64 通道取 tar（与 fetch-bundle 同一通道，不依赖 scp 路径转换）。
    状态：fetched/missing/skipped 是终态，error 保持待拉可重试。结果写进 rec["artifacts_state"]。"""
    paths = rec.get("artifact_paths") or []
    host = cli.job_host(cfg, rec)
    job, repo = rec["job"], os.path.realpath(rec["repo"])
    results = []
    kinds = {}
    try:
        if host["platform"] == "windows":
            kinds = native.request(host, "artifact-kinds", {"job": job, "paths": paths}) or {}
        else:
            probe = "".join(f'if [ -d {shlex.quote(p)} ]; then echo "d {p}"; elif [ -e {shlex.quote(p)} ]; then echo "f {p}"; '
                            f'else echo "m {p}"; fi\n' for p in paths)
            # 热槽位代次不一致（已被后来的任务接手）时 WT 为空：不拉，避免拉到别的任务的文件。
            r = cli.ssh(host, warm.resolve_sh(rec) + 'if [ -z "$WT" ]; then echo T; exit 0; fi\n'
                          f'cd "$WT" 2>/dev/null || {{ echo W; exit 0; }}\n{probe}', check=False, timeout=60)
            if r is not None and r.returncode == 0:
                for line in r.stdout.splitlines():
                    if line == "T":
                        kinds = {p: "t" for p in paths}
                        break
                    if line == "W":
                        kinds = {p: "w" for p in paths}
                        break
                    kind, _, path = line.partition(" ")
                    kinds[path] = kind
    except (OSError, ValueError, SystemExit, subprocess.SubprocessError):
        kinds = {}
    for path in paths:
        kind = kinds.get(path)
        if kind is None:
            results.append(dict(path=path, status="error", error="无法连接目标机核对路径"))
        elif kind == "x":
            results.append(dict(path=path, status="error", error="目标机路径含符号链接或越界，已拒绝"))
        elif kind == "t":
            results.append(dict(path=path, status="skipped", error="工作树已被后来的任务接手，产物不可再拉"))
        elif kind in ("m", "w"):
            results.append(dict(path=path, status="missing", error="目标机 worktree 里没有该路径"))
        else:
            guard = cli.local_artifact_guard(repo, path, kind == "d")
            if guard:
                results.append(dict(path=path, status=guard[0], error=guard[1]))
            elif host["platform"] == "windows":
                results.append(cli.native_artifact(host, job, repo, path, kind == "d", timeout))
            else:
                results.append(cli.rsync_artifact(host, job, repo, path, kind == "d", timeout, worktree=warm.job_worktree(rec)))
    number = rec.get("rounds") or 1
    state = dict(round=number, paths=list(paths), results=results, at=time.strftime("%Y-%m-%d %H:%M:%S"))
    rec["artifacts_state"] = state  # 最近一次；按轮次的状态在 artifacts_rounds
    rec.setdefault("artifacts_rounds", {})[str(number)] = state
    cli.save_job(rec)
    if warm.is_warm(rec) and host["platform"] != "windows" and not artifacts.artifacts_pending(rec):
        # 产物已处理完，解除目标机上的钉住标记，槽位可被接手。
        cli.ssh(host, warm.pin_script(rec, artifacts=False), check=False, timeout=30)
    return results


def native_artifact(host, job, repo, path, is_dir, timeout=cli.ARTIFACT_FETCH_TIMEOUT):
    """Windows：worker 打 tar，本地校验成员后解到临时目录再替换；失败记 error，保持可重试。"""
    dest = os.path.join(repo, *path.split("/"))
    try:
        with tempfile.TemporaryDirectory(dir=os.path.dirname(dest)) as temporary:
            bundle = Path(temporary) / "artifact.tar"
            with bundle.open("wb") as stream:
                native.request(host, "fetch-artifact", {"job": job, "relative": path}, output=stream, timeout=timeout)
            staged = Path(temporary) / "staged"
            staged.mkdir()
            with tarfile.open(bundle) as archive:
                for member in archive.getmembers():
                    if not (member.isfile() or member.isdir()) or not artifacts.safe_member(member.name):
                        return dict(path=path, status="error", error=f"目标机回传了不安全的条目：{member.name}")
                archive.extractall(staged, filter="data") if sys.version_info >= (3, 12) else archive.extractall(staged)
            if is_dir:
                os.makedirs(dest, exist_ok=True)
                shutil.copytree(staged, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(staged / os.path.basename(dest), dest)
    except (OSError, ValueError, tarfile.TarError, SystemExit, subprocess.SubprocessError) as error:
        return dict(path=path, status="error", error=f"Windows 产物取回失败：{error}"[:300])
    files, size = artifacts.local_size(dest)
    return dict(path=path, status="fetched", type="dir" if is_dir else "file", files=files, bytes=size)


def rsync_supports_protect_args():
    try:
        p = subprocess.run(["rsync", "--help"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--protect-args" in (p.stdout or "") + (p.stderr or "")


def rsync_artifact(host, job, repo, path, is_dir, timeout=cli.ARTIFACT_FETCH_TIMEOUT, worktree=None):
    dest = os.path.join(repo, *path.split("/"))
    source = cli.scp_path(host, f"{worktree or 'wt/' + job}/{path}")
    command = ["rsync", "-a", "--safe-links"]
    if not cli.is_local(host):
        command += ["-e", "ssh " + host["ssh_opts"]]
        prefix = host["ssh"] + ":"
        if cli.rsync_supports_protect_args():
            command.append("-s")  # --protect-args：远端路径不经 shell 拆词
        elif source.startswith(prefix):
            # openrsync/旧 rsync 把远端路径交给远端 shell，手工加引号防空格拆词。
            source = prefix + shlex.quote(source[len(prefix):])
    command += ["--", source + "/", dest + "/"] if is_dir else ["--", source, dest]
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(path=path, status="error", error=f"rsync 超过 {timeout} 秒未完成，保持待拉；可用 fetch --artifacts 重试")
    except OSError as error:
        return dict(path=path, status="error", error=f"rsync 无法完成：{error}")
    if p.returncode:
        return dict(path=path, status="error", error=f"rsync 退出码 {p.returncode}：{(p.stderr or '').strip()[:200]}")
    files, size = artifacts.local_size(dest)
    return dict(path=path, status="fetched", type="dir" if is_dir else "file", files=files, bytes=size)


def auto_pull_artifacts(cfg, rec, snapshot):
    """任务已结束且尚未成功拉回时自动拉取一次；返回结果列表，无产物或不适用返回 None。"""
    if not rec.get("artifact_paths") or snapshot.get("state") not in ("done", "dead"):
        return None
    if artifacts.artifacts_pending(rec) or not artifacts.round_state(rec).get("results"):
        try:
            return cli.pull_artifacts(cfg, rec, cli.ARTIFACT_AUTO_TIMEOUT)
        except (OSError, SystemExit, subprocess.SubprocessError) as error:
            print(f"产物拉回未完成：{error}；稍后用 rdispatch fetch {rec['job']} --artifacts 重试。", file=sys.stderr)
            return None
    return artifacts.round_state(rec)["results"]
