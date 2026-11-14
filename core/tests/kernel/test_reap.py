"""Reaping: committed runtime state blocks the reap and deletes nothing; stale
files past their age are removed, fresh ones kept."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from core.kernel import reap


class ReapTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_committed_state_blocks_reap_and_deletes_nothing(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        state = self.root / ".gategraph" / "run.pid"
        state.parent.mkdir(parents=True)
        state.write_text("123\n", encoding="utf-8")
        os.utime(state, (time.time() - 30 * 86400,) * 2)
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "--", ".gategraph"],
                       check=True)
        result = reap.reap(self.root)
        self.assertEqual(result.exit_code, 1)
        self.assertIn(".gategraph/run.pid", result.committed)
        self.assertTrue(state.exists())

    def test_stale_file_removed_fresh_file_kept(self):
        old, fresh = self.root / "old.pid", self.root / "fresh.pid"
        old.write_text("1\n", encoding="utf-8")
        fresh.write_text("2\n", encoding="utf-8")
        os.utime(old, (time.time() - 10 * 86400,) * 2)
        result = reap.reap(self.root, max_age_days=7)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual([p.name for p in result.removed], ["old.pid"])

    def test_dry_run_deletes_nothing(self):
        old = self.root / "old.pid"
        old.write_text("1\n", encoding="utf-8")
        os.utime(old, (time.time() - 10 * 86400,) * 2)
        result = reap.reap(self.root, max_age_days=7, dry_run=True)
        self.assertTrue(old.exists())
        self.assertEqual(result.removed, [])
        self.assertEqual([p.name for p in result.victims], ["old.pid"])


if __name__ == "__main__":
    unittest.main()
