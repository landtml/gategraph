#!/usr/bin/env python3
"""posttool_lint.py, PostToolUse full-severity lint.

Normal mode (matcher Write|Edit|NotebookEdit): scan the written file with the
adapter deny policies and run matching adapter checks. A fatal finding -> exit
2 with the reason on stderr (fed back to the model); advisory -> exit 0 with a
note on stdout. Arms the Stop gate (touched-<session_id>) when the write lands
in the active unit.

--bash-relint mode (matcher Bash): closes the Bash-redirect bypass, a sed /
tee / >> edit cannot dodge the Write hook. Early-exits when no unit is active;
otherwise re-lints every file in the active unit dir (registered or not).

Fail-open: any internal error is logged and returns 0.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

# Total wall-clock budget for adapter checks per hook invocation. Must sit
# WELL UNDER the hook's own timeout in .claude/settings.json (seed: 90s) or
# the host kills the hook and delivers nothing, a slow check then silently
# disables enforcement (F31). Checks that need longer belong in `make check`.
HOOK_CHECK_BUDGET_S = float(os.environ.get("FH_HOOK_CHECK_BUDGET", "75"))


def _report(fatal: list, warn: list) -> int:
    if fatal:
        print("PostToolUse lint BLOCKED: fatal policy violation(s):", file=sys.stderr)
        for x in fatal:
            print(f"  ✗ {x}", file=sys.stderr)
        print("Fix the content and re-write it (this content was flagged, "
              "not accepted as final).", file=sys.stderr)
        return 2
    if warn:
        print("PostToolUse lint (advisory, not blocking):")
        for x in warn:
            print(f"  · {x}")
    return 0


def main(argv) -> int:
    event = c.read_event()
    root = c.project_root()
    session_id = event.get("session_id")

    try:
        from core import config, lint, paths
        cfg = config.load(root)
    except Exception as exc:
        c.log_exception("posttool_lint: config load failed", exc)
        return 0  # fail-open; pretool_guard already fail-closes writes

    if "--bash-relint" in argv:
        active = paths.read_active(root, cfg)
        if not active:
            return 0
        files = lint.unit_files(root, cfg, active["slug"])
        fatal, warn = lint.scan_files(root, cfg, files)
        cf, cw = lint.run_adapter_checks(root, cfg, files, active["slug"],
                                         budget_s=HOOK_CHECK_BUDGET_S)
        return _report(fatal + cf, warn + cw)

    rel = c.rel_to_root(root, c.target_path(event))
    if rel is None or rel.startswith("harness/logs/"):
        return 0
    path = root / rel

    active = paths.read_active(root, cfg)
    if active and rel.startswith(f"{cfg.unit_dir}/{active['slug']}/"):
        paths.touch_marker(root, session_id)  # arm the Stop gate

    fatal, warn = lint.scan_files(root, cfg, [path])
    slug = active["slug"] if active else None
    cf, cw = lint.run_adapter_checks(root, cfg, [path], slug,
                                     budget_s=HOOK_CHECK_BUDGET_S)
    return _report(fatal + cf, warn + cw)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        c.log_exception("posttool_lint: unhandled", exc)
        sys.exit(0)
