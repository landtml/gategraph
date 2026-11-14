"""Maintenance rituals: a ritual is DUE until a receipt is recorded, receipts
are appended to the state log, and an unknown ritual is refused."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.kernel import maintenance
from fixtures_v2 import make_project


class MaintenanceTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")

    def tearDown(self):
        self._tmp.cleanup()

    def test_due_until_receipt_recorded(self):
        now = datetime(2026, 11, 13, 9, 0, 0)
        before = maintenance.status_text(self.root, now=now)
        self.assertIn("DUE  ledger-triage", before)
        maintenance.record(self.root, "ledger-triage", note="reviewed", now=now)
        after = maintenance.status_text(self.root, now=now + timedelta(days=1))
        self.assertIn("ok   ledger-triage", after)
        self.assertIn("done 2026-11-13 (1d ago)", after)
        self.assertIn("DUE  corpus-rotation", after)

    def test_old_receipt_makes_ritual_due_again(self):
        then = datetime(2026, 10, 1, 9, 0, 0)
        maintenance.record(self.root, "corpus-rotation", now=then)
        text = maintenance.status_text(self.root, now=then + timedelta(days=31))
        self.assertIn("DUE  corpus-rotation", text)

    def test_done_command_refuses_unknown_ritual(self):
        self.assertEqual(maintenance.main(["--root", str(self.root), "done", "nope"]), 1)
        self.assertEqual(maintenance.main(["--root", str(self.root), "done", "ledger-triage"]), 0)
        self.assertIn("ledger-triage", maintenance.log_path(self.root).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
