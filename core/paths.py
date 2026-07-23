"""paths.py, repo-root discovery and unit/state layout helpers.

Root discovery order:
  1. $CLAUDE_PROJECT_DIR (set by Claude Code inside hooks), if it holds CLAUDE.md.
  2. Walk up from `start` (or CWD) to the directory containing CLAUDE.md.

The `<unit_dir>/.active` file records the single in-flight unit as
`{slug, session_id}` and is owned exclusively by gate.py.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required (pip install pyyaml)") from exc

MARKER = "CLAUDE.md"

# Safety (not style) validation for runtime unit slugs: no path separators,
# no leading '-'/'.', so a slug can never escape units_dir or read as a CLI
# option. gatebench.py enforces the stricter style SLUG_RE at init time.
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def valid_slug(slug) -> bool:
    return isinstance(slug, str) and bool(SLUG_RE.match(slug))


def find_repo_root(start: Path | None = None) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and (Path(env) / MARKER).exists():
        return Path(env)
    base = (Path(start) if start is not None else Path.cwd()).resolve()
    for d in [base, *base.parents]:
        if (d / MARKER).exists():
            return d
    # Fallback: harness/core/paths.py -> the tree that contains harness/.
    return Path(__file__).resolve().parents[2]


def units_dir(root: Path, cfg) -> Path:
    return Path(root) / cfg.unit_dir


def unit_path(root: Path, cfg, slug: str) -> Path:
    return units_dir(root, cfg) / slug


def manifest_path(root: Path, cfg, slug: str) -> Path:
    return unit_path(root, cfg, slug) / "manifest.yaml"


def active_path(root: Path, cfg) -> Path:
    return units_dir(root, cfg) / ".active"


def lock_path(root: Path, cfg) -> Path:
    # A stable file, NOT .active itself: atomic_write replaces .active's inode
    # on every update, which would silently detach any flock held on it. Lives
    # in state/ (gitignored) because the lock's scope is the project root.
    return state_dir(root) / "gate.lock"


class LockHeld(Exception):
    """Another session holds this project's gate lock."""

    def __init__(self, holder: dict | None):
        self.holder = holder or {}
        who = self.holder.get("session_id") or "unknown session"
        slug = self.holder.get("slug")
        super().__init__(f"held by {who}" + (f" (unit {slug})" if slug else ""))


class session_lock:
    """Advisory flock enforcing ONE mutating gate session per project root
    (Part 1 item 5). Non-blocking: raises LockHeld, with the current .active
    holder for the refusal message, instead of waiting. POSIX-only, like the
    rest of the template."""

    def __init__(self, root: Path, cfg):
        self.root, self.cfg = Path(root), cfg
        self._fh = None

    def __enter__(self):
        import fcntl
        lp = lock_path(self.root, self.cfg)
        lp.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(lp, "w", encoding="utf-8")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = read_active(self.root, self.cfg)
            self._fh.close()
            self._fh = None
            raise LockHeld(holder) from None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            import fcntl
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        return False


def state_dir(root: Path) -> Path:
    return Path(root) / "harness" / "state"


# --- session markers (arm the Stop gate for the session that did the work) ---
def marker_path(root: Path, session_id: str) -> Path:
    return state_dir(root) / f"touched-{session_id}"


def touch_marker(root: Path, session_id: str | None) -> None:
    if not session_id:
        return
    p = marker_path(root, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def marker_exists(root: Path, session_id: str | None) -> bool:
    return bool(session_id) and marker_path(root, session_id).exists()


# --- the .active pointer ------------------------------------------------------
def read_active(root: Path, cfg) -> dict | None:
    p = active_path(root, cfg)
    if not p.exists():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or "slug" not in data:
        return None
    return data


def write_active(root: Path, cfg, slug: str, session_id: str | None) -> None:
    atomic_write(active_path(root, cfg),
                 yaml.safe_dump({"slug": slug, "session_id": session_id},
                                sort_keys=True))


def clear_active(root: Path, cfg) -> None:
    active_path(root, cfg).unlink(missing_ok=True)


def atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix="~")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
