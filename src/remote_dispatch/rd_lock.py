"""Non-blocking job file locks shared by CLI and future service write paths."""
import errno
import os
from pathlib import Path

from .rd_messages import job_id


class JobBusy(Exception):
    """Another writer owns this job; a service may map this to round_in_progress."""


class JobLock:
    """Use ``with JobLock(state, job):`` or acquire()/release().

    Acquire before loading the job and hold through the target launch/stop.
    Separate instances contend across threads and processes. Locks are not
    reentrant. The OS releases them on process exit; never unlink lock files,
    since writers must keep locking the same inode.
    """
    def __init__(self, state, job):
        self.job = job_id(job)
        self.path = Path(state).resolve() / "locks" / (self.job + ".lock")
        self._stream = None

    def acquire(self):
        if self._stream is not None:
            raise RuntimeError("job lock already acquired")
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("任务锁路径不得是符号链接。")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        stream = os.fdopen(descriptor, "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise JobBusy(self.job) from None
            raise
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        return self

    def release(self):
        if self._stream is not None:
            # Closing releases the OS lock even when unwinding a failed command.
            self._stream.close()
            self._stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
