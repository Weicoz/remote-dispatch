"""待派登记：编排方登记还没派出的工作项及其依赖。

数据按用户 ID 和项目分开存放在控制端状态目录；写入持文件锁，原子替换。
"""
import re
import time
from pathlib import Path

from . import rd_messages as messages
from .rd_lock import JobLock
from .rd_profiles import user_id

OPEN, DONE, CANCELLED = "open", "done", "cancelled"
# 对外状态：dispatched 已派，pending 待派，blocked 待派但依赖未完成
DISPATCHED, PENDING, BLOCKED = "dispatched", "pending", "blocked"
ITEM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PROJECT_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


def item_name(value):
    if not isinstance(value, str) or not ITEM_PATTERN.fullmatch(value):
        raise ValueError("工作项名只能含英文、数字、点、下划线、连字符，且以英文或数字开头，不超过 64 字符。")
    return value


def find_cycle(items):
    """返回依赖环上的工作项名列表；无环返回 None。"""
    state = {}

    def visit(name, path):
        state[name] = 1
        for dep in items[name]["depends"]:
            if dep not in items:
                continue
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep] if dep in path else [dep, dep]
            if dep not in state:
                found = visit(dep, path + [dep])
                if found:
                    return found
        state[name] = 2
        return None

    for name in sorted(items):
        if name not in state:
            found = visit(name, [name])
            if found:
                return found
    return None


class PlanStore:
    def __init__(self, state, uid, project):
        if not isinstance(project, str) or not PROJECT_PATTERN.fullmatch(project) or project in (".", ".."):
            raise ValueError("项目名必须是英文、数字、点、下划线或连字符，不得包含路径。")
        self.state = Path(state)
        self.uid = user_id(uid)
        self.project = project
        self.path = self.state / "plan" / self.uid / (project + ".json")

    def lock(self):
        lock = JobLock(self.state, self.uid)
        lock.path = self.state.resolve() / "locks" / "plan" / self.uid / (self.project + ".lock")
        return lock.acquire(blocking=True)

    def read(self):
        return (messages.read_json(self.path, limit=None) or {}).get("items", {})

    def mutate(self, change):
        """持锁读取、修改、校验无环后原子写回；change 返回要给调用方的值。"""
        lock = self.lock()
        try:
            items = self.read()
            result = change(items)
            cycle = find_cycle(items)
            if cycle:
                raise ValueError("工作项依赖成环：" + " -> ".join(cycle))
            messages.atomic_json(self.path, {"items": items}, limit=None)
            return result
        finally:
            lock.release()

    @staticmethod
    def clean_list(values, check):
        result = []
        for value in values or []:
            if check(value) not in result:
                result.append(value)
        return result

    def add(self, name, title=None, depends=None, after=None):
        item_name(name)
        depends = self.clean_list(depends, item_name)
        after = self.clean_list(after, messages.job_id)

        def change(items):
            if name in items:
                raise ValueError(f"工作项已存在：{name}")
            self.require(items, depends)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            items[name] = dict(id=name, title=title or "", status=OPEN, depends=depends, after=after,
                               jobs=[], created=now, updated=now)
            return items[name]
        return self.mutate(change)

    def update(self, name, title=None, depends=None, after=None):
        """只替换传入的字段。"""
        if depends is not None:
            depends = self.clean_list(depends, item_name)
        if after is not None:
            after = self.clean_list(after, messages.job_id)

        def change(items):
            item = self.get(items, name)
            self.require(items, depends or [])
            for key, value in (("title", title), ("depends", depends), ("after", after)):
                if value is not None:
                    item[key] = value
            item["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return item
        return self.mutate(change)

    def set_status(self, name, status):
        def change(items):
            item = self.get(items, name)
            item["status"] = status
            item["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return item
        return self.mutate(change)

    def remove(self, name):
        def change(items):
            self.get(items, name)
            users = sorted(other for other, row in items.items() if name in row["depends"])
            if users:
                raise ValueError(f"工作项 {name} 仍被 {', '.join(users)} 依赖，先改依赖再删除。")
            return items.pop(name)
        return self.mutate(change)

    def attach_job(self, name, job, after=None):
        """run --plan-item 调用：把派单记到工作项上，--after 的前序派单记为它的依赖。"""
        after = self.clean_list(after, messages.job_id)

        def change(items):
            item = self.get(items, name)
            if item["status"] != OPEN:
                raise ValueError(f"工作项 {name} 已{'完成' if item['status'] == DONE else '取消'}，不能再关联派单。")
            if job not in item["jobs"]:
                item["jobs"].append(job)
            item["after"] = item["after"] + [j for j in after if j not in item["after"]]
            item["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            return item
        return self.mutate(change)

    @staticmethod
    def get(items, name):
        item_name(name)
        if name not in items:
            raise ValueError(f"工作项不存在：{name}")
        return items[name]

    @staticmethod
    def require(items, names):
        missing = [name for name in names if name not in items]
        if missing:
            raise ValueError("依赖的工作项不存在：" + "、".join(missing))

    def rows(self):
        """稳定结构：每项带状态与阻塞它的未完成依赖。"""
        items = self.read()
        rows = []
        for name in sorted(items):
            item = items[name]
            waiting = sorted(dep for dep in item["depends"] if items.get(dep, {}).get("status") == OPEN)
            if item["status"] != OPEN:
                state = item["status"]
            elif item["jobs"]:
                state = DISPATCHED
            else:
                state = BLOCKED if waiting else PENDING
            rows.append(dict(id=name, title=item["title"], state=state, depends=item["depends"],
                             after=item["after"], jobs=item["jobs"], blocked_by=waiting if state == BLOCKED else [],
                             created=item["created"], updated=item["updated"]))
        return rows

    def summary(self):
        counts = dict.fromkeys((DISPATCHED, PENDING, BLOCKED, DONE, CANCELLED), 0)
        for row in self.rows():
            counts[row["state"]] += 1
        return counts

    def referrers(self, job):
        """仍引用该派单的后续工作项名：未完成未取消、尚无派单的工作项，
        在 --after 里点名了该派单，或依赖的工作项已关联该派单。"""
        items = self.read()
        names = []
        for name in sorted(items):
            item = items[name]
            if item["status"] != OPEN or item["jobs"]:
                continue
            if job in item["after"] or any(job in items.get(dep, {}).get("jobs", []) for dep in item["depends"]):
                names.append(name)
        return names
