"""Single-instance lock, two loop processes sharing one state dir corrupts the registry.

A cooperative pidfile under the state dir: ``acquire`` writes our PID, but only if no *live*
process already holds it. A crash leaves a stale pidfile whose PID is dead, ``os.kill(pid, 0)``
detects that and reclaims it, so a killed run can be restarted without manual cleanup.
Limitation (fails SAFE): if the OS has meanwhile REUSED the dead PID for an unrelated process,
the liveness probe cannot tell the difference, acquire refuses and a human removes the stale
file; we never reclaim a lock that might be held. Advisory (POSIX cooperative), which is the
right strength here: the only writers are loop processes, and they all check.
A plain POSIX pidfile is sufficient here: no external lock service, no extra dependency,
just the liveness primitive the OS already provides.
"""
from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Raised when a live loop process already holds the lock for this state dir."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence/permission check, doesn't actually signal
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user, treat as alive (don't stomp it)
    return True


class PidFile:
    def __init__(self, path: "str | os.PathLike") -> None:
        self.path = Path(path)

    def read_pid(self) -> "int | None":
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def is_held_by_live_process(self) -> bool:
        pid = self.read_pid()
        return pid is not None and pid != os.getpid() and _pid_alive(pid)

    def acquire(self) -> "PidFile":
        """Take the lock, reclaiming a stale one. Raises :class:`AlreadyRunning` if a live peer holds it."""
        if self.is_held_by_live_process():
            raise AlreadyRunning(
                f"another loop process (pid {self.read_pid()}) holds {self.path}; "
                f"refuse to start a second instance on the same state dir"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(str(os.getpid()), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic
        return self

    def release(self) -> None:
        """Remove the lock, but only if it's still ours (never delete a peer's live pidfile)."""
        if self.read_pid() == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "PidFile":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
