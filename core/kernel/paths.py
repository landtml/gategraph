"""paths.py, project-root discovery, state layout, slug validation and atomic writes.

Kernel module: standard library only.

Root discovery (find_root), in order:
  1. An explicit `start`: walk up from it to the first directory that holds
     the adapter file. Nothing else is consulted when `start` is given.
  2. The first set environment variable of <ENV_PREFIX>PROJECT_DIR, then the
     legacy <prefix>PROJECT_DIR names, then CLAUDE_PROJECT_DIR. It must name a
     directory that holds the adapter file; a set variable that does not is an
     error, not a hint to keep searching.
  3. Walk up from the current working directory.
If none of these finds the adapter file, RootNotFound is raised. Nothing is
guessed, and there is no fallback to the location of this module.

A nested CLAUDE.md does not mark a root: only the adapter file does.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from core import brand

ADAPTER_REL = Path("harness") / "adapter" / "adapter.yaml"

# Safety validation for slugs and session identifiers: no path separators, no
# leading '-' or '.', no trailing newline, bounded length. A value that passes
# can be used as a file-name component without escaping its directory.
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RootNotFound(LookupError):
    """No directory holding the adapter file was found."""


def valid_slug(slug) -> bool:
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None


def _has_adapter(directory: Path) -> bool:
    return (directory / ADAPTER_REL).is_file()


def _walk_up(start: Path) -> Path | None:
    base = Path(start).resolve()
    for directory in [base, *base.parents]:
        if _has_adapter(directory):
            return directory
    return None


def _env_names() -> tuple[str, ...]:
    return (brand.ENV_PREFIX + "PROJECT_DIR",
            *(prefix + "PROJECT_DIR" for prefix in brand.LEGACY_ENV_PREFIXES),
            "CLAUDE_PROJECT_DIR")


def find_root(start=None) -> Path:
    """Return the project root, the directory that holds harness/adapter/adapter.yaml."""
    if start is not None:
        found = _walk_up(Path(start))
        if found is None:
            raise RootNotFound(f"no {ADAPTER_REL} at or above {start}")
        return found
    for name in _env_names():
        value = os.environ.get(name)
        if value:
            variant = Path(value).resolve()
            if not _has_adapter(variant):
                raise RootNotFound(f"{name} is set to {value}, which holds no {ADAPTER_REL}")
            return variant
    found = _walk_up(Path.cwd())
    if found is None:
        raise RootNotFound(f"no {ADAPTER_REL} at or above the working directory")
    return found


def state_dir(root) -> Path:
    """The project state directory. Written under brand.STATE_DIRNAME only."""
    return Path(root) / brand.STATE_DIRNAME


def legacy_state_dir(root) -> Path | None:
    """An existing legacy state directory, for reading only; None if there is none."""
    for name in brand.LEGACY_STATE_DIRNAMES:
        variant = Path(root) / name
        if variant.is_dir():
            return variant
    return None


def unit_path(root, unit_dir: str, slug: str) -> Path:
    """Directory of one unit. `unit_dir` is the adapter's unit_dir, relative to root."""
    if not valid_slug(slug):
        raise ValueError(f"invalid unit slug: {slug!r}")
    return Path(root) / unit_dir / slug


def marker_path(root, session_id) -> Path:
    """Path of the session marker file. The session identifier comes from an
    event, so it is validated before it is used in a file name (B45)."""
    if not valid_slug(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    return state_dir(root) / f"touched-{session_id}"


def _target_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def atomic_write(path, text: str) -> None:
    """Replace `path` with `text` atomically. Keeps the previous mode of an
    existing file, or applies the umask to a new one (B46). mkstemp alone
    would leave every file at 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = _target_mode(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix="~")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
