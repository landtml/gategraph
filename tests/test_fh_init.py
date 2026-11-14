"""P0 tests: `gategraph.py init` stamps a correct, verifiable, git-committed project.

Contract under test:
- the stamped tree matches the schema-2 layout: harness/adapter/adapter.yaml, harness/core
  (without the full core suite: only tests/selftest.py), harness/.fh/MANIFEST.json;
- the hash manifest covers exactly the stamped core files, and every hash verifies;
- the stamped adapter loads with core.kernel.config.load (the schema-2 loader);
- name substitution is applied to seed files; no leftover {{FH_*}} tokens;
- target is a git repo with an initial commit;
- the stamp proves itself alive: its selftest exits 0, and `make check` exits 0 (B35);
- init refuses to stamp a dir that already has harness/.fh/MANIFEST.json;
- the questionnaire works over stdin (eight prompts); --answers works non-interactively;
- the legacy loop is not stamped (plan X1 deletes core/loop/).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent
FH = TEMPLATE / "gategraph.py"
# test_stamp_v2 owns the venv and `make check` helpers; reuse them (see its docstring).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_stamp_v2 import make_check, require_template_venv, seed_venv  # noqa: E402

ANSWERS = {
    "project": "scratchproj",
    "artifact_noun": "note",
    "unit_dir": "notes",
    "gates": ["composed", "checked", "verified"],
    "hashed_gates": [],
    "seed_deny": True,
}

# The questionnaire, in order: project, artifact noun, unit dir, gate chain, approval gates,
# approval channel, packs, seed deny. Empty lines take the default.
QUESTIONNAIRE_STDIN = "iview\nreport\nreports\ncomposed,reviewed\n\n\n\nn\n"

# Reads a stamped adapter with the stamp's own schema-2 loader, in a child interpreter whose
# core is the stamped harness/core (the same check init runs before it moves a stamp into place).
CONFIG_CODE = """
import json, sys
sys.path.insert(0, "harness")
from core.kernel import config
cfg = config.load(".")
print(json.dumps({
    "schema": cfg.schema, "project": cfg.project, "unit_noun": cfg.unit_noun,
    "unit_dir": cfg.unit_dir, "packs": list(cfg.packs),
    "gates": [g.id for g in cfg.gates], "deny": [dict(d) for d in cfg.deny],
    "checks": [c.id for c in cfg.checks],
}))
"""


def run_init(target: Path, answers: dict | None = None, stdin: str | None = None,
             extra: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(FH), "init", str(target)]
    if answers is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(answers, f)
        cmd += ["--answers", f.name]
    if extra:
        cmd += extra
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True)


def load_config(root: Path) -> dict:
    """The stamped adapter, as core.kernel.config.load reads it (raises on an invalid one)."""
    proc = subprocess.run([sys.executable, "-c", CONFIG_CODE], cwd=root,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("core.kernel.config.load refused the stamped adapter:\n"
                             + proc.stdout + proc.stderr)
    return json.loads(proc.stdout)


STAMPED_TESTS = ("tests/selftest.py", "tests/run_checks.sh")


def expected_stamped_core() -> dict:
    """{core-relative path: template source} for the files a stamp carries: every file under
    core/ except __pycache__, the packs (stamped per selection), and the test suite, whose only
    stamped members are tests/selftest.py and the shim tests/run_checks.sh."""
    keep = {}
    for src in (TEMPLATE / "core").rglob("*"):
        if not src.is_file() or "__pycache__" in src.parts or src.suffix == ".pyc":
            continue
        rel = src.relative_to(TEMPLATE / "core").as_posix()
        if rel.startswith("packs/"):
            continue
        if rel.startswith("tests/") and rel not in STAMPED_TESTS:
            continue
        keep[rel] = src
    return keep


class TestInitStamp(unittest.TestCase):
    """Assertions over one shared stamped project (read-only)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.target = Path(cls.tmp.name) / "scratchproj"
        cls.proc = run_init(cls.target, answers=ANSWERS)
        if cls.proc.returncode != 0:
            raise RuntimeError(f"init failed:\n{cls.proc.stdout}\n{cls.proc.stderr}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_stamped_tree_matches_layout(self):
        expected = [
            "CLAUDE.md", "HANDOFF.md", "PROGRESS.yaml", "MEMORY.md", "Makefile",
            ".gitignore",
            ".claude/settings.json",
            ".claude/agents/reviewer.md",
            ".claude/skills/scratchproj/SKILL.md",
            ".claude/skills/scratchproj/workflow.md",
            "harness/adapter/adapter.yaml",
            "harness/adapter/review_rubric.md",
            "harness/adapter/checks/policy_scan.py",
            "harness/adapter/checks/verify_claims.py",
            "harness/core/tests/selftest.py",
            "harness/core/tests/run_checks.sh",
            "harness/core/cli/gate.py",
            "harness/core/kernel/selflint.py",
            "harness/.fh/MANIFEST.json",
            "harness/.fh/answers.yaml",
        ]
        for rel in expected:
            self.assertTrue((self.target / rel).is_file(), f"missing file: {rel}")
        for rel in ["harness/adapter/checks", "harness/core/tests"]:
            self.assertTrue((self.target / rel).is_dir(), f"missing dir: {rel}")
        # run_checks.sh is stamped on purpose: a short shim that runs the selflint gating tier and
        # selftest, so a migrated project's unedited `make check` works. No other test file is
        # stamped; the full suite stays in the template.
        shim = (self.target / "harness/core/tests/run_checks.sh").read_text(encoding="utf-8")
        self.assertLessEqual(len(shim.splitlines()), 20, "run_checks.sh is a short shim")
        self.assertIn("core.kernel.selflint --tier gating", shim)
        self.assertIn("selftest.py", shim)
        stamped_tests = sorted(p.name for p in (self.target / "harness/core/tests").rglob("*")
                               if p.is_file() and "__pycache__" not in p.parts)
        self.assertEqual(stamped_tests, ["run_checks.sh", "selftest.py"])
        self.assertFalse((self.target / "harness/adapter/oracle.py").exists())
        self.assertFalse((self.target / "packs").exists(), "no pack was selected")
        # state/ and logs/ are gitignored
        gitignore = (self.target / ".gitignore").read_text()
        self.assertIn("harness/state/", gitignore)
        self.assertIn("harness/logs/", gitignore)

    def test_manifest_covers_core_and_hashes_verify(self):
        manifest = json.loads((self.target / "harness/.fh/MANIFEST.json").read_text())
        self.assertEqual(manifest["version"], (TEMPLATE / "VERSION").read_text().strip())
        files = manifest["files"]
        self.assertTrue(files, "manifest files map is empty")
        core_files = {str(p.relative_to(self.target))
                      for p in (self.target / "harness/core").rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(set(files), core_files,
                         "manifest must cover exactly the stamped core files")
        for rel, digest in files.items():
            actual = hashlib.sha256((self.target / rel).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, f"hash mismatch: {rel}")

    def test_core_stamped_verbatim_from_template(self):
        # The verbatim check covers the stamped file list: exactly the files
        # expected_stamped_core() names, each byte-identical to its template source.
        expected = expected_stamped_core()
        stamped = {p.relative_to(self.target / "harness" / "core").as_posix()
                   for p in (self.target / "harness" / "core").rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(stamped, set(expected),
                         "stamped core must be the template core minus packs and tests")
        for rel, src in expected.items():
            dst = self.target / "harness/core" / rel
            self.assertEqual(src.read_bytes(), dst.read_bytes(),
                             f"core file not verbatim: {rel}")

    def test_name_substitution_and_no_leftover_tokens(self):
        claude_md = (self.target / "CLAUDE.md").read_text()
        self.assertIn("scratchproj", claude_md)
        for rel in ["CLAUDE.md", "HANDOFF.md", "MEMORY.md", "Makefile",
                    ".claude/skills/scratchproj/SKILL.md",
                    ".claude/skills/scratchproj/workflow.md",
                    "harness/adapter/adapter.yaml"]:
            text = (self.target / rel).read_text()
            self.assertNotIn("{{FH_", text, f"leftover template token in {rel}")

    def test_adapter_yaml_valid(self):
        # The schema-2 loader is the judge: a stamped adapter it refuses is a broken stamp.
        cfg = load_config(self.target)
        self.assertEqual(cfg["schema"], 2)
        self.assertEqual(cfg["project"], "scratchproj")
        self.assertEqual(cfg["unit_noun"], "note")
        self.assertEqual(cfg["unit_dir"], "notes")
        self.assertEqual(cfg["gates"], ["composed", "checked", "verified"])
        # deny seeded (answers said yes) and linked via enforces
        self.assertEqual(len(cfg["deny"]), 1, "deny policy not seeded")
        self.assertEqual(cfg["deny"][0]["enforces"], "no-acct")
        # the policy check every gate names is declared
        self.assertIn("policy", cfg["checks"])
        # the legacy loop and the schema-1 keys are not stamped (plan X1 deletes core/loop/)
        raw = yaml.safe_load((self.target / "harness/adapter/adapter.yaml").read_text())
        for key in ("loop", "workflow", "fh_schema"):
            self.assertNotIn(key, raw, f"schema-1 or legacy key {key!r} is stamped")

    def test_answers_persisted(self):
        answers = yaml.safe_load((self.target / "harness/.fh/answers.yaml").read_text())
        self.assertEqual(answers["project"], "scratchproj")
        self.assertEqual(answers["gates"], ["composed", "checked", "verified"])

    def test_git_repo_with_initial_commit(self):
        out = subprocess.run(["git", "-C", str(self.target), "log", "--oneline"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(out.stdout.strip().splitlines()), 1)
        # state/ and logs/ must not be committed
        tracked = subprocess.run(["git", "-C", str(self.target), "ls-files"],
                                 capture_output=True, text=True).stdout
        self.assertNotIn("harness/state/", tracked)
        self.assertNotIn("harness/logs/", tracked)

    def test_stamped_make_check_green(self):
        # The stamped `make check` runs the stamped run_checks.sh shim; it is the project's done entry.
        require_template_venv()
        seed_venv(self.target)
        proc = make_check(self.target)
        self.assertEqual(proc.returncode, 0, f"make check failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("check: OK", proc.stdout)

    def test_harness_lint_gating_green_on_fresh_stamp(self):
        # the same selflint the stamped `make check` runs first (PYTHONPATH=harness)
        env = dict(os.environ, PYTHONPATH="harness")
        out = subprocess.run(
            [sys.executable, "-m", "core.kernel.selflint", "--tier", "gating", "--root", "."],
            cwd=self.target, capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0,
                         f"fresh stamp must be lint-clean:\n{out.stdout}\n{out.stderr}")

    def test_settings_json_registers_frozen_hooks(self):
        settings = json.loads((self.target / ".claude/settings.json").read_text())
        blob = json.dumps(settings)
        for hook in ["pretool_guard.py", "posttool_lint.py", "stop_gate.py",
                     "session_start.py"]:
            self.assertIn(f"harness/core/hosts/shims/{hook}", blob,
                          f"hook not registered: {hook}")


class TestInitModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_reinit(self):
        target = self.base / "p"
        self.assertEqual(run_init(target, answers=ANSWERS).returncode, 0)
        again = run_init(target, answers=ANSWERS)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already", (again.stdout + again.stderr).lower())

    def test_questionnaire_via_stdin(self):
        target = self.base / "iview"
        proc = run_init(target, stdin=QUESTIONNAIRE_STDIN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        cfg = load_config(target)
        self.assertEqual(cfg["project"], "iview")
        self.assertEqual(cfg["unit_noun"], "report")
        self.assertEqual(cfg["unit_dir"], "reports")
        self.assertEqual(cfg["gates"], ["composed", "reviewed"])
        self.assertEqual(cfg["deny"], [])

    def test_no_oracle_when_loop_disabled(self):
        # the loop is not stamped in any project; no oracle skeleton is written
        target = self.base / "noloop"
        proc = run_init(target, answers=dict(ANSWERS, project="noloop"))
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((target / "harness/adapter/oracle.py").exists())


class TestInitErgonomics(unittest.TestCase):
    """Part 5 cold-use friction (observed live in the NewRepo production
    copy, 2026-07-06): a non-interactive init must refuse typed instead of
    tracebacking, --defaults must exist, and a .gitignore that would swallow
    the stamp must be refused BEFORE anything is written."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_non_tty_questionnaire_refused_typed(self):
        target = self.base / "proj"
        r = run_init(target, stdin="")  # piped stdin, no --answers
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr,
                         "EOF at the questionnaire must be a typed refusal")
        self.assertIn("--answers", r.stderr)
        self.assertIn("--defaults", r.stderr)
        self.assertFalse(target.exists(), "nothing may be written")

    def test_defaults_flag_stamps_without_questionnaire(self):
        target = self.base / "proj"
        r = run_init(target, stdin="", extra=["--defaults"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = load_config(target)
        self.assertEqual(cfg["project"], "proj")
        self.assertEqual(cfg["gates"], ["composed", "checked", "verified"])
        raw = yaml.safe_load((target / "harness/adapter/adapter.yaml").read_text())
        self.assertNotIn("loop", raw, "the legacy loop is not stamped")
        log = subprocess.run(["git", "-C", str(target), "log", "--oneline"],
                             capture_output=True, text=True)
        self.assertIn("gategraph init", log.stdout)

    def test_gitignored_harness_refused_before_write(self):
        target = self.base / "proj"
        target.mkdir()
        subprocess.run(["git", "-C", str(target), "init", "-q"], check=True)
        (target / ".gitignore").write_text("/harness/" + "\n")
        (target / "app.py").write_text("print('hi')" + "\n")
        r = run_init(target, answers=ANSWERS, extra=["--retrofit"])
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr,
                         "must be a typed refusal, not a git crash mid-stamp")
        self.assertIn(".gitignore", r.stderr)
        self.assertIn("harness", r.stderr)
        self.assertFalse((target / "harness").exists(),
                         "refusal must come BEFORE any write, a partial "
                         "stamp is the exact failure observed in the wild")


class TestAnswerValidation(unittest.TestCase):
    """B27: artifact_noun and unit_dir reach adapter.yaml, so they must be
    safe YAML scalars (quoted) and confined (no ../, no absolute paths)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_B27_yaml_boolean_words_load_as_strings(self):
        target = self.base / "nounproj"
        proc = run_init(target, answers=dict(ANSWERS, project="nounproj",
                                             artifact_noun="yes", unit_dir="on"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        cfg = load_config(target)
        self.assertEqual(cfg["unit_noun"], "yes")
        self.assertEqual(cfg["unit_dir"], "on")

    def test_B27_unsafe_unit_dir_refused_before_any_write(self):
        for i, bad in enumerate(["a: b", "../x", "/abs/x", "units/../../x"]):
            target = self.base / f"bad{i}"
            proc = run_init(target, answers=dict(ANSWERS, unit_dir=bad))
            self.assertNotEqual(proc.returncode, 0, f"unit_dir {bad!r} accepted")
            self.assertNotIn("Traceback", proc.stderr, f"unit_dir {bad!r}")
            self.assertIn("unit_dir", proc.stderr, f"unit_dir {bad!r}")
            self.assertFalse((target / "harness").exists(),
                             f"unit_dir {bad!r} left a partial stamp")
            self.assertFalse((self.base / "x").exists())


DRIVER = """\
import importlib.util, sys
spec = importlib.util.spec_from_file_location("gategraph_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
def injected_failure(*args, **kwargs):
    mod.die("injected failure after the stamp was rendered")
mod._validate_stage = injected_failure
mod.main(["init", sys.argv[2], "--answers", sys.argv[3]])
"""


class TestTransactionalInit(unittest.TestCase):
    """B28: a failed init leaves no stamp, so a retry is not refused."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.answers = self.base / "answers.yaml"
        self.answers.write_text(yaml.safe_dump(ANSWERS))
        self.driver = self.base / "driver.py"
        self.driver.write_text(DRIVER)

    def tearDown(self):
        self._tmp.cleanup()

    def test_B28_failed_init_leaves_no_stamp(self):
        target = self.base / "txn"
        r1 = subprocess.run([sys.executable, str(self.driver), str(FH), str(target),
                             str(self.answers)], capture_output=True, text=True)
        self.assertNotEqual(r1.returncode, 0, "the injected failure did not fire")
        self.assertIn("injected failure", r1.stdout + r1.stderr)
        self.assertFalse((target / "harness" / ".fh" / "MANIFEST.json").exists(),
                         "a failed init left a stamp manifest behind")
        self.assertFalse((target / "harness").exists(),
                         "a failed init left a partial stamp")
        self.assertEqual([p.name for p in self.base.iterdir()
                          if p.name.startswith(".txn")], [],
                         "a failed init left its staging directory behind")

        r2 = run_init(target, answers=ANSWERS)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertNotIn("already stamped", r2.stdout + r2.stderr)


if __name__ == "__main__":
    unittest.main()
