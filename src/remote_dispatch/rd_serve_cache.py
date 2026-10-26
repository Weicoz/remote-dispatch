"""Local JSON read views, invalidated by file identity rather than a TTL."""
import os
from pathlib import Path
import re
import threading

from . import rd_messages as messages


class LocalFiles:
    def __init__(self, state):
        self.state = state
        self.lock = threading.Lock()
        self.groups = {}
        self.views = {}
        self.versions = {}

    def snapshot(self, group, *, nested=False, filename=None, job_rounds=None):
        """Stat every retained file; parse only changed files, including in-place edits.

        Directory mtimes alone miss edits to existing receipts. Readers receive a
        separate mapping so a concurrent refresh cannot change their read view.
        Symlinks are rejected even when a previously cached file is replaced.
        """
        with self.lock:
            root = self.state / group
            if any(path.is_symlink() for path in (root, *root.parents)):
                raise ValueError("cached state path must not be a symbolic link")
            identity = (group, None if job_rounds is None else frozenset(job_rounds.items()))
            previous = self.groups.get(identity, {})
            current = {}

            def scan(directory, descend):
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if descend and job_rounds is not None and entry.name not in job_rounds:
                                continue
                            if not descend and job_rounds is not None:
                                stem = "r" + str(job_rounds[os.path.basename(directory)])
                                if entry.name not in (stem + ".json", stem + ".ack.json", stem + ".review.json"):
                                    continue
                            if filename and entry.name != filename:
                                continue
                            if group == "results" and not descend and not re.fullmatch(
                                    r"dispatch\.json|r[1-9][0-9]*(?:\.ack|\.review)?\.json", entry.name):
                                continue
                            if entry.is_symlink():
                                raise ValueError("cached state path must not be a symbolic link")
                            if descend and entry.is_dir(follow_symlinks=False):
                                scan(entry.path, False)
                            elif entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                                try:
                                    stat = entry.stat(follow_symlinks=False)
                                except FileNotFoundError:
                                    continue
                                signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                                saved = previous.get(entry.path)
                                current[entry.path] = saved if saved and saved[0] == signature else (
                                    signature, messages.read_json(entry.path, limit=messages.RECEIPT_BYTES
                                                                  if group == "results" else None))
                except FileNotFoundError:
                    # Concurrent cleanup is reflected by the next read as well.
                    return

            scan(root, nested)
            if current != previous:
                self.versions[identity] = self.versions.get(identity, 0) + 1
                self.views[identity] = {Path(path): item[1] for path, item in current.items()}
            # Keep one current-membership view; do not retain each old job set.
            for old in list(self.groups):
                if job_rounds is not None and old[0] == group and old[1] is not None and old != identity:
                    self.groups.pop(old, None)
                    self.views.pop(old, None)
                    self.versions.pop(old, None)
            self.groups[identity] = current
            return self.versions.get(identity, 0), self.views.get(identity, {})
