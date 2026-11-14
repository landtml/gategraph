"""The six compat shims that X1 B5b left in core/ (D2): each stamped path still runs, and the
one with no successor command refuses non-zero with a message (never a silent 0).

Run alone with: python3 core/tests/kernel/test_compat_shims.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures_v2 import make_project

CORE = Path(_bootstrap.REPO) / "core"
SHIMS = ("harness_lint.py", "memory.py", "reap.py", "gen.py", "maintenance.py")


def run(script, *args, cwd):
    return subprocess.run([sys.executable, str(CORE / script), *args],
                          capture_output=True, text=True, cwd=str(cwd))


class TestShimsRun(unittest.TestCase):
    def test_each_shim_runs_its_kernel_module(self):
        for script in SHIMS:
            r = run(script, "--help", cwd=_bootstrap.REPO)
            self.assertEqual(r.returncode, 0, f"{script}: {r.stderr}")
            self.assertNotIn("Traceback", r.stderr, script)

    def test_harness_lint_shim_gates_unregistered_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp) / "p", "minimal")
            (root / "harness" / "adapter" / "checks").mkdir(parents=True, exist_ok=True)
            (root / "harness" / "adapter" / "checks" / "lint.py").write_text("# check\n")
            (root / "CLAUDE.md").write_text("# fixture\n")
            (root / "MEMORY.md").write_text("# MEMORY\n")
            (root / "HANDOFF.md").write_text("# HANDOFF\n\n## Now\n\nx\n\n## Next\n\ny\n")
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text(
                '{"hooks": {"h": [{"command": "x"}]}}')
            r = run("harness_lint.py", "--tier", "gating", "--root", str(root), cwd=root)
            # the settings above register no shims, so the gating tier must name them, not pass
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("hook not registered", r.stdout)

    def test_handoff_refuses_with_a_message_naming_the_owner(self):
        r = run("handoff.py", cwd=_bootstrap.REPO)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("core/hosts/session.py", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
