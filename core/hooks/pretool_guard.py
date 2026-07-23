#!/usr/bin/env python3
"""pretool_guard.py, PreToolUse guard for Write|Edit|NotebookEdit.

Denies only deterministic, zero-false-positive things:
  - a write to a protected path (adapter protected_paths, harness-owned files)
  - written content matching a deny policy (unless the policy opts out with
    pretool: false; nuanced policies belong in PostToolUse / checks instead)

Asks (not denies) for writes into adapter ask-policy trees.

Fail-safety: fail OPEN on any internal error, EXCEPT fail CLOSED (deny) when
adapter.yaml itself cannot be loaded, a policy set we cannot read must not
silently pass. Writes under harness/adapter/ stay allowed in that state so the
config can be repaired.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}


def main() -> int:
    event = c.read_event()
    if event.get("tool_name") not in WRITE_TOOLS:
        return 0
    root = c.project_root()
    rel = c.rel_to_root(root, c.target_path(event))
    content = c.written_content(event)

    try:
        from core import config, policy
        cfg = config.load(root)
        # Compile inside the fail-closed guard: a policy set we cannot
        # compile must refuse writes, exactly like a config we cannot load.
        pols = [p for p in policy.compile_policies(cfg) if p["pretool"]]
    except Exception as exc:
        c.log_exception("pretool_guard: config load failed", exc)
        if rel is None or rel.startswith("harness/adapter/"):
            return 0  # outside the repo, or repairing the config itself
        c.emit_pre_decision(
            "deny", "harness/adapter/adapter.yaml could not be loaded, so policy "
                    "enforcement cannot run, refusing this write (fail-closed). "
                    "Fix adapter.yaml first (writes there are still allowed).")
        return 0

    if rel and policy.is_protected(cfg, rel):
        c.emit_pre_decision(
            "deny", f"{rel} is harness-writable only: gates and the .active "
                    f"pointer are managed by harness/core tools, never edited.")
        return 0

    for pol in pols:
        if rel is not None and not policy.scope_match(pol["scope"], rel):
            continue
        if pol["scope"] != ["any"] and rel is None:
            continue
        if pol["regex"].search(content):
            c.emit_pre_decision("deny", f"[{pol['id']}] {pol['summary']} "
                                        f"(adapter.yaml deny policy)")
            return 0

    if rel:
        reason = policy.ask_reason(cfg, rel)
        if reason:
            c.emit_pre_decision("ask", reason)
            return 0
    return 0  # allow silently


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never brick the session
        c.log_exception("pretool_guard: unhandled", exc)
        sys.exit(0)
