"""Self-lint: prose is not a gate name (B13), only manifest directories are
units and short slugs are skipped with a warning (B37), and the product name
is spelled only in core/brand.py (brand-literal)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import subprocess
import tempfile
import unittest
from pathlib import Path

from core import brand
from core.kernel import config, selflint
from fixtures_v2 import make_project


class SelfLintTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")
        self.cfg = config.load(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_B13_prose_approve_is_not_a_gate_name(self):
        self._write("README.md", "Humans approve changes before merge.\n")
        findings = selflint.run_lint(self.root, self.cfg, tier="gating")
        drift = [f for f in findings if f.check == "doc-cmd-drift"]
        self.assertEqual(drift, [], [f.message for f in drift])

    def test_B13_code_span_naming_unknown_gate_still_fails(self):
        self._write("README.md", "Run `approve no-such-gate` to continue.\n")
        findings = selflint.run_lint(self.root, self.cfg, tier="gating")
        self.assertTrue(any(f.check == "doc-cmd-drift"
                            and "unknown gate 'no-such-gate'" in f.message
                            for f in findings))

    def test_B37_only_dirs_with_manifest_are_units(self):
        units = self.root / self.cfg.unit_dir
        # make_project already creates unit-001 with no manifest: not a unit
        (units / "unit-001").mkdir(parents=True, exist_ok=True)
        (units / "unit-002").mkdir()
        (units / "unit-002" / "manifest.yaml").write_text("gate: composed\n")
        slugs, _ = selflint.unit_slugs(self.root, self.cfg.unit_dir)
        self.assertEqual(slugs, ["unit-002"])

    def test_B37_short_slug_skipped_with_warning(self):
        units = self.root / self.cfg.unit_dir
        (units / "ab").mkdir(parents=True)
        (units / "ab" / "manifest.yaml").write_text("gate: composed\n")
        (units / "unit-003").mkdir()
        (units / "unit-003" / "manifest.yaml").write_text("gate: composed\n")
        slugs, warnings = selflint.unit_slugs(self.root, self.cfg.unit_dir, min_len=3)
        self.assertEqual(slugs, ["unit-003"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("'ab'", warnings[0])
        findings = selflint.run_lint(self.root, self.cfg, tier="warning")
        self.assertTrue(any(f.check == "unit-slug" and f.tier == "warning"
                            and "'ab'" in f.message for f in findings))

    def test_brand_literal_flags_spelling_outside_brand_module(self):
        spelled = brand.NAME  # assembled at run time, never a literal in source
        self._write("core/kernel/leaky.py", f'TITLE = "{spelled} tool"\n')
        self._write("core/brand.py", f'NAME = "{spelled}"\n')
        findings = selflint.run_lint(self.root, self.cfg, tier="gating")
        hits = [f for f in findings if f.check == "brand-literal"]
        self.assertEqual(len(hits), 1, [f.message for f in hits])
        self.assertIn("core/kernel/leaky.py:1", hits[0].message)


class TemplateModeTests(unittest.TestCase):
    """X2 exit check: the template root has no adapter. Template mode skips only the
    project-only checks (adapter-complete, hook registration) and runs the rest."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "template"
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "guide.md").write_text("Run `make test` first.\n",
                                                     encoding="utf-8")
        (self.root / "Makefile").write_text("test:\n\techo ok\n", encoding="utf-8")

    def test_missing_adapter_is_template_mode_and_clean(self):
        self.assertTrue(selflint.is_template(self.root))
        self.assertEqual(selflint.run_lint(self.root, tier="gating"), [])

    def test_template_mode_still_runs_the_doc_checks(self):
        (self.root / "docs" / "guide.md").write_text("Run `make nothing` first.\n",
                                                     encoding="utf-8")
        findings = selflint.run_lint(self.root, tier="gating")
        self.assertEqual([f.check for f in findings], ["doc-cmd-drift"])

    def test_invalid_adapter_is_not_template_mode(self):
        (self.root / "harness" / "adapter").mkdir(parents=True)
        (self.root / "harness" / "adapter" / "adapter.yaml").write_text("{ bad: [\n",
                                                                       encoding="utf-8")
        self.assertFalse(selflint.is_template(self.root))
        findings = selflint.run_lint(self.root, tier="gating")
        self.assertEqual([f.check for f in findings], ["adapter-complete"])

    def test_run_by_path_from_another_directory_prints_clean(self):
        script = Path(__file__).resolve().parents[2] / "kernel" / "selflint.py"
        proc = subprocess.run([sys.executable, str(script), "--tier", "gating",
                               "--root", str(self.root)],
                              cwd=self._tmp.name, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clean (gating)", proc.stdout)


if __name__ == "__main__":
    unittest.main()
