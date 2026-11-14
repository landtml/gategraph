#!/usr/bin/env python3
"""Compat shim (X1 D2): the handoff replacement is core/hosts/session.py.

Staleness (is_stale), structure (validate_handoff) and the automatic stub
(write_fallback_stub) moved there with the stop and session hooks. The legacy
`check` and `stub` commands have no successor command, so running this file refuses
with a message that names the module that owns the handoff now.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hosts.session import *  # noqa: E402,F401,F403
from core.hosts.session import validate_handoff as validate  # noqa: E402,F401


def main(argv=None) -> int:
    print("handoff: this command has no successor. The stop hook (core/hosts/session.py "
          "stop_reply) and the session start hook own HANDOFF.md now.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
