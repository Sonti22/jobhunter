"""OS-owned advisory lock: automatically released after process termination."""
from __future__ import annotations

import os
from pathlib import Path


class LockBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise LockBusy("Another worker holds " + self.path.name) from exc
        self.stream = stream
        return self

    def __exit__(self, *_exc):
        if self.stream:
            self.stream.close()
            self.stream = None
        # Never unlink: another process may already hold the same inode.
