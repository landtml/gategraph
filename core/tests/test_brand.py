"""Brand constants, the single-spelling rule (B41) and the Python floor (B51).

Runs on the system python3: core/brand.py is standard library only.
"""
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import ast
import re
import unittest

from core import brand

REPO = _bootstrap.REPO
# Directories where the product name must not appear. core/brand.py is the
# only file that may spell it out.
NAME_FREE_DIRS = ("core/kernel", "core/graph", "core/agent",
                  "core/tools", "core/hosts")


class TestBrandConstants(unittest.TestCase):
    def test_required_constants_exist(self):
        for attr in ("NAME", "SLUG", "STATE_DIRNAME", "GEN_MARKER",
                     "LEGACY_GEN_MARKERS", "ENV_PREFIX", "LEGACY_ENV_PREFIXES",
                     "MIN_PYTHON"):
            self.assertTrue(hasattr(brand, attr), f"core/brand.py lacks {attr}")

    def test_min_python_is_3_11(self):
        self.assertEqual(brand.MIN_PYTHON, (3, 11))

    def test_B51_matrix_starts_at_min_python(self):
        matrix = brand.python_matrix()
        self.assertEqual(matrix[0], "3.11")
        self.assertEqual(matrix, sorted(matrix, key=lambda v: tuple(map(int, v.split(".")))))
        self.assertNotIn("3.10", matrix)
        self.assertNotIn("3.9", matrix)

    def test_B51_ci_fast_matrix_equals_brand_python_matrix(self):
        ci = REPO / ".github/workflows/ci.yml"
        if not ci.is_file():
            # A stamped project has no .github/ (this test is copied into
            # harness/core/tests and self-checked there). Template only.
            self.skipTest("template repo only (no .github/workflows/ci.yml in stamps)")
        text = ci.read_text(encoding="utf-8")
        found = re.findall(r"^\s*python:\s*(\[[^\]]*\])\s*$", text, re.MULTILINE)
        self.assertEqual(len(found), 1, "expected exactly one literal python matrix in ci.yml")
        self.assertEqual(list(ast.literal_eval(found[0])), brand.python_matrix())
        self.assertNotIn('"3.10"', text)


class TestB41SingleSpelling(unittest.TestCase):
    """Docs and code used to disagree: docs said gategraph:gen and .gategraph/,
    code recognised fh:gen and .fh/. Written spellings must be the new ones;
    the legacy ones are read only."""

    def test_B41_written_spellings_are_new(self):
        self.assertEqual(brand.GEN_MARKER, "gategraph:gen")
        self.assertEqual(brand.STATE_DIRNAME, ".gategraph")
        self.assertEqual(brand.ENV_PREFIX, "GATEGRAPH_")

    def test_B41_legacy_spellings_are_read_only_list(self):
        self.assertIn("fh:gen", brand.LEGACY_GEN_MARKERS)
        self.assertIn("fh:auto-handoff", brand.LEGACY_GEN_MARKERS)
        self.assertIn("FH_", brand.LEGACY_ENV_PREFIXES)
        self.assertIn("fh_schema", brand.LEGACY_SCHEMA_KEYS)
        self.assertIn(".fh", brand.LEGACY_STATE_DIRNAMES)
        for legacy in brand.LEGACY_GEN_MARKERS + brand.LEGACY_ENV_PREFIXES:
            self.assertNotEqual(legacy, brand.GEN_MARKER)
            self.assertNotEqual(legacy, brand.ENV_PREFIX)

    def test_B41_name_only_in_brand_py(self):
        needle = brand.NAME.lower().encode("utf-8")
        offenders = []
        for d in NAME_FREE_DIRS:
            root = REPO / d
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                if needle in path.read_bytes().lower():
                    offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "product name found outside core/brand.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
