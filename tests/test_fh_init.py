"""P0 tests: `gatebench.py init` stamps a correct, verifiable, git-committed project.

Contract under test (plan.md §4, §6, §17 P0):
- stamped tree matches the project layout in §4
- harness/.fh/MANIFEST.json records {version, files:{path: sha256}} covering
  every file under harness/core/, and every hash verifies
- name substitution applied to seed files; no leftover {{FH_*}} tokens
- harness/adapter/adapter.yaml is valid: fh_schema, project, gates,
  a gate_commands entry per gate, loop.enabled false by default
- target is a git repo with an initial commit
- the stamp proves itself alive: harness/core/tests/run_checks.sh exits 0
- init refuses to stamp a dir that already has harness/.fh/MANIFEST.json
- questionnaire works over stdin; --answers works non-interactively
- loop=yes stamps harness/adapter/oracle.py skeleton and flips loop.enabled
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent
FH = TEMPLATE / "gatebench.py"

ANSWERS = {
    "project": "scratchproj",
    "artifact_noun": "note",
    "unit_dir": "notes",
    "gates": ["composed", "checked", "verified"],
    "hashed_gates": [],
    "seed_deny": True,
    "loop_enabled": False,
}


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
            "harness/core/tests/run_checks.sh",
            "harness/.fh/MANIFEST.json",
            "harness/.fh/answers.yaml",
        ]
        for rel in expected:
            self.assertTrue((self.target / rel).is_file(), f"missing file: {rel}")
        for rel in ["harness/adapter/checks", "harness/eval", "harness/state",
                    "harness/logs"]:
            self.assertTrue((self.target / rel).is_dir(), f"missing dir: {rel}")
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
        for src in (TEMPLATE / "core").rglob("*"):
            if not src.is_file() or "__pycache__" in src.parts:
                continue
            rel = src.relative_to(TEMPLATE / "core")
            dst = self.target / "harness/core" / rel
            self.assertTrue(dst.is_file(), f"core file not stamped: {rel}")
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
        adapter = yaml.safe_load((self.target / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual(adapter["fh_schema"], 1)
        self.assertEqual(adapter["project"], "scratchproj")
        self.assertEqual(adapter["artifact_noun"], "note")
        self.assertEqual(adapter["unit_dir"], "notes")
        wf = adapter["workflow"]
        self.assertEqual(wf["gates"], ["composed", "checked", "verified"])
        for gate in wf["gates"]:
            self.assertIn(gate, wf["gate_commands"], f"no gate_command for {gate}")
        # terminal_states was dead schema (F30): parsed, never consumed, gone
        self.assertNotIn("terminal_states", wf)
        # deny seeded (answers said yes) and linked via enforces
        self.assertTrue(adapter["policies"]["deny"], "deny policy not seeded")
        self.assertEqual(adapter["policies"]["deny"][0]["enforces"], "no-acct")
        # loop stamped but inert; backstops non-null (§5, §8)
        self.assertFalse(adapter["loop"]["enabled"])
        for key in ["max_iterations", "cost_budget_usd", "wall_clock_minutes"]:
            self.assertIsNotNone(adapter["loop"]["backstops"][key])

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

    def test_run_checks_green(self):
        out = subprocess.run(["bash", "harness/core/tests/run_checks.sh"],
                             cwd=self.target, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0,
                         f"run_checks.sh failed:\n{out.stdout}\n{out.stderr}")

    def test_harness_lint_gating_green_on_fresh_stamp(self):
        out = subprocess.run(
            [sys.executable, "harness/core/harness_lint.py", "--tier", "gating"],
            cwd=self.target, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0,
                         f"fresh stamp must be lint-clean:\n{out.stdout}\n{out.stderr}")

    def test_settings_json_registers_frozen_hooks(self):
        settings = json.loads((self.target / ".claude/settings.json").read_text())
        blob = json.dumps(settings)
        for hook in ["pretool_guard.py", "posttool_lint.py", "stop_gate.py",
                     "session_start.py"]:
            self.assertIn(f"harness/core/hooks/{hook}", blob,
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
        # project, noun, unit dir, gates, hashed gates, seed deny?, loop?
        stdin = "iview\nreport\nreports\ncomposed,reviewed\n\nn\nn\n"
        proc = run_init(target, stdin=stdin)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        adapter = yaml.safe_load((target / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual(adapter["project"], "iview")
        self.assertEqual(adapter["workflow"]["gates"], ["composed", "reviewed"])
        self.assertEqual(adapter["policies"]["deny"], [])

    def test_loop_enabled_stamps_oracle_skeleton(self):
        target = self.base / "loopy"
        answers = dict(ANSWERS, project="loopy", loop_enabled=True)
        proc = run_init(target, answers=answers)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((target / "harness/adapter/oracle.py").is_file())
        adapter = yaml.safe_load((target / "harness/adapter/adapter.yaml").read_text())
        self.assertTrue(adapter["loop"]["enabled"])
        self.assertEqual(adapter["loop"]["oracle"], "harness.adapter.oracle:Oracle")

    def test_no_oracle_when_loop_disabled(self):
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
        adapter = yaml.safe_load(
            (target / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual(adapter["project"], "proj")
        self.assertFalse(adapter["loop"]["enabled"])
        log = subprocess.run(["git", "-C", str(target), "log", "--oneline"],
                             capture_output=True, text=True)
        self.assertIn("gatebench init", log.stdout)

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


if __name__ == "__main__":
    unittest.main()
