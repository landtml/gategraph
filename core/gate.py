#!/usr/bin/env python3
"""gate.py, the version-1 gate command (brief D1, D11): a short script shim.

Verbs with a schema-2 equivalent forward to core/cli/gate.py: start (without --title), status,
pause, abandon, escalate. Verbs with no equivalent refuse, exit 1, and name the command to run
next: approve (bug B01: a gate is set only by its own attempt or by a human answer to a request),
proceed (un-pause), repair, and start --title.

Usage (stamped): python3 harness/core/gate.py <verb> ...  (new: harness/core/cli/gate.py)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.compat.shims.gate import *  # noqa: E402,F401,F403

NEW = ".venv/bin/python harness/core/cli/gate.py"


def _refuse(message: str, next_step: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    print(f"next: {next_step}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None)
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", parents=[common])
    s.add_argument("--unit", required=True)
    s.add_argument("--title")
    s.add_argument("--session-id", dest="session_id")
    a = sub.add_parser("approve", parents=[common])
    a.add_argument("gate")
    a.add_argument("--unit", required=True)
    a.add_argument("--note")
    a.add_argument("--by")
    st = sub.add_parser("status", parents=[common])
    st.add_argument("--unit")
    e = sub.add_parser("escalate", parents=[common])
    e.add_argument("--unit", required=True)
    e.add_argument("--note", required=True)
    for name in ("pause", "proceed", "abandon", "repair"):
        q = sub.add_parser(name, parents=[common])
        q.add_argument("--unit", required=True)
        if name in ("proceed", "pause", "abandon"):
            q.add_argument("--session-id", dest="session_id")
    args = p.parse_args(argv)
    slug = getattr(args, "unit", None) or "<slug>"

    if args.cmd == "approve":
        return _refuse(
            f"approve is retired: a gate is set by its attempt, or by answering the unit's "
            f"open request (bug B01). Gate {args.gate!r} was not set.",
            f"{NEW} proceed --decision approve --unit {slug} --by <name>")
    if args.cmd == "proceed":
        return _refuse("proceed (un-pause) is retired: a paused unit continues when its open "
                       "request is answered",
                       f"{NEW} pending --unit {slug}, then answer it with proceed --decision")
    if args.cmd == "repair":
        return _refuse("repair is retired: the schema-2 projection is rebuilt by the runner",
                       f"{NEW} verify --unit {slug}")
    if args.cmd == "start" and args.title is not None:
        return _refuse("start --title has no schema-2 equivalent (the title comes from the "
                       "adapter)", f"{NEW} start --unit {slug}")

    forward = []
    if args.root:
        forward += ["--root", args.root]
    if args.cmd == "start":
        forward += ["start", "--unit", args.unit]
        if args.session_id:
            forward += ["--session-id", args.session_id]
    elif args.cmd == "status":
        forward += ["status"] + (["--unit", args.unit] if args.unit else [])
    elif args.cmd == "escalate":
        forward += ["escalate", "--unit", args.unit, "--note", args.note]
    else:  # pause, abandon
        forward += [args.cmd, "--unit", args.unit]
        if args.session_id:
            forward += ["--session-id", args.session_id]

    from core.cli.gate import main as new_main
    return new_main(forward)


if __name__ == "__main__":
    sys.exit(main())
