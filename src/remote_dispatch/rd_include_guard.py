"""--include 输入的内容散列：派单时记录，回收时只豁免内容未变的原输入。

目录下新增或改动的文件（执行方写的成果）按未提交内容阻塞，不再随目录整体豁免。
软链接按「link:目标」文本散列，两端一致；文件数超过上限时不记录散列，回收退回按目录豁免的旧行为。
"""
import hashlib
import os
import shlex

MAX_FILES = 50000
SKIP_DIRS = (".git",)


def covers(include_paths, path):
    """path 是否落在某个 --include 路径之内（"." 表示整个仓库）。"""
    return any(item == "." or path == item or path.startswith(item.rstrip("/") + "/") for item in include_paths)


def _digest_file(path):
    if os.path.islink(path):
        return hashlib.sha256(("link:" + os.readlink(path)).encode()).hexdigest()
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_includes(repo, include_paths):
    """返回 {仓库内相对路径: sha256}；没有 include 返回 {}，文件数超限或读不到返回 None（不记录）。"""
    result = {}
    try:
        for item in include_paths:
            base = os.path.join(repo, item)
            if os.path.isdir(base) and not os.path.islink(base):
                for folder, dirs, files in os.walk(base):
                    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                    for name in dirs + files:
                        full = os.path.join(folder, name)
                        if os.path.islink(full) or not os.path.isdir(full):
                            result[os.path.relpath(full, repo).replace(os.sep, "/")] = _digest_file(full)
                    if len(result) > MAX_FILES:
                        return None
            else:
                result[item] = _digest_file(base)
    except OSError:
        return None
    return result if len(result) <= MAX_FILES else None


def remote_hash_script(paths):
    """目标机上按输入顺序每行输出一个 sha256；文件不存在输出 missing。需在 worktree 目录内执行。"""
    lines = ['rd_h() { if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d" " -f1; '
             'else shasum -a 256 | cut -d" " -f1; fi; }']
    for path in paths:
        quoted = shlex.quote(path)
        lines.append(f'if [ -L {quoted} ]; then printf "link:%s" "$(readlink {quoted})" | rd_h; '
                     f'elif [ -f {quoted} ]; then rd_h < {quoted}; else echo missing; fi')
    return "\n".join(lines) + "\n"


def changed_paths(paths, recorded, remote_lines):
    """对比目标机现状与派单时散列，返回内容已变、新增或缺失的路径。"""
    changed = []
    for index, path in enumerate(paths):
        now = remote_lines[index].strip() if index < len(remote_lines) else ""
        if not recorded or recorded.get(path) != now:
            changed.append(path)
    return changed
