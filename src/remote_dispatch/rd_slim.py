"""可再生子路径声明：解析与校验（控制端用）。

声明在 [project:NAME] 的 regenerable_paths，多项用分号分隔，每项是相对 worktree 的具体子路径
（例如 target、node_modules）。瘦身只删这些路径和 setup 完成标记，其余文件一律保留。
"""
import posixpath
import re

# 路径任一层级命中这些名字即拒绝：证据与回执目录不得被瘦身删除，.git 是仓库本体。
FORBIDDEN = {"evidence", "reports", ".git"}
UNSAFE = set("*?[]{}$`\"'=")


def parse_paths(text):
    """返回规范化后的相对路径列表；声明不合法时抛 ValueError。"""
    items = []
    for part in (text or "").split(";"):
        raw = part.strip()
        if not raw:
            continue
        if not raw.isprintable():
            raise ValueError(f"路径含控制字符：{raw!r}")
        if UNSAFE & set(raw):
            raise ValueError(f"必须是具体子路径，不得含通配符或 shell 字符：{raw}")
        path = raw.replace("\\", "/")
        if path.startswith(("/", "~")) or re.match(r"[A-Za-z]:", path):
            raise ValueError(f"必须是相对 worktree 的路径：{raw}")
        if ".." in path.split("/"):
            raise ValueError(f"不得越出 worktree：{raw}")
        norm = posixpath.normpath(path)
        if norm in (".", ""):
            raise ValueError(f"不得指向 worktree 根：{raw}")
        bad = [name for name in norm.split("/") if name.lower() in FORBIDDEN]
        if bad:
            raise ValueError(f"路径不得含 {bad[0]}：{raw}")
        if norm not in items:
            items.append(norm)
    return items
