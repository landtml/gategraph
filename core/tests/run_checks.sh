#!/usr/bin/env bash
# Stamped shim (item 10): the legacy `make check` path runs this file, so a migrated project's
# own Makefile still reaches the kernel. It runs the two checks that the seed Makefile's `check`
# runs now: the self-lint gating tier, then the kernel self-test. Exit 0 = green.
# Stamped location: harness/core/tests/run_checks.sh. The full core suite is not stamped.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
cd "$ROOT"
PYTHONPATH="$ROOT/harness" python3 -m core.kernel.selflint --tier gating --root .
python3 "$HERE/selftest.py"
