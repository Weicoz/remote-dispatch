#!/usr/bin/env python3
"""Plan/prune declared regenerable caches after acceptance; never remove a worktree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

try:
    from . import rd_messages as messages
except ImportError:  # standalone copy on the worker
    import rd_messages as messages


def physical(path):
    path = Path(path).absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("保留符号链接及其目标；只接受物理任务路径。")
    return path


def retained_hashes(paths):
    result = {}
    for path in paths:
        entries = [path] + (sorted(path.rglob("*")) if path.is_dir() else [])
        tree = hashlib.sha256()
        total = 0
        for item in entries:
            physical(item)
            info = item.stat()
            row = {"mode": stat.S_IMODE(info.st_mode), "type": stat.S_IFMT(info.st_mode)}
            if item.is_file():
                digest = hashlib.sha256()
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                row.update(bytes=info.st_size, sha256=digest.hexdigest())
                after = item.stat()
                if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError("保留文件读取时发生变化。")
                total += info.st_size
            tree.update(json.dumps([str(item.relative_to(path)), row], sort_keys=True).encode() + b"\n")
        result[str(path)] = {"entries": len(entries), "bytes": total, "tree_sha256": tree.hexdigest()}
    return result


def has_open_files(path):
    command = shutil.which("lsof")
    if not command:
        raise ValueError("无法核对缓存是否仍被进程使用：缺少lsof；只保留清理计划。")
    try:
        result = subprocess.run([command, "-nP", "+D", str(path), "-F", "pfn"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("缓存使用状态未确认；保留缓存。") from error
    if result.stdout.strip():
        return True
    if result.stderr.strip() or result.returncode != 1:
        raise ValueError("缓存使用状态未确认；保留缓存。")
    return False


def prune(worktree, merged_into, closure, caches, retain, apply=False, record=None):
    root = physical(worktree)
    uid, job = os.environ.get("RD_USER_ID"), os.environ.get("RD_JOB")
    if not uid or not job or not os.environ.get("RD_WT"):
        raise ValueError("必须使用本任务RD_USER_ID、RD_JOB和RD_WT，不猜员工归属。")
    owned = physical(os.environ["RD_WT"])
    if root != owned and not root.is_relative_to(owned / "private/worktrees"):
        raise ValueError("只能清理本任务工作树或其独立票据工作树。")
    for i, part in enumerate(root.parts[:-1]):
        if part == "users" and root.parts[i + 1] != uid:
            raise ValueError("不得跨员工ID清理。")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    if physical(git("rev-parse", "--show-toplevel")) != root or git("status", "--porcelain"):
        raise ValueError("工作树必须干净；保留未提交和未跟踪文件。")
    head = git("rev-parse", "HEAD")
    if not merged_into or merged_into.startswith("-"):
        raise ValueError("必须提供有效的integration引用。")
    accepted = git("rev-parse", "--verify", merged_into + "^{commit}")
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", head, accepted],
                      capture_output=True).returncode:
        raise ValueError("当前提交尚未合入指定integration。")
    live = closure.get("observed_current", closure.get("current_matching_births"))
    source_matches = closure.get("head") == head
    if not source_matches and isinstance(closure.get("head"), str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", closure["head"]):
        old = closure["head"]
        ancestor = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", old, head], capture_output=True)
        if ancestor.returncode == 0:
            changed = git("diff", "--name-only", old, head).splitlines()
            source_matches = bool(changed) and all(Path(p).suffix.lower() == ".md" for p in changed)
    if (not source_matches or closure.get("all_owned_resources_closed") is not True
            or closure.get("source_freeze_ended") is not True or live != []
            or closure.get("job", job) != job):
        raise ValueError("缺少当前来源、自有进程关闭及来源冻结结束的实际回执。")
    if not caches or not retain:
        raise ValueError("必须明确缓存路径与保留的当前产物／证据。")
    protected = [physical(root / p) for p in retain]
    candidates = [physical(root / p) for p in caches]
    tracked = git("ls-files", "-z").split("\0")
    worktrees = [Path(row[len("worktree "):]) for row in git("worktree", "list", "--porcelain", "-z").split("\0")
                 if row.startswith("worktree ")]
    for path in protected + candidates:
        if not path.is_relative_to(root / "private") or not path.exists():
            raise ValueError("只能处理当前工作树private内已存在的物理路径。")
    for path in candidates:
        parts = path.relative_to(root).parts
        derived = "DerivedData" in parts or any(p.endswith("-derived") for p in parts)
        allowed = ((path.name in ("Intermediates.noindex", "ModuleCache.noindex", "Index.noindex") and derived)
                   or (path.name == "incremental" and "target" in parts)
                   or (path.name == ".cache" and "node_modules" in parts))
        if not allowed or not path.is_dir() or any("backup" in p.lower() for p in parts):
            raise ValueError("只回收已声明的中间缓存；保留源码、产品、依赖、备份及worktree。")
        if any(p == path or p.is_relative_to(path) or path.is_relative_to(p) for p in protected):
            raise ValueError("缓存与保留产物／证据重叠。")
        if any((root / p).is_relative_to(path) for p in tracked if p):
            raise ValueError("缓存内存在Git跟踪文件。")
        if any(p == path or p.is_relative_to(path) for p in worktrees):
            raise ValueError("缓存内存在登记的工作树。")
        for child in path.rglob("*"):
            physical(child)
            if (child.name.casefold() == ".git" or
                    (child.is_dir() and (child / "HEAD").is_file()
                     and (child / "objects").is_dir() and (child / "refs").is_dir())):
                raise ValueError("缓存内存在Git目录或工作树，必须保留。")
    if len(set(candidates)) != len(candidates) or any(a != b and a.is_relative_to(b) for a in candidates for b in candidates):
        raise ValueError("缓存路径重复或嵌套。")
    before = retained_hashes(protected)
    result = dict(schema="rd-cache-v1", worktree=str(root), job=job, user_id=uid, head=head, accepted_merge=accepted, state="planned",
                  candidates=[str(p) for p in candidates], deleted=[], retained_before=before)
    if record:
        record(result)
    try:
        if apply:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ValueError("当前Python缺少安全目录删除支持；保留计划。")
            identities = {p: (p.stat().st_dev, p.stat().st_ino) for p in candidates}
            if any(has_open_files(path) for path in candidates):
                raise ValueError("缓存仍有打开文件；等待本任务工具结束。")
            for path in candidates:
                physical(path)
                if (path.stat().st_dev, path.stat().st_ino) != identities[path]:
                    raise ValueError("缓存目录身份变化；保留剩余目录。")
                parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    shutil.rmtree(path.name, dir_fd=parent)
                finally:
                    os.close(parent)
                result["deleted"].append(str(path))
                if record:
                    record(result)
        result["retained_after"] = retained_hashes(protected)
        if before != result["retained_after"] or git("rev-parse", "HEAD") != head or git("status", "--porcelain"):
            raise ValueError("保留产物或工作树变化；查看本次清理回执。")
        result["state"] = "completed" if apply else "planned"
    except (OSError, ValueError):
        result["state"] = "failed"
        raise
    finally:
        if record:
            record(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--merged-into", required=True)
    parser.add_argument("--closed-receipt", type=Path, required=True)
    parser.add_argument("--cache", action="append", required=True)
    parser.add_argument("--retain", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="只执行本次明确路径的缓存回收，不删除工作树")
    args = parser.parse_args()
    try:
        root, output = physical(args.worktree), physical(args.output)
        if not output.is_relative_to(root / "private") or any(output.is_relative_to(root / p) for p in args.cache):
            raise ValueError("回执必须在本任务private内且位于待删缓存之外。")
        closed_path = physical(args.closed_receipt)
        protected = [physical(root / p) for p in args.retain]
        if output == closed_path or any(output == p or output.is_relative_to(p) or p.is_relative_to(output) for p in protected):
            raise ValueError("输出回执不能覆盖或写入保留产物及关闭证据。")
        previous = messages.read_json(output)
        if previous is not None and (not isinstance(previous, dict) or previous.get("schema") != "rd-cache-v1"
                or previous.get("job") != os.environ.get("RD_JOB") or previous.get("user_id") != os.environ.get("RD_USER_ID")
                or previous.get("worktree") != str(root)):
            raise ValueError("保留已有文件；输出只能新建或更新本任务自己的缓存回执。")
        closure = messages.read_json(closed_path)
        if not isinstance(closure, dict):
            raise ValueError("关闭回执不存在或格式无效。")
        result = prune(root, args.merged_into, closure, args.cache, args.retain, args.apply,
                       record=lambda row: messages.atomic_json(output, row))
        print("缓存回收：" + result["state"] + "，删除目录" + str(len(result["deleted"])))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(4, str(error) + "\n")


if __name__ == "__main__":
    main()
