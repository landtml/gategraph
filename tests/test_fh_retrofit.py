"""F11: `gatebench init` must be safe on dirty real repos (+F10 selfcheck skip).

The P6 retrofit near-loss, as tests: the seed OVERWROTE a live-edited
HANDOFF.md and the stamp commit's `git add -A` swept ~106 uncommitted user
files. The contract now:

- default `gatebench init` REFUSES (typed, before writing anything) when the target
  already contains files the seed would overwrite, naming them and pointing
  at --retrofit;
- `gatebench init --retrofit` preserves every existing seed-target file (template
  version lands beside it as `.fhnew`, the same convention `gatebench update` uses) and the
  stamp commit contains ONLY what init created, user files stay untracked /
  unstaged / staged-but-uncommitted exactly as they were;
- F10: FH_SKIP_STAMP_SELFCHECK=1 skips the stamp self-check for test rigs
  that stamp many projects (default stays ON).
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent

ANSWERS = {"project": "retroproj", "artifact_noun": "dossier",
           "unit_dir": "apps", "gates": ["composed", "checked", "verified"],
           "hashed_gates": [], "seed_deny": True, "loop_enabled": False}


def git(repo: Path, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=u",
                           "-c", "user.email=u@x", *args],
                          capture_output=True, text=True, check=check)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = self.base / "userrepo"
        self.repo.mkdir()
        self.answers = self.base / "answers.yaml"
        self.answers.write_text(yaml.safe_dump(ANSWERS))

    def tearDown(self):
        self._tmp.cleanup()

    def init(self, *extra, env_extra=None):
        env = dict(os.environ, **(env_extra or {}))
        return subprocess.run(
            [sys.executable, str(TEMPLATE / "gatebench.py"), "init", str(self.repo),
             "--answers", str(self.answers), *extra],
            capture_output=True, text=True, env=env)


class TestDefaultInitRefusesConflicts(Base):
    def test_existing_seed_target_is_a_typed_refusal_before_any_write(self):
        (self.repo / "HANDOFF.md").write_text("# the user's live handoff\n")
        r = self.init()
        self.assertNotEqual(r.returncode, 0,
                            "init overwrote a user file with exit 0:\n" + r.stdout)
        out = r.stdout + r.stderr
        self.assertIn("HANDOFF.md", out)
        self.assertIn("--retrofit", out)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.repo / "harness").exists(),
                         "refusal must come BEFORE anything is written")
        self.assertEqual((self.repo / "HANDOFF.md").read_text(),
                         "# the user's live handoff\n")


class TestRetrofitInit(Base):
    def test_preserves_conflicts_and_stages_only_what_it_created(self):
        # a real dirty repo: committed file with unstaged edits, an untracked
        # research file, and a third file the user had ALREADY staged
        git(self.repo, "init", "-b", "main")
        (self.repo / "app.tex").write_text("committed content\n")
        (self.repo / "HANDOFF.md").write_text("# the user's live handoff\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "user work")
        (self.repo / "app.tex").write_text("committed content + LIVE EDIT\n")
        (self.repo / "research.txt").write_text("untracked notes\n")
        (self.repo / "staged.txt").write_text("staged but not committed\n")
        git(self.repo, "add", "staged.txt")

        r = self.init("--retrofit",
                      env_extra={"FH_SKIP_STAMP_SELFCHECK": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # conflict preserved, template version beside it
        self.assertEqual((self.repo / "HANDOFF.md").read_text(),
                         "# the user's live handoff\n",
                         "retrofit must never overwrite a user file")
        self.assertTrue((self.repo / "HANDOFF.md.fhnew").exists(),
                        "template version must land as .fhnew")
        self.assertIn("HANDOFF.md", r.stdout)  # the report names it

        # the harness itself is stamped and committed
        self.assertTrue((self.repo / "harness" / "core" / "gate.py").exists())
        head = git(self.repo, "show", "--name-only", "--format=", "HEAD").stdout
        self.assertIn("harness/core/gate.py", head)
        self.assertIn("CLAUDE.md", head)

        # user files were NOT swept into the stamp commit
        for leftover in ("research.txt", "staged.txt", "app.tex"):
            self.assertNotIn(leftover, head,
                             f"stamp commit swept the user's {leftover}")
        status = git(self.repo, "status", "--porcelain").stdout
        self.assertIn("?? research.txt", status)
        self.assertIn("A  staged.txt", status, "user's staged file must stay staged")
        self.assertIn(" M app.tex", status, "user's unstaged edit must survive")


class TestSelfcheckSkipEnv(Base):
    def test_skip_env_bypasses_the_stamp_selfcheck(self):
        r = self.init(env_extra={"FH_SKIP_STAMP_SELFCHECK": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SKIPPED", r.stdout,
                      "the skip must be explicit in the output")
        self.assertNotIn("stamp self-check: GREEN", r.stdout)
        self.assertTrue((self.repo / "harness" / ".fh" / "MANIFEST.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
