"""_common.py, shared, defensive plumbing for all core hooks.

Design: every hook FAILS OPEN. A hook that cannot parse its input or reach the
repo must never brick the session, it logs to harness/logs/hooks.log
(gitignored) and exits 0. The single exception is pretool_guard's fail-CLOSED
deny when adapter.yaml itself cannot be loaded (a lint we cannot run must not
silently pass).
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import paths  # noqa: E402


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and (Path(env) / "CLAUDE.md").exists():
        return Path(env)
    return paths.find_repo_root()


def read_event() -> dict:
    """Parse the JSON event from stdin. Returns {} on any failure (fail-open)."""
    try:
        data = json.loads(sys.stdin.read())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def log_exception(where: str, exc: BaseException | str) -> None:
    """Append a diagnostic to harness/logs/hooks.log; never raise."""
    try:
        logdir = project_root() / "harness" / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        with open(logdir / "hooks.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] {where}\n")
            if isinstance(exc, str):
                f.write(exc + "\n")
            else:
                f.write("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)))
    except Exception:
        pass  # logging must never break a hook


def tool_input(event: dict) -> dict:
    ti = event.get("tool_input")
    return ti if isinstance(ti, dict) else {}


def target_path(event: dict) -> str | None:
    ti = tool_input(event)
    for key in ("file_path", "notebook_path", "path"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def written_content(event: dict) -> str:
    """The text a Write/Edit/NotebookEdit would put on disk."""
    ti = tool_input(event)
    parts = [v for key in ("content", "new_string", "new_source", "source")
             if isinstance((v := ti.get(key)), str)]
    return "\n".join(parts)


def rel_to_root(root: Path, path_str: str | None) -> str | None:
    """Repo-relative posix path, or None if outside the repo / unresolvable."""
    if not path_str:
        return None
    try:
        p = Path(path_str)
        if not p.is_absolute():
            p = root / p
        return p.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def emit_pre_decision(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
