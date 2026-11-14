#!/usr/bin/env python3
"""SessionStart hook shim: reads the host event, asks core/hosts/session.py for the context.

Fails open: any error exits 0 with nothing on stdout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.hosts import bridge, session  # noqa: E402


def main() -> int:
    host = bridge.HOSTS["claude_code"]
    try:
        event = host.parse(sys.stdin.read())
        reply = session.start_reply(session.project_root(event.raw), event)
    except Exception as exc:
        print(f"session_start: fail open: {exc!r}", file=sys.stderr)
        return 0
    text, code = host.render(reply, "session_start")
    if text:
        sys.stdout.write(text + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
