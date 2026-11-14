"""F1+F8 (re-expressed for schema 2, M2 step 3): config editing done right.

`gategraph add-check` and `gategraph set-gate-checks` edit harness/adapter/adapter.yaml as targeted
text operations (comments survive, F8). Each edit is validated by the project's own kernel loader
(core.kernel.config.load) and rolled back when refused, and the gen blocks are regenerated in the
same step (F1). A stamped project regenerates locally with `PYTHONPATH=harness python3 -m
core.kernel.gen`, and dup-canon's remedy names that command.

The gate command is generated (plan 1.7), so there is no command that writes gate command text.
`set-gate-command` (schema 1: rewrote workflow.gate_commands) is deleted.

Each test works on a scratch COPY of the template repo (same pattern as test_fh_sync) so nothing
here can touch the real template.
"""
import importlib.util
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
           "gates": ["composed", "checked", "verified"], "seed_deny": True}

# A comment line of the schema-2 seed that F8 requires to survive programmatic edits.
CANARY_COMMENT = "Nothing else sets a gate."
# A gen block as the stamped docs carry it (brand.GEN_MARKER is "gategraph:gen").
GEN_OPEN = "<!-- gategraph:gen gate-table -->"
GEN_CLOSE = "<!-- /gategraph:gen -->"


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
        answers = base / "answers.yaml"
        answers.write_text(yaml.safe_dump(ANSWERS))
        self.project = base / "editproj"
        env = {**os.environ, "FH_SKIP_STAMP_SELFCHECK": "1"}
        r = subprocess.run(
            [sys.executable, str(self.template / "gategraph.py"), "init",
             str(self.project), "--answers", str(answers)],
            capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        self.adapter = self.project / "harness/adapter/adapter.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def fh(self, *args):
        return subprocess.run([sys.executable, str(self.template / "gategraph.py"), *args],
                              capture_output=True, text=True)

    def project_run(self, *argv):
        """Run python inside the stamped project, with the project's own harness on the path."""
        env = {**os.environ, "PYTHONPATH": str(self.project / "harness")}
        return subprocess.run([sys.executable, *argv], cwd=self.project, env=env,
                              capture_output=True, text=True)

    def lint(self):
        return self.project_run("-m", "core.kernel.selflint", "--tier", "gating", "--root", ".")

    def kernel_load(self):
        return self.project_run("-c", "from pathlib import Path\n"
                                "from core.kernel import config\n"
                                "config.load(Path('.').resolve())")

    def claims_md(self) -> str:
        return (self.project / "CLAUDE.md").read_text(encoding="utf-8")

    def add_gate_table(self, body="(stale)"):
        c = self.project / "CLAUDE.md"
        c.write_text(c.read_text(encoding="utf-8") + f"\n{GEN_OPEN}\n{body}\n{GEN_CLOSE}\n",
                     encoding="utf-8")

    def assert_comments_survive(self, before: str):
        after = self.adapter.read_text(encoding="utf-8")
        for line in before.splitlines():
            if line.lstrip().startswith("#"):
                self.assertIn(line, after.splitlines(),
                              f"programmatic edit destroyed a comment (F8): {line!r}")
        self.assertIn(CANARY_COMMENT, after, "programmatic edit destroyed the seed canary (F8)")


class TestAddCheck(Base):
    CMD = "python3 harness/adapter/checks/verify_claims.py --unit {unit}"

    def add(self, *extra):
        return self.fh("add-check", str(self.project), "--id", "claims",
                       "--cmd", self.CMD, "--on", "notes/**/*.md",
                       "--severity", "fatal", "--gate", "checked", *extra)

    def test_check_lands_on_the_gate_and_config_still_loads(self):
        r = self.add()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = yaml.safe_load(self.adapter.read_text())
        checks = {c["id"]: c for c in cfg["checks"]}
        self.assertIn("claims", checks)
        self.assertEqual(checks["claims"]["command"], self.CMD)
        self.assertEqual(checks["claims"]["on"], ["notes/**/*.md"])
        self.assertEqual(checks["claims"]["severity"], "fatal")
        gates = {g["id"]: g for g in cfg["gates"]}
        self.assertEqual(gates["checked"]["checks"], ["policy", "claims"],
                         "the check must be attached to the named gate, keeping its checks")
        self.assertNotIn("claims", gates["composed"].get("checks", []))
        load = self.kernel_load()
        self.assertEqual(load.returncode, 0, load.stdout + load.stderr)

    def test_next_command_is_the_generated_attempt_command(self):
        r = self.add()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gate command: .venv/bin/python harness/core/cli/gate.py "
                      "attempt checked --unit <slug>", r.stdout)
        self.assertNotIn("approve", r.stdout)

    def test_comments_survive_the_edit(self):
        before = self.adapter.read_text(encoding="utf-8")
        r = self.add()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_comments_survive(before)

    def test_gen_block_regenerated_and_gating_lint_green_immediately_after(self):
        self.add_gate_table()
        r = self.add()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("claims", self.claims_md(),
                      "the gate-table gen block must show the new check in the same step (F1)")
        lint = self.lint()
        self.assertEqual(lint.returncode, 0,
                         "add-check must leave the project lint-green in one step (F1): "
                         + lint.stdout + lint.stderr)

    def test_duplicate_id_refused_file_untouched(self):
        self.add()
        before = self.adapter.read_text()
        r = self.add()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claims", r.stderr)
        self.assertEqual(self.adapter.read_text(), before)

    def test_unknown_gate_refused_names_known_gates_file_untouched(self):
        before = self.adapter.read_text()
        r = self.fh("add-check", str(self.project), "--id", "claims", "--cmd", self.CMD,
                    "--gate", "bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bogus", r.stderr)
        self.assertIn("composed", r.stderr, "refusal should list the real gates")
        self.assertEqual(self.adapter.read_text(), before)

    def test_an_edit_the_loader_refuses_is_rolled_back(self):
        # An id outside the schema pattern: the project's own loader refuses the edit, so it
        # is rolled back at edit time and nothing is planted for a later lint to find (F1).
        self.add_gate_table()
        before = self.adapter.read_text()
        claims_before = self.claims_md()
        r = self.fh("add-check", str(self.project), "--id", "Bad Id", "--cmd", self.CMD,
                    "--gate", "checked")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rolled back", r.stderr)
        self.assertEqual(self.adapter.read_text(), before)
        self.assertEqual(self.claims_md(), claims_before)


class TestSetGateChecks(Base):
    def test_checks_updated_gen_block_regenerated_and_config_loads(self):
        # `claims` must exist before a gate can run it.
        r0 = self.fh("add-check", str(self.project), "--id", "claims",
                     "--cmd", "python3 harness/adapter/checks/verify_claims.py --unit {unit}",
                     "--gate", "checked")
        self.assertEqual(r0.returncode, 0, r0.stdout + r0.stderr)
        self.add_gate_table()
        r = self.fh("set-gate-checks", str(self.project), "composed", "policy", "claims")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = yaml.safe_load(self.adapter.read_text())
        gates = {g["id"]: g for g in cfg["gates"]}
        self.assertEqual(gates["composed"]["checks"], ["policy", "claims"])
        self.assertIn("policy, claims", self.claims_md(),
                      "the gate-table gen block must be regenerated in the same step (F1)")
        self.assertIn("gate command: .venv/bin/python harness/core/cli/gate.py "
                      "attempt composed --unit <slug>", r.stdout)
        load = self.kernel_load()
        self.assertEqual(load.returncode, 0, load.stdout + load.stderr)
        lint = self.lint()
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_comments_survive_the_edit(self):
        before = self.adapter.read_text(encoding="utf-8")
        r = self.fh("set-gate-checks", str(self.project), "composed", "policy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_comments_survive(before)

    def test_unknown_gate_refused_names_known_gates(self):
        before = self.adapter.read_text()
        r = self.fh("set-gate-checks", str(self.project), "bogus", "policy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bogus", r.stderr)
        self.assertIn("composed", r.stderr, "refusal should list the real gates")
        self.assertEqual(self.adapter.read_text(), before)

    def test_unknown_check_refused_file_untouched(self):
        before = self.adapter.read_text()
        r = self.fh("set-gate-checks", str(self.project), "composed", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nope", r.stderr)
        self.assertEqual(self.adapter.read_text(), before)


class TestProjectLocalRegen(Base):
    """F1(a): a stamped project has no gategraph binary. Regeneration runs as
    `PYTHONPATH=harness python3 -m core.kernel.gen`, and dup-canon's remedy names it."""

    def test_gen_entrypoint_exists_and_repairs_drift(self):
        self.add_gate_table(body="stale text")
        r = self.project_run("-m", "core.kernel.gen", "--root", ".")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("stale text", self.claims_md())
        self.assertIn("| 1 | `composed` |", self.claims_md())
        lint = self.lint()
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_dup_canon_remedy_names_the_project_local_command(self):
        self.add_gate_table(body="stale text")
        r = self.lint()
        self.assertNotEqual(r.returncode, 0, "dup-canon should have fired")
        out = r.stdout + r.stderr
        self.assertIn("python3 -m core.kernel.gen", out,
                      "the remedy must be a command the stamped project HAS, "
                      "`gategraph gen` is template-repo-only (F1)")


class TestTemplateCoreList(Base):
    """`update` and `diff` use the same core file list as init (M2): no core tests except
    selftest.py and the run_checks.sh shim, no packs. Before M2 the sync list was all of core/,
    so an update would have copied the test suite into every project."""

    def test_sync_list_equals_the_stamping_list(self):
        spec = importlib.util.spec_from_file_location("gategraph_listing",
                                                      TEMPLATE / "gategraph.py")
        gb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gb)
        stamped = {src.relative_to(gb.CORE_SRC).as_posix() for src in gb.core_files()}
        listed = set(gb._template_core_files())
        self.assertEqual(listed, stamped)
        self.assertIn("tests/selftest.py", listed)
        self.assertIn("tests/run_checks.sh", listed)
        self.assertFalse([p for p in listed if p.startswith("packs/")])
        self.assertEqual(sorted(p for p in listed if p.startswith("tests/")),
                         ["tests/run_checks.sh", "tests/selftest.py"])

    def test_diff_after_init_reports_nothing_new_upstream(self):
        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("new upstream", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
