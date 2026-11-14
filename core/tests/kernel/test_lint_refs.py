"""Wikilink integrity (the version-1 `lint refs` tests, ported): an unresolved [[wikilink]] is a
gating orphan-refs finding, and resolved links are clean. Kernel layer: standard library only."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import tempfile
import types
import unittest
from pathlib import Path

from core.kernel import selflint


class RefsTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = types.SimpleNamespace(checks=[])

    def tearDown(self):
        self._tmp.cleanup()

    def test_unresolved_wikilink_is_flagged(self):
        (self.root / "MEMORY.md").write_text("see [[missing-doc]]\n", encoding="utf-8")
        found = selflint.check_orphan_refs(self.root, self.cfg)
        self.assertTrue(any("missing-doc" in f.message for f in found), [f.message for f in found])

    def test_resolved_wikilink_is_clean(self):
        (self.root / "MEMORY.md").write_text("see [[CLAUDE]]\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("# claude\n", encoding="utf-8")
        found = selflint.check_orphan_refs(self.root, self.cfg)
        self.assertFalse([f for f in found if "unresolved wikilink" in f.message],
                         [f.message for f in found])


if __name__ == "__main__":
    unittest.main()
