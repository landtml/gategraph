"""F1+F8: config editing done right.

`gatebench add-check` / `gatebench set-gate-command` must edit adapter.yaml as targeted
text operations (comments survive, F8) and regenerate the project's gen
blocks in the same step (dup-canon can never be left broken, F1). A stamped
project without gatebench must be able to regenerate locally
(`python3 harness/core/gen.py`), and dup-canon's remedy text must name that
command, not the template-repo-only `gatebench gen`.

Each test works on a scratch COPY of the template repo (same pattern as
test_fh_sync) so nothing here can touch the real template.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent

ANSWERS = {"project": "editproj", "artifact_noun": "note", "unit_dir": "notes",
           "gates": ["composed", "checked", "verified"], "hashed_gates": [],
           "seed_deny": True, "loop_enabled": False}

# The seed's inline schema documentation that F8 saw destroyed in all three
# P5-era consumers; it must survive programmatic edits.
CANARY_COMMENT = "THE ONLY place next-step text lives"


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
        answers = base / "answers.yaml"
        answers.write_text(yaml.safe_dump(ANSWERS))
        self.project = base / "editproj"
        env = {**os.environ, "FH_SKIP_STAMP_SELFCHECK": "1"}
        r = subprocess.run(
            [sys.executable, str(self.template / "gatebench.py"), "init",
             str(self.project), "--answers", str(answers)],
            capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        self.adapter = self.project / "harness/adapter/adapter.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def fh(self, *args):
        return subprocess.run([sys.executable, str(self.template / "gatebench.py"), *args],
                              capture_output=True, text=True)

    def lint(self):
        return subprocess.run(
            [sys.executable, "harness/core/harness_lint.py", "--tier", "gating"],
            cwd=self.project, capture_output=True, text=True)

    def workflow_text(self) -> str:
        return (self.project / ".claude/skills/editproj/workflow.md").read_text()


class TestAddCheck(Base):
    CMD = "python3 harness/adapter/checks/verify_claims.py {files}"

    def add(self, *extra):
        return self.fh("add-check", str(self.project), "--id", "claims",
                       "--cmd", self.CMD, "--on", "notes/**/*.md",
                       "--severity", "fatal", "--enforces", "claims-verified",
                       *extra)

    def annotate_rule(self):
        c = self.project / "CLAUDE.md"
        c.write_text(c.read_text() + "\n<!-- rule:claims-verified -->\n"
                                     "- Memos must carry verified claims.\n")

    def test_check_lands_and_config_still_loads(self):
        self.annotate_rule()
        r = self.add()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = yaml.safe_load(self.adapter.read_text())
        checks = {c["id"]: c for c in cfg["checks"]}
        self.assertIn("claims", checks)
        self.assertEqual(checks["claims"]["cmd"], self.CMD)
        self.assertEqual(checks["claims"]["on"], ["notes/**/*.md"])
        self.assertEqual(checks["claims"]["severity"], "fatal")
        self.assertEqual(checks["claims"]["enforces"], "claims-verified")

    def test_comments_survive_the_edit(self):
        self.annotate_rule()
        before = self.adapter.read_text()
        self.assertIn(CANARY_COMMENT, before, "seed canary comment missing?")
        r = self.add()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.adapter.read_text()
        self.assertIn(CANARY_COMMENT, after,
                      "programmatic edit destroyed adapter comments (F8)")
        self.assertIn("gitleaks", after,
                      "the doctor advisory example comment must survive too")

    def test_gating_lint_green_immediately_after(self):
        self.annotate_rule()
        added = self.add()
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        r = self.lint()
        self.assertEqual(r.returncode, 0,
                         "add-check must leave the project lint-green in one "
                         "step (F1): " + r.stdout + r.stderr)

    def test_duplicate_id_refused_file_untouched(self):
        self.annotate_rule()
        self.add()
        before = self.adapter.read_text()
        r = self.add()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claims", r.stderr)
        self.assertEqual(self.adapter.read_text(), before)

    def test_orphaned_enforces_refused_up_front(self):
        # No <!-- rule:claims-verified --> annotation anywhere: accepting the
        # check would plant a rule-coverage failure at the NEXT lint, the
        # F1 delayed-breakage pattern one layer up. Refuse at edit time.
        before = self.adapter.read_text()
        r = self.add()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claims-verified", r.stderr)
        self.assertIn("rule:", r.stderr, "refusal should show the annotation syntax")
        self.assertEqual(self.adapter.read_text(), before)


class TestSetGateCommand(Base):
    NEW_CMD = "python3 harness/core/gate.py approve composed --unit {slug} --by editor"

    def test_command_updated_and_gen_blocks_regenerated(self):
        r = self.fh("set-gate-command", str(self.project), "composed", self.NEW_CMD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = yaml.safe_load(self.adapter.read_text())
        self.assertEqual(cfg["workflow"]["gate_commands"]["composed"], self.NEW_CMD)
        self.assertIn(self.NEW_CMD, self.workflow_text(),
                      "the gate-table gen block must be regenerated in the "
                      "same step (F1): this is exactly what broke wire.py")
        lint = self.lint()
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_comments_survive_the_edit(self):
        r = self.fh("set-gate-command", str(self.project), "composed", self.NEW_CMD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(CANARY_COMMENT, self.adapter.read_text(),
                      "programmatic edit destroyed adapter comments (F8)")

    def test_unknown_gate_refused_names_known_gates(self):
        before = self.adapter.read_text()
        r = self.fh("set-gate-command", str(self.project), "bogus", self.NEW_CMD)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bogus", r.stderr)
        self.assertIn("composed", r.stderr, "refusal should list the real gates")
        self.assertEqual(self.adapter.read_text(), before)


class TestProjectLocalRegen(Base):
    """F1(a): a stamped project has no fh, regeneration must be runnable as
    `python3 harness/core/gen.py`, and dup-canon must say so."""

    def _break_gate_table(self):
        text = self.adapter.read_text()
        old = "python3 harness/core/gate.py approve composed --unit {slug}"
        self.assertIn(old, text)
        self.adapter.write_text(text.replace(
            old, old + " --by hand-edit"))

    def test_gen_entrypoint_exists_and_repairs_drift(self):
        self._break_gate_table()
        r = subprocess.run([sys.executable, "harness/core/gen.py"],
                           cwd=self.project, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--by hand-edit", self.workflow_text())
        lint = self.lint()
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_dup_canon_remedy_names_the_project_local_command(self):
        self._break_gate_table()
        r = self.lint()
        self.assertNotEqual(r.returncode, 0, "dup-canon should have fired")
        out = r.stdout + r.stderr
        self.assertIn("python3 harness/core/gen.py", out,
                      "the remedy must be a command the stamped project HAS, "
                      "`gatebench gen` is template-repo-only (F1)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
