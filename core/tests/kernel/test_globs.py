"""Glob dialect (B34): `*` never crosses a separator, `**` matches zero or more
directories. One function, used by every pattern match in the kernel."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import unittest

from core.kernel.globs import glob_match


class TestGlobDialect(unittest.TestCase):
    def test_B34_star_does_not_cross_separator(self):
        self.assertTrue(glob_match("units/*/manifest.yaml", "units/a/manifest.yaml"))
        self.assertFalse(glob_match("units/*/manifest.yaml", "units/a/b/manifest.yaml"))
        self.assertFalse(glob_match("dossiers/*/*.tex", "dossiers/case/sub/x.tex"))
        self.assertFalse(glob_match("*.tex", "a/x.tex"))
        self.assertTrue(glob_match("*.tex", "x.tex"))

    def test_B34_doublestar_matches_zero_dirs(self):
        self.assertTrue(glob_match("units/**/*.tex", "units/x.tex"))
        self.assertTrue(glob_match("units/**/*.tex", "units/a/x.tex"))
        self.assertTrue(glob_match("units/**/*.tex", "units/a/b/x.tex"))
        self.assertFalse(glob_match("units/**/*.tex", "other/x.tex"))
        self.assertTrue(glob_match("**/main.tex", "main.tex"))
        self.assertTrue(glob_match("**/main.tex", "a/b/main.tex"))

    def test_trailing_doublestar_needs_something_under_it(self):
        self.assertTrue(glob_match("a/**", "a/x"))
        self.assertTrue(glob_match("a/**", "a/x/y"))
        self.assertFalse(glob_match("a/**", "b/x"))

    def test_question_mark_and_classes_stay_inside_a_segment(self):
        self.assertTrue(glob_match("doc?.tex", "doc1.tex"))
        self.assertFalse(glob_match("doc?.tex", "doc/.tex"))
        self.assertTrue(glob_match("doc[12].tex", "doc2.tex"))
        self.assertFalse(glob_match("doc[!12].tex", "doc2.tex"))

    def test_paths_are_normalized_before_matching(self):
        self.assertTrue(glob_match("units/**/*.tex", "./units//a/x.tex"))

    def test_match_is_case_sensitive_and_whole_path(self):
        self.assertFalse(glob_match("units/*.tex", "Units/x.tex"))
        self.assertFalse(glob_match("units/*.tex", "units/x.tex.bak"))

    def test_literal_metacharacters_are_escaped(self):
        self.assertTrue(glob_match("a+b(1).txt", "a+b(1).txt"))
        self.assertFalse(glob_match("a+b(1).txt", "aab(1).txt"))


if __name__ == "__main__":
    unittest.main()
