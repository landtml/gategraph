"""The maintenance checklist, ported from the legacy core/tests/test_instruments.py (X1 B5b):
T13 status and receipts, and the make target in the template and in the seed.

The flags-verify ritual is not ported: the kernel module drops it (the Claude CLI pin check
moved to the claude_cli port), so its test is deleted with the feature.

Run alone with: python3 core/tests/kernel/test_maintenance_port.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from core.kernel import maintenance
from fixtures_v2 import make_project

TEMPLATE = Path(_bootstrap.REPO)


class TestMaintenance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")

    def cli(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = maintenance.main(["--root", str(self.root), *args])
        return rc, buf.getvalue(), err.getvalue()

    def test_fresh_root_everything_due(self):
        rc, out, _ = self.cli("status")
        self.assertEqual(rc, 0, "the checklist informs; it never gates")
        self.assertIn("corpus-rotation", out)
        self.assertIn("ledger-triage", out)
        self.assertIn("never", out)
        self.assertIn("DUE", out)

    def test_done_records_a_receipt_and_clears_due(self):
        rc, out, _ = self.cli("done", "ledger-triage", "--note", "clean")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.cli("status")
        triage_line = next(ln for ln in out.splitlines() if "ledger-triage" in ln)
        self.assertNotIn("DUE", triage_line)
        self.assertTrue(maintenance.log_path(self.root).is_file(),
                        "the rhythm must leave a receipt")

    def test_unknown_ritual_refused(self):
        rc, out, err = self.cli("done", "bogus-ritual")
        self.assertEqual(rc, 1)
        self.assertIn("unknown ritual", (out + err).lower())


class TestMaintenanceMakeTargets(unittest.TestCase):
    """`make maintenance` must exist in the template and in every stamped project (seed).
    Template-only: the seed Makefile is stamped once, and new core tests may not assert on
    seed-generation surface, so the seed check skips when there is no seed/ (a stamp)."""

    def test_template_makefile_target_runs_the_kernel_module(self):
        text = (TEMPLATE / "Makefile").read_text(encoding="utf-8")
        self.assertIn("maintenance:", text)
        self.assertIn("core.kernel.maintenance", text)

    def test_seed_makefile_target_runs_the_stamped_shim(self):
        seed = TEMPLATE / "seed" / "Makefile"
        if not seed.is_file():
            self.skipTest("no seed in this tree (stamped project)")
        text = seed.read_text(encoding="utf-8")
        self.assertIn("maintenance:", text)
        self.assertIn("harness/core/maintenance.py", text)  # the shim keeps this path working


if __name__ == "__main__":
    unittest.main(verbosity=2)
