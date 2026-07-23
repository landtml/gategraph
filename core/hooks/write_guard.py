#!/usr/bin/env python3
"""PreToolUse hook, the PRIMARY write-scope guard for loop workers.

The FLAGS.md spike found that ``--allowedTools "Write(<glob>)"`` path-scoping does **not**
reliably apply in headless ``claude -p`` (the parenthesised path rule denied every write). So we
allow file writes bluntly at the permission layer (``--permission-mode acceptEdits`` / bare
``Write`` allow) to avoid hangs, and enforce the *actual* path scope here, deterministically.

Contract (Claude Code PreToolUse): the event JSON arrives on stdin with ``tool_name`` and
``tool_input``. Exit 0 → allow the tool; exit 2 → block it and surface stderr to the model.
This guard **fails closed**: unparseable payload, unset scope, or a missing/parent-escaping path
all deny. Scope comes from ``$HARNESS_WRITE_SCOPE`` (an absolute directory the driver sets per
variant). Injected per-worker via a driver-written ``--settings`` file, deliberately NOT
registered in the project's .claude/settings.json (it would deny every interactive write).

Deliberately minimal and dependency-free, stdlib only, so it drops into any headless
Claude Code harness unchanged.
"""
from __future__ import annotations

import json
import os
import sys

# File-mutating tools we scope. Read/Grep/Glob are read-only; Bash/Task/etc. are blocked upstream
# via --disallowedTools, but we also deny them here as defence in depth if one slips through.
MUTATORS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _target_path(tool_input: dict) -> str:
    return (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("path")
        or ""
    )


def decide(event: dict, scope: "str | None") -> "tuple[int, str]":
    """Pure decision: return ``(exit_code, reason)``. Kept import-testable (no I/O)."""
    tool = event.get("tool_name", "")
    if tool not in MUTATORS:
        return 0, ""  # not a mutator; nothing to scope
    if not scope:
        return 2, "HARNESS_WRITE_SCOPE unset: denying all writes (fail-closed)"
    path = _target_path(event.get("tool_input") or {})
    if not path:
        return 2, f"{tool}: no target path in tool_input, deny"
    scope_abs = os.path.realpath(scope)
    target_abs = os.path.realpath(path if os.path.isabs(path) else os.path.join(os.getcwd(), path))
    if target_abs == scope_abs or target_abs.startswith(scope_abs + os.sep):
        return 0, ""
    return 2, f"{tool}: blocked write to {target_abs} outside scope {scope_abs}"


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001, fail closed on any parse failure
        print(f"write_guard: unparseable hook payload ({exc})", file=sys.stderr)
        return 2
    code, reason = decide(event, os.environ.get("HARNESS_WRITE_SCOPE"))
    if code != 0:
        print(f"write_guard: {reason}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
