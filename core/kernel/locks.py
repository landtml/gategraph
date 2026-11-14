"""locks.py, non-blocking exclusive file locks that name their holder.

Kernel module: standard library only. POSIX (fcntl.flock), like the rest of
the template.

The lock is held on an open file. The holder writes a small JSON record into
the file after it has locked it. A caller that fails to lock reads that record
and reports who holds the lock, instead of waiting.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


class LockHeld(Exception):
    """Another holder has the lock. `holder` is the record it wrote, or {}."""

    def __init__(self, holder: dict | None):
        self.holder = dict(holder or {})
        who = self.holder.get("owner") or "unknown owner"
        pid = self.holder.get("pid")
        detail = f" (pid {pid})" if pid else ""
        super().__init__(f"held by {who}{detail}")


def _read_holder(fd: int) -> dict:
    try:
        raw = os.pread(fd, 4096, 0)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def file_lock(path, owner: str | None = None):
    """Hold an exclusive lock on `path` for the duration of the block.

    Raises LockHeld at once if another holder has it. Yields the record this
    holder wrote: {"owner": ..., "pid": ...}.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockHeld(_read_holder(fd)) from None
        record = {"owner": owner or f"pid {os.getpid()}", "pid": os.getpid()}
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record, sort_keys=True).encode("utf-8"), 0)
        try:
            yield record
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
