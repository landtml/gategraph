#!/usr/bin/env python3
"""Stop hook shim: reads the host event, asks core/hosts/session.py, renders the reply.

Fails open: any error exits 0 with nothing on stdout, so a broken hook never holds a session.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.hosts import bridge, session  # noqa: E402


def main() -> int:
    host = bridge.HOSTS["claude_code"]
    try:
        event = host.parse(sys.stdin.read())
        reply = session.stop_reply(session.project_root(event.raw), event)
    except Exception as exc:
        print(f"stop_gate: fail open: {exc!r}", file=sys.stderr)
        return 0
    text, code = host.render(reply, "stop")
    if text:
        sys.stdout.write(text + "\n")
    if reply.reason and not reply.block:
        print(reply.reason, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
