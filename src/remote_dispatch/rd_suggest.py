"""建议令牌与建议内容指纹。

令牌绑定建议内容指纹、用户 ID 和发起会话，默认 30 分钟内单次有效。
做法与 `profile sync` 的确认指纹一致：接受时重算指纹并比对，不符就拒绝。
令牌记录按用户 ID 存放在控制端状态目录，写入持文件锁，原子替换。
"""
import hashlib
import json
import secrets
import time
from pathlib import Path

from . import rd_messages as messages
from .rd_lock import JobLock
from .rd_profiles import user_id

TTL_SECONDS = 30 * 60
KEEP_SECONDS = 24 * 3600


class TokenRejected(ValueError):
    """令牌不可用；消息就是给用户的原因。"""


def fingerprint(content):
    """建议内容的指纹；键顺序无关。"""
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def descendants(items, item):
    """依赖 item（直接或间接）、仍开放且尚无派单的工作项，按依赖拓扑排序，同层按名字。"""
    below = set()
    changed = True
    while changed:
        changed = False
        for name, row in items.items():
            if name not in below and any(dep == item or dep in below for dep in row.get("depends") or []):
                below.add(name)
                changed = True
    waiting = {name for name in below if items[name].get("status") == "open" and not items[name].get("jobs")}
    ordered = []
    while waiting:
        ready = sorted(name for name in waiting
                       if not any(dep in waiting for dep in items[name].get("depends") or []))
        if not ready:  # 依赖环由待派登记拒绝；防御性退出，避免死循环
            ordered += sorted(waiting)
            break
        ordered += ready
        waiting -= set(ready)
    return ordered


class TokenStore:
    def __init__(self, state, uid):
        self.state = Path(state)
        self.uid = user_id(uid)
        self.directory = self.state / "suggest" / self.uid

    def lock(self):
        lock = JobLock(self.state, self.uid)
        lock.path = self.state.resolve() / "locks" / "suggest" / (self.uid + ".lock")
        return lock.acquire(blocking=True)

    def issue(self, print_, owner, now=None):
        """发一枚令牌；顺手清掉早已过期的记录。返回 (令牌, 过期时间戳)。"""
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(18)
        expires = now + TTL_SECONDS
        lock = self.lock()
        try:
            for path in self.directory.glob("*.json") if self.directory.exists() else []:
                saved = messages.read_json(path, limit=None) or {}
                if now - float(saved.get("expires") or 0) > KEEP_SECONDS:
                    path.unlink(missing_ok=True)
            messages.atomic_json(self.directory / (token + ".json"),
                                 dict(fingerprint=print_, uid=self.uid, session=owner, expires=expires, used=False),
                                 limit=None)
        finally:
            lock.release()
        return token, expires

    def consume(self, token, print_, owner, now=None):
        """校验并一次性用掉令牌；不可用时抛 TokenRejected。"""
        now = time.time() if now is None else now
        if not isinstance(token, str) or not token or not all(c.isalnum() or c in "-_" for c in token):
            raise TokenRejected("建议令牌无效")
        path = self.directory / (token + ".json")
        lock = self.lock()
        try:
            saved = messages.read_json(path, limit=None)
            if not saved:
                raise TokenRejected("建议令牌无效或不属于当前用户 ID")
            if saved.get("uid") != self.uid:
                raise TokenRejected("建议令牌不属于当前用户 ID")
            if saved.get("session") != owner:
                raise TokenRejected("建议令牌绑定的发起会话与当前会话不符")
            if saved.get("used"):
                raise TokenRejected("建议令牌已使用过")
            if now > float(saved.get("expires") or 0):
                raise TokenRejected("建议令牌已过期；请重新运行 suggest")
            if saved.get("fingerprint") != print_:
                raise TokenRejected("建议内容已变化，令牌与当前建议不符；请重新运行 suggest")
            saved["used"] = True
            messages.atomic_json(path, saved, limit=None)
        finally:
            lock.release()
