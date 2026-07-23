"""P3 sync tests: gatebench update / diff / upstream / gen.

Each test works on a scratch COPY of the template repo so it can mutate core
files and VERSION freely; the real template is never touched.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent

ANSWERS = {"project": "syncproj", "artifact_noun": "note", "unit_dir": "notes",
           "gates": ["composed", "checked", "verified"], "hashed_gates": [],
           "seed_deny": True, "loop_enabled": False}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.template = base / "template"
        self.template.mkdir()
        for item in ("gatebench.py", "VERSION", "core", "seed"):
            src = TEMPLATE / item
            if src.is_dir():
                shutil.copytree(src, self.template / item,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, self.template / item)
        self.project = base / "syncproj"
        r = self.fh("init", str(self.project), "--answers", self._answers(base))
        assert r.returncode == 0, r.stdout + r.stderr

    def tearDown(self):
        self._tmp.cleanup()

    def _answers(self, base: Path) -> str:
        p = base / "answers.yaml"
        p.write_text(yaml.safe_dump(ANSWERS))
        return str(p)

    def fh(self, *args):
        return subprocess.run([sys.executable, str(self.template / "gatebench.py"), *args],
                              capture_output=True, text=True)

    def bump_template(self, rel="core/PROTOCOL.md",
                      extra="\n## New template guidance\n\nAdded upstream.\n"):
        """Simulate a template release: change a core file + bump VERSION."""
        f = self.template / rel
        f.write_text(f.read_text() + extra)
        self.template.joinpath("VERSION").write_text("0.0.2\n")


class TestUpdate(Base):
    def test_pristine_file_overwritten_and_rerecorded(self):
        self.bump_template()
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stamped = (self.project / "harness/core/PROTOCOL.md").read_text()
        self.assertIn("New template guidance", stamped)
        # manifest re-recorded: project's own stamp check must stay green
        check = subprocess.run(["bash", "harness/core/tests/run_checks.sh"],
                               cwd=self.project, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        manifest = (self.project / "harness/.fh/MANIFEST.json").read_text()
        self.assertIn("0.0.2", manifest)

    def test_modified_file_gets_fhnew_and_drift_report(self):
        target = self.project / "harness/core/PROTOCOL.md"
        target.write_text(target.read_text() + "\nLOCAL HOTFIX\n")
        self.bump_template()
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("LOCAL HOTFIX", target.read_text(),
                      "modified file must never be clobbered")
        fhnew = target.with_suffix(".md.fhnew")
        self.assertTrue(fhnew.exists(), ".fhnew sidecar missing")
        self.assertIn("New template guidance", fhnew.read_text())
        self.assertIn("PROTOCOL.md", r.stdout)  # drift report names the file

    def test_new_template_file_is_stamped(self):
        newfile = self.template / "core" / "brand_new.py"
        newfile.write_text('"""new core module"""\n')
        self.bump_template()
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.project / "harness/core/brand_new.py").exists())

    def test_removed_template_file_reaped_when_pristine(self):
        (self.template / "core" / "doomed.py").write_text("x = 1\n")
        r = self.fh("update", str(self.project))  # stamp it first
        self.assertEqual(r.returncode, 0)
        (self.template / "core" / "doomed.py").unlink()
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.project / "harness/core/doomed.py").exists())


class TestDiff(Base):
    def test_reports_modified_and_pristine(self):
        target = self.project / "harness/core/PROTOCOL.md"
        target.write_text(target.read_text() + "\nLOCAL EDIT\n")
        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 1, "drift present -> exit 1")
        self.assertIn("PROTOCOL.md", r.stdout)

    def test_clean_project_exits_zero(self):
        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestUpstream(Base):
    def test_promotes_project_edit_into_template(self):
        target = self.project / "harness/core/PROTOCOL.md"
        target.write_text(target.read_text() + "\n## Battle-tested addition\n")
        r = self.fh("upstream", str(self.project), "harness/core/PROTOCOL.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Battle-tested addition",
                      (self.template / "core/PROTOCOL.md").read_text())
        # the project file is pristine again afterwards
        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestGen(Base):
    def test_regenerates_gate_table_after_adapter_change(self):
        adapter_path = self.project / "harness/adapter/adapter.yaml"
        adapter = yaml.safe_load(adapter_path.read_text())
        wf = adapter["workflow"]
        wf["gates"] = ["composed", "checked", "verified", "shipped"]
        wf["gate_commands"]["shipped"] = \
            "python3 harness/core/gate.py approve shipped --unit {slug}"
        adapter_path.write_text(yaml.safe_dump(adapter, sort_keys=False))
        r = self.fh("gen", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        wf_doc = (self.project / ".claude/skills/syncproj/workflow.md").read_text()
        self.assertIn("shipped", wf_doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
