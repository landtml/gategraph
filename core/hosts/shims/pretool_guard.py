"""PreToolUse shim (H1). Replaces core/hooks/pretool_guard.py at X1; the decision is in core/hosts."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from core.hosts import claude_code  # noqa: E402

if __name__ == "__main__":
    sys.exit(claude_code.main("pre_tool"))
