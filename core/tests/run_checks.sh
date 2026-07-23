#!/usr/bin/env bash
# Frozen entrypoint: the harness core's own test suite. `make check` runs this.
# Exit-code contract: 0 = green, non-zero = the project is not "done".
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
python3 -m unittest discover -s harness/core/tests -p 'test_*.py' -v
