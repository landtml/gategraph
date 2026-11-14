"""Generated doc blocks: the current and legacy marker spellings are read, only
the current one is written, and a malformed marker structure is refused."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path

from core import brand
from core.kernel import config, gen
from fixtures_v2 import make_project

LEGACY = brand.LEGACY_GEN_MARKERS[0]


def _cfg(root):
    return config.load(root)


class GenMarkerTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name) / "p", "five_gate")
        self.cfg = _cfg(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_current_and_legacy_markers_read_and_only_current_written(self):
        text = (f"<!-- {LEGACY} gate-table -->\nstale\n<!-- /{LEGACY} -->\n\n"
                f"<!-- {brand.GEN_MARKER} gate-table -->\nstale\n"
                f"<!-- /{brand.GEN_MARKER} -->\n")
        new, touched = gen.regenerate_text(text, self.cfg)
        self.assertEqual(touched, ["gate-table", "gate-table"])
        self.assertNotIn("stale", new)
        self.assertNotIn(f"<!-- {LEGACY} ", new)
        self.assertNotIn(f"<!-- /{LEGACY} -->", new)
        self.assertEqual(new.count(f"<!-- {brand.GEN_MARKER} gate-table -->"), 2)
        self.assertEqual(new.count(f"<!-- /{brand.GEN_MARKER} -->"), 2)
        self.assertIn("| # | Gate |", new)

    def test_unbalanced_markers_are_refused(self):
        text = f"<!-- {brand.GEN_MARKER} gate-table -->\nno closer\n"
        with self.assertRaises(gen.GenError):
            gen.regenerate_text(text, self.cfg)
        self.assertIn("unbalanced", gen.check_markers(text))

    def test_regenerate_project_writes_nothing_when_any_doc_is_malformed(self):
        good = self.root / "README.md"
        good.write_text(f"<!-- {brand.GEN_MARKER} gate-table -->\nx\n"
                        f"<!-- /{brand.GEN_MARKER} -->\n", encoding="utf-8")
        bad = self.root / "docs" / "bad.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(f"<!-- {LEGACY} gate-table -->\n", encoding="utf-8")
        before = good.read_text(encoding="utf-8")
        with self.assertRaises(gen.GenError):
            gen.regenerate(self.root, self.cfg)
        self.assertEqual(good.read_text(encoding="utf-8"), before)

    def test_unknown_block_name_is_left_alone(self):
        text = f"<!-- {brand.GEN_MARKER} no-such-view -->\nkeep\n<!-- /{brand.GEN_MARKER} -->\n"
        new, touched = gen.regenerate_text(text, self.cfg)
        self.assertEqual(new, text)
        self.assertEqual(touched, [])


if __name__ == "__main__":
    unittest.main()
