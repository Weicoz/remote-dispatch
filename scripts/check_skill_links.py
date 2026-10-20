#!/usr/bin/env python3
"""检查技能 SKILL.md 的相对链接。

技能副本只会同步 SKILL.md，不带 references/ 等目录，因此链接必须是
http(s) URL、页内锚点或 mailto，不能是相对路径。
"""

import re
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def main(path):
    text = Path(path).read_text(encoding="utf-8")
    bad = [
        match.group(1).strip()
        for match in LINK.finditer(text)
        if not match.group(1).strip().startswith(("http://", "https://", "#", "mailto:"))
    ]
    if bad:
        print(
            "技能 SKILL.md 含相对链接：" + ", ".join(bad) + "；技能副本不带 references/，请改成 https:// 地址",
            file=sys.stderr,
        )
        return 1
    print("技能链接检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
