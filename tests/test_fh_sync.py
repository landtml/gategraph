"""P3 sync tests: gategraph update / diff / upstream / gen.

Each test works on a scratch COPY of the template repo so it can mutate core
files and VERSION freely; the real template is never touched.
"""
import json
import os
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
           "seed_deny": True}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.template = base / "template"
        self.template.mkdir()
        for item in ("gategraph.py", "VERSION", "core", "seed"):
            src = TEMPLATE / item
            if src.is_dir():
                shutil.copytree(src, self.template / item,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, self.template / item)
        # the stamp is taken at 0.0.1, so bump_template (0.0.2) is an upgrade
        self.template.joinpath("VERSION").write_text("0.0.1\n")
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
        return subprocess.run([sys.executable, str(self.template / "gategraph.py"), *args],
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
        # manifest re-recorded: project's own stamp check (the stamped selftest, which init
        # runs too) must stay green. run_checks.sh is not stamped (M2).
        check = subprocess.run([sys.executable, "harness/core/tests/selftest.py"],
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


class TestUpstreamGuards(Base):
    def test_B16_upstream_refuses_when_template_moved_on(self):
        # the project stamped PROTOCOL.md at 0.0.1, then the template moved on
        self.bump_template()
        target = self.project / "harness/core/PROTOCOL.md"
        target.write_text(target.read_text() + "\nPROJECT EDIT\n")
        template_file = self.template / "core/PROTOCOL.md"
        before = template_file.read_text()
        r = self.fh("upstream", str(self.project), "harness/core/PROTOCOL.md")
        self.assertNotEqual(r.returncode, 0,
                            "upstream overwrote newer template work:\n" + r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(template_file.read_text(), before,
                         "the template's newer work must survive a refused upstream")
        self.assertIn("New template guidance", r.stdout,
                      "the refusal must print the diff it refused on")

    def test_B16_upstream_path_confined_to_core(self):
        (self.project / "harness" / "escape.txt").write_text("outside core\n")
        r = self.fh("upstream", str(self.project), "../escape.txt")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.template / "escape.txt").exists(),
                         "a ../ path escaped core/ into the template root")

    def test_B16_upstream_absolute_path_refused(self):
        outside = Path(self._tmp.name) / "abs_escape.txt"
        outside.write_text("absolute path outside core\n")
        r = self.fh("upstream", str(self.project), str(outside))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(outside.read_text(), "absolute path outside core\n")


class TestUpdateDowngrade(Base):
    def test_B26_update_refuses_downgrade(self):
        # the project was stamped by a NEWER template (0.2.0) than this one (0.0.2)
        manifest_path = self.project / "harness/.fh/MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "0.2.0"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        self.bump_template(extra="\nOLDER TEMPLATE TEXT\n")
        target = self.project / "harness/core/PROTOCOL.md"
        before = target.read_text()

        r = self.fh("update", str(self.project))
        self.assertNotEqual(r.returncode, 0, "downgrade accepted:\n" + r.stdout)
        self.assertIn("--allow-downgrade", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(target.read_text(), before,
                         "a refused downgrade must not touch any core file")

        r = self.fh("update", str(self.project), "--allow-downgrade")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OLDER TEMPLATE TEXT", target.read_text())


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
        # Schema 2: the gate chain is adapter `gates`, one {id, checks, seals_unit} per gate.
        # Add a fourth gate and move the seal onto it.
        adapter_path = self.project / "harness/adapter/adapter.yaml"
        adapter = yaml.safe_load(adapter_path.read_text())
        adapter["gates"][-1].pop("seals_unit")
        adapter["gates"].append({"id": "shipped", "checks": ["policy"], "seals_unit": True})
        adapter_path.write_text(yaml.safe_dump(adapter, sort_keys=False))
        # The seed's workflow doc carries no gate-table block, so one is planted here; the
        # regeneration under test rewrites it from the adapter.
        wf_doc = self.project / ".claude/skills/syncproj/workflow.md"
        wf_doc.write_text(wf_doc.read_text()
                          + "\n<!-- gategraph:gen gate-table -->\nstale\n<!-- /gategraph:gen -->\n")
        # The stamped core's own generator (core/kernel/gen.py), run as the stamped project runs it.
        env = dict(os.environ, PYTHONPATH="harness")
        r = subprocess.run([sys.executable, "-m", "core.kernel.gen", "--root", "."],
                           cwd=self.project, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("shipped", wf_doc.read_text())
        self.assertNotIn("stale", wf_doc.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
